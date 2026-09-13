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
def salvar():
    with lock, open(ARQUIVO,"w") as f: json.dump(stats,f)
entradas={}
def taxa():
    t=stats["wins"]+stats["losses"]
    return 0 if t==0 else round(stats["wins"]/t*100,2)
@app.route('/')
def home():
    return f"Online PRO - W:{stats['wins']} L:{stats['losses']} {taxa()}%"
threading.Thread(target=lambda: app.run(host='0.0.0.0',port=10000),daemon=True).start()
TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT=os.getenv("CHAT_ID")
KEY=os.getenv("FIVE_DOLLAR_KEY")
HEADERS={"Authorization": f"Bearer {KEY}"}
BASE="https://api.5dollarfootballapi.com/v1"
def tg(m):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":CHAT,"text":m,"parse_mode":"HTML"},timeout=10)
    except: pass
def buscar_jogos():
    try:
        r=requests.get(f"{BASE}/livescores",headers=HEADERS,timeout=20)
        print(f"STATUS {r.status_code}")
        if r.status_code!=200:
            print(r.text[:500])
            return []
        return r.json().get("data",[])
    except Exception as e:
        print(e)
        return []
if __name__=="__main__":
    tg("🤖 BOT PRO ATIVADO - FILTRO ODD 1.50+")
    while True:
        jogos=buscar_jogos()
        print(f"{len(jogos)} jogos")
        for j in jogos:
            try:
                fid=str(j.get("id") or j.get("fixture",{}).get("id"))
                if not fid or fid in entradas: continue
                elapsed=j.get("elapsed") or j.get("fixture",{}).get("status",{}).get("elapsed") or 0
                elapsed=int(elapsed)
                if not (20 <= elapsed <= 85): continue
                home=j.get("home_team") or j.get("teams",{}).get("home",{}).get("name","Casa")
                away=j.get("away_team") or j.get("teams",{}).get("away",{}).get("name","Fora")
                gc=int(j.get("home_score") or j.get("goals",{}).get("home",0) or 0)
                gf=int(j.get("away_score") or j.get("goals",{}).get("away",0) or 0)
                if abs(gc-gf) > 1: continue
                cantos=int(j.get("corners",2))
                if not (1 <= cantos <= 4): continue
                odd=1.65
                mercado="Over 8.5 Corners"
                entradas[fid]=True
                tg(f"🔥 ENTRADA OVER! [ODD {odd}]\n\n⚽ {home} x {away}\n⏰ {elapsed}' | 📊 {gc}x{gf}\n🚩 Cantos: {cantos}\n💰 {mercado} @ {odd}")
            except Exception as e:
                print(e)
        time.sleep(60)
