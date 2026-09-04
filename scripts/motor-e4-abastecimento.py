#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/motor-e4-abastecimento.py  —  MOTOR E4: gerador da grade de abastecimento.
#
# Motor NOVO e PRÓPRIO do módulo Abastecimento. NÃO compartilha arquivo/workflow
# com os outros motores — só reaproveita o PADRÃO do Motor E1 (urllib puro contra
# o PostgREST do Supabase via service_role, zero dependência de pip).
#
# FLUXO:
#   1) POST em rpc/abastecimento_grade_refresh (body {}). Recalcula o cache —
#      leva ~15s. Falhou → aborta com exit 1. A resposta traz {linhas, gerado}:
#      os dois vão pro log. (--sem-refresh pula este passo.)
#   2) GET em abastecimento_grade_cache, PAGINADO com header Range (0-999,
#      1000-1999, ...) até vir menos de 1000. O PostgREST corta em 1000 por
#      padrão e devolve a página truncada CALADO — isso já mordeu hoje.
#   3) CONFERE: linhas lidas == linhas devolvidas pelo refresh. Diferiu → exit 1
#      SEM gravar. Arquivo incompleto é pior que arquivo nenhum.
#   4) Grava saida/grade.csv (UTF-8, vírgula, cabeçalho, sem índice).
#   5) Grava saida/manifesto.json: gerado_em (ISO UTC), data_estoque, linhas,
#      lojas, soma_estoque, soma_venda_90d, soma_venda_45d_frente.
#   6) TRAVA extra: lojas < 25 ou linhas < 1500 → exit 1 SEM gravar.
#
#   As validações (3 e 6) rodam ANTES de qualquer escrita e valem TAMBÉM no
#   dry-run: são defeito de DADO, não de gravação. Se falharem, o job fica
#   vermelho nos dois modos.
#
# FLAGS: --dry-run (faz tudo, não grava) | --sem-refresh (lê o cache como está)
#
# INFRA: só SUPABASE_SERVICE_KEY no ambiente. SUPA_URL hardcoded igual ao E1.
#        A chave NUNCA é impressa. Erro HTTP → corpo da resposta CRU.
#        O repo é PÚBLICO: saida/ está no .gitignore. O dado só vive no
#        rede-meo-dados (privado), publicado pelo workflow.
#
# COMO RODAR:
#   export SUPABASE_SERVICE_KEY="...service_role..."
#   python3 scripts/motor-e4-abastecimento.py --dry-run
#   python3 scripts/motor-e4-abastecimento.py --sem-refresh
#   python3 scripts/motor-e4-abastecimento.py          # refresh + grade completa
# =============================================================================

import os
import sys
import csv
import json
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
TIMEOUT_REFRESH = 600   # o refresh recalcula o cache inteiro (~15s, folga larga)
TIMEOUT_LEITURA = 300

TABELA = "abastecimento_grade_cache"
COLUNAS = ["loja", "categoria", "celula", "faixa", "faixa_ordem",
           "estoque", "venda_90d", "venda_45d_frente", "data_estoque"]
ORDEM = "loja,categoria,celula,faixa_ordem"

PAGINA = 1000           # o corte padrão do PostgREST
MAX_PAGINAS = 500       # cinto de segurança contra laço infinito (500k linhas)

DIR_SAIDA = "saida"
ARQ_CSV = os.path.join(DIR_SAIDA, "grade.csv")
ARQ_MANIFESTO = os.path.join(DIR_SAIDA, "manifesto.json")

# Travas de sanidade — grade menor que isso é sintoma, não é grade.
MIN_LOJAS = 25
MIN_LINHAS = 1500


def exigir_env():
    if not SUPA_KEY:
        print("ERRO: defina no ambiente: SUPABASE_SERVICE_KEY")
        sys.exit(1)


# ── helpers Supabase REST (service_role — mesmo padrão do Motor E1) ──────────

def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    if extra:
        h.update(extra)
    return h


def refresh_grade():
    """RPC abastecimento_grade_refresh (body {}). Retorna (ok, dados|erro).
    dados = {linhas, gerado}."""
    url = f"{SUPA_URL}/rest/v1/rpc/abastecimento_grade_refresh"
    req = urllib.request.Request(
        url, data=b"{}",
        headers=supa_headers({"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_REFRESH) as r:
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
    if not isinstance(obj, dict):
        return False, f"resposta inesperada: {corpo[:200]}"
    return True, obj


def ler_pagina(ini, fim, contar):
    """Uma página do cache via header Range. Retorna (linhas, content_range)."""
    url = (f"{SUPA_URL}/rest/v1/{TABELA}"
           f"?select={','.join(COLUNAS)}&order={ORDEM}")
    extra = {"Range-Unit": "items", "Range": f"{ini}-{fim}"}
    if contar:
        # Só na 1ª página: o total do servidor é um canário barato contra
        # paginação silenciosamente truncada.
        extra["Prefer"] = "count=exact"
    req = urllib.request.Request(url, headers=supa_headers(extra))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_LEITURA) as r:
            corpo = r.read().decode("utf-8")
            content_range = r.headers.get("Content-Range")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} na faixa {ini}-{fim}: {e.read().decode()[:400]}")
    except Exception as e:
        raise RuntimeError(f"rede/timeout na faixa {ini}-{fim}: {e!r}")
    try:
        linhas = json.loads(corpo) if corpo.strip() else []
    except json.JSONDecodeError:
        raise RuntimeError(f"resposta não-JSON na faixa {ini}-{fim}: {corpo[:200]}")
    if not isinstance(linhas, list):
        raise RuntimeError(f"resposta não é lista na faixa {ini}-{fim}: {corpo[:200]}")
    return linhas, content_range


def ler_cache_paginado():
    """Lê a grade INTEIRA em páginas de 1000 até vir menos de 1000.
    Retorna (linhas, total_servidor|None)."""
    todas = []
    total_servidor = None
    for pag in range(MAX_PAGINAS):
        ini = pag * PAGINA
        fim = ini + PAGINA - 1
        linhas, content_range = ler_pagina(ini, fim, contar=(pag == 0))
        if pag == 0 and content_range and "/" in content_range:
            cauda = content_range.split("/")[-1].strip()
            if cauda.isdigit():
                total_servidor = int(cauda)
        todas.extend(linhas)
        print(f"  página {pag:>3} (Range {ini}-{fim}): {len(linhas)} linha(s) | acumulado={len(todas)}")
        if len(linhas) < PAGINA:
            return todas, total_servidor
    raise RuntimeError(
        f"paginação passou de {MAX_PAGINAS} páginas ({len(todas)} linhas) sem terminar — abortando")


# ── agregados / manifesto ────────────────────────────────────────────────────

def num(v):
    """Valor numérico tolerante: None/vazio/não-numérico → 0.0."""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def limpo(f):
    """Soma bonita: int quando inteira, senão float com 2 casas."""
    f = round(f, 2)
    return int(f) if f.is_integer() else f


def resumir(linhas):
    """(lojas_distintas, data_estoque, datas_distintas, somas)."""
    lojas = {r.get("loja") for r in linhas if r.get("loja") is not None}
    datas = sorted({str(r.get("data_estoque")) for r in linhas if r.get("data_estoque")})
    somas = {
        "soma_estoque": limpo(sum(num(r.get("estoque")) for r in linhas)),
        "soma_venda_90d": limpo(sum(num(r.get("venda_90d")) for r in linhas)),
        "soma_venda_45d_frente": limpo(sum(num(r.get("venda_45d_frente")) for r in linhas)),
    }
    # data_estoque do manifesto = a MAIS RECENTE. Se houver mais de uma, é um
    # canário (loja atrasada no E1) — vai gritar no log, mas não trava.
    data_estoque = datas[-1] if datas else None
    return lojas, data_estoque, datas, somas


def gravar_csv(linhas):
    os.makedirs(DIR_SAIDA, exist_ok=True)
    with open(ARQ_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        w.writeheader()
        for r in linhas:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in COLUNAS})
    return os.path.getsize(ARQ_CSV)


def gravar_manifesto(manifesto):
    os.makedirs(DIR_SAIDA, exist_ok=True)
    with open(ARQ_MANIFESTO, "w", encoding="utf-8") as f:
        json.dump(manifesto, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return os.path.getsize(ARQ_MANIFESTO)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Motor E4 — grade de abastecimento.")
    ap.add_argument("--dry-run", action="store_true",
                    help="faz tudo (refresh, leitura, conferência) mas NÃO grava os arquivos")
    ap.add_argument("--sem-refresh", action="store_true",
                    help="pula o refresh e lê o cache como está")
    args = ap.parse_args()
    exigir_env()

    modo = "DRY-RUN" if args.dry_run else "GRAVAÇÃO"
    print("=" * 78)
    print(f"MOTOR E4 — grade de abastecimento | modo={modo} | "
          f"refresh={'NÃO (--sem-refresh)' if args.sem_refresh else 'SIM'}")
    print("=" * 78)

    t_ini = time.time()

    # 1) REFRESH do cache ────────────────────────────────────────────────────
    linhas_refresh = None
    if args.sem_refresh:
        print("\n[1/6] REFRESH pulado (--sem-refresh). Lendo o cache como está.")
        print("      ⚠️ Sem o refresh não há número do servidor pra conferir — o passo 3 fica sem base.")
    else:
        print("\n[1/6] REFRESH — rpc/abastecimento_grade_refresh (~15s)...")
        t0 = time.time()
        ok, dados = refresh_grade()
        seg = time.time() - t0
        if not ok:
            print(f"      ✗ ERRO no refresh (t={seg:.2f}s) | {dados}")
            print("\nSTATUS=FALHA etapa=refresh")
            sys.exit(1)
        linhas_refresh = dados.get("linhas")
        print(f"      ✓ OK (t={seg:.2f}s) | linhas={linhas_refresh} | gerado={dados.get('gerado')}")
        if linhas_refresh is None:
            print("      ⚠️ o refresh não devolveu 'linhas' — o passo 3 fica sem base de conferência.")

    # 2) LEITURA PAGINADA ────────────────────────────────────────────────────
    print(f"\n[2/6] LEITURA paginada de {TABELA} (páginas de {PAGINA})...")
    try:
        linhas, total_servidor = ler_cache_paginado()
    except RuntimeError as e:
        print(f"      ✗ ERRO na leitura | {e}")
        print("\nSTATUS=FALHA etapa=leitura")
        sys.exit(1)
    print(f"      ✓ {len(linhas)} linha(s) lidas"
          + (f" | total informado pelo servidor: {total_servidor}" if total_servidor is not None else ""))

    # 3) CONFERÊNCIA — antes de gravar qualquer coisa ────────────────────────
    print("\n[3/6] CONFERÊNCIA...")
    falhas = []
    if linhas_refresh is None:
        print("      — sem número do refresh: conferência do passo 3 NÃO executada.")
    elif len(linhas) != linhas_refresh:
        falhas.append(f"lidas={len(linhas)} != refresh={linhas_refresh}")
        print(f"      ✗ DIVERGÊNCIA: lidas={len(linhas)} | refresh={linhas_refresh}")
    else:
        print(f"      ✓ lidas == refresh ({len(linhas)})")
    # Canário independente: o Content-Range do próprio PostgREST.
    if total_servidor is not None and total_servidor != len(linhas):
        falhas.append(f"lidas={len(linhas)} != Content-Range={total_servidor}")
        print(f"      ✗ DIVERGÊNCIA: lidas={len(linhas)} | Content-Range={total_servidor}"
              " (paginação incompleta)")

    lojas, data_estoque, datas, somas = resumir(linhas)

    # 6) TRAVAS de sanidade — também antes de gravar ─────────────────────────
    print("\n[4/6] TRAVAS de sanidade...")
    if len(lojas) < MIN_LOJAS:
        falhas.append(f"lojas={len(lojas)} < {MIN_LOJAS}")
        print(f"      ✗ lojas={len(lojas)} (mínimo {MIN_LOJAS})")
    else:
        print(f"      ✓ lojas={len(lojas)} (mínimo {MIN_LOJAS})")
    if len(linhas) < MIN_LINHAS:
        falhas.append(f"linhas={len(linhas)} < {MIN_LINHAS}")
        print(f"      ✗ linhas={len(linhas)} (mínimo {MIN_LINHAS})")
    else:
        print(f"      ✓ linhas={len(linhas)} (mínimo {MIN_LINHAS})")
    if len(datas) > 1:
        print(f"      ⚠️ ATENÇÃO — mais de uma data_estoque na grade: {datas}."
              " Alguma loja ficou pra trás no E1. Não trava, mas confira.")

    if falhas:
        print("\n  MOTIVOS:")
        for m in falhas:
            print(f"    - {m}")
        print("\n  NADA foi gravado. Arquivo incompleto é pior que arquivo nenhum.")
        print(f"\nSTATUS=FALHA etapa=validacao motivos={len(falhas)}")
        sys.exit(1)

    manifesto = {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "data_estoque": data_estoque,
        "linhas": len(linhas),
        "lojas": len(lojas),
        "soma_estoque": somas["soma_estoque"],
        "soma_venda_90d": somas["soma_venda_90d"],
        "soma_venda_45d_frente": somas["soma_venda_45d_frente"],
    }

    # 4) e 5) GRAVAÇÃO ───────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n[5/6] DRY-RUN — {ARQ_CSV} e {ARQ_MANIFESTO} NÃO foram gravados.")
    else:
        print(f"\n[5/6] GRAVANDO {ARQ_CSV} e {ARQ_MANIFESTO}...")
        tam_csv = gravar_csv(linhas)
        tam_man = gravar_manifesto(manifesto)
        print(f"      ✓ {ARQ_CSV} ({tam_csv/1024.0:.1f} KB)")
        print(f"      ✓ {ARQ_MANIFESTO} ({tam_man} bytes)")

    # ── RELATÓRIO FINAL ──────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("MANIFESTO")
    print("=" * 78)
    for k, v in manifesto.items():
        print(f"  {k:.<24}: {v}")
    print(f"\n  tempo total ............: {time.time() - t_ini:.1f}s")

    print("\nSTATUS=OK")
    print("Fim.")
    sys.exit(0)


if __name__ == "__main__":
    main()
