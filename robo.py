import threading, time, requests, json, os
from flask import Flask
app = Flask(__name__)

ARQUIVO = "stats.json"
stats = json.load(open(ARQUIVO)) if os.path.exists(ARQUIVO) else {"wins": 0, "losses": 0}
def salvar_stats():
    with open(ARQUIVO, "w") as f: json.dump(stats, f, indent=4)

entradas_pendentes = {}
cache_odds = {}

def calcular_taxa():
    total = stats["wins"] + stats["losses"]
    return 0.0 if total == 0 else round((stats["wins"]/total)*100, 2)

@app.route('/')
def home():
    return f"Robo Online - Wins: {stats['wins']} | Losses: {stats['losses']} | Taxa: {calcular_taxa()}%"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}

def enviar_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
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
                if s.get("type") == "Corner Kicks" and s.get("value") is not None:
                    total += s["value"]
        return total
    except: return None

def obter_favorito_por_odd(jogo):
    # CORREÇÃO: Não usa API de ODDS paga - usa placar
    gc = jogo["goals"]["home"] or 0
    gf = jogo["goals"]["away"] or 0
    if gc < gf: return "home", 1.85
    if gf < gc: return "away", 1.85
    return "home", 1.90

def checar_jogos_encerrados():
    for fid in list(entradas_pendentes.keys()):
        try:
            r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"id": fid}, timeout=15)
            dados = r.json().get("response", [])
            if not dados: continue
            if dados[0]["fixture"]["status"]["short"] in ["FT", "AET", "PEN"]:
                cantos_finais = obter_cantos(fid)
                info = entradas_pendentes[fid]
                if cantos_finais is not None:
                    if cantos_finais > info["cantos_entrada"]:
                        stats["wins"] += 1; msg = f"✅ <b>GREEN!</b>\n⚽ {info['time_casa']} vs {info['time_fora']}\n🚩 {info['cantos_entrada']} -> {cantos_finais}\n📈 {calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)"
                    else:
                        stats["losses"] += 1; msg = f"❌ <b>RED!</b>\n⚽ {info['time_casa']} vs {info['time_fora']}\n🚩 {info['cantos_entrada']} -> {cantos_finais}\n📈 {calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)"
                    salvar_stats()
                    enviar_telegram(msg)
                del entradas_pendentes[fid]
        except: pass

if __name__ == "__main__":
    print("Bot iniciado...")
    enviar_telegram("🤖 <b>Bot Inicializado! Filtro CORRIGIDO 20-85min</b>")
    while True:
        jogos = buscar_jogos_ao_vivo()
        for jogo in jogos:
            try:
                fid = jogo["fixture"]["id"]
                tempo = jogo["fixture"]["status"]["elapsed"]
                if not tempo or not (20 <= tempo <= 85): continue
                if fid in entradas_pendentes: continue

                time_casa = jogo["teams"]["home"]["name"]
                time_fora = jogo["teams"]["away"]["name"]
                gc, gf = jogo["goals"]["home"] or 0, jogo["goals"]["away"] or 0
                if not (gc == gf or abs(gc-gf) == 1): continue

                fav, odd = obter_favorito_por_odd(jogo)
                cantos_atuais = obter_cantos(fid)
                if cantos_atuais is None or cantos_atuais > 4: continue

                entradas_pendentes[fid] = {"cantos_entrada": cantos_atuais, "favorito": fav, "time_casa": time_casa, "time_fora": time_fora}

                favorito_nome = time_casa if fav == "home" else time_fora
                enviar_telegram(
                    f"🔥 <b>ENTRADA OVER 8.5!</b>\n\n"
                    f"⚽ <b>Jogo:</b> {time_casa} x {time_fora}\n"
                    f"⏰ <b>Tempo:</b> {tempo}'\n"
                    f"📊 <b>Placar:</b> {gc}x{gf}\n"
                    f"🎯 <b>Favorito:</b> {favorito_nome} {'EMPATANDO' if gc==gf else 'PERDENDO por 1'}\n"
                    f"📍 <b>Cantos agora:</b> {cantos_atuais}\n\n"
                    f"👉 <b>ENTRADA: OVER 8.5 FT</b>\n"
                    f"📈 <i>{calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)</i>"
                )
            except Exception as e: print(e)
        checar_jogos_encerrados()
        time.sleep(60)
