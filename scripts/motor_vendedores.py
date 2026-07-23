#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# scripts/motor_vendedores.py  —  Motor de sincronização de vendedores.
#
# ⚠️ ESCRITA PARCIAL (esta versão): grava (1) NOVOS ATIVOS (par novo ativo='S')
#    via upsert on_conflict=(cod_microvix_loja,cod_vendedor); e (2) S→N — PATCH
#    ativo=false + data_saida=HOJE_BRT (só se estava NULL).
#    e (3) ESPELHO da API em EXISTENTES — PATCH mínimo só dos campos divergentes
#    (nome/funcao/cargo/data_admissao/meta_peso/conta_meta). Ordem no run:
#    NOVOS ATIVOS → S→N → ESPELHO (por último; não toca em 'ativo', não conflita).
#    N→S, novos já-inativos e ausentes da API seguem SÓ como log.
#    usuario_id NUNCA é enviado; data_saida não vem da API; e `ativo` é EXCLUSIVO
#    da fase S→N (o espelho não toca em ativo — ver comentário em CAMPOS_ESPELHO).
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
import time
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
AGORA_BRT = datetime.now(BRT).isoformat()  # timestamptz p/ atualizado_em

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


def meta_ou_none(v):
    """meta_peso da API ('1,000000'/'0'/'') → float; inválido/vazio → 0.0."""
    s = (v or "").strip().replace(",", ".")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


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


# "VENDEDOR 1" como PALAVRA (limite \b): não casa "VENDEDOR 10..19"/"VENDEDOR 1xx".
_VEND1 = re.compile(r"\bVENDEDOR 1\b")


def conta_meta_de(nome_raw, funcao):
    """conta_meta com VETO pelo nome. Em meta, o conservador vence: se o cadastro
    tem GERENTE/SUPERVISOR no NOME, é gerente/supervisor — a `funcao` é que
    provavelmente está desatualizada. Se for engano, o RH corrige o NOME no
    Microvix. Ordem:
    1) funcao == 'GERENTE'                                   → False
    2) 'GERENTE' ou 'SUPERVISOR' no nome (VETO, mesmo com
       funcao preenchida como Vendedor)                      → False
    3) 'VENDEDOR 1' como palavra no nome (não pega 10-19)    → False
    4) senão                                                 → True
    """
    if (funcao or "").strip().upper() == "GERENTE":
        return False
    up = (nome_raw or "").upper()
    if "GERENTE" in up or "SUPERVISOR" in up:      # veto pelo nome
        return False
    if _VEND1.search(up):
        return False
    return True


def _txt(v):
    """Normaliza texto p/ comparar (None e '' são equivalentes)."""
    return (v or "").strip()


def _num(v):
    """Normaliza numérico p/ comparar (6 casas; None/'' → None)."""
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace(",", ".")), 6)
    except (TypeError, ValueError):
        return None


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

CAMPOS_ALVO = ["cod_vendedor", "nome_vendedor", "ativo", "data_admissao", "cargo", "funcao", "meta_peso"]


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
               f"?select=cod_microvix_loja,cod_vendedor,nome,cargo,funcao,ativo,"
               f"data_saida,data_admissao,meta_peso,conta_meta")
        headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
                   "Range-Unit": "items", "Range": f"{off}-{off + passo - 1}"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.load(r)
        for x in rows:
            loja = int(x["cod_microvix_loja"])
            cod = norm(x["cod_vendedor"])
            out[(loja, cod)] = {
                "nome": x.get("nome"),
                "cargo": x.get("cargo"),
                "funcao": x.get("funcao"),
                "ativo": x["ativo"],
                "data_saida": x["data_saida"],
                "data_admissao": x["data_admissao"],
                "meta_peso": x.get("meta_peso"),
                "conta_meta": x.get("conta_meta"),
            }
        if len(rows) < passo:
            break
        off += passo
    return out


def upsert_novos_ativos(registros):
    """ÚNICA fase que ESCREVE. INSERT via on_conflict=(cod_microvix_loja,
    cod_vendedor) com merge-duplicates → idempotente (rodar 2x não duplica).
    Devolve quantos foram enviados. Só é chamada DEPOIS da trava de segurança."""
    if not registros:
        return 0
    url = f"{SUPA_URL}/rest/v1/vendas_vendedores?on_conflict=cod_microvix_loja,cod_vendedor"
    headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    enviados = 0
    for i in range(0, len(registros), 500):
        batch = registros[i:i + 500]
        req = urllib.request.Request(url, data=json.dumps(batch).encode("utf-8"),
                                     headers=headers, method="POST")
        for tent in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180):
                    enviados += len(batch)
                    break
            except urllib.error.HTTPError as e:
                print(f"      ERRO {e.code}: {e.read().decode()[:200]}")
                if tent == 2:
                    raise
                time.sleep(5)
    return enviados


def patch_saida(linhas):
    """ESCRITA da fase S→N: um PATCH por par (filtro cod_microvix_loja+cod_vendedor).
    Sempre ativo=false + atualizado_em; data_saida=HOJE_BRT SÓ se ainda estava NULL
    (nunca sobrescreve saída já preenchida). NÃO toca conta_meta/nome/usuario_id.
    Idempotente: quem já é inativo deixou de ser S→N; e o carimbo só ocorre se NULL.
    Devolve (ativados_off, carimbados)."""
    ativados_off, carimbados = 0, 0
    headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    for x in linhas:
        body = {"ativo": False, "atualizado_em": AGORA_BRT}
        if x["data_saida_seria"]:                       # só carimba se estava NULL
            body["data_saida"] = HOJE_BRT.isoformat()
        url = (f"{SUPA_URL}/rest/v1/vendas_vendedores"
               f"?cod_microvix_loja=eq.{x['loja']}&cod_vendedor=eq.{x['cod']}")
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="PATCH")
        for tent in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60):
                    ativados_off += 1
                    if x["data_saida_seria"]:
                        carimbados += 1
                    break
            except urllib.error.HTTPError as e:
                print(f"      ERRO {e.code} (loja {x['loja']} cod {x['cod']}): {e.read().decode()[:160]}")
                if tent == 2:
                    raise
                time.sleep(5)
    return ativados_off, carimbados


# Guarda defensiva: campos que o ESPELHO nunca pode enviar, aconteça o que
# acontecer (ativo é exclusivo do S→N; usuario_id é elo do app; data_saida não
# vem da API).
PROIBIDOS_ESPELHO = {"ativo", "usuario_id", "data_saida"}


def patch_espelho(linhas):
    """ESCRITA do ESPELHO: um PATCH por par (filtro cod_microvix_loja+cod_vendedor)
    contendo SÓ os campos que divergem (patch mínimo) + atualizado_em.
    Idempotente: quem já está espelhado não diverge e nem chega aqui.
    Devolve (patchados, por_campo)."""
    patchados, por_campo = 0, {}
    headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    for e in linhas:
        body = {c: novo for c, (_atual, novo) in e["mud"].items()
                if c not in PROIBIDOS_ESPELHO}
        if not body:
            continue
        body["atualizado_em"] = AGORA_BRT
        url = (f"{SUPA_URL}/rest/v1/vendas_vendedores"
               f"?cod_microvix_loja=eq.{e['loja']}&cod_vendedor=eq.{e['cod']}")
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="PATCH")
        for tent in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60):
                    patchados += 1
                    for c in body:
                        if c != "atualizado_em":
                            por_campo[c] = por_campo.get(c, 0) + 1
                    break
            except urllib.error.HTTPError as ex:
                print(f"      ERRO {ex.code} (loja {e['loja']} cod {e['cod']}): {ex.read().decode()[:160]}")
                if tent == 2:
                    raise
                time.sleep(5)
    return patchados, por_campo


# ── Execução (DRY-RUN) ───────────────────────────────────────────────────────
print("=" * 74)
print(f"MOTOR VENDEDORES — escrita: NOVOS ATIVOS + S→N (N→S em log) | {HOJE_BRT} BRT")
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
            "cargo_raw": (r.get(mapa["cargo"]) or "") if mapa["cargo"] else "",
            "funcao_raw": (r.get(mapa["funcao"]) or "") if mapa["funcao"] else "",
            "meta_raw": (r.get(mapa["meta_peso"]) or "") if mapa["meta_peso"] else "",
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
# novos_ativos = pares novos com ativo='S' (seriam inseridos).
# novos_inativos = pares novos que já vêm ativo='N' (ou sem flag): NÃO inserir —
#   nunca estiveram no banco, então nunca geram S→N; inofensivos pro divisor
#   (nunca contam meta). Só logamos pra rastreabilidade.
novos_ativos, novos_inativos, s_to_n, n_to_s = [], [], [], []
# espelho = existentes cujo registro DIVERGE da API (campo a campo). DRY-RUN
# nesta etapa: só calcula e reporta, NÃO grava.
espelho, existentes_total = [], 0
# Campos espelhados da API. FORA do espelho, de propósito:
#   usuario_id — elo com o login do app, não existe na API;
#   data_saida — não vem da API (só o carimbo do fluxo S→N);
#   ativo      — EXCLUSIVO da fase S→N. Se o espelho marcasse ativo=false sem
#                carimbar data_saida junto, a pessoa viraria "inativo sem data" e
#                sumiria do divisor em TODOS os dias, inclusive os que trabalhou
#                (furaria a meta retroativamente).
CAMPOS_ESPELHO = ["nome", "funcao", "cargo", "data_admissao", "meta_peso", "conta_meta"]
for (loja, cod), a in api.items():
    nome = limpa_nome(a["nome_raw"]) or f"(cod {cod})"
    api_ativo = ativo_bool(a["ativo_raw"])          # True / False / None
    prev = db.get((loja, cod))
    if prev is None:
        # Regra de inserção: só entra quem está ATIVO='S' agora na API. Se depois
        # esse par vier 'N', já estará no banco → cai no S→N naturalmente.
        if api_ativo is True:
            novos_ativos.append({
                "loja": loja, "cod": cod, "nome": nome,
                "ativo": api_ativo,
                "admissao": data_ou_none(a["adm_raw"]),
                "conta_meta": conta_meta_de(a["nome_raw"], a["funcao_raw"]),
                "cargo": (a["cargo_raw"] or "").strip(),
                "funcao": (a["funcao_raw"] or "").strip(),
                "meta_peso": meta_ou_none(a["meta_raw"]),
            })
        else:
            # Novo já inativo (entrou e saiu sem passar por ativo): não inserir.
            novos_inativos.append({"loja": loja, "cod": cod, "nome": nome,
                                   "ativo_raw": (a["ativo_raw"] or "").strip() or "?"})
        continue
    prev_ativo = prev["ativo"]                       # bool ou None
    if prev_ativo is True and api_ativo is False:
        s_to_n.append({"loja": loja, "cod": cod, "nome": nome,
                       "data_saida_seria": prev["data_saida"] is None})
    elif prev_ativo is False and api_ativo is True:
        n_to_s.append({"loja": loja, "cod": cod, "nome": nome})
    # demais casos: sem transição de ativo → nada a fazer

    # ── ESPELHO da API (DRY-RUN): a API é a fonte da verdade do RH. Calcula o
    #    valor desejado de cada campo espelhado e guarda só o que DIVERGE.
    existentes_total += 1
    desejado = {
        "nome": limpa_nome(a["nome_raw"]),
        "funcao": _txt(a["funcao_raw"]) or None,
        "cargo": _txt(a["cargo_raw"]) or None,
        "data_admissao": data_ou_none(a["adm_raw"]),
        "meta_peso": meta_ou_none(a["meta_raw"]),
        "conta_meta": conta_meta_de(a["nome_raw"], a["funcao_raw"]),
    }
    mud = {}
    for campo in CAMPOS_ESPELHO:
        atual, novo = prev.get(campo), desejado[campo]
        if campo == "meta_peso":
            diverge = _num(atual) != _num(novo)
        elif campo == "conta_meta":
            diverge = atual != novo
        else:
            diverge = _txt(atual) != _txt(novo)
        if diverge:
            mud[campo] = (atual, novo)
    if mud:
        espelho.append({"loja": loja, "cod": cod, "nome": nome,
                        "funcao_api": _txt(a["funcao_raw"]), "mud": mud})
# pares no banco AUSENTES da API: intencionalmente NÃO tocados.
ausentes_api = [k for k in db if k not in api]

# 5) ESCRITA — novos_ativos (insert) + S→N (patch). N→S / novos_inativos /
#    ausentes: NÃO escrevem (seguem como log). Só aqui, já passada a trava.
print("\n[4] Escrevendo NOVOS ATIVOS (insert)…")
rows_ins = [{
    "cod_microvix_loja": x["loja"],
    "cod_vendedor": int(x["cod"]),
    "nome": x["nome"],
    "cargo": x["cargo"] or None,
    "funcao": x["funcao"] or None,
    "ativo": True,
    "data_admissao": x["admissao"],   # já None se vazio/sentinela
    "data_saida": None,
    "conta_meta": x["conta_meta"],
    "meta_peso": x["meta_peso"],
    "usuario_id": None,
    "atualizado_em": AGORA_BRT,
} for x in novos_ativos]
inseridos = upsert_novos_ativos(rows_ins)
print(f"    enviados ao banco (upsert on_conflict): {inseridos}")

print("\n[5] Escrevendo S→N (ativo=false + carimbo de data_saida se NULL)…")
sn_off, sn_carimbados = patch_saida(s_to_n)
print(f"    ativo→false: {sn_off} | data_saida carimbada: {sn_carimbados}")

# ESPELHO por ÚLTIMO: como não toca em 'ativo', não conflita com o S→N acima.
print("\n[6] Escrevendo ESPELHO (patch mínimo dos campos divergentes)…")
esp_patchados, esp_por_campo = patch_espelho(espelho)
print(f"    existentes patchados: {esp_patchados}")

# ── SAÍDA ────────────────────────────────────────────────────────────────────
def _cod(x):  # ordenação numérica estável
    return (int(x["loja"]), int(x["cod"]) if str(x["cod"]).lstrip("-").isdigit() else 0)

print("\n" + "=" * 74)
print("RESULTADO (escrita: NOVOS ATIVOS + S→N; N→S apenas logado)")
print("=" * 74)

print(f"\n── NOVOS ATIVOS (ativo=S; INSERIDOS: {inseridos}) ──")
print(f"   {'loja':>4} {'cod':>6} {'admissao':>11} {'conta_meta':>10}  nome")
for x in sorted(novos_ativos, key=_cod):
    print(f"   {x['loja']:>4} {x['cod']:>6} {str(x['admissao'] or '—'):>11} "
          f"{str(x['conta_meta']):>10}  {x['nome'][:34]}")

print(f"\n── NOVOS JÁ-INATIVOS (NÃO inseridos; só log p/ rastreabilidade): {len(novos_inativos)} ──")
print("   (novo par que já veio ativo≠'S' — entrou e saiu sem passar por ativo)")
print(f"   {'loja':>4} {'cod':>6} {'ativo':>5}  nome")
for x in sorted(novos_inativos, key=_cod):
    print(f"   ⚠️ {x['loja']:>4} {x['cod']:>6} {x['ativo_raw']:>5}  {x['nome'][:34]}")

print(f"\n── S→N (ativo→false: {sn_off}; data_saida carimbada: {sn_carimbados}) ──")
print(f"   {'loja':>4} {'cod':>6} {'data_saida':>12}  nome")
for x in sorted(s_to_n, key=_cod):
    ds = HOJE_BRT.isoformat() if x["data_saida_seria"] else "mantida"
    print(f"   {x['loja']:>4} {x['cod']:>6} {ds:>12}  {x['nome'][:34]}")

print(f"\n── N→S (inativo voltou ativo na API → REVISÃO, NÃO reativa): {len(n_to_s)} ──")
print(f"   {'loja':>4} {'cod':>6}  nome")
for x in sorted(n_to_s, key=_cod):
    print(f"   {x['loja']:>4} {x['cod']:>6}  {x['nome'][:34]}")

# ── ESPELHO DA API em EXISTENTES (ESCRITA LIGADA) ────────────────────────────
print("\n" + "=" * 74)
print("ESPELHO DA API em EXISTENTES — PATCHADOS")
print("=" * 74)
print(f"  existentes comparados:            {existentes_total}")
print(f"  com ALGUM campo divergente:       {len(espelho)}")
print(f"  PATCHADOS (escrita):              {esp_patchados}")

print("\n  resumo por campo (quantos foram alterados):")
for c in CAMPOS_ESPELHO:
    print(f"    {c:16} {esp_por_campo.get(c, 0)}")

cm = [e for e in espelho if "conta_meta" in e["mud"]]
print(f"\n  ⬅⬅ MUDANÇAS DE conta_meta (MEXE NO DIVISOR DA META): {len(cm)}")
if cm:
    print(f"     {'loja':>4} {'cod':>6}  {'funcao (API)':<24} {'de':>5} → {'para':<5}  nome")
    for e in sorted(cm, key=_cod):
        de, para = e["mud"]["conta_meta"]
        fa = e["funcao_api"] or "(vazia)"
        print(f"     {e['loja']:>4} {e['cod']:>6}  {fa[:24]:<24} {str(de):>5} → {str(para):<5}  {e['nome'][:30]}")
else:
    print("     (nenhuma — o divisor da meta não muda)")

print("\n" + "=" * 74)
print("RESUMO")
print("=" * 74)
print(f"  empresas OK:            {empresas_ok}/{len(CNPJS)}")
print(f"  empresas com erro:      {len(empresas_erro)}  {[c for c,_ in empresas_erro] or ''}")
print(f"  pares lidos da API:     {total_pares}")
print(f"  pares no banco:         {len(db)}")
print(f"  NOVOS ATIVOS detectados:               {len(novos_ativos)}")
print(f"  NOVOS ATIVOS INSERIDOS (escrita):      {inseridos}")
print(f"  novos já-inativos (ignorados/logados): {len(novos_inativos)}")
print(f"  S→N detectados:             {len(s_to_n)}")
print(f"  S→N ativo→false (escrita):  {sn_off}")
print(f"  S→N data_saida carimbada:   {sn_carimbados}")
print(f"  N→S (revisão, só log):      {len(n_to_s)}")
print(f"  ESPELHO: divergentes {len(espelho)}/{existentes_total} · PATCHADOS {esp_patchados}")
print(f"    dos quais mudaram conta_meta: {len(cm)}")
print(f"  no banco, ausentes API: {len(ausentes_api)}  (NÃO tocados)")
print("\n  ESCRITA (nesta ordem): NOVOS ATIVOS (upsert) → S→N (ativo/carimbo) →")
print("                         ESPELHO (patch mínimo dos divergentes). Idempotente.")
print("  NÃO escritos: N→S, novos já-inativos, ausentes da API (seguem em log).")
print("  NUNCA enviados pelo espelho: ativo (exclusivo do S→N), usuario_id, data_saida.")
print("Fim.")
