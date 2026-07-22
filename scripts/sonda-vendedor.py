#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/sonda-vendedor.py  —  SONDAGEM read-only de UM vendedor no LinxVendedores.
#
# Imprime o registro CRU COMPLETO de um vendedor (TODAS as colunas <C>, com nome e
# valor de CADA campo, mesmo vazios). Alvo default: empresa 31, cod_vendedor 776
# (SARA) — ajustável por env EMPRESA / COD_VENDEDOR (inputs do workflow).
#
# Objetivo: com a férias marcada no Microvix, ver se aparece QUALQUER campo com
# data de início/fim de férias, ou valor de férias/afastamento em 'funcao'.
# Destaca: (1) todo campo cujo valor seja uma DATA diferente de admissão/saída;
#          (2) os valores exatos de funcao / cargo / tipo_vendedor / ativo.
#
# NÃO grava nada. NÃO usa Supabase. Só GET na API. Chave nunca impressa.
#
# COMO RODAR:
#   export LINX_CHAVE="...";  [EMPRESA=31 COD_VENDEDOR=776]
#   python3 scripts/sonda-vendedor.py
# =============================================================================

import os
import sys
import re
import calendar
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date

CHAVE = os.environ.get("LINX_CHAVE")
if not CHAVE:
    print('ERRO: defina LINX_CHAVE no ambiente:  export LINX_CHAVE="...sua-chave..."')
    sys.exit(1)

EMPRESA = int(os.environ.get("EMPRESA", "31"))
COD_ALVO = os.environ.get("COD_VENDEDOR", "776").strip()

LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
METODO = "LinxVendedores"
DATA_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")   # detecta valor com cara de data

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


def norm(v):
    if v is None:
        return ""
    s = str(v).strip()
    try:
        return str(int(float(s)))
    except ValueError:
        return s


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


def chamar(params):
    body = montar_body(METODO, params).encode("utf-8")
    req = urllib.request.Request(LINX, data=body, headers={"Content-Type": "text/xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8")


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


def acha_campo(cols, *chaves):
    for c in cols:
        if any(k in c.lower() for k in chaves):
            return c
    return None


# ── Execução ─────────────────────────────────────────────────────────────────
print("=" * 74)
print(f"SONDA LinxVendedores — 1 vendedor | empresa {EMPRESA} · cod {COD_ALVO} | {date.today()}")
print(f"  cnpj {mascara_cnpj(CNPJS.get(EMPRESA, ''))} | chave lida do env (não impressa)")
print("=" * 74)

if EMPRESA not in CNPJS:
    print(f"  ✗ empresa {EMPRESA} não está no dict de CNPJs.")
    sys.exit(1)

cols, regs = [], []
for rotulo, extra in VARIACOES:
    try:
        xml = chamar({"chave": CHAVE, "cnpjEmp": CNPJS[EMPRESA], **extra})
    except Exception as e:
        print(f"  ✗ [{rotulo}] {e!r}"[:160])
        continue
    cols, regs = parse(xml)
    if cols:
        print(f"  ✓ parâmetros: [{rotulo}] {extra} | vendedores na empresa: {len(regs)}")
        break

if not cols:
    print("  ✗ nenhum combo retornou schema — API indisponível ou parâmetros mudaram.")
    sys.exit(1)

campo_cv = acha_campo(cols, "cod_vendedor") or acha_campo(cols, "vendedor")
alvo = next((r for r in regs if campo_cv and norm(r.get(campo_cv)) == norm(COD_ALVO)), None)

if alvo is None:
    print(f"\n  ⚠️ cod_vendedor {COD_ALVO} NÃO encontrado na empresa {EMPRESA}.")
    disponiveis = sorted((norm(r.get(campo_cv)) for r in regs if campo_cv),
                         key=lambda x: int(x) if x.isdigit() else 0)
    print(f"  cods retornados ({len(disponiveis)}): {disponiveis}")
    sys.exit(0)

# 1) Registro CRU COMPLETO — todos os campos, mesmo vazios
print("\n" + "=" * 74)
print(f"REGISTRO CRU COMPLETO — {len(cols)} colunas")
print("=" * 74)
for c in cols:
    v = (alvo.get(c) or "").strip()
    if "cnpj" in c.lower() and v:
        v = mascara_cnpj(v)
    print(f"  {c:30} = {v!r}")

# 2) Destaque: campos com DATA que não sejam admissão/saída
print("\n" + "=" * 74)
print("(1) CAMPOS COM VALOR DE DATA (fora de admissão/saída)")
print("=" * 74)
outras_datas = []
for c in cols:
    v = (alvo.get(c) or "").strip()
    if v and DATA_RE.search(v):
        base = c.lower()
        if "admiss" in base or "saida" in base or "saída" in base:
            continue
        outras_datas.append((c, v))
if outras_datas:
    for c, v in outras_datas:
        print(f"  ⬅⬅ {c} = {v!r}   (POSSÍVEL data de férias/afastamento)")
else:
    print("  ✅ nenhuma outra data além de admissão/saída neste registro.")

# 3) Destaque: valores exatos de papel/status
print("\n" + "=" * 74)
print("(2) PAPEL/STATUS EXATOS")
print("=" * 74)
for rot, chaves in [("funcao", ("funcao", "função", "funç")),
                    ("cargo", ("cargo",)),
                    ("tipo_vendedor", ("tipo_vendedor", "tipo")),
                    ("ativo", ("ativo",))]:
    campo = acha_campo(cols, *chaves)
    val = (alvo.get(campo) or "").strip() if campo else None
    print(f"  {rot:14} (coluna API: {campo!r:22}) = {val!r}")

print("\nFim.")
