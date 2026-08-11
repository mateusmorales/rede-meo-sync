#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/motor-e1-estoque.py  —  MOTOR E1: foto diária do estoque.
#
# Motor NOVO e PRÓPRIO do módulo Estoque. NÃO compartilha arquivo/workflow com os
# Motores 1/2/3 do Performance — só reaproveita o PADRÃO (auth/XML da Linx e a
# conexão REST com o Supabase via service_role).
#
# FLUXO:
#   1) Lê a lista de lojas ATIVAS do banco (REST): cod_microvix de
#      lojas where ativa=true and cod_microvix is not null. Loja encerrada sai
#      sozinha. O dict CNPJS serve SÓ pra traduzir cod_microvix → cnpjEmp.
#   2) Por loja (ordem crescente de cod_microvix): LinxProdutosInventario com
#      cod_deposito=1,7 (OBRIGATÓRIO — sem ele a API devolve só o depósito 1,
#      calada). data_inventario = ONTEM (validada ao centavo) ou --data.
#   3) Grava via RPC gravar_estoque_loja (DELETE+INSERT em transação no servidor,
#      recusa payload vazio). Loga {loja_id, antes, gravados, variacao}.
#      API devolveu 0 linhas → NÃO chama a RPC (ALERTA, segue).
#
# FLAGS: --dry-run | --limit N | --data YYYY-MM-DD | --loja N
#
# INFRA: LINX_CHAVE + SUPABASE_SERVICE_KEY no ambiente. try/except por loja.
#        Erro de parâmetro da Linx → mensagem CRUA. Não cria workflow (só o script).
#
# COMO RODAR:
#   export LINX_CHAVE="..."; export SUPABASE_SERVICE_KEY="...service_role..."
#   python3 scripts/motor-e1-estoque.py --dry-run
#   python3 scripts/motor-e1-estoque.py --limit 3
#   python3 scripts/motor-e1-estoque.py              # foto de ONTEM, todas as lojas
# =============================================================================

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, timedelta

CHAVE = os.environ.get("LINX_CHAVE")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
TIMEOUT = 600  # inventário cheio de uma loja pode ser grande

COD_DEP = "1,7"  # ESTOQUE(1) + OUTLET(7) — OBRIGATÓRIO em toda chamada.

# CNPJs por cod_microvix — MESMA fonte dos motores (dict, não é segredo). Serve
# SÓ pra traduzir cod_microvix → cnpjEmp. A LISTA de lojas vem do BANCO.
CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}


def exigir_env():
    faltam = [n for n, v in (("LINX_CHAVE", CHAVE), ("SUPABASE_SERVICE_KEY", SUPA_KEY)) if not v]
    if faltam:
        print("ERRO: defina no ambiente: " + ", ".join(faltam))
        sys.exit(1)


# ── helpers Linx (mesmo padrão dos motores / testar-estoque-api.py) ──────────

def montar_body(metodo, params):
    ps = "".join(f'<Parameter id="{k}">{v}</Parameter>' for k, v in params.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<LinxMicrovix><Authentication user="linx_export" password="linx_export"/>'
        f'<ResponseFormat>xml</ResponseFormat><Command><Name>{metodo}</Name>'
        f'<Parameters>{ps}</Parameters></Command></LinxMicrovix>'
    )


def parse_cr(xml):
    """(cols, [regs dict]) — esquema <C><D>col</D></C><R><D>val</D></R>."""
    rd = ET.fromstring(xml).find("ResponseData")
    if rd is None or rd.find("C") is None:
        return [], []
    cols = [(d.text or "") for d in rd.find("C").findall("D")]
    ix = {c: i for i, c in enumerate(cols)}
    regs = []
    for r in rd.findall("R"):
        ds = r.findall("D")
        regs.append({c: ((ds[ix[c]].text or "").strip() if ix[c] < len(ds) else "") for c in cols})
    return cols, regs


def inventario(cnpj, data_inv):
    """Chama LinxProdutosInventario CRONOMETRADA. cod_deposito=1,7 SEMPRE.
    Retorna (erro, cols, regs, seg). erro != None → resposta sem esquema C/R
    (a mensagem CRUA da Linx, p/ debug de parâmetro)."""
    params = {"chave": CHAVE, "cnpjEmp": cnpj, "data_inventario": data_inv, "cod_deposito": COD_DEP}
    body = montar_body("LinxProdutosInventario", params).encode("utf-8")
    req = urllib.request.Request(LINX, data=body, headers={"Content-Type": "text/xml; charset=utf-8"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            xml = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:400]}", [], [], time.time() - t0
    except Exception as e:
        return f"rede/timeout: {e!r}", [], [], time.time() - t0
    seg = time.time() - t0
    cols, regs = parse_cr(xml)
    erro = None if cols else (xml[:400].replace("\n", " ").strip() or "(resposta vazia, sem esquema C/R)")
    return erro, cols, regs, seg


def achar_col(cols, *subs):
    low = [(c, c.lower()) for c in cols]
    for s in subs:
        for orig, l in low:
            if s in l:
                return orig
    return None


def num(v):
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def qtd_val(v):
    """Quantidade: int quando inteira, senão float (JSON preserva)."""
    f = num(v)
    return int(f) if f.is_integer() else f


# ── helpers Supabase REST (service_role — mesmo padrão do motor 2) ───────────

def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    if extra:
        h.update(extra)
    return h


def buscar_lojas_ativas():
    """Lista de cod_microvix de lojas ativas com cod_microvix != null (do BANCO).
    Loja encerrada sai sozinha (ativa=false). Ordem crescente."""
    url = (f"{SUPA_URL}/rest/v1/lojas?select=cod_microvix"
           "&ativa=eq.true&cod_microvix=not.is.null")
    req = urllib.request.Request(url, headers=supa_headers())
    with urllib.request.urlopen(req, timeout=120) as r:
        linhas = json.load(r)
    cods = []
    for x in linhas:
        try:
            cods.append(int(x["cod_microvix"]))
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(set(cods))


def gravar_estoque_loja(cod, data_foto, itens):
    """RPC gravar_estoque_loja (DELETE+INSERT transacional; recusa vazio).
    Retorna (ok, dados|erro). dados = {loja_id, antes, gravados, variacao}."""
    url = f"{SUPA_URL}/rest/v1/rpc/gravar_estoque_loja"
    payload = {"p_cod_microvix": cod, "p_data_foto": data_foto, "p_itens": itens}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=supa_headers({"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            corpo = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:400]}"
    except Exception as e:
        return False, f"rede/timeout: {e!r}"
    try:
        obj = json.loads(corpo) if corpo.strip() else {}
    except json.JSONDecodeError:
        return False, f"resposta não-JSON: {corpo[:200]}"
    # PostgREST pode devolver a função como objeto direto OU lista de 1 linha.
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    return True, obj


# ── extração dos itens do inventário ─────────────────────────────────────────

def extrair_itens(cols, regs):
    """(itens, dep1, dep7, distintos, descartadas, outros_dep, erro_cols).
    descartadas = linhas com cod_deposito/cod_produto NÃO-parseável (não entram no
    payload — antes viravam 0 em silêncio). outros_dep = linhas com cod_deposito
    != 1 e != 7 (entram no payload, mas são um CANÁRIO: deveria ser 0 porque
    filtramos 1,7 na API — se aparecer, a API ignorou o filtro).
    itens = [{cod_deposito, cod_produto, quantidade}]."""
    col_prod = achar_col(cols, "cod_produto", "produto")
    col_dep = achar_col(cols, "cod_deposito", "deposito")
    col_qtd = achar_col(cols, "quantidade", "saldo", "estoque", "qtd")
    if not (col_prod and col_qtd and col_dep):
        return [], 0.0, 0.0, 0, 0, 0, f"colunas esperadas não encontradas: campos={cols}"
    itens = []
    dep1 = dep7 = 0.0
    distintos = set()
    descartadas = 0
    outros_dep = 0
    for r in regs:
        # Parse ESTRITO de dep/prod (sem num(), que engoliria o erro virando 0 e
        # inseriria linha-fantasma): raw vazio/não-numérico → descarta e conta.
        raw_dep = str(r.get(col_dep) or "").strip()
        raw_prod = str(r.get(col_prod) or "").strip()
        try:
            dep = int(float(raw_dep))
            prod = int(float(raw_prod))
        except (ValueError, TypeError):
            descartadas += 1
            continue
        q = qtd_val(r.get(col_qtd))
        itens.append({"cod_deposito": dep, "cod_produto": prod, "quantidade": q})
        distintos.add(prod)
        if dep == 1:
            dep1 += q
        elif dep == 7:
            dep7 += q
        else:
            outros_dep += 1
    return itens, dep1, dep7, len(distintos), descartadas, outros_dep, None


def kb(payload):
    return len(json.dumps(payload).encode("utf-8")) / 1024.0


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Motor E1 — foto diária do estoque.")
    ap.add_argument("--dry-run", action="store_true", help="monta o payload mas NÃO chama a RPC")
    ap.add_argument("--limit", type=int, default=0, help="processa só as N primeiras lojas (0 = todas)")
    ap.add_argument("--data", type=str, default=None, help="override do data_inventario (YYYY-MM-DD)")
    ap.add_argument("--loja", type=int, default=None, help="processa só este cod_microvix")
    args = ap.parse_args()
    exigir_env()

    data_inv = args.data or (date.today() - timedelta(days=1)).isoformat()

    print("=" * 78)
    print(f"MOTOR E1 — foto de estoque | data_inventario={data_inv} | cod_deposito={COD_DEP}"
          f" | modo={'DRY-RUN' if args.dry_run else 'GRAVAÇÃO'}")
    print("=" * 78)

    # 1) LISTA DE LOJAS — do BANCO
    try:
        cods = buscar_lojas_ativas()
    except Exception as e:
        print(f"ERRO fatal ao ler lojas do banco: {e!r}")
        sys.exit(1)
    print(f"Lojas ativas no banco: {len(cods)} → {cods}")

    if args.loja is not None:
        cods = [c for c in cods if c == args.loja]
        if not cods:
            print(f"⚠️ --loja {args.loja} não está na lista de lojas ativas. Nada a fazer.")
            sys.exit(0)
    if args.limit and args.limit > 0:
        cods = cods[:args.limit]
    print(f"Vou processar {len(cods)} loja(s): {cods}\n")

    t_ini = time.time()
    resultados = []  # dicts por loja

    for cod in cods:
        r = {"cod": cod, "linhas": 0, "dep1": 0.0, "dep7": 0.0,
             "descartadas": 0, "outros_dep": 0,
             "antes": None, "gravados": None, "variacao": None,
             "seg": 0.0, "status": "?", "motivo": ""}
        try:
            cnpj = CNPJS.get(cod)
            if not cnpj:
                # cod_microvix ativo mas sem CNPJ no dict → ERRO da loja, segue.
                r["status"] = "erro"
                r["motivo"] = "cod_microvix ativo sem CNPJ no dict CNPJS"
                print(f"cod {cod:>2}: ✗ ERRO — {r['motivo']}")
                resultados.append(r)
                continue

            erro, cols, regs, seg = inventario(cnpj, data_inv)
            r["seg"] = seg
            if erro:
                r["status"] = "erro"
                r["motivo"] = f"Linx: {erro}"
                print(f"cod {cod:>2}: ✗ ERRO Linx (t={seg:.2f}s) | mensagem CRUA: {erro}")
                resultados.append(r)
                continue

            itens, dep1, dep7, distintos, descartadas, outros_dep, erro_cols = extrair_itens(cols, regs)
            r["linhas"] = len(itens)
            r["dep1"] = dep1
            r["dep7"] = dep7
            r["descartadas"] = descartadas
            r["outros_dep"] = outros_dep
            if erro_cols:
                r["status"] = "erro"
                r["motivo"] = erro_cols
                print(f"cod {cod:>2}: ✗ ERRO — {erro_cols}")
                resultados.append(r)
                continue

            # Coerência: itens (parseadas) + descartadas deve == linhas da API.
            if len(itens) + descartadas != len(regs):
                print(f"cod {cod:>2}: ⚠️ INCOERÊNCIA de linhas: itens={len(itens)} + "
                      f"descartadas={descartadas} != regs={len(regs)}")

            # API devolveu 0 linhas → NÃO chama a RPC (ALERTA, segue).
            if len(itens) == 0:
                r["status"] = "alerta_vazio"
                r["motivo"] = "API devolveu 0 linhas"
                print(f"cod {cod:>2}: ⚠️ ALERTA — 0 linhas (t={seg:.2f}s). RPC não chamada.")
                resultados.append(r)
                continue

            payload = {"p_cod_microvix": cod, "p_data_foto": data_inv, "p_itens": itens}

            if args.dry_run:
                r["status"] = "dry"
                print(f"cod {cod:>2}: [dry] t={seg:.2f}s | linhas={len(itens)} | descart={descartadas} | "
                      f"outros_dep={outros_dep} | distintos={distintos} | dep1={dep1:g} dep7={dep7:g} | "
                      f"payload={kb(payload):.1f} KB")
                resultados.append(r)
                continue

            # 3) GRAVA via RPC (uma chamada por loja — a RPC apaga a loja e insere).
            ok, dados = gravar_estoque_loja(cod, data_inv, itens)
            if not ok:
                r["status"] = "erro"
                r["motivo"] = f"RPC: {dados}"
                print(f"cod {cod:>2}: ✗ ERRO RPC (t={seg:.2f}s) | {dados}")
                resultados.append(r)
                continue

            r["antes"] = dados.get("antes")
            r["gravados"] = dados.get("gravados")
            r["variacao"] = dados.get("variacao")
            # descartadas > 0 → status ALERTA (não OK), pra aparecer na cara.
            r["status"] = "alerta" if descartadas > 0 else "ok"
            marca = "⚠️ ALERTA" if descartadas > 0 else "✓ OK"
            print(f"cod {cod:>2}: {marca} t={seg:.2f}s | linhas={len(itens)} | descart={descartadas} | "
                  f"outros_dep={outros_dep} | loja_id={dados.get('loja_id')} antes={r['antes']} "
                  f"gravados={r['gravados']} variacao={r['variacao']}")
            resultados.append(r)

        except Exception as e:
            # try/except por loja: uma falhar não derruba as outras.
            r["status"] = "erro"
            r["motivo"] = f"exceção: {e!r}"
            print(f"cod {cod:>2}: ✗ EXCEÇÃO — {e!r}")
            resultados.append(r)

    tempo_total = time.time() - t_ini

    # ── RELATÓRIO FINAL ──────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("RELATÓRIO FINAL")
    print("=" * 78)
    cab = (f"{'cod':>4} | {'linhas':>6} | {'descart':>7} | {'outros':>6} | {'dep1':>10} | {'dep7':>10} | "
           f"{'antes':>7} | {'gravados':>8} | {'variacao':>8} | {'tempo':>6}")
    print(cab)
    print("-" * len(cab))

    def cel(v):
        return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))

    for r in resultados:
        print(f"{r['cod']:>4} | {r['linhas']:>6} | {r['descartadas']:>7} | {r['outros_dep']:>6} | "
              f"{r['dep1']:>10g} | {r['dep7']:>10g} | "
              f"{cel(r['antes']):>7} | {cel(r['gravados']):>8} | {cel(r['variacao']):>8} | {r['seg']:>5.2f}s")

    ok = [r for r in resultados if r["status"] == "ok"]
    dry = [r for r in resultados if r["status"] == "dry"]
    com_descarte = [r for r in resultados if r["status"] == "alerta"]  # gravou, mas descartou linha(s)
    alertas = [r for r in resultados if r["status"] == "alerta_vazio"]
    erros = [r for r in resultados if r["status"] == "erro"]
    total_gravadas = sum((r["gravados"] or 0) for r in (ok + com_descarte))

    print("\nRESUMO:")
    if args.dry_run:
        print(f"  modo DRY-RUN — nada gravado. {len(dry)} loja(s) montaram payload.")
    print(f"  lojas OK ..............: {len(ok)}")
    print(f"  lojas gravadas c/ DESCARTE: {len(com_descarte)}"
          + (f" → {[r['cod'] for r in com_descarte]}" if com_descarte else ""))
    print(f"  lojas com ALERTA (0 lin): {len(alertas)}"
          + (f" → {[r['cod'] for r in alertas]}" if alertas else ""))
    print(f"  lojas com ERRO ........: {len(erros)}")
    print(f"  total de linhas gravadas: {total_gravadas}")
    print(f"  tempo total ...........: {tempo_total:.1f}s")

    if erros:
        print("\n  FALHAS (loja → motivo):")
        for r in erros:
            print(f"    cod {r['cod']}: {r['motivo']}")

    # ── Status final + exit code (pro agendamento marcar VERMELHO quando falha) ──
    # Falha = loja com ERRO ou loja com ALERTA (0 linhas = anômalo). Descarte NÃO é
    # falha (gravou), mas imprime um aviso bem visível.
    falhou = len(erros) > 0 or len(alertas) > 0

    if com_descarte and not falhou:
        print("\n" + "⚠️ " * 12)
        print(f"⚠️ ATENÇÃO — {len(com_descarte)} loja(s) gravaram COM DESCARTE de linhas: "
              f"{[r['cod'] for r in com_descarte]}.")
        print("   O estoque foi gravado, mas confira as linhas descartadas acima.")
        print("⚠️ " * 12)

    # Linha final de status (fácil de achar/grep no log do Actions).
    if falhou:
        print(f"\nSTATUS=FALHA lojas_erro={len(erros)} lojas_alerta={len(alertas)}")
    else:
        print("\nSTATUS=OK")
    print("Fim.")

    # Exit code: dry-run NUNCA falha o job (não é execução de produção).
    if args.dry_run:
        sys.exit(0)
    sys.exit(1 if falhou else 0)


if __name__ == "__main__":
    main()
