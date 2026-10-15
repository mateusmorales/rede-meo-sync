#!/usr/bin/env python3
# =============================================================================
# scripts/backfill-fiscal-entradas.py — recarrega os CAMPOS FISCAIS das notas de
# entrada (vendas_itens, operacao='E') a partir do LinxMovimento.
#
# Campos: aliquota_ipi, base_ipi, valor_ipi, desconto_total_item, cst_icms,
#         aliquota_icms, base_icms, valor_icms.
#
# COMO FUNCIONA
#   - Por loja e por MÊS DE LANÇAMENTO (o LinxMovimento filtra por data_lancamento;
#     a data_documento pode ser anterior), de --ini (2024-01-01) até --fim (hoje BRT).
#   - Paginação e ritmo iguais aos do Motor 2 (cursor timestamp, pausa 0,15 s).
#   - Grava SÓ via rpc/vendas_itens_atualiza_fiscal: UPDATE dos 8 campos em linhas que
#     JÁ existem, casando pela PK (transacao) e operacao='E'. Nunca insere, nunca
#     duplica, não toca em nenhuma outra coluna. Rodar de novo é inofensivo.
#   - Janela que falhar (rede/Linx/RPC esgotou as tentativas) é registrada e o script
#     segue; no fim lista as janelas com erro e sai com código 1.
#
# ENV: LINX_CHAVE, SUPABASE_SERVICE_KEY (GitHub Secrets). Opcionais: IN_DATA_INI,
#      IN_DATA_FIM, IN_LOJAS ("10,8,27"), IN_DRY ("true" = lê a Linx e não grava).
# =============================================================================
import os, sys, json, time, calendar, urllib.request, urllib.error, xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone, timedelta

SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
SUPA_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CHAVE = os.environ["LINX_CHAVE"]
LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
BR = timezone(timedelta(hours=-3))
LOTE = 1000
MAX_PAGINAS = 2000   # por loja+mês; estourar = ERRO da janela (nunca corta calado)
TENTATIVAS = 5

# mesma fonte dos motores (dict, não é segredo)
CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}


def dec(s):
    # mesmo conversor dos motores: vírgula ou ponto decimal → número exato como texto
    s = (s or "").strip()
    if not s: return None
    if "," in s: s = s.replace(".", "").replace(",", ".")
    try: return str(Decimal(s))
    except InvalidOperation: return None


def get_lojas():
    url = f"{SUPA_URL}/rest/v1/lojas?select=id,cod_microvix&cod_microvix=not.is.null"
    req = urllib.request.Request(url, headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return {int(x["cod_microvix"]): x["id"] for x in json.load(r)}


def meses(ini, fim):
    out, a, m = [], ini.year, ini.month
    while (a, m) <= (fim.year, fim.month):
        mi = date(a, m, 1); mf = date(a, m, calendar.monthrange(a, m)[1])
        out.append((max(mi, ini).isoformat(), min(mf, fim).isoformat()))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return out


def chamar_linx(cnpj, di, df, ts):
    body = (f'<?xml version="1.0" encoding="utf-8"?><LinxMicrovix><Authentication user="linx_export" '
            f'password="linx_export"/><ResponseFormat>xml</ResponseFormat><Command><Name>LinxMovimento</Name>'
            f'<Parameters><Parameter id="chave">{CHAVE}</Parameter><Parameter id="cnpjEmp">{cnpj}</Parameter>'
            f'<Parameter id="timestamp">{ts}</Parameter><Parameter id="data_inicial">{di}</Parameter>'
            f'<Parameter id="data_fim">{df}</Parameter></Parameters></Command></LinxMicrovix>')
    req = urllib.request.Request(LINX, data=body.encode("utf-8"), headers={"Content-Type": "text/xml; charset=utf-8"})
    for t in range(1, TENTATIVAS + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            if t == TENTATIVAS: raise
            print(f"      linx retry {t}/{TENTATIVAS}: {e!r}"[:200], flush=True)
            time.sleep(4 * t)


def entradas_da_janela(cnpj, di, df):
    """Itens operacao='E' lançados em [di..df] (paginação do Motor 2). Devolve (itens, paginas)."""
    itens, ts, vistos, pag = {}, 0, set(), 0
    while True:
        pag += 1
        if pag > MAX_PAGINAS:
            raise RuntimeError(f"passou de {MAX_PAGINAS} páginas — janela não concluída")
        xml = chamar_linx(cnpj, di, df, ts)
        rd = ET.fromstring(xml.lstrip("﻿")).find("ResponseData")
        if rd is None or rd.find("C") is None: break
        cols = [(d.text or "") for d in rd.find("C").findall("D")]; ix = {c: i for i, c in enumerate(cols)}
        rows = rd.findall("R")
        if not rows: break
        maxts = ts; novo = 0
        for r in rows:
            ds = r.findall("D")
            def g(n, ds=ds, ix=ix):
                i = ix.get(n); return (ds[i].text or "").strip() if i is not None and i < len(ds) else ""
            try: maxts = max(maxts, int(g("timestamp")))
            except ValueError: pass
            tx = g("transacao")
            if not tx or tx in vistos: continue
            vistos.add(tx); novo += 1
            if g("operacao") != "E": continue
            itens[tx] = {"transacao": int(tx),
                         "aliquota_ipi": dec(g("aliquota_ipi")), "base_ipi": dec(g("base_ipi")),
                         "valor_ipi": dec(g("valor_ipi")), "desconto_total_item": dec(g("desconto_total_item")),
                         "cst_icms": g("cst_icms") or None, "aliquota_icms": dec(g("aliquota_icms")),
                         "base_icms": dec(g("base_icms")), "valor_icms": dec(g("valor_icms"))}
        if novo == 0: break
        ts = maxts - 1 if maxts > 0 else 0
        time.sleep(0.15)
    return list(itens.values()), pag


def gravar(itens):
    """rpc/vendas_itens_atualiza_fiscal em lotes. Devolve (recebidos, atualizados)."""
    rec = atu = 0
    url = f"{SUPA_URL}/rest/v1/rpc/vendas_itens_atualiza_fiscal"
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
    for i in range(0, len(itens), LOTE):
        data = json.dumps({"p_itens": itens[i:i + LOTE]}).encode("utf-8")
        for t in range(1, TENTATIVAS + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"),
                                            timeout=300) as r:
                    res = json.load(r)
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]
                if t == TENTATIVAS: raise RuntimeError(f"RPC HTTP {e.code}: {msg}")
                print(f"      rpc retry {t}/{TENTATIVAS}: HTTP {e.code} {msg}"[:200], flush=True); time.sleep(5 * t)
            except Exception as e:
                if t == TENTATIVAS: raise
                print(f"      rpc retry {t}/{TENTATIVAS}: {e!r}"[:200], flush=True); time.sleep(5 * t)
        rec += res["recebidos"]; atu += res["atualizados"]
    return rec, atu


def main():
    hoje = datetime.now(BR).date()
    ini = date.fromisoformat((os.environ.get("IN_DATA_INI") or "").strip() or "2024-01-01")
    fim = date.fromisoformat((os.environ.get("IN_DATA_FIM") or "").strip() or hoje.isoformat())
    dry = (os.environ.get("IN_DRY") or "").strip().lower() == "true"
    filtro = {int(x) for x in (os.environ.get("IN_LOJAS") or "").replace(" ", "").split(",") if x}
    if ini > fim: sys.exit(f"ERRO: data inicial {ini} posterior à final {fim}")

    lojas = get_lojas()
    cods = sorted(c for c in CNPJS if c in lojas and (not filtro or c in filtro))
    if filtro - set(cods): print(f"⚠️ lojas pedidas fora da lista: {sorted(filtro - set(cods))}")
    janelas = meses(ini, fim)
    print(f"BACKFILL FISCAL ENTRADAS | lançamento {ini}..{fim} | {len(cods)} lojas x {len(janelas)} meses"
          f" | {'DRY-RUN (não grava)' if dry else 'GRAVAÇÃO'}", flush=True)

    t0 = time.time(); erros = []; tot = {"E": 0, "atualizados": 0, "paginas": 0}
    for cod in cods:
        tl = time.time(); le = la = 0
        for di, df in janelas:
            try:
                itens, pags = entradas_da_janela(CNPJS[cod], di, df)
                tot["paginas"] += pags
                rec, atu = (len(itens), 0) if dry or not itens else gravar(itens)
                le += len(itens); la += atu
                if itens:
                    falt = "" if dry or atu == rec else f" | {rec - atu} sem linha no banco"
                    print(f"   loja {cod:>2} {di[:7]}: {len(itens):>5} E | {pags} pág | atualizados {atu}{falt}", flush=True)
            except Exception as e:
                erros.append((cod, di, df, f"{type(e).__name__}: {e}"[:200]))
                print(f"   ✗ loja {cod:>2} {di[:7]}: ERRO {erros[-1][3]}", flush=True)
        tot["E"] += le; tot["atualizados"] += la
        print(f"== loja {cod:>2}: {le} itens E | {la} atualizados | {time.time()-tl:.0f}s", flush=True)

    print("\nRESUMO")
    print(f"  itens E lidos da Linx ..: {tot['E']}")
    print(f"  linhas atualizadas .....: {tot['atualizados']}{' (dry-run)' if dry else ''}")
    print(f"  páginas lidas ..........: {tot['paginas']}")
    print(f"  tempo ..................: {(time.time()-t0)/60:.1f} min")
    print(f"  janelas com erro .......: {len(erros)}")
    for e in erros: print(f"    loja {e[0]} {e[1]}..{e[2]}: {e[3]}")
    sys.exit(1 if erros else 0)


if __name__ == "__main__":
    main()
