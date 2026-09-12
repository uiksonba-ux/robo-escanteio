import threading, time, requests, json, os
from flask import Flask
app = Flask(__name__)

ARQUIVO = "stats.json"
stats = json.load(open(ARQUIVO)) if os.path.exists(ARQUIVO) else {"wins": 0, "losses": 0}
def salvar_stats():
    with open(ARQUIVO, "w") as f: json.dump(stats, f)

entradas_pendentes = {}
cache_odds = {} # pra não gastar requisição atoa

def calcular_taxa():
    total = stats["wins"] + stats["losses"]
    return 0.0 if total == 0 else round((stats["wins"]/total)*100, 2)

@app.route('/')
def home():
    return f"Robo Online - Wins: {stats['wins']} | Losses: {stats['losses']} | Taxa: {calcular_taxa()}%"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "COLOQUE_O_TOKEN_NOVO_AQUI")
CHAT_ID = os.getenv("CHAT_ID", "8863811629")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "82010ba2c58cf9a791512c38bcbc44e8")
HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}

def enviar_telegram(msg):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: print(e)

def buscar_jogos_ao_vivo():
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"live": "all"}, timeout=15)
        return r.json().get("response", [])
    except: return []

def obter_cantos(fid):
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures/statistics", headers=HEADERS, params={"fixture": fid}, timeout=15)
        total = 0
        for t in r.json().get("response", []):
            for s in t.get("statistics", []):
                if s.get("type") == "Corner Kicks" and s.get("value"): total += s["value"]
        return total
    except: return None

def obter_favorito_por_odd(fid):
    # Usa cache pra economizar
    if fid in cache_odds: return cache_odds[fid]
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/odds", headers=HEADERS, params={"fixture": fid, "bookmaker": "8", "bet": "1"}, timeout=15)
        resp = r.json().get("response", [])
        if not resp: return None, None
        values = resp[0]["bookmakers"][0]["bets"][0]["values"]
        oc = of = 999
        for v in values:
            if v["value"] == "Home": oc = float(v["odd"])
            if v["value"] == "Away": of = float(v["odd"])
        if oc < 2.00:
            cache_odds[fid] = ("home", oc)
            return "home", oc
        if of < 2.00:
            cache_odds[fid] = ("away", of)
            return "away", of
        cache_odds[fid] = (None, None)
        return None, None
    except:
        return None, None

def checar_jogos_encerrados():
    for fid in list(entradas_pendentes.keys()):
        try:
            r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"id": fid}, timeout=15)
            dados = r.json().get("response", [])
            if not dados: continue
            if dados[0]["fixture"]["status"]["short"] in ["FT", "AET", "PEN"]:
                cantos = obter_cantos(fid)
                nome = entradas_pendentes[fid]["nome"]
                if cantos is not None:
                    if cantos >= 9:
                        stats["wins"] += 1
                        res = "✅ <b>GREEN!</b>"
                    else:
                        stats["losses"] += 1
                        res = "❌ <b>RED!</b>"
                    salvar_stats()
                    enviar_telegram(f"{res}\n⚽ {nome}\n🚩 Finais: {cantos}\n📊 {stats['wins']}W/{stats['losses']}L - {calcular_taxa()}%")
                    del entradas_pendentes[fid]
        except Exception as e: print(e)

print("🤖 ROBÔ LIGADO - FAVORITO POR ODD < 2.00")
enviar_telegram("<b>🤖 ROBÔ ATUALIZADO!</b>\nAgora favorito = ODD menor que 2.00")

while True:
    jogos = buscar_jogos_ao_vivo()
    for jogo in jogos:
        fid = jogo["fixture"]["id"]
        tempo = jogo["fixture"]["status"]["elapsed"]
        if tempo is None or fid in entradas_pendentes: continue
        if 20 <= tempo <= 60:
            home, away = jogo["teams"]["home"], jogo["teams"]["away"]
            gc, gf = jogo["goals"]["home"] or 0, jogo["goals"]["away"] or 0

            # Só busca ODD se já estiver empatado ou perdendo por 1 (economiza API)
            if not (gc == gf or abs(gc-gf) == 1): continue

            fav_tipo, odd_fav = obter_favorito_por_odd(fid)
            if not fav_tipo: continue

            motivo = None
            if fav_tipo == "home" and (gc == gf or gf - gc == 1):
                motivo = f"Favorito {home['name']} (Odd {odd_fav}) {'Empatando' if gc==gf else 'Perdendo por 1'}"
            elif fav_tipo == "away" and (gc == gf or gc - gf == 1):
                motivo = f"Favorito {away['name']} (Odd {odd_fav}) {'Empatando' if gc==gf else 'Perdendo por 1'}"

            if motivo:
                cantos = obter_cantos(fid)
                if cantos is None: cantos = 0 # FIX do Palmeiras que não entrou
                if cantos <= 4:
                    nome = f"{home['name']} x {away['name']}"
                    enviar_telegram(f"""🔥 <b>FAVORITO ODD {odd_fav} PRESSIONANDO!</b>\n⚽ {nome}\n⏰ {tempo}' | {gc}x{gf}\n🚩 {cantos} cantos\n🎯 {motivo}\n👉 <b>OVER 8.5 FT</b>\n📊 {calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)""")
                    entradas_pendentes[fid] = {"nome": nome}

    checar_jogos_encerrados()
    time.sleep(180)
