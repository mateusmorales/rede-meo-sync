import urllib.request, urllib.error, json, xml.etree.ElementTree as ET, time, os
from datetime import date

SUPA_URL = "https://cqvdhzxwmgdfnpypelcy.supabase.co"
SUPA_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CHAVE = os.environ["LINX_CHAVE"]
LINX = "https://webapi.microvix.com.br/1.0/api/integracao"

HOJE = date.today().isoformat()

CNPJS = {1:"19942423000159",2:"21135935000155",3:"21267421000153",5:"09098956000142",
7:"16634813000173",8:"05849808000161",9:"30295460000155",10:"35440782000164",
11:"35440879000177",12:"35440752000158",13:"35440732000187",14:"42469235000177",
15:"43807892000140",16:"43974413000180",17:"43974436000194",18:"40106367000109",
19:"52681552000106",20:"55209904000113",21:"55221013000182",22:"55123110000132",
23:"55155561000151",24:"55188762000155",25:"55219628000174",26:"55191634000160",
27:"55189150000187",28:"55100245000182",29:"55229921000112",30:"55293736000197",
31:"60126160000103",32:"62681844000100",33:"62688707000190",34:"62670520000169",
35:"62670142000113",36:"62654703000190",37:"62660397000103"}

def num(s):
    try: return float(s) if s not in (None,"") else None
    except: return None
def inteiro(s):
    try: return int(float(s)) if s not in (None,"") else None
    except: return None
def dt(s): return s[:10] if s else None
def hora(s):
    if not s: return None
    s = s.strip()
    return s if s else None

def get_lojas():
    url=f"{SUPA_URL}/rest/v1/lojas?select=id,cod_microvix&cod_microvix=not.is.null"
    req=urllib.request.Request(url,headers={"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}"})
    with urllib.request.urlopen(req,timeout=60) as r:
        return {int(x["cod_microvix"]):x["id"] for x in json.load(r)}

def upsert(registros):
    if not registros: return
    url=f"{SUPA_URL}/rest/v1/vendas_itens?on_conflict=transacao"
    headers={"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}","Content-Type":"application/json",
             "Prefer":"resolution=merge-duplicates,return=minimal"}
    for i in range(0,len(registros),500):
        batch=registros[i:i+500]
        req=urllib.request.Request(url,data=json.dumps(batch).encode("utf-8"),headers=headers,method="POST")
        for tent in range(3):
            try:
                with urllib.request.urlopen(req,timeout=120): break
            except urllib.error.HTTPError as e:
                if tent==2: raise
                time.sleep(3)

def realizado(it):
    if it["soma_relatorio"]!="S" or it["cancelado"]!="N" or it["excluido"]!="N": return 0
    if (it["tipo_transacao"] or "")=="J": return 0
    op=it["operacao"]; v=it["valor_total"] or 0
    if op=="S": return v
    if op=="DS": return -v
    if op=="E" and "TROCA ORIUNDA" in (it["natureza_operacao"] or "").upper(): return -v
    return 0

t0=time.time()
LOJAS=get_lojas()
print(f"MOTOR 3 - tempo real | dia {HOJE}")
print("="*48)
gt=0.0; gi=0
for cod in sorted(CNPJS):
    if cod not in LOJAS: continue
    cnpj=CNPJS[cod]; loja_id=LOJAS[cod]
    itens={}; ts=0; vistos=set(); loops=0
    while True:
        loops+=1
        if loops>100: break
        body=f'<?xml version="1.0" encoding="utf-8"?><LinxMicrovix><Authentication user="linx_export" password="linx_export"/><ResponseFormat>xml</ResponseFormat><Command><Name>LinxMovimento</Name><Parameters><Parameter id="chave">{CHAVE}</Parameter><Parameter id="cnpjEmp">{cnpj}</Parameter><Parameter id="timestamp">{ts}</Parameter><Parameter id="data_inicial">{HOJE}</Parameter><Parameter id="data_fim">{HOJE}</Parameter></Parameters></Command></LinxMicrovix>'
        req=urllib.request.Request(LINX,data=body.encode("utf-8"),headers={"Content-Type":"text/xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req,timeout=120) as r: xml=r.read().decode("utf-8")
        except Exception as e: time.sleep(3); continue
        rd=ET.fromstring(xml).find("ResponseData")
        if rd is None or rd.find("C") is None: break
        cols=[(d.text or "") for d in rd.find("C").findall("D")]; ix={c:i for i,c in enumerate(cols)}
        rows=rd.findall("R")
        if not rows: break
        maxts=ts; novo=0
        for r in rows:
            ds=r.findall("D")
            def g(n,ds=ds,ix=ix):
                i=ix.get(n); return (ds[i].text or "").strip() if i is not None and i<len(ds) else ""
            try: maxts=max(maxts,int(g("timestamp")))
            except: pass
            tx=g("transacao")
            if not tx or tx in vistos: continue
            vistos.add(tx); novo+=1
            itens[tx]={
              "transacao":inteiro(tx),"documento":g("documento") or None,"identificador":g("identificador") or None,
              "chave_nf":g("chave_nf") or None,"serie":g("serie") or None,"modelo_nf":g("modelo_nf") or None,
              "cnpj_emp":g("cnpj_emp") or None,"empresa":cod,"loja_id":loja_id,
              "data_documento":dt(g("data_documento")),"data_lancamento":dt(g("data_lancamento")),
              "hora_lancamento":hora(g("hora_lancamento")),
              "cod_vendedor":inteiro(g("cod_vendedor")),"codigo_cliente":inteiro(g("codigo_cliente")),
              "cod_produto":inteiro(g("cod_produto")),"id_setor":None,"id_linha":None,
              "quantidade":num(g("quantidade")) or 0,"valor_total":num(g("valor_total")) or 0,
              "valor_liquido":num(g("valor_liquido")),"preco_unitario":num(g("preco_unitario")),
              "preco_tabela_epoca":num(g("preco_tabela_epoca")),"desconto":num(g("desconto")) or 0,
              "tabela_preco":inteiro(g("tabela_preco")),"nome_tabela_preco":g("nome_tabela_preco") or None,
              "deposito":g("deposito") or None,"operacao":g("operacao") or None,
              "tipo_transacao":g("tipo_transacao") or None,"soma_relatorio":g("soma_relatorio") or None,
              "cancelado":g("cancelado") or None,"excluido":g("excluido") or None,
              "natureza_operacao":g("natureza_operacao") or None,"id_cfop":inteiro(g("id_cfop")),
              "desc_cfop":g("desc_cfop") or None,"timestamp_microvix":inteiro(g("timestamp")),
              "dt_insert":g("dt_insert") or None,"dt_update":g("dt_update") or None,
            }
        if novo==0: break
        ts=maxts-1 if maxts>0 else 0
    regs=list(itens.values())
    upsert(regs)
    sub=round(sum(realizado(x) for x in regs),2)
    gt+=sub; gi+=len(regs)
    if regs: print(f"  loja {cod:>2}  {len(regs):>3} itens | R$ {sub:>10,.2f}", flush=True)

def chama():
    url=f"{SUPA_URL}/rest/v1/rpc/preenche_categoria_bloco"
    headers={"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}","Content-Type":"application/json"}
    req=urllib.request.Request(url,data=json.dumps({"p_limite":50000}).encode("utf-8"),headers=headers,method="POST")
    with urllib.request.urlopen(req,timeout=300) as r: return int(r.read().decode().strip())
catn=0
while True:
    n=chama(); catn+=n
    if n==0: break

print("="*48)
print(f"MOTOR 3: {gi} itens hoje | R$ {gt:,.2f} | {catn} categorizados | {time.time()-t0:.0f}s")
