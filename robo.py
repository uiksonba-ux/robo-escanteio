import os, json, time, threading, tempfile
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

app = Flask(__name__)
BASE_API = "https://api.5dollarfootballapi.com/v1"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json", "User-Agent": "robo-pre/1.0"}
ARQUIVO_STATS = "stats.json"
ARQUIVO_HISTORICO = "historico.json"
PORT = int(os.getenv("PORT", "10000"))

# ===== TURBO CONFIG - MAX SINAIS =====
ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))
QTD_POR_RODADA = int(os.getenv("QTD_BILHETES", "8")) # 8 por rodada
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "600")) # 10 min
INTERVALO_RESULTADOS = 300

lock = threading.RLock()
STATS_PADRAO = {"gols_wins":0,"gols_losses":0,"cantos_wins":0,"cantos_losses":0,"total_sinais":0}
stats = STATS_PADRAO.copy()

def carregar_json(arquivo, padrao):
    if not os.path.exists(arquivo): return padrao.copy() if isinstance(padrao, dict) else []
    try:
        with open(arquivo,"r",encoding="utf-8") as f: dados=json.load(f)
        if isinstance(padrao,dict):
            r=padrao.copy()
            if isinstance(dados,dict): r.update(dados)
            return r
        return dados if isinstance(dados,list) else padrao
    except: return padrao.copy() if isinstance(padrao,dict) else []

stats = carregar_json(ARQUIVO_STATS, STATS_PADRAO)
historico = carregar_json(ARQUIVO_HISTORICO, [])
entradas_pre = {}
pendentes_pre = {}

def salvar_json_seguro(arquivo,dados):
    try:
        fd,tmp=tempfile.mkstemp(prefix="tmp_",suffix=".json",dir=".")
        with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(dados,f,ensure_ascii=False,indent=2)
        os.replace(tmp,arquivo)
    except Exception as e: print(f"ERRO SALVAR {e}",flush=True)

def salvar_dados():
    with lock:
        salvar_json_seguro(ARQUIVO_STATS,stats)
        salvar_json_seguro(ARQUIVO_HISTORICO,historico)

def taxa(w,l): return 0.0 if w+l==0 else round(w/(w+l)*100,2)
def primeiro_valor(*v):
    for x in v:
        if x is not None and x!="": return x
    return None
def numero(v,p=0.0):
    try:
        if v is None or v=="": return p
        if isinstance(v,str): v=v.replace(",",".").strip()
        return float(v)
    except: return p
def inteiro(v,p=0):
    try: return int(float(numero(v,p)))
    except: return p
def nome_time(d,p):
    if isinstance(d,dict): return str(primeiro_valor(d.get("name"),d.get("short_name"),d.get("title"),p))
    return str(d) if d else p
def extrair_id(j):
    f=j.get("fixture",{})
    return str(primeiro_valor(j.get("id"),f.get("id"),j.get("fixture_id"),""))
def extrair_times(j):
    t=j.get("teams",{}) or {}
    casa=nome_time(primeiro_valor(j.get("home_team"),j.get("home"),t.get("home")),"Casa")
    fora=nome_time(primeiro_valor(j.get("away_team"),j.get("away"),t.get("away")),"Fora")
    return casa,fora
def extrair_placar(j):
    g=j.get("goals",{}) or {}; s=j.get("score",{}) or {}
    casa=inteiro(primeiro_valor(j.get("home_score"),g.get("home"),s.get("home"),0))
    fora=inteiro(primeiro_valor(j.get("away_score"),g.get("away"),s.get("away"),0))
    return casa,fora
def extrair_status(j):
    f=j.get("fixture",{}) or {}; st=f.get("status",{}) or {}
    return str(primeiro_valor(j.get("status"),st.get("short"),st.get("long"),"")).lower()
def eh_finalizado(j): return any(x in extrair_status(j) for x in ["ft","finished","aet","pen"])
def extrair_cantos(j):
    c=primeiro_valor(j.get("corners"),j.get("corner"))
    if isinstance(c,(int,float,str)): return inteiro(c)
    if isinstance(c,dict):
        tot=primeiro_valor(c.get("total"),c.get("value"))
        if tot is not None: return inteiro(tot)
        return inteiro(primeiro_valor(c.get("home"),0))+inteiro(primeiro_valor(c.get("away"),0))
    return 0
def extrair_odds(j):
    o=primeiro_valor(j.get("odds"),j.get("pre_odds"),j.get("bets"),[])
    if isinstance(o,dict): o=o.get("data") or o.get("markets") or []
    return [x for x in o if isinstance(x,dict)] if isinstance(o,list) else []
def texto_odds(o): return " ".join(str(o.get(k,"")) for k in ["market","name","selection","label","outcome","line"]).lower()
def valor_odd(o): return numero(primeiro_valor(o.get("odd"),o.get("price"),o.get("value"),o.get("rate")),0)

def tg_msg(m):
    if not TOKEN or not CHAT_ID: print(m,flush=True); return
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":CHAT_ID,"text":m,"parse_mode":"HTML"},timeout=15)
    except Exception as e: print(f"ERRO TG {e}",flush=True)

def api_get(endpoint, params=None):
    url=f"{BASE_API}/{endpoint.lstrip('/')}"
    try:
        r=requests.get(url,headers=HEADERS,params=params or {},timeout=25)
        print(f"API {endpoint} HTTP {r.status_code}",flush=True)
        if r.status_code!=200:
            print(f"RESPOSTA {r.text[:500]}",flush=True)
            return []
        d=r.json()
        if isinstance(d,list): return d
        res=primeiro_valor(d.get("data"),d.get("response"),[])
        if isinstance(res,dict): res=res.get("data") or res.get("fixtures") or []
        return res if isinstance(res,list) else []
    except Exception as e:
        print(f"ERRO API {e}",flush=True)
        return []

def unix_hoje():
    agora=datetime.now(timezone.utc)
    inicio=agora.replace(hour=0,minute=0,second=0,microsecond=0)
    fim=agora.replace(hour=23,minute=59,second=59,microsecond=0)
    return int(inicio.timestamp()), int(fim.timestamp())

def buscar_pre():
    s,e=unix_hoje()
    jogos=api_get("fixtures",{"start_time":s,"end_time":e})
    print(f"PRE: {len(jogos)} jogos",flush=True)
    return jogos

def buscar_finalizados():
    s,e=unix_hoje()
    jogos=api_get("fixtures",{"start_time":s,"end_time":e})
    fin=[j for j in jogos if eh_finalizado(j)]
    print(f"FIN: {len(fin)} finalizados",flush=True)
    return fin

def filtra_over_gols(jogo):
    melhores=[]
    for odd in extrair_odds(jogo):
        t=texto_odds(odd).replace(",",".")
        p=valor_odd(odd)
        if not (ODD_MIN<=p<=ODD_MAX): continue
        if ("over" in t or "mais de" in t) and ("goal" in t or "gol" in t):
            if any(x in t for x in ["0.5","1.5","2.5"]):
                linha = 0.5 if "0.5" in t else 1.5 if "1.5" in t else 2.5
                melhores.append((p,f"Over {linha} Gols",linha))
    if not melhores: return None
    melhores.sort(key=lambda x: -x[0])
    return melhores[0]

def filtra_over_cantos(jogo):
    melhores=[]
    for odd in extrair_odds(jogo):
        t=texto_odds(odd).replace(",",".")
        p=valor_odd(odd)
        if not (ODD_MIN<=p<=ODD_MAX): continue
        if "corner" in t or "escante" in t or "canto" in t:
            if any(x in t for x in ["6.5","7.5","8.5","9","9.5","10","10.5"]):
                linha = primeiro_valor(odd.get("line"), odd.get("handicap"), "9.5")
                melhores.append((p,f"Over {linha} Escanteios",linha))
    if not melhores: return None
    melhores.sort(key=lambda x: -x[0])
    return melhores[0]

def analisar_pre(jogo):
