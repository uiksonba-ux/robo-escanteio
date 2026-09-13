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
PORT = int(os.getenv("PORT", "10000"))
ODD_MIN = 1.35
ODD_MAX = 3.50
QTD_POR_RODADA = int(os.getenv("QTD_BILHETES", "8"))
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "600"))
INTERVALO_RESULTADOS = 300
lock = threading.RLock()
stats = {"gols_wins":0,"gols_losses":0,"cantos_wins":0,"cantos_losses":0,"total_sinais":0}
entradas_pre = {}
pendentes_pre = {}

def primeiro_valor(*v):
    for x in v:
        if x is not None and x!= "":
            return x
    return None
def numero(v,p=0.0):
    try:
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
    return inteiro(primeiro_valor(j.get("home_score"),g.get("home"),s.get("home"),0)), inteiro(primeiro_valor(j.get("away_score"),g.get("away"),s.get("away"),0))
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
    try:
        r=requests.get(f"{BASE_API}/{endpoint.lstrip('/')}",headers=HEADERS,params=params or {},timeout=25)
        print(f"API {endpoint} HTTP {r.status_code}",flush=True)
        if r.status_code!=200: return []
        d=r.json()
        if isinstance(d,list): return d
        res=primeiro_valor(d.get("data"),d.get("response"),[])
        if isinstance(res,dict): res=res.get("data") or res.get("fixtures") or []
        return res if isinstance(res,list) else []
    except Exception as e:
        print(f"ERRO API {e}",flush=True); return []
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
    return [j for j in api_get("fixtures",{"start_time":s,"end_time":e}) if eh_finalizado(j)]
def filtra_over_gols(jogo):
    melhores=[]
    for odd in extrair_odds(jogo):
        t=texto_odds(odd).replace(",","."); p=valor_odd(odd)
        if not (ODD_MIN<=p<=ODD_MAX): continue
        if ("over" in t) and ("goal" in t or "gol" in t):
            if any(x in t for x in ["0.5","1.5","2.5"]):
                linha = 0.5 if "0.5" in t else 1.5 if "1.5" in t else 2.5
                melhores.append((p,f"Over {linha} Gols",linha))
    if not melhores: return None
    melhores.sort(key=lambda x: -x[0])
    return melhores[0]
def filtra_over_cantos(jogo):
    melhores=[]
    for odd in extrair_odds(jogo):
        t=texto_odds(odd).replace(",","."); p=valor_odd(odd)
        if not (ODD_MIN<=p<=ODD_MAX): continue
        if "corner" in t or "escante" in t or "canto" in t:
            if any(x in t for x in ["6.5","7.5","8.5","9","9.5","10","10.5"]):
                linha = primeiro_valor(odd.get("line"), odd.get("handicap"), "9.5")
                melhores.append((p,f"Over {linha} Escanteios",linha))
    if not melhores: return None
    melhores.sort(key=lambda x: -x[0])
    return melhores[0]
def analisar_pre(jogo):
    fid=extrair_id(jogo)
    if not fid: return False
    casa,fora=extrair_times(jogo)
    gerou=False
    key_gols=f"{fid}_GOLS"
    if key_gols not in entradas_pre:
        r=filtra_over_gols(jogo)
        if r:
            odd,mercado,linha=r
            entradas_pre[key_gols]=True
            pendentes_pre[key_gols]={"fid":fid,"tipo":"GOLS","home":casa,"away":fora,"odd":odd,"mercado":mercado,"linha":linha}
            stats["total_sinais"]+=1
            tg_msg(f"⚽ <b>PRE - {mercado.upper()}</b>\n\n🏟️ {casa} x {fora}\n💰 {mercado} @ {odd:.2f}")
            gerou=True
    key_cantos=f"{fid}_CANTOS"
    if key_cantos not in entradas_pre:
        r=filtra_over_cantos(jogo)
        if r:
            odd,mercado,linha=r
            entradas_pre[key_cantos]=True
            pendentes_pre[key_cantos]={"fid":fid,"tipo":"CANTOS","home":casa,"away":fora,"odd":odd,"mercado":mercado,"linha":linha}
            stats["total_sinais"]+=1
            tg_msg(f"🚩 <b>PRE - {mercado.upper()}</b>\n\n🏟️ {casa} x {fora}\n💰 {mercado} @ {odd:.2f}")
            gerou=True
    return gerou
def finalizar(key,jogo):
    if key not in pendentes_pre: return
    info=pendentes_pre.pop(key)
    cg,fg=extrair_placar(jogo); tot_gols=cg+fg; tot_cantos=extrair_cantos(jogo)
    if info["tipo"]=="GOLS":
        win=tot_gols>float(str(info["linha"]).replace(",","."))
        if win: stats["gols_wins"]+=1; emo="🟢"; res="GREEN"
        else: stats["gols_losses"]+=1; emo="🔴"; res="RED"
        tg_msg(f"{emo} <b>{res} - {info['mercado']}</b>\n🏟️ {info['home']} x {info['away']}\n⚽ {tot_gols} gols")
    else:
        win=tot_cantos>float(str(info["linha"]).replace(",","."))
        if win: stats["cantos_wins"]+=1; emo="🟢"; res="GREEN"
        else: stats["cantos_losses"]+=1; emo="🔴"; res="RED"
        tg_msg(f"{emo} <b>{res} - {info['mercado']}</b>\n🏟️ {info['home']} x {info['away']}\n🚩 {tot_cantos} cantos")
def iniciar_bot():
    tg_msg(f"🤖 <b>TURBO ONLINE - {QTD_POR_RODADA} a cada {INTERVALO_PRE//60}min</b>")
    print("BOT TURBO INICIADO",flush=True)
    ultimo_pre=0; ultimo_res=0
    while True:
        try:
            if time.time()-ultimo_pre>=INTERVALO_PRE:
                ultimo_pre=time.time()
                gerados=0
                for jogo in buscar_pre():
                    if gerados>=QTD_POR_RODADA: break
                    if analisar_pre(jogo):
                        gerados+=1
                        time.sleep(1)
                print(f"TURBO: {gerados} GERADOS",flush=True)
            if time.time()-ultimo_res>=300 and pendentes_pre:
                ultimo_res=time.time()
                mapa={}
                for j in buscar_finalizados(): mapa[extrair_id(j)]=j
                for k in list(pendentes_pre.keys()):
                    fid=pendentes_pre[k].get("fid")
                    if fid in mapa: finalizar(k,mapa[fid])
            time.sleep(20)
        except Exception as e:
            print(f"ERRO {e}",flush=True); time.sleep(30)
@app.route("/")
def home(): return f"TURBO ONLINE | {QTD_POR_RODADA}x / {INTERVALO_PRE//60}min | Total {stats['total_sinais']}"
@app.route("/health")
def health(): return jsonify({"online":True})
def iniciar_thread(): threading.Thread(target=iniciar_bot,daemon=True).start()
iniciar_thread()
if __name__=="__main__": app.run(host="0.0.0.0",port=PORT)
