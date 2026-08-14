#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/carregar-precos.py  —  MOTOR E3: preço de varejo.
#
# Varre o catálogo de PREÇOS de UMA loja (loja 1) na API Linx e grava no Supabase
# via RPC public.gravar_produtos_preco(p_itens jsonb), em lotes de 5.000.
#
# FONTE (paginação já validada em testar-produtos-detalhes.py):
#   LinxProdutosDetalhes | retornar_saldo_zero=1 | data_mov_ini=2024-01-01
#   | data_mov_fim=HOJE | cursor por timestamp | páginas de 5.000 | trava 80 pág.
#
# TRAVAS DE SEGURANÇA (padrão do motor-e1-estoque.py):
#   • 0 registros no total  → NÃO grava. ALERTA + exit 1.
#   • < 150.000 produtos    → NÃO grava (a última varredura completa deu 203.750;
#                             queda dessa ordem = paginação parou no meio). exit 1.
#   • preço parse ESTRITO   → não-numérico é DESCARTE contado, nunca 0.
#                             descartados > 0 → status ALERTA → exit 1.
#   • exit code: erro/alerta → STATUS=FALHA + exit 1 ; tudo ok → STATUS=OK + exit 0.
#   • --dry-run: varre, imprime o resumo, NÃO grava, e SEMPRE sai com 0.
#
# INFRA (ambiente): LINX_CHAVE, SUPABASE_SERVICE_KEY. A URL do Supabase é
# hardcoded (padrão do motor-e1-estoque.py), não vem de env. O --dry-run não
# exige SUPABASE_SERVICE_KEY (não grava).
#
# COMO RODAR:
#   export LINX_CHAVE="..."; export SUPABASE_SERVICE_KEY="...service_role..."
#   python3 scripts/carregar-precos.py --dry-run
#   python3 scripts/carregar-precos.py
# =============================================================================

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date

CHAVE = os.environ.get("LINX_CHAVE")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# URL hardcoded (mesmo padrão do motor-e1-estoque.py) — não vem de secret/env.
SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
TIMEOUT = 600
METODO = "LinxProdutosDetalhes"

COD_LOJA = 1
DATA_INI = "2024-01-01"

MIN_PAGINA_CHEIA = 5000    # página com menos = última
MAX_PAGINAS = 80           # trava de segurança
LOTE_RPC = 5000            # itens por chamada da RPC
MIN_PRODUTOS = 180000      # piso de sanidade (~88% do baseline 203.750; catálogo só cresce)

# CNPJs por cod_microvix — MESMA fonte dos motores (dict, não é segredo).
CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}

SUB_PROD = ("cod_produto", "codproduto", "produto")
SUB_TS = ("timestamp",)
SUB_PV = ("preco_venda", "precovenda", "valor_venda", "valorvenda", "preco_a_vista", "vlr_venda")


def exigir_env(precisa_supa):
    # SUPA_URL é hardcoded (não é env). Só a service_role vem de secret, e apenas
    # quando vai gravar (dry-run não exige SUPABASE_SERVICE_KEY).
    faltam = [n for n, v in [("LINX_CHAVE", CHAVE)] if not v]
    if precisa_supa:
        faltam += [n for n, v in (("SUPABASE_SERVICE_KEY", SUPA_KEY),) if not v]
    if faltam:
        print("ERRO: defina no ambiente: " + ", ".join(faltam))
        sys.exit(1)


# ── Helpers Linx (idênticos ao motor-e1-estoque.py) ──────────────────────────

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


def parse_preco(raw):
    """Parse ESTRITO: retorna float OU None (nunca 0 por falha). Vazio/não-numérico
    → None (o chamador conta como DESCARTE, não grava 0)."""
    s = str(raw).strip().replace(",", ".")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_cod(raw):
    """cod_produto → int OU None (descarte se não-numérico)."""
    s = str(raw).strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def mascarar(params):
    return {k: ("***" if k == "chave" else v) for k, v in params.items()}


def chamar_linx(params):
    body = montar_body(METODO, params).encode("utf-8")
    req = urllib.request.Request(LINX, data=body, headers={"Content-Type": "text/xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            xml = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')}", [], []
    except Exception as e:
        return f"rede/timeout: {e!r}", [], []
    cols, regs = parse_cr(xml)
    erro = None if cols else (xml.strip() or "(resposta vazia, sem esquema C/R)")
    return erro, cols, regs


# ── Supabase REST (service_role — mesmo padrão do motor-e1-estoque.py) ────────

def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    if extra:
        h.update(extra)
    return h


def gravar_lote(itens):
    """RPC gravar_produtos_preco(p_itens). Retorna (ok, obj|erro)."""
    url = f"{SUPA_URL}/rest/v1/rpc/gravar_produtos_preco"
    data = json.dumps({"p_itens": itens}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=supa_headers({"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            corpo = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
    except Exception as e:
        return False, f"rede/timeout: {e!r}"
    try:
        obj = json.loads(corpo) if corpo.strip() else {}
    except json.JSONDecodeError:
        return False, f"resposta não-JSON: {corpo[:200]}"
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    # HTTP 200 NÃO garante sucesso: a RPC pode devolver {"ok": false, "erro": ...}.
    # ok explicitamente False → o lote FALHA (gravar_tudo aborta, script sai 1).
    if isinstance(obj, dict) and obj.get("ok") is False:
        return False, "RPC recusou: " + str(obj.get("erro"))
    return True, obj


def gravar_tudo(itens):
    """Grava em lotes de LOTE_RPC. Agrega o retorno: soma recebidos/novos/
    atualizados; linhas_antes = do 1º lote; linhas_depois = do último.
    Retorna (ok, agregado|erro, n_lotes)."""
    lotes = [itens[i:i + LOTE_RPC] for i in range(0, len(itens), LOTE_RPC)]
    agg = {"recebidos": 0, "novos": 0, "atualizados": 0, "linhas_antes": None, "linhas_depois": None}
    for k, lote in enumerate(lotes, 1):
        ok, obj = gravar_lote(lote)
        if not ok:
            return False, f"lote {k}/{len(lotes)}: {obj}", len(lotes)
        agg["recebidos"] += int(num(obj.get("recebidos")))
        agg["novos"] += int(num(obj.get("novos")))
        agg["atualizados"] += int(num(obj.get("atualizados")))
        if agg["linhas_antes"] is None:
            agg["linhas_antes"] = obj.get("linhas_antes")
        agg["linhas_depois"] = obj.get("linhas_depois")
        print(f"    lote {k}/{len(lotes)}: {len(lote)} itens | recebidos={obj.get('recebidos')} "
              f"novos={obj.get('novos')} atualizados={obj.get('atualizados')} "
              f"antes={obj.get('linhas_antes')} depois={obj.get('linhas_depois')}")
    return True, agg, len(lotes)


# ── Varredura paginada (loja 1) ──────────────────────────────────────────────

def varrer(cnpj, data_fim):
    """Retorna (precos, paginas, total_regs, aviso). precos = {cod_produto_raw: preco_raw}
    (1ª ocorrência). Para em: vazia / <5000 / cursor parado / 80 páginas."""
    precos = {}
    total_regs = 0
    pagina = 0
    ts = 0
    aviso = None
    while True:
        if pagina >= MAX_PAGINAS:
            aviso = f"trava de {MAX_PAGINAS} páginas — pode haver mais"
            break
        pagina += 1
        cursor_ini = ts
        params = {"chave": CHAVE, "cnpjEmp": cnpj, "retornar_saldo_zero": 1,
                  "data_mov_ini": DATA_INI, "data_mov_fim": data_fim, "timestamp": ts}
        print(f"  → pág {pagina} | {METODO} | params={mascarar(params)}")
        erro, cols, regs = chamar_linx(params)
        if erro:
            print("  ✗ ERRO Linx — resposta CRUA completa:")
            print("    " + erro.replace("\n", "\n    "))
            aviso = f"erro na página {pagina}"
            break
        col_prod = achar_col(cols, *SUB_PROD)
        col_ts = achar_col(cols, *SUB_TS)
        col_pv = achar_col(cols, *SUB_PV)
        x = len(regs)
        total_regs += x
        cursor_fim = cursor_ini
        novos = 0
        for r in regs:
            if col_ts:
                try:
                    cursor_fim = max(cursor_fim, int(num(r.get(col_ts))))
                except (ValueError, TypeError):
                    pass
            cp = str(r.get(col_prod, "")).strip() if col_prod else ""
            if cp and cp not in precos:
                precos[cp] = str(r.get(col_pv, "")).strip() if col_pv else ""
                novos += 1
        avancou = cursor_fim > cursor_ini
        print(f"  pag {pagina} | registros={x} | cursor_ini={cursor_ini} | cursor_fim={cursor_fim} | "
              f"avancou={'SIM' if avancou else 'NAO'} | produtos_novos={novos} | acumulado={len(precos)}")
        if not col_ts:
            aviso = "sem coluna 'timestamp' no esquema — não dá p/ paginar"
            print(f"  ⚠️ {aviso}")
            break
        if x == 0:
            break
        if x < MIN_PAGINA_CHEIA:
            break
        if not avancou:
            aviso = "cursor não avançou"
            break
        ts = cursor_fim
        time.sleep(0.15)
    return precos, pagina, total_regs, aviso


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Motor E3 — preço de varejo (loja 1).")
    ap.add_argument("--dry-run", action="store_true", help="varre e resume, mas NÃO grava (sempre exit 0)")
    args = ap.parse_args()
    exigir_env(precisa_supa=not args.dry_run)

    data_fim = date.today().isoformat()
    cnpj = CNPJS.get(COD_LOJA)

    print("=" * 90)
    print(f"MOTOR E3 — preços | loja {COD_LOJA} (CNPJ {cnpj}) | data_mov {DATA_INI}→{data_fim} | "
          f"modo={'DRY-RUN' if args.dry_run else 'GRAVAÇÃO'}")
    print("=" * 90)
    if not cnpj:
        print(f"ERRO fatal: loja {COD_LOJA} sem CNPJ no dict CNPJS.")
        sys.exit(1)

    t0 = time.time()
    precos, paginas, total_regs, aviso_varredura = varrer(cnpj, data_fim)

    # ── Monta itens com parse ESTRITO (descarte contado, nunca zero) ─────────
    itens = []
    descartados = 0
    exemplos_descarte = []
    for cp_raw, pv_raw in precos.items():
        cod = parse_cod(cp_raw)
        preco = parse_preco(pv_raw)
        if cod is None or preco is None:
            descartados += 1
            if len(exemplos_descarte) < 10:
                exemplos_descarte.append({"cod_produto": cp_raw, "preco_venda": pv_raw})
            continue
        itens.append({"cod_produto": cod, "preco_venda": preco})

    distintos = len(precos)
    tempo_varredura = time.time() - t0

    print("\n" + "=" * 90)
    print("RESUMO DA VARREDURA")
    print("=" * 90)
    print(f"  páginas ................: {paginas}")
    print(f"  registros lidos ........: {total_regs}")
    print(f"  cod_produto distintos ..: {distintos}")
    print(f"  itens válidos p/ gravar : {len(itens)}")
    print(f"  DESCARTADOS ............: {descartados}"
          + (f"  (ex.: {exemplos_descarte[:5]})" if descartados else ""))
    if aviso_varredura:
        print(f"  ⚠️ aviso da varredura ..: {aviso_varredura}")
    print(f"  tempo de varredura .....: {tempo_varredura:.1f}s")

    # ── DRY-RUN: nunca grava, sempre sai 0 ───────────────────────────────────
    if args.dry_run:
        # Sinaliza (sem falhar) se as travas de produção BLOQUEARIAM esta carga.
        bloquearia = []
        if total_regs == 0:
            bloquearia.append("0 registros")
        if distintos < MIN_PRODUTOS:
            bloquearia.append(f"distintos {distintos} < {MIN_PRODUTOS}")
        if descartados > 0:
            bloquearia.append(f"{descartados} descartados (alerta)")
        print("\n  [DRY-RUN] RPC NÃO chamada.")
        if bloquearia:
            print(f"  [DRY-RUN] em produção isto seria bloqueado/alertado por: {', '.join(bloquearia)}")
        print("\nSTATUS=OK (dry-run — nada gravado)")
        print("Fim.")
        sys.exit(0)

    # ── TRAVA 1: 0 registros → não grava, ALERTA, exit 1 ─────────────────────
    if total_regs == 0:
        print("\n⚠️ ALERTA — a API devolveu 0 registros no total. RPC NÃO chamada.")
        print("STATUS=FALHA motivo=zero_registros")
        sys.exit(1)

    # ── TRAVA 1b: varredura com AVISO → parou anormalmente (erro de página,
    # cursor travado, sem coluna timestamp, ou trava de 80 páginas) → catálogo
    # PARCIAL. NÃO grava. A parada NORMAL (página < 5.000) não seta aviso. ────
    if aviso_varredura:
        print("\n" + "⚠️ " * 12)
        print(f"⚠️ ALERTA — varredura terminou com AVISO: {aviso_varredura}. Catálogo pode "
              f"estar PARCIAL. RPC NÃO chamada.")
        print("⚠️ " * 12)
        print("STATUS=FALHA motivo=varredura_incompleta_aviso")
        sys.exit(1)

    # ── TRAVA 2: < piso de produtos → não grava, exit 1 ──────────────────────
    if distintos < MIN_PRODUTOS:
        print(f"\n⚠️ ALERTA — só {distintos} produtos distintos (< {MIN_PRODUTOS}); a varredura "
              f"provavelmente parou no meio (baseline completo = 203.750). RPC NÃO chamada.")
        print(f"STATUS=FALHA motivo=varredura_incompleta distintos={distintos}")
        sys.exit(1)

    # ── GRAVA em lotes de 5.000 ──────────────────────────────────────────────
    print(f"\n  Gravando {len(itens)} itens em lotes de {LOTE_RPC}...")
    ok, resultado, n_lotes = gravar_tudo(itens)
    tempo_total = time.time() - t0

    if not ok:
        print(f"\n✗ ERRO na RPC: {resultado}")
        print(f"STATUS=FALHA motivo=rpc lotes={n_lotes}")
        sys.exit(1)

    print("\n" + "=" * 90)
    print("RELATÓRIO FINAL")
    print("=" * 90)
    print(f"  páginas ................: {paginas}")
    print(f"  registros lidos ........: {total_regs}")
    print(f"  cod_produto distintos ..: {distintos}")
    print(f"  descartados ............: {descartados}")
    print(f"  lotes enviados .........: {n_lotes}")
    print(f"  RPC retornou (agregado): {json.dumps(resultado, ensure_ascii=False)}")
    print(f"  tempo total ............: {tempo_total:.1f}s")

    # ── Status final: descartados > 0 → ALERTA → exit 1 ──────────────────────
    if descartados > 0:
        print("\n" + "⚠️ " * 12)
        print(f"⚠️ ATENÇÃO — {descartados} produto(s) DESCARTADO(s) por preço/código não-numérico. "
              f"O restante foi gravado, mas confira os descartes.")
        print("⚠️ " * 12)
        print(f"STATUS=FALHA motivo=descartes descartados={descartados}")
        sys.exit(1)

    print("\nSTATUS=OK")
    print("Fim.")
    sys.exit(0)


if __name__ == "__main__":
    main()
