#!/usr/bin/env python3
"""
MOTOR F360 → Supabase (schema financeiro)
Rede MEO · rede-meo-sync

Camadas:
  incremental    diária 06:00 BRT · parcelas + cartões por tipoDatas=Atualização, janela 7 dias · cadastros
  recarga_curta  domingo          · 3 últimas competências completas (pega exclusão) · cadastros
  recarga_longa  dia 1º           · ano corrente completo · cadastros
  extrato_diario também roda ao fim do incremental · extrato bancário das contas SANTANDER, últimos 10 dias
  extrato_carga  manual           · extrato bancário mês a mês, EXTRATO_INI..EXTRATO_FIM (padrão 2025-01-01..hoje)

Variáveis de ambiente (GitHub Secrets):
  F360_KEY                chave da API pública F360 Finanças
  FINANCEIRO_DATABASE_URL postgresql://financeiro_motor.<ref>:<senha>@aws-1-sa-east-1.pooler.supabase.com:5432/postgres

Regras (MANUAL-api-f360-v4):
  R1 competência filtra-se no rateio (a RPC guarda tudo; o dre_montar filtra)
  R2 sinal em coluna gerada · R3 cancelada · R4 trava de ouro na RPC (sinal relativo, bruto|líquido, cancelada)
  R6 todo download decodificado com utf-8-sig
  Pausa de 0,5 s entre páginas: ~45 chamadas seguidas travam a tela do F360 para quem está logado
"""
import os, sys, json, time, calendar, collections, datetime as dt
import urllib.request, urllib.parse, urllib.error, http.client
import psycopg2, psycopg2.extras

B = 'https://financas.f360.com.br'
PAUSA = 0.5
LOTE = 500
BRT = dt.timezone(dt.timedelta(hours=-3))


# ----------------------------------------------------------------------------- API
def login():
    r = urllib.request.Request(B + '/PublicLoginAPI/DoLogin',
                               data=json.dumps({'token': os.environ['F360_KEY']}).encode(),
                               headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=120).read().decode('utf-8-sig'))['Token']


def _get(jwt, path, params=None, tentativas=3):
    q = ('?' + urllib.parse.urlencode(params)) if params else ''
    rq = urllib.request.Request(B + path + q, headers={'Authorization': 'Bearer ' + jwt,
                                                        'Content-Type': 'application/json'})
    for t in range(1, tentativas + 1):
        try:
            d = json.loads(urllib.request.urlopen(rq, timeout=180).read().decode('utf-8-sig'))
            if not d.get('Ok'):
                raise RuntimeError(f'API nao-OK em {path}: {str(d)[:200]}')
            return d['Result']
        except (OSError, RuntimeError, json.JSONDecodeError, http.client.HTTPException) as e:
            # OSError cobre TimeoutError, ConnectionError e urllib.error.URLError/HTTPError
            if t == tentativas:
                raise
            print(f'   retry {t}/{tentativas} em {path}: {type(e).__name__}: {str(e)[:120]}', flush=True)
            time.sleep(20 * t)


def _post(jwt, path, corpo, tentativas=3):
    rq = urllib.request.Request(B + path, data=json.dumps(corpo).encode(),
                                headers={'Authorization': 'Bearer ' + jwt,
                                         'Content-Type': 'application/json'})
    for t in range(1, tentativas + 1):
        try:
            d = json.loads(urllib.request.urlopen(rq, timeout=180).read().decode('utf-8-sig'))
            if d.get('Ok') is False or not isinstance(d.get('Result'), dict):
                raise RuntimeError(f'API nao-OK/sem Result em {path}: {str(d)[:200]}')
            return d['Result']
        except (OSError, RuntimeError, json.JSONDecodeError, http.client.HTTPException) as e:
            if t == tentativas:
                raise
            print(f'   retry {t}/{tentativas} em {path}: {type(e).__name__}: {str(e)[:120]}', flush=True)
            time.sleep(20 * t)


def listar_extrato(jwt, cnpj, ini, fim):
    """Todas as páginas da janela, na ordem da API. Qualquer falha propaga: janela incompleta não é enviada."""
    out, pag = [], 1
    while True:
        res = _post(jwt, '/ExtratoBancarioPublicAPI/ObterExtratoBancario',
                    {'DataInicio': ini, 'DataFim': fim, 'CNPJEmpresas': [cnpj], 'Status': 'Todos',
                     'ModeloRelatorio': 'Sintetico', 'Pagina': pag})
        if not isinstance(res.get('Extratos'), list) or 'QuantidadeDePaginas' not in res:
            raise RuntimeError(f'resposta sem Extratos/QuantidadeDePaginas na pagina {pag}: {str(res)[:200]}')
        out += res['Extratos']
        if pag >= res['QuantidadeDePaginas']:
            break
        pag += 1
        time.sleep(PAUSA)
    return out


def listar_parcelas(jwt, tipo, ini, fim, tipo_datas):
    out, pag = [], 1
    while True:
        res = _get(jwt, '/ParcelasDeTituloPublicAPI/ListarParcelasDeTitulos',
                   {'pagina': pag, 'tipo': tipo, 'inicio': ini, 'fim': fim,
                    'tipoDatas': tipo_datas, 'status': 'Todos'})
        out += res['Parcelas']
        if pag >= res['QuantidadeDePaginas']:
            break
        pag += 1
        time.sleep(PAUSA)
    return out


def listar_cartoes(jwt, ini, fim, tipo_datas):
    out, pag = [], 1
    while True:
        res = _get(jwt, '/ParcelasDeCartoesPublicAPI/ListarParcelasDeCartoes',
                   {'pagina': pag, 'tipo': 'ambos', 'inicio': ini, 'fim': fim, 'tipoDatas': tipo_datas})
        out += res['Parcelas']
        if pag >= res['QuantidadeDePaginas']:
            break
        pag += 1
        time.sleep(PAUSA)
    return out


def cadastros(jwt):
    return (_get(jwt, '/PlanoDeContasPublicAPI/ListarPlanosContas'),
            _get(jwt, '/CentroDeCustoPublicAPI/ListarCentrosDeCusto'),
            _get(jwt, '/ContaBancariaPublicAPI/ListarContasBancarias'))


# ----------------------------------------------------------------------------- BANCO
def conectar():
    con = psycopg2.connect(os.environ['FINANCEIRO_DATABASE_URL'], sslmode='require')
    con.autocommit = True
    return con


def sync(cur, rpc, camada, tipo_datas, ini, fim, itens):
    tot, rej = collections.Counter(), []
    for i in range(0, len(itens), LOTE):
        cur.execute(f'select financeiro.{rpc}(%s,%s,%s,%s,%s)',
                    (camada, tipo_datas, ini, fim, psycopg2.extras.Json(itens[i:i + LOTE])))
        r = cur.fetchone()[0]
        for k in ('parcelas_gravadas', 'rateios_gravados', 'vendas_upsert'):
            tot[k] += r.get(k, 0)
        rej += r.get('rejeitadas', [])
    return tot, rej


def sync_cadastros(cur, jwt):
    planos, centros, contas = cadastros(jwt)
    cur.execute('select financeiro.f360_sync_cadastros(%s,%s,%s)',
                (psycopg2.extras.Json(planos), psycopg2.extras.Json(centros), psycopg2.extras.Json(contas)))
    r = cur.fetchone()[0]
    novos = r.get('planos_novos') or []
    if novos:
        print(f'⚠️  PLANOS DE CONTAS NOVOS NA F360 ({len(novos)}): {novos}', flush=True)
        print('    → precisam de linha no DRE (dre_linha_plano) ou marca fora_por_desenho', flush=True)
    else:
        print('   cadastros: sem plano novo', flush=True)
    return r


def pos_sync(cur):
    cur.execute('select financeiro.f360_resolver_de_para()')
    print('   de-para:', json.dumps(cur.fetchone()[0], ensure_ascii=False), flush=True)
    cur.execute('select plano_contas, valor_com_sinal from financeiro.dre_planos_orfaos()')
    orf = cur.fetchall()
    if orf:
        print(f'⚠️  PLANOS COM MOVIMENTO E SEM LINHA NO DRE ({len(orf)}):', flush=True)
        for nome, v in orf:
            print(f'      {v:>12,.2f}  {nome}', flush=True)
    cur.execute('select financeiro.f360_rejeicoes_pendentes()')
    print(f'   rejeicoes: {cur.fetchone()[0]}', flush=True)

    # ---- trava do DRE: os meses fechados continuam iguais ao que foram fechados? ----
    cur.execute('select financeiro.dre_auditar()')
    a = cur.fetchone()[0]
    if a['fechamentos_conferidos'] == 0:
        print('   dre: nenhum mes fechado ainda', flush=True)
    elif a['divergentes'] > 0:
        print(f"\u26a0\ufe0f  DRE FECHADO MUDOU \u2014 {a['divergentes']} de {a['fechamentos_conferidos']} fechamentos:", flush=True)
        for d in a['detalhe']:
            print(f"      loja {d['loja']} \u00b7 {d['competencia']} \u00b7 resultado "
                  f"{d['resultado_no_fechamento']} \u2192 {d['resultado_agora']}", flush=True)
            for x in d['linhas_que_mudaram']:
                print(f"         linha {x['linha']}: {x['no_fechamento']} \u2192 {x['agora']} ({x['diferenca']:+.2f})", flush=True)
    else:
        msg = f"   dre: {a['fechamentos_conferidos']} fechamentos conferidos, nenhum mudou"
        if a['voltaram_ao_normal']: msg += f" ({a['voltaram_ao_normal']} voltaram ao normal)"
        print(msg, flush=True)

    # ---- lancamento que caiu em mes JA FECHADO ----
    cur.execute('select financeiro.dre_retroativos_contagem(7)')
    r = cur.fetchone()[0]
    if r['lancamentos']:
        print(f"\u26a0\ufe0f  {r['lancamentos']} lancamentos entraram em MES JA FECHADO nos ultimos 7 dias "
              f"({r['lojas']} lojas, {r['competencias_afetadas']} competencias, R$ {r['valor']:,.2f})", flush=True)
        print('      detalhe: select * from financeiro.dre_retroativos_em_mes_fechado(7)', flush=True)


# ----------------------------------------------------------------------------- JANELAS
def hoje_brt():
    return dt.datetime.now(BRT).date()


def mes_ini_fim(ano, mes):
    return f'{ano}-{mes:02d}-01', f'{ano}-{mes:02d}-{calendar.monthrange(ano, mes)[1]:02d}'


def ultimas_competencias(n):
    h = hoje_brt()
    out = []
    for k in range(n):
        m = (h.month - 1 - k) % 12 + 1
        a = h.year + ((h.month - 1 - k) // 12)
        out.append((a, m))
    return out  # da mais recente para a mais antiga


# ----------------------------------------------------------------------------- CAMADAS
def camada_incremental(cur, dias=7):
    fim = hoje_brt()
    ini = fim - dt.timedelta(days=dias)
    ini, fim = ini.isoformat(), fim.isoformat()
    print(f'== INCREMENTAL {ini}..{fim} (tipoDatas=Atualização)', flush=True)
    jwt = login()
    sync_cadastros(cur, jwt)

    parc = listar_parcelas(jwt, 'Despesa', ini, fim, 'Atualização') + \
           listar_parcelas(jwt, 'Receita', ini, fim, 'Atualização')
    tot, rej = sync(cur, 'f360_sync_parcelas', 'incremental', 'Atualização', ini, fim, parc)
    print(f'   parcelas: {len(parc)} recebidas | {dict(tot)} | rejeitadas {len(rej)}', flush=True)

    try:
        cart = listar_cartoes(jwt, ini, fim, 'Atualização')
        tot, rej = sync(cur, 'f360_sync_cartoes', 'incremental', 'Atualização', ini, fim, cart)
        print(f'   cartoes:  {len(cart)} recebidas | {dict(tot)} | rejeitadas {len(rej)}', flush=True)
    except RuntimeError as e:
        # tipoDatas=Atualização em cartões ainda nao foi confirmado na API — fallback por data de venda (45 dias)
        print(f'   cartoes por Atualização falhou ({str(e)[:100]}); usando tipoDatas=Venda, 45 dias', flush=True)
        ini2 = (hoje_brt() - dt.timedelta(days=45)).isoformat()
        cart = listar_cartoes(jwt, ini2, fim, 'Venda')
        tot, rej = sync(cur, 'f360_sync_cartoes', 'incremental', 'Venda', ini2, fim, cart)
        print(f'   cartoes:  {len(cart)} recebidas | {dict(tot)} | rejeitadas {len(rej)}', flush=True)
    pos_sync(cur)


def recarga_competencias(cur, camada, meses):
    for ano, mes in meses:
        ini, fim = mes_ini_fim(ano, mes)
        print(f'== {camada.upper()} {ano}-{mes:02d}', flush=True)
        jwt = login()
        parc = listar_parcelas(jwt, 'Despesa', ini, fim, 'Competência') + \
               listar_parcelas(jwt, 'Receita', ini, fim, 'Competência')
        tot, rej = sync(cur, 'f360_sync_parcelas', camada, 'Competência', ini, fim, parc)
        print(f'   parcelas: {len(parc)} | {dict(tot)} | rejeitadas {len(rej)}', flush=True)
        cart = listar_cartoes(jwt, ini, fim, 'Venda')
        tot, rej = sync(cur, 'f360_sync_cartoes', camada, 'Venda', ini, fim, cart)
        print(f'   cartoes:  {len(cart)} | {dict(tot)} | rejeitadas {len(rej)}', flush=True)
        # reconciliação de contagem (exclusão é invisível ao incremental)
        cur.execute("select financeiro.f360_reconciliar_competencia(%s)", (f'{ano}-{mes:02d}',))
        no_banco = cur.fetchone()[0]
        ids_api = {p['ParcelaId'] for p in parc}
        print(f'   reconciliacao {ano}-{mes:02d}: {len(ids_api)} parcelas na API x {no_banco} no banco com rateio nesta competencia', flush=True)
    jwt = login()
    sync_cadastros(cur, jwt)
    pos_sync(cur)


# ----------------------------------------------------------------------------- EXTRATO BANCÁRIO
# Carga por substituição de janela: f360_sync_extrato APAGA a conta na janela e grava o que vier.
# Por isso só se envia janela completa (todas as páginas OK), uma chamada por conta+janela.
def extrato_contas(cur):
    cur.execute('select cnpj, conta, cod_prefixo from financeiro.f360_extrato_contas() order by cod_prefixo')
    return cur.fetchall()


def sync_extrato_janela(cur, jwt, cnpj, conta, ini, fim, resumo):
    try:
        todos = listar_extrato(jwt, cnpj, ini, fim)
    except Exception as e:
        resumo['erros'] += 1
        print(f'   ⚠️  {conta} {ini}..{fim}: download falhou, janela NAO enviada — {type(e).__name__}: {str(e)[:150]}', flush=True)
        return
    itens = [x for x in todos if x.get('Conta') == conta]
    try:
        cur.execute('select financeiro.f360_sync_extrato(%s,%s,%s,%s)',
                    (cnpj, ini, fim, psycopg2.extras.Json(itens)))
        r = cur.fetchone()[0]
    except Exception as e:
        resumo['erros'] += 1
        print(f'   ⚠️  {conta} {ini}..{fim}: f360_sync_extrato falhou — {type(e).__name__}: {str(e)[:200]}', flush=True)
        return
    resumo['janelas'] += 1
    resumo['gravados'] += len(itens)
    print(f'   {conta} {ini}..{fim}: api {len(todos)} | desta conta {len(itens)} | banco {json.dumps(r, ensure_ascii=False)}', flush=True)


def extrato_rodar(cur, janelas):
    contas = extrato_contas(cur)
    resumos = []
    for cnpj, conta, cod in contas:
        resumo = collections.Counter()
        jwt = login()
        for i, (ini, fim) in enumerate(janelas):
            if i:
                time.sleep(PAUSA)
            sync_extrato_janela(cur, jwt, cnpj, conta, ini, fim, resumo)
        resumos.append((conta, resumo))
    print('== RESUMO EXTRATO', flush=True)
    print(f"   {'conta':<28} {'janelas':>8} {'lancamentos':>12} {'erros':>6}", flush=True)
    for conta, r in resumos:
        print(f"   {conta:<28} {r['janelas']:>8} {r['gravados']:>12} {r['erros']:>6}{'  ⚠️' if r['erros'] else ''}", flush=True)
    return sum(r['erros'] for _, r in resumos)


def camada_extrato_diario(cur, dias=10):
    fim = hoje_brt()
    ini = fim - dt.timedelta(days=dias)
    print(f'== EXTRATO DIARIO {ini}..{fim}', flush=True)
    return extrato_rodar(cur, [(ini.isoformat(), fim.isoformat())])


def janelas_mensais(ini, fim):
    out, a, m = [], ini.year, ini.month
    while (a, m) <= (fim.year, fim.month):
        mi, mf = mes_ini_fim(a, m)
        out.append((max(mi, ini.isoformat()), min(mf, fim.isoformat())))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return out


def camada_extrato_carga(cur):
    ini = dt.date.fromisoformat(os.environ.get('EXTRATO_INI') or '2025-01-01')
    fim = dt.date.fromisoformat(os.environ.get('EXTRATO_FIM') or hoje_brt().isoformat())
    janelas = janelas_mensais(ini, fim)
    print(f'== EXTRATO CARGA {ini}..{fim} · {len(janelas)} janelas mensais por conta', flush=True)
    erros = extrato_rodar(cur, janelas)
    if erros:
        print(f'⚠️  {erros} janelas com erro — rode de novo o periodo para reenviar', flush=True)
        sys.exit(1)


def camada_recarga_curta(cur):
    recarga_competencias(cur, 'recarga_curta', ultimas_competencias(3))


def camada_recarga_longa(cur):
    h = hoje_brt()
    recarga_competencias(cur, 'recarga_longa', [(h.year, m) for m in range(h.month, 0, -1)])


# ----------------------------------------------------------------------------- MAIN
if __name__ == '__main__':
    camada = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('CAMADA', 'incremental')).strip()
    t0 = time.time()
    print(f'MOTOR F360 · camada={camada} · {dt.datetime.now(BRT):%Y-%m-%d %H:%M} BRT', flush=True)
    con = conectar()
    cur = con.cursor()
    try:
        {'incremental': camada_incremental,
         'recarga_curta': camada_recarga_curta,
         'recarga_longa': camada_recarga_longa,
         'extrato_diario': camada_extrato_diario,
         'extrato_carga': camada_extrato_carga}[camada](cur)
        if camada == 'incremental':
            # extrato roda DEPOIS de títulos/cartões; falha aqui só avisa, não derruba o motor
            try:
                camada_extrato_diario(cur)
            except Exception as e:
                print(f'⚠️  EXTRATO DIARIO falhou: {type(e).__name__}: {str(e)[:200]}', flush=True)
    finally:
        con.close()
    print(f'FIM · {(time.time() - t0)/60:.1f} min', flush=True)
