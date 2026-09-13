import threading, time, requests, json, os
from flask import Flask
app = Flask(__name__)

ARQUIVO="stats.json"
lock=threading.Lock()
stats={"wins":0,"losses":0}
if os.path.exists(ARQUIVO):
    try:
        with open(ARQUIVO,"r") as f: stats=json.load(f)
    except: pass

entradas_live={}
entradas_pre=set()

def taxa():
    t=stats["wins"]+stats["losses"]
    return 0 if t==0 else round(stats["wins"]/t*100,2)

@app.route('/')
def home():
    return f"Online - Live Fav 20-45 Over9 + Pre Over2 - {taxa()}%"

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
            # fallback se vier array 1x2
            if "1x2" in market:
                if o.get("home",10) < home_odd: home_odd=o.get("home")
                if o.get("away",10) < away_odd: away_odd=o.get("away")

        fav=None; fav_odd=0
        # Sua regra: odd <=1.5 qualquer lado OU casa <=1.7
        if home_odd <= 1.5:
            fav="HOME"; fav_odd=home_odd
        elif away_odd <= 1.5:
            fav="AWAY"; fav_odd=away_odd
        elif home_odd <= 1.7:
            fav="HOME"; fav_odd=home_odd

        return fav, fav_odd, home_odd, away_odd
    except:
        return None,0,10,10

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
    tg("🤖 <b>BOT FINAL ATIVADO</b>\n\n🔥 LIVE 20-45' Fav ≤1.5 ou Casa ≤1.7 perdendo até 2 gols => Over 9 Escanteios\n⚽ PRE Over 2 Gols @1.70-2.50")

    ultimo_pre=0
    while True:
        # LIVE 20-45 OVER 9
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

                # Fav perdendo até 2 gols
                perdendo=False; placar_txt=""
                if fav=="HOME":
                    diff=gf-gc
                    if 1 <= diff <= 2:
                        perdendo=True
                        placar_txt=f"{gc}x{gf} - Casa fav perde por {diff}"
                else:
                    diff=gc-gf
                    if 1 <= diff <= 2:
                        perdendo=True
                        placar_txt=f"{gc}x{gf} - Fora fav perde por {diff}"

                if not perdendo: continue

                cantos=j.get("corners",0)
                if isinstance(cantos, dict): cantos=cantos.get("total",0)
                cantos=int(cantos)

                entradas_live[fid]=True
                tg(f"🔥 <b>LIVE - OVER 9 ESCANTEIOS</b>\n\n🏟️ {home} x {away}\n⏰ {elapsed}' | 📊 {placar_txt}\n⭐ Favorito: {fav} @ {fav_odd} (Casa {home_odd} / Fora {away_odd})\n🚩 Cantos agora: {cantos}\n\n👉 <b>ENTRADA: Over 9.0 Escanteios FT</b>\n📌 Motivo: Fav pré ≤1.5 (ou Casa ≤1.7) perdendo até 2 no 1ºT - Pressão total")

            except Exception as e:
                print(f"Erro live: {e}")

        # PRE OVER 2 - MANTIDO IGUAL
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
                    entradas_pre.add(fid)
                    tg(f"⚽ <b>PRÉ-JOGO - OVER 2 GOLS</b>\n\n🏟️ {home} x {away}\n💰 {mercado} @ {odd_val}\n✅ Odd entre 1.70 e 2.50\n\n👉 Mais de 2 gols na partida")
                    break
                except: pass

        time.sleep(60)
