import threading, time, requests, json, os
from flask import Flask
from datetime import datetime
app = Flask(__name__)

ARQUIVO="stats.json"
lock=threading.Lock()

# Stats separados por mercado
stats={"live_wins":0,"live_losses":0,"pre_wins":0,"pre_losses":0}
if os.path.exists(ARQUIVO):
    try:
        with open(ARQUIVO,"r") as f: stats=json.load(f)
    except: pass

def salvar():
    with lock, open(ARQUIVO,"w") as f: json.dump(stats,f)

def taxa(w,l):
    t=w+l
    return 0 if t==0 else round(w/t*100,2)

# Guarda jogos com info pra conferir depois
# live: {fid: {home,away, cantos_entrada, hora, over}}
# pre: {fid: {home,away, odd, hora, over}}
entradas_live={}
entradas_pre={}
pendentes_live={}
pendentes_pre={}

@app.route('/')
def home():
    tl=taxa(stats["live_wins"], stats["live_losses"])
    tp=taxa(stats["pre_wins"], stats["pre_losses"])
    tg=taxa(stats["live_wins"]+stats["pre_wins"], stats["live_losses"]+stats["pre_losses"])
    return f"Online - Live {tl}% ({stats['live_wins']}W-{stats['live_losses']}L) | Pre {tp}% ({stats['pre_wins']}W-{stats['pre_losses']}L) | Geral {tg}%"

threading.Thread(target=lambda: app.run(host='0.0.0.0',port=10000),daemon=True).start()

TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT=os.getenv("CHAT_ID")
KEY=os.getenv("FIVE_DOLLAR_KEY")
HEADERS={"Authorization": f"Bearer {KEY}"}
BASE="https://api.5dollarfootballapi.com/v1"

def tg(m):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":CHAT,"text":m,"parse_mode":"HTML"},timeout=15)
    except Exception as e: print(e)

def buscar_live():
    try:
        r=requests.get(f"{BASE}/livescores",headers=HEADERS,timeout=20)
        if r.status_code!=200: return []
        return r.json().get("data",[])
    except: return []

def buscar_fixtures_finalizados():
    try:
        # Busca jogos finalizados hoje pra conferir
        r=requests.get(f"{BASE}/fixtures",headers=HEADERS,params={"date":"today","status":"finished"},timeout=20)
        if r.status_code!=200:
            r=requests.get(f"{BASE}/livescores",headers=HEADERS,timeout=20)
        if r.status_code!=200: return []
        return r.json().get("data",[])
    except: return []

def buscar_pre():
    try:
        r=requests.get(f"{BASE}/fixtures",headers=HEADERS,params={"date":"today"},timeout=20)
        if r.status_code!=200:
            r=requests.get(f"{BASE}/matches",headers=HEADERS,timeout=20)
        if r.status_code!=200: return []
        return r.json().get("data", r.json().get("response", []))
    except: return []

def get_favorito_info(jogo):
    try:
        odds=jogo.get("odds",[]) or jogo.get("pre_odds",[])
        home_odd=10; away_odd=10
        for o in odds:
            market=str(o.get("market","") or o.get("name","")).lower()
            price=float(o.get("odd",0) or o.get("price",0) or 0)
            if price < 1.01: continue
            team=str(o.get("team","") or o.get("side","") or o.get("selection","")).lower()
            if "home" in team or "home" in market or market=="1":
                if price < home_odd: home_odd=price
            if "away" in team or "away" in market or market=="2":
                if price < away_odd: away_odd=price
        fav=None; fav_odd=0
        if home_odd <= 1.5: fav="HOME"; fav_odd=home_odd
        elif away_odd <= 1.5: fav="AWAY"; fav_odd=away_odd
        elif home_odd <= 1.7: fav="HOME"; fav_odd=home_odd
        return fav, fav_odd, home_odd, away_odd
    except: return None,0,10,10

def filtra_over2(jogo):
    try:
        odds=jogo.get("odds",[]) or jogo.get("pre_odds",[])
        for o in odds:
            market=str(o.get("market","") or o.get("name","") or "").lower()
            price=float(o.get("odd",0) or o.get("price",0) or 0)
            if "over" in market and "goal" in market and ("2" in market or "2.5" in market):
                if 1.70 <= price <= 2.50:
                    return price, o.get("market","Over 2.5 Gols")
        return None
    except: return None

if __name__=="__main__":
    tg(f"🤖 <b>BOT COM RESOLUÇÃO ATIVADO</b>\n\n📊 Live: {stats['live_wins']}W-{stats['live_losses']}L ({taxa(stats['live_wins'],stats['live_losses'])}%)\n📊 Pré: {stats['pre_wins']}W-{stats['pre_losses']}L ({taxa(stats['pre_wins'],stats['pre_losses'])}%)\n\n🔥 LIVE 20-45' Fav ≤1.5/Casa ≤1.7 perdendo até 2 => Over 9 Esc\n⚽ PRE Over 2 @1.70-2.50")

    ultimo_pre=0
    ultimo_check=0
    while True:
        # 1. LIVE 20-45 OVER 9
        for j in buscar_live():
            try:
                fid=str(j.get("id") or j.get("fixture",{}).get("id"))
                if not fid or fid in entradas_live: continue
                elapsed=int(j.get("elapsed") or j.get("fixture",{}).get("status",{}).get("elapsed") or 0)
                if not (20 <= elapsed <= 45): continue
                home=j.get("home_team") or j.get("teams",{}).get("home",{}).get("name","Casa")
                away=j.get("away_team") or j.get("teams",{}).get("away",{}).get("name","Fora")
                gc=int(j.get("home_score") or j.get("goals",{}).get("home",0) or 0)
                gf=int(j.get("away_score") or j.get("goals",{}).get("away",0) or 0)
                fav, fav_odd, home_odd, away_odd = get_favorito_info(j)
                if not fav: continue
                perdendo=False; placar_txt=""
                if fav=="HOME":
                    diff=gf-gc
                    if 1 <= diff <= 2: perdendo=True; placar_txt=f"{gc}x{gf} - Casa fav perde {diff}"
                else:
                    diff=gc-gf
                    if 1 <= diff <= 2: perdendo=True; placar_txt=f"{gc}x{gf} - Fora fav perde {diff}"
                if not perdendo: continue
                cantos=j.get("corners",0)
                if isinstance(cantos, dict): cantos=cantos.get("total",0)
                cantos=int(cantos)

                entradas_live[fid]=True
                pendentes_live[fid]={"home":home,"away":away,"over":9,"hora":datetime.now().strftime("%H:%M"),"fav":fav,"odd":fav_odd}

                tl=taxa(stats["live_wins"], stats["live_losses"])
                tg(f"🔥 <b>LIVE - OVER 9 ESCANTEIOS</b>\n\n🏟️ {home} x {away}\n⏰ {elapsed}' | 📊 {placar_txt}\n⭐ Fav: {fav} @ {fav_odd}\n🚩 Cantos: {cantos}\n\n👉 <b>ENTRADA: Over 9.0 FT</b>\n📊 Live até agora: {stats['live_wins']}W-{stats['live_losses']}L ({tl}%)")

            except Exception as e: print(f"Erro live: {e}")

        # 2. PRE OVER 2
        if time.time() - ultimo_pre > 1800:
            ultimo_pre=time.time()
            for j in buscar_pre():
                try:
                    fid=str(j.get("id") or j.get("fixture",{}).get("id"))
                    if fid in entradas_pre: continue
                    res=filtra_over2(j)
                    if not res: continue
                    odd_val, mercado=res
                    home=j.get("home_team") or j.get("teams",{}).get("home",{}).get("name","Casa")
                    away=j.get("away_team") or j.get("teams",{}).get("away",{}).get("name","Fora")
                    entradas_pre[fid]=True
                    pendentes_pre[fid]={"home":home,"away":away,"odd":odd_val,"over":2.5,"hora":datetime.now().strftime("%H:%M")}
                    tp=taxa(stats["pre_wins"], stats["pre_losses"])
                    tg(f"⚽ <b>PRE - OVER 2 GOLS</b>\n\n🏟️ {home} x {away}\n💰 {mercado} @ {odd_val}\n📊 Pré até agora: {stats['pre_wins']}W-{stats['pre_losses']}L ({tp}%)\n\n👉 Mais de 2 gols")
                    break
                except: pass

        # 3. CHECAGEM DE RESULTADO - a cada 5 min
        if time.time() - ultimo_check > 300 and (pendentes_live or pendentes_pre):
            ultimo_check=time.time()
            finalizados=buscar_fixtures_finalizados()
            for j in finalizados:
                try:
                    fid=str(j.get("id") or j.get("fixture",{}).get("id"))
                    status=str(j.get("status") or j.get("fixture",{}).get("status",{}).get("short","")).lower()
                    if "ft" not in status and "finished" not in status: continue

                    # Confere LIVE Over 9 Escanteios
                    if fid in pendentes_live:
                        info=pendentes_live.pop(fid)
                        total_cantos=j.get("corners",0)
                        if isinstance(total_cantos, dict): total_cantos=total_cantos.get("total",0)
                        total_cantos=int(total_cantos or 0)
                        total_gols=int(j.get("home_score",0) or j.get("goals",{}).get("home",0) or 0) + int(j.get("away_score",0) or j.get("goals",{}).get("away",0) or 0)

                        if total_cantos > 9:
                            stats["live_wins"]+=1; res="✅ GREEN"; emoji="🟢"
                        else:
                            stats["live_losses"]+=1; res="❌ RED"; emoji="🔴"
                        salvar()
                        tl=taxa(stats["live_wins"], stats["live_losses"])
                        tg(f"{emoji} <b>{res} - LIVE OVER 9 ESC</b>\n\n🏟️ {info['home']} x {info['away']}\n🚩 Final: {total_cantos} cantos | Gols: {total_gols}\nEntrada: Over 9.0\n\n📊 <b>Live agora: {stats['live_wins']}W-{stats['live_losses']}L ({tl}%)</b>")

                    # Confere PRE Over 2 Gols
                    if fid in pendentes_pre:
                        info=pendentes_pre.pop(fid)
                        total_gols=int(j.get("home_score",0) or j.get("goals",{}).get("home",0) or 0) + int(j.get("away_score",0) or j.get("goals",{}).get("away",0) or 0)
                        if total_gols > 2:
                            stats["pre_wins"]+=1; res="✅ GREEN"; emoji="🟢"
                        else:
                            stats["pre_losses"]+=1; res="❌ RED"; emoji="🔴"
                        salvar()
                        tp=taxa(stats["pre_wins"], stats["pre_losses"])
                        tg(f"{emoji} <b>{res} - PRE OVER 2</b>\n\n🏟️ {info['home']} x {info['away']}\n⚽ Final: {total_gols} gols\nEntrada: Over 2.5 @ {info['odd']}\n\n📊 <b>Pré agora: {stats['pre_wins']}W-{stats['pre_losses']}L ({tp}%)</b>")

                except Exception as e:
                    print(f"Erro check: {e}")

        time.sleep(60)
