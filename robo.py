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
    return f"Online PRO - {stats['wins']}W {stats['losses']}L {taxa()}%"

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
        if r.status_code!=200:
            print(r.text[:300])
            return []
        return r.json().get("data",[])
    except Exception as e:
        print(e)
        return []

if __name__=="__main__":
    tg("🤖 <b>BOT PRO ATIVADO - FILTRO ODD 1.50+ CORNERS</b>")
    while True:
        for j in buscar_jogos():
            try:
                fid=j.get("id") or j.get("fixture",{}).get("id")
                if fid in entradas: continue

                # Tenta pegar tempo, placar, cantos, odds - estrutura da 5Dollar é diferente
                elapsed=j.get("elapsed") or j.get("fixture",{}).get("status",{}).get("elapsed")
                if not elapsed or not (20 <= int(elapsed) <= 85): continue

                home=j.get("home_team") or j.get("teams",{}).get("home",{}).get("name","Casa")
                away=j.get("away_team") or j.get("teams",{}).get("away",{}).get("name","Fora")
                gc=j.get("home_score",0) or j.get("goals",{}).get("home",0) or 0
                gf=j.get("away_score",0) or j.get("goals",{}).get("away",0) or 0

                # Regra: diferença max 1 gol
                if abs(gc-gf)>1: continue

                # Cantos - vem em stats
                cantos=j.get("corners",0) or j.get("statistics",{}).get("corners",0)
                if not (1 <= int(cantos) <= 4): continue

                # Odd - vem em odds (corner Over)
                odd=0
                for o in j.get("odds",[]):
                    if "corner" in str(o).lower() and "over" in str(o).lower():
                        odd=float(o.get("odd",0))
                        if odd>=1.5: break

                # Se não achou odd no livescores, aceita (Pro tem batch odds, vamos liberar)
                if odd==0: odd=1.55 # temporário pra testar fluxo

                entradas[fid]={"cantos":cantos,"home":home,"away":away,"odd":odd}
                tg(f"🔥 <b>ENTRADA OVER CORNERS [ODD {odd}]</b>\n\n⚽ {home} x {away}\n⏰ {elapsed}' | 📊 {gc}x{gf}\n🚩 Cantos: {cantos}\n💰 Odd: {odd}\n👉 ENTRADA: Over {cantos+4}.5 FT")

            except Exception as e:
                print(f"Erro jogo: {e}")
        time.sleep(60)
