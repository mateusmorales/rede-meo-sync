#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/sonda-schema-vendedores.py  —  SONDAGEM read-only do LinxVendedores.
#
# Objetivo ÚNICO: imprimir o SCHEMA CRU COMPLETO — TODAS as colunas <C> que a API
# devolve pra um vendedor (não só as 9 que os motores mapeiam), com nome exato de
# cada coluna e valores de exemplo, pra caçar qualquer campo de situação/
# afastamento/férias/licença que nunca foi mapeado.
#
# NÃO grava nada. NÃO usa Supabase. Só lê a API. A chave NUNCA é impressa; o CNPJ
# aparece mascarado. Reaproveita a máquina de chamada dos motores.
#
# COMO RODAR:
#   export LINX_CHAVE="...sua-chave..."
#   python3 scripts/sonda-schema-vendedores.py
# =============================================================================

import os
import sys
import calendar
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import date

CHAVE = os.environ.get("LINX_CHAVE")
if not CHAVE:
    print('ERRO: defina LINX_CHAVE no ambiente:  export LINX_CHAVE="...sua-chave..."')
    sys.exit(1)

LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
METODO = "LinxVendedores"

# Palavras que denunciam um campo de situação/afastamento/férias/licença.
ALVO_SITUACAO = ["situ", "afast", "feria", "féria", "licenc", "licença", "status",
                 "ausen", "desliga", "demiss", "motivo", "condic", "estado"]

CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}


def mascara_cnpj(v):
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return (s[:2] + "*" * (len(s) - 6) + s[-4:]) if len(s) >= 6 else "***"


def _meses_atras(d, n):
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12; y -= 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


DATA_INI = _meses_atras(date.today(), 24).isoformat()
DATA_FIM = date.today().isoformat()

VARIACOES = [
    ("chave+cnpjEmp", {}),
    ("+timestamp=0", {"timestamp": 0}),
    ("+data_mov 24m", {"data_mov_ini": DATA_INI, "data_mov_fim": DATA_FIM}),
    ("+data_mov+timestamp", {"data_mov_ini": DATA_INI, "data_mov_fim": DATA_FIM, "timestamp": 0}),
]


def montar_body(metodo, params):
    ps = "".join(f'<Parameter id="{k}">{v}</Parameter>' for k, v in params.items())
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<LinxMicrovix><Authentication user="linx_export" password="linx_export"/>'
            f'<ResponseFormat>xml</ResponseFormat><Command><Name>{metodo}</Name>'
            f'<Parameters>{ps}</Parameters></Command></LinxMicrovix>')


def chamar(metodo, params):
    body = montar_body(metodo, params).encode("utf-8")
    req = urllib.request.Request(LINX, data=body, headers={"Content-Type": "text/xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.getcode(), r.read().decode("utf-8")


def parse(xml):
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


def vendedores(cnpj, extra):
    params = {"chave": CHAVE, "cnpjEmp": cnpj, **extra}
    try:
        _, xml = chamar(METODO, params)
    except Exception as e:
        return [], f"{e!r}"[:200]
    cols, regs = parse(xml)
    return regs, (None if cols else "sem esquema")


# ── Execução ─────────────────────────────────────────────────────────────────
print("=" * 74)
print(f"SONDA LinxVendedores — SCHEMA CRU COMPLETO (read-only) | {date.today()}")
print("=" * 74)
print("  chave lida de LINX_CHAVE (não impressa); CNPJ mascarado")

# 1) Descobre o combo de parâmetros + schema (tenta as 1as empresas)
cols0, extra_ok = [], None
for cod in sorted(CNPJS)[:3]:
    for rotulo, extra in VARIACOES:
        try:
            _, xml = chamar(METODO, {"chave": CHAVE, "cnpjEmp": CNPJS[cod], **extra})
            c, _r = parse(xml)
        except Exception:
            c = []
        if c:
            cols0, extra_ok = c, extra
            print(f"  ✓ parâmetros: [{rotulo}] {extra}  (empresa {cod})")
            break
    if cols0:
        break

if not cols0:
    print("  ✗ nenhum combo retornou schema — API indisponível ou parâmetros mudaram.")
    sys.exit(1)

# 2) Varre TODAS as empresas acumulando valores de exemplo por coluna
exemplos = OrderedDict((c, []) for c in cols0)   # coluna -> até N valores distintos
total_regs, empresas_ok, empresas_erro = 0, 0, 0
primeiro_reg = None
for cod in sorted(CNPJS):
    regs, erro = vendedores(CNPJS[cod], extra_ok)
    if erro:
        empresas_erro += 1
        continue
    empresas_ok += 1
    for r in regs:
        total_regs += 1
        if primeiro_reg is None:
            primeiro_reg = (cod, r)
        for c in cols0:
            v = (r.get(c) or "").strip()
            # guarda 'cnpj' mascarado; ignora vazios; até 6 distintos por coluna
            if "cnpj" in c.lower() and v:
                v = mascara_cnpj(v)
            if v and v not in exemplos.setdefault(c, []) and len(exemplos[c]) < 6:
                exemplos[c].append(v)

print(f"  empresas OK: {empresas_ok}/{len(CNPJS)} | com erro: {empresas_erro} | vendedores: {total_regs}")

# 3) SCHEMA COMPLETO — toda coluna, com valores de exemplo e flag de situação
print("\n" + "=" * 74)
print(f"TODAS AS COLUNAS QUE A API RETORNA: {len(cols0)}")
print("=" * 74)
suspeitas = []
for i, c in enumerate(cols0, 1):
    low = c.lower()
    flag = ""
    if any(k in low for k in ALVO_SITUACAO):
        flag = "  ⬅⬅ POSSÍVEL SITUAÇÃO/AFASTAMENTO/FÉRIAS"
        suspeitas.append(c)
    vals = exemplos.get(c, [])
    amostra = " | ".join(vals) if vals else "(sempre vazio)"
    print(f"  {i:>2}. {c}{flag}")
    print(f"       ex.: {amostra[:180]}")

# 4) Registro completo de exemplo (todos os campos de 1 vendedor)
if primeiro_reg:
    cod, r = primeiro_reg
    print("\n" + "=" * 74)
    print(f"REGISTRO COMPLETO DE EXEMPLO (empresa {cod}, cnpj {mascara_cnpj(CNPJS[cod])})")
    print("=" * 74)
    for c in cols0:
        v = (r.get(c) or "").strip()
        if "cnpj" in c.lower() and v:
            v = mascara_cnpj(v)
        print(f"  {c:28} = {v!r}")

# 5) Veredito
print("\n" + "=" * 74)
print("VEREDITO")
print("=" * 74)
if suspeitas:
    print(f"  ⚠️ Colunas candidatas a situação/afastamento/férias/licença: {suspeitas}")
    print("     → confira os valores de exemplo acima pra ver se trazem esse dado.")
else:
    print("  ✅ NENHUMA coluna de situação/afastamento/férias/licença no schema.")
    print("     O único status é 'ativo' (S/N). Não há como distinguir saída de férias aqui.")
print(f"  Total de colunas cruas: {len(cols0)}  (os motores mapeiam 9).")
print("Fim.")
