#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/motor_vendedores.py  —  Motor de sincronização de vendedores.
#
# ⚠️ MODO DRY-RUN (esta versão): SÓ LEITURA + DIFF. NÃO faz NENHUM insert/update/
#    upsert. Nenhuma escrita no banco. Só imprime o que FARIA.
#
# Fluxo (desenho aprovado):
#   1. Lê LinxVendedores das 35 empresas (empresa que falha → registra e PULA).
#   2. TRAVA DE SEGURANÇA: empresas_ok < 33/35 OU total_pares < ~1000 → ABORTA
#      sem processar (não age sobre coleta parcial).
#   3. Lê o estado atual de public.vendas_vendedores (chave composta
#      cod_microvix_loja + cod_vendedor).
#   4. DIFF por par (loja, cod): NOVOS / S→N / N→S. Pares no banco AUSENTES da
#      API → NÃO tocar.
#   5. data_admissao vem da API; data_saida da API é IGNORADA (vem vazia p/
#      saídas recentes → inferência pelo flag ativo, carimbo no S→N).
#
# Reaproveita a máquina de chamada dos motores (endpoint, envelope XML, auth
# linx_export, dict de CNPJs) e o padrão Supabase REST. A chave NUNCA é impressa.
#
# COMO RODAR (dry-run):
#   export LINX_CHAVE="...";  export SUPABASE_SERVICE_KEY="..."
#   python3 scripts/motor_vendedores.py
# =============================================================================

import os
import sys
import json
import re
import urllib.request
import urllib.error
import calendar
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta

CHAVE = os.environ.get("LINX_CHAVE")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
if not CHAVE or not SUPA_KEY:
    print("ERRO: defina LINX_CHAVE e SUPABASE_SERVICE_KEY no ambiente.")
    sys.exit(1)

SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
LINX = "https://webapi.microvix.com.br/1.0/api/integracao"
METODO = "LinxVendedores"

# Travas de segurança (não agir sobre coleta parcial).
EMPRESAS_MIN = 33
PARES_MIN = 1000

# Carimbo de data_saida usaria a data LOCAL BRT (não now() UTC). Aqui só exibimos.
BRT = timezone(timedelta(hours=-3))
HOJE_BRT = datetime.now(BRT).date()

# Sentinelas de data a descartar (inclui os lixos 1900/1990/1991-01-01 da API).
SENTINELAS = {"", "0", "null", "none", "0000-00-00", "1900-01-01", "1990-01-01", "1991-01-01"}

CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}


# ── Helpers de dado ──────────────────────────────────────────────────────────
def norm(v):
    """Normaliza cod pra comparar API (str) com banco (int): '5.0'→'5'."""
    if v is None:
        return ""
    s = str(v).strip()
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def data_ou_none(v):
    """'YYYY-MM-DD...' → 'YYYY-MM-DD'; vazio/sentinela-lixo → None."""
    s = (v or "").strip()
    if s[:10].lower() in SENTINELAS or s.lower() in SENTINELAS:
        return None
    return s[:10]


def ativo_bool(v):
    """'S'→True, 'N'→False, resto→None (desconhecido)."""
    s = (v or "").strip().upper()
    if s == "S":
        return True
    if s == "N":
        return False
    return None


_SO_MARCADOR = re.compile(r"(?i)(cbmo|gerente|vendedor|\d+|\s+|/)")


def limpa_nome(raw):
    """Remove prefixo de loja do começo do nome (mesma regra da carga)."""
    n = (raw or "").strip()
    while "-" in n:
        left, _, right = n.partition("-")
        resto = _SO_MARCADOR.sub("", left)
        if resto == "" and right.strip():
            n = right.strip()
        else:
            break
    return n.strip()


def conta_meta_de(nome_raw):
    """GERENTE ou 'VENDEDOR 1' no nome → conta_meta=False; senão True."""
    up = (nome_raw or "").upper()
    return not ("GERENTE" in up or "VENDEDOR 1" in up)


# ── API LinxVendedores (idêntico aos motores) ────────────────────────────────
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

CAMPOS_ALVO = ["cod_vendedor", "nome_vendedor", "ativo", "data_admissao", "meta_peso"]


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


def mapear_colunas(cols):
    """Casa cada campo-alvo com o nome REAL na API (exato, senão substring)."""
    mapa = {}
    baixadas = {c.lower(): c for c in cols}
    for alvo in CAMPOS_ALVO:
        real = baixadas.get(alvo.lower())
        if real is None:
            real = next((c for c in cols if alvo.lower() in c.lower()), None)
        mapa[alvo] = real
    return mapa


def detectar_parametros():
    """Descobre o combo de parâmetros + mapa de colunas (tenta 1as empresas)."""
    for cod in sorted(CNPJS)[:3]:
        for rotulo, extra in VARIACOES:
            try:
                _, xml = chamar(METODO, {"chave": CHAVE, "cnpjEmp": CNPJS[cod], **extra})
            except Exception:
                continue
            cols, _ = parse(xml)
            if cols:
                print(f"  ✓ parâmetros: [{rotulo}] {extra}  (detectado na empresa {cod})")
                return extra, mapear_colunas(cols)
    return None, None


# ── Supabase (LEITURA apenas) ────────────────────────────────────────────────
def ler_estado():
    """Lê public.vendas_vendedores paginando. Chave: (loja_int, cod_str)."""
    out = {}
    passo, off = 1000, 0
    while True:
        url = (f"{SUPA_URL}/rest/v1/vendas_vendedores"
               f"?select=cod_microvix_loja,cod_vendedor,ativo,data_saida,data_admissao")
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
                   "Range-Unit": "items", "Range": f"{off}-{off + passo - 1}"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.load(r)
        for x in rows:
            loja = int(x["cod_microvix_loja"])
            cod = norm(x["cod_vendedor"])
            out[(loja, cod)] = {
                "ativo": x["ativo"],
                "data_saida": x["data_saida"],
                "data_admissao": x["data_admissao"],
            }
        if len(rows) < passo:
            break
        off += passo
    return out


# ── Execução (DRY-RUN) ───────────────────────────────────────────────────────
print("=" * 74)
print(f"MOTOR VENDEDORES — DRY-RUN (sem escrita) | {HOJE_BRT} BRT")
print("=" * 74)
print("  chaves lidas do env (não impressas)")

extra_ok, mapa = detectar_parametros()
if extra_ok is None:
    print("  ✗ nenhum combo de parâmetros retornou esquema — ABORTA (API indisponível).")
    sys.exit(1)
print(f"  colunas da API: {mapa}")
faltando = [a for a, r in mapa.items() if r is None]
if faltando:
    print(f"  AVISO: campos sem correspondência na API: {faltando}")

# 1) Ler as 35 empresas
print("\n[1] Lendo LinxVendedores das 35 empresas…")
api = {}            # (loja, cod) -> {nome_raw, ativo_raw, adm_raw}
empresas_ok, empresas_erro = 0, []
for cod_loja in sorted(CNPJS):
    regs, erro = vendedores(CNPJS[cod_loja], extra_ok)
    if erro:
        empresas_erro.append((cod_loja, erro))
        print(f"    empresa {cod_loja:>2}: ERRO ({erro}) → PULA")
        continue
    empresas_ok += 1
    for r in regs:
        cod = norm(r.get(mapa["cod_vendedor"])) if mapa["cod_vendedor"] else ""
        if not cod:
            continue
        api[(cod_loja, cod)] = {
            "nome_raw": (r.get(mapa["nome_vendedor"]) or "") if mapa["nome_vendedor"] else "",
            "ativo_raw": (r.get(mapa["ativo"]) or "") if mapa["ativo"] else "",
            "adm_raw": (r.get(mapa["data_admissao"]) or "") if mapa["data_admissao"] else "",
        }

total_pares = len(api)
print(f"    empresas OK: {empresas_ok}/{len(CNPJS)} | com erro: {len(empresas_erro)}")
print(f"    total de pares (loja,cod) lidos: {total_pares}")

# 2) TRAVA DE SEGURANÇA
if empresas_ok < EMPRESAS_MIN or total_pares < PARES_MIN:
    print("\n" + "!" * 74)
    print("TRAVA DE SEGURANÇA ACIONADA — coleta parcial. ABORTA sem processar.")
    print(f"  empresas_ok={empresas_ok} (mín {EMPRESAS_MIN}) | total_pares={total_pares} (mín {PARES_MIN})")
    if empresas_erro:
        print("  empresas com erro:")
        for cod_loja, erro in empresas_erro:
            print(f"    empresa {cod_loja}: {erro}")
    print("!" * 74)
    sys.exit(1)

# 3) Ler estado atual do banco
print("\n[2] Lendo estado atual de public.vendas_vendedores…")
db = ler_estado()
print(f"    pares no banco: {len(db)}")

# 4) DIFF
print("\n[3] Calculando diff…")
novos, s_to_n, n_to_s = [], [], []
for (loja, cod), a in api.items():
    nome = limpa_nome(a["nome_raw"]) or f"(cod {cod})"
    api_ativo = ativo_bool(a["ativo_raw"])          # True / False / None
    prev = db.get((loja, cod))
    if prev is None:
        novos.append({
            "loja": loja, "cod": cod, "nome": nome,
            "ativo": api_ativo,
            "admissao": data_ou_none(a["adm_raw"]),
            "conta_meta": conta_meta_de(a["nome_raw"]),
        })
        continue
    prev_ativo = prev["ativo"]                       # bool ou None
    if prev_ativo is True and api_ativo is False:
        s_to_n.append({"loja": loja, "cod": cod, "nome": nome,
                       "data_saida_seria": prev["data_saida"] is None})
    elif prev_ativo is False and api_ativo is True:
        n_to_s.append({"loja": loja, "cod": cod, "nome": nome})
    # demais casos: sem transição de ativo → nada a fazer
# pares no banco AUSENTES da API: intencionalmente NÃO tocados.
ausentes_api = [k for k in db if k not in api]

# ── SAÍDA ────────────────────────────────────────────────────────────────────
def _cod(x):  # ordenação numérica estável
    return (int(x["loja"]), int(x["cod"]) if str(x["cod"]).lstrip("-").isdigit() else 0)

print("\n" + "=" * 74)
print("RESULTADO DO DRY-RUN (nada foi gravado)")
print("=" * 74)

print(f"\n── NOVOS (seriam inseridos): {len(novos)} ──")
print(f"   {'loja':>4} {'cod':>6} {'ativo':>6} {'admissao':>11} {'conta_meta':>10}  nome")
for x in sorted(novos, key=_cod):
    at = "S" if x["ativo"] else ("N" if x["ativo"] is False else "?")
    print(f"   {x['loja']:>4} {x['cod']:>6} {at:>6} {str(x['admissao'] or '—'):>11} "
          f"{str(x['conta_meta']):>10}  {x['nome'][:34]}")

print(f"\n── S→N (ativo→inativo; carimbaria data_saida={HOJE_BRT}): {len(s_to_n)} ──")
print(f"   {'loja':>4} {'cod':>6}  nome  (carimbo: 'sim' se data_saida ainda NULL)")
for x in sorted(s_to_n, key=_cod):
    print(f"   {x['loja']:>4} {x['cod']:>6}  {x['nome'][:34]:<34} carimbaria={'sim' if x['data_saida_seria'] else 'não(já preenchida)'}")

print(f"\n── N→S (inativo voltou ativo na API → REVISÃO, NÃO reativa): {len(n_to_s)} ──")
print(f"   {'loja':>4} {'cod':>6}  nome")
for x in sorted(n_to_s, key=_cod):
    print(f"   {x['loja']:>4} {x['cod']:>6}  {x['nome'][:34]}")

print("\n" + "=" * 74)
print("RESUMO")
print("=" * 74)
print(f"  empresas OK:            {empresas_ok}/{len(CNPJS)}")
print(f"  empresas com erro:      {len(empresas_erro)}  {[c for c,_ in empresas_erro] or ''}")
print(f"  pares lidos da API:     {total_pares}")
print(f"  pares no banco:         {len(db)}")
print(f"  NOVOS (inseriria):      {len(novos)}")
print(f"  S→N (carimbaria saída): {len(s_to_n)}")
print(f"  N→S (revisão):          {len(n_to_s)}")
print(f"  no banco, ausentes API: {len(ausentes_api)}  (NÃO tocados)")
print("\n  ⚠️ DRY-RUN: nenhum INSERT/UPDATE/UPSERT executado. Nenhuma escrita no banco.")
print("Fim.")
