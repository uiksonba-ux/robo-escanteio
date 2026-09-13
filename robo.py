import threading
import time
import requests
import json
import os
from flask import Flask

app = Flask(__name__)

ARQUIVO = "stats.json"
lock = threading.Lock()

# Carregamento seguro dos dados
if os.path.exists(ARQUIVO):
    try:
        with open(ARQUIVO, "r") as f:
            stats = json.load(f)
    except Exception:
        stats = {"wins": 0, "losses": 0}
else:
    stats = {"wins": 0, "losses": 0}

def salvar_stats():
    with lock:
        with open(ARQUIVO, "w") as f:
            json.dump(stats, f, indent=4)

entradas_pendentes = {}

def calcular_taxa():
    with lock:
        total = stats["wins"] + stats["losses"]
        return 0.0 if total == 0 else round((stats["wins"] / total) * 100, 2)

@app.route('/')
def home():
    with lock:
        w, l = stats['wins'], stats['losses']
    return f"Robo Online - Wins: {w} | Losses: {l} | Taxa: {calcular_taxa()}% | Filtro: ODD 1.5+ FAV PERD/EMP"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}

def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram Token ou Chat ID não configurados.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

def buscar_jogos_ao_vivo():
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"live": "all"}, timeout=15)
        r.raise_for_status()
        return r.json().get("response", [])
    except Exception as e:
        print(f"Erro ao buscar jogos ao vivo: {e}")
        return []

def obter_cantos(fid):
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures/statistics", headers=HEADERS, params={"fixture": fid}, timeout=15)
        r.raise_for_status()
        total = 0
        for t in r.json().get("response", []):
            for s in t.get("statistics", []):
                if s.get("type") == "Corner Kicks" and s.get("value") is not None:
                    total += s["value"]
        return total
    except Exception as e:
        print(f"Erro ao buscar cantos para {fid}: {e}")
        return None

def obter_odd_escanteio(fid):
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/odds", headers=HEADERS, params={"fixture": fid, "bookmaker": "8", "bet": "8"}, timeout=15)
        r.raise_for_status()
        resp = r.json().get("response", [])
        if not resp:
            return None
        
        bookmakers = resp[0].get("bookmakers", [])
        if not bookmakers:
            return None
            
        for bet in bookmakers[0].get("bets", []):
            for v in bet.get("values", []):
                val_str = str(v.get("value", ""))
                if any(target in val_str for target in ["Over 8.5", "Over 9.5", "Over 7.5"]):
                    odd = float(v["odd"])
                    if odd >= 1.50:
                        return odd, val_str
        return None
    except Exception as e:
        print(f"Erro ao obter odd para {fid}: {e}")
        return None

def checar_jogos_encerrados():
    for fid in list(entradas_pendentes.keys()):
        try:
            r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"id": fid}, timeout=15)
            r.raise_for_status()
            dados = r.json().get("response", [])
            if not dados:
                continue
                
            status_short = dados[0]["fixture"]["status"]["short"]
            if status_short in ["FT", "AET", "PEN"]:
                cantos_finais = obter_cantos(fid)
                info = entradas_pendentes[fid]
                if cantos_finais is not None:
                    with lock:
                        if cantos_finais > info["cantos_entrada"]:
                            stats["wins"] += 1
                            resultado_tag = "✅ <b>GREEN!</b>"
                        else:
                            stats["losses"] += 1
                            resultado_tag = "❌ <b>RED!</b>"
                        salvar_stats()
                        
                        msg = (
                            f"{resultado_tag}\n"
                            f"⚽ {info['time_casa']} vs {info['time_fora']}\n"
                            f"🚩 Cantos: Entrada ({info['cantos_entrada']}) -> Final ({cantos_finais}) | Odd: {info['odd']}\n"
                            f"📈 Taxa: {calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)"
                        )
                    enviar_telegram(msg)
                    del entradas_pendentes[fid]
        except Exception as e:
            print(f"Erro ao checar jogo encerrado {fid}: {e}")

if __name__ == "__main__":
    print("Bot iniciado com filtro ODD 1.5+...")
    enviar_telegram("🤖 <b>Bot Atualizado!</b>\nFiltro: Favorito perdendo/empatando + ODD 1.50+ escanteio")
    
    while True:
        jogos = buscar_jogos_ao_vivo()
        for jogo in jogos:
            try:
                fid = jogo["fixture"]["id"]
                tempo = jogo["fixture"]["status"]["elapsed"]
                
                if not tempo or not (20 <= tempo <= 85):
                    continue
                if fid in entradas_pendentes:
                    continue

                time_casa = jogo["teams"]["home"]["name"]
                time_fora = jogo["teams"]["away"]["name"]
                gc = jogo["goals"]["home"] if jogo["goals"]["home"] is not None else 0
                gf = jogo["goals"]["away"] if jogo["goals"]["away"] is not None else 0

                # REGRA: Placar Empatado ou Diferença de 1 gol
                diferenca = abs(gc - gf)
                if diferenca > 1:
                    continue

                cantos_atuais = obter_cantos(fid)
                if cantos_atuais is None or not (1 <= cantos_atuais <= 4):
                    continue

                resultado_odd = obter_odd_escanteio(fid)
                if not resultado_odd:
                    continue
                
                odd_valor, mercado = resultado_odd

                entradas_pendentes[fid] = {
                    "cantos_entrada": cantos_atuais,
                    "time_casa": time_casa,
                    "time_fora": time_fora,
                    "odd": odd_valor
                }

                status_fav = "EMPATANDO" if gc == gf else "PERDENDO por 1"
                enviar_telegram(
                    f"🔥 <b>ENTRADA OVER!</b> [ODD {odd_valor}]\n\n"
                    f"⚽ <b>Jogo:</b> {time_casa} x {time_fora}\n"
                    f"⏰ <b>Tempo:</b> {tempo}'\n"
                    f"📊 <b>Placar:</b> {gc}x{gf} ({status_fav})\n"
                    f"📍 <b>Cantos Atuais:</b> {cantos_atuais}\n"
                    f"💰 <b>Odd:</b> {odd_valor} no {mercado}\n\n"
                    f"👉 <b>ENTRADA: {mercado} FT</b>\n"
                    f"📈 <i>Taxa Atual: {calcular_taxa()}% ({stats['wins']}W/{stats['losses']}L)</i>"
                )
            except Exception as e:
                print(f"Erro no processamento do jogo: {e}")
        
        checar_jogos_encerrados()
        time.sleep(60)
