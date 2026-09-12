import threading
import time
import requests
import json
import os
from flask import Flask

app = Flask(__name__)

# --- STATS PERSISTENTE ---
ARQUIVO = "stats.json"
stats = json.load(open(ARQUIVO)) if os.path.exists(ARQUIVO) else {"wins": 0, "losses": 0}

def salvar_stats():
    with open(ARQUIVO, "w") as f:
        json.dump(stats, f, indent=4)

entradas_pendentes = {}  # {fixture_id: {"cantos_entrada": int, "favorito": str, "time_casa": str, "time_fora": str}}
cache_odds = {}

def calcular_taxa():
    total = stats["wins"] + stats["losses"]
    return 0.0 if total == 0 else round((stats["wins"] / total) * 100, 2)

@app.route('/')
def home():
    return f"Robo Online - Wins: {stats['wins']} | Losses: {stats['losses']} | Taxa: {calcular_taxa()}%"

def iniciar_servidor_web():
    app.run(host='0.0.0.0', port=10000)

# Inicia o servidor Flask em background (porta para o Render / Railway)
threading.Thread(target=iniciar_servidor_web, daemon=True).start()

# --- CONFIGURAÇÕES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
}

def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"[LOG TELEGRAM]: {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")

def buscar_jogos_ao_vivo():
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"live": "all"}, timeout=15)
        return r.json().get("response", [])
    except Exception as e:
        print(f"Erro ao buscar jogos ao vivo: {e}")
        return []

def obter_cantos(fid):
    try:
        r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures/statistics", headers=HEADERS, params={"fixture": fid}, timeout=15)
        total = 0
        for t in r.json().get("response", []):
            for s in t.get("statistics", []):
                if s.get("type") == "Corner Kicks" and s.get("value") is not None:
                    total += s["value"]
        return total
    except Exception as e:
        print(f"Erro ao buscar cantos ({fid}): {e}")
        return None

def obter_favorito_por_odd(fixture_id):
    """Retorna 'home' ou 'away' se a odd for < 2.00, senão None"""
    if fixture_id in cache_odds:
        return cache_odds[fixture_id]
    
    url = "https://api-football-v1.p.rapidapi.com/v3/odds"
    params = {"fixture": fixture_id, "bookmaker": "8", "bet": "1"}  # 8 = Bet365, bet 1 = 1x2
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp = r.json().get("response", [])
        if not resp:
            cache_odds[fixture_id] = (None, None)
            return None, None
            
        values = resp[0].get("bookmakers", [])[0].get("bets", [])[0].get("values", [])
        odd_casa = odd_fora = 999.0
        for v in values:
            if v["value"] == "Home":
                odd_casa = float(v["odd"])
            if v["value"] == "Away":
                odd_fora = float(v["odd"])
                
        print(f"DEBUG ODD Fixture {fixture_id}: Casa {odd_casa} | Fora {odd_fora}")
        
        if odd_casa < 2.00:
            cache_odds[fixture_id] = ("home", odd_casa)
            return "home", odd_casa
        if odd_fora < 2.00:
            cache_odds[fixture_id] = ("away", odd_fora)
            return "away", odd_fora
            
        cache_odds[fixture_id] = (None, None)
        return None, None
    except Exception as e:
        print(f"Erro ao buscar odd ({fixture_id}): {e}")
        return None, None

def checar_jogos_encerrados():
    """Valida se os jogos das entradas pendentes já foram encerrados e apura Green/Red"""
    for fid in list(entradas_pendentes.keys()):
        try:
            r = requests.get("https://api-football-v1.p.rapidapi.com/v3/fixtures", headers=HEADERS, params={"id": fid}, timeout=15)
            dados = r.json().get("response", [])
            if not dados:
                continue

            status = dados[0]["fixture"]["status"]["short"]
            
            # FT = Full Time, AET = Extra Time, PEN = Penalties
            if status in ["FT", "AET", "PEN"]:
                cantos_finais = obter_cantos(fid)
                dados_entrada = entradas_pendentes[fid]
                cantos_iniciais = dados_entrada["cantos_entrada"]
                
                if cantos_finais is not None:
                    # Regra de Validação: Precisa ter saído pelo menos +1 canto até o final do jogo
                    if cantos_finais > cantos_iniciais:
                        stats["wins"] += 1
                        msg = f"<b>GREEN!</b> ✅\n" \
                              f"<b>Jogo:</b> {dados_entrada['time_casa']} vs {dados_entrada['time_fora']}\n" \
                              f"<b>Escanteios na Entrada:</b> {cantos_iniciais}\n" \
                              f"<b>Escanteios Finais:</b> {cantos_finais}"
                    else:
                        stats["losses"] += 1
                        msg = f"<b>RED!</b> ❌\n" \
                              f"<b>Jogo:</b> {dados_entrada['time_casa']} vs {dados_entrada['time_fora']}\n" \
                              f"<b>Escanteios na Entrada:</b> {cantos_iniciais}\n" \
                              f"<b>Escanteios Finais:</b> {cantos_finais}"
                    
                    salvar_stats()
                    enviar_telegram(msg)
                    
                # Remove o jogo processado do monitoramento
                del entradas_pendentes[fid]
                
        except Exception as e:
            print(f"Erro ao checar encerramento do jogo {fid}: {e}")

def analisar_e_fazer_entradas():
    """Analisa jogos ao vivo para encontrar padrões de entrada"""
    jogos = buscar_jogos_ao_vivo()
    
    for jogo in jogos:
        try:
            fid = jogo["fixture"]["id"]
            tempo = jogo["fixture"]["status"]["elapsed"]
            
            # Pula jogos sem minutagem definida ou fora da janela estratégica (ex: entre 75' e 85')
            if not tempo or tempo < 75 or tempo > 85:
                continue

            # Se já fez entrada neste jogo, ignora
            if fid in entradas_pendentes:
                continue

            time_casa = jogo["teams"]["home"]["name"]
            time_fora = jogo["teams"]["away"]["name"]
            
            # Filtro por odd do favorito
            fav, odd = obter_favorito_por_odd(fid)
            if not fav:
                continue

            cantos_atuais = obter_cantos(fid)
            if cantos_atuais is None:
                continue

            # Registra a nova entrada
            entradas_pendentes[fid] = {
                "cantos_entrada": cantos_atuais,
                "favorito": fav,
                "time_casa": time_casa,
                "time_fora": time_fora
            }

            msg_sinal = (
                f"🚨 <b>ENTRADA CONFIRMADA!</b> 🚨\n\n"
                f"⚽ <b>Jogo:</b> {time_casa} vs {time_fora}\n"
                f"⏱ <b>Tempo:</b> {tempo}' min\n"
                f"🚩 <b>Escanteios Atuais:</b> {cantos_atuais}\n"
                f"📊 <b>Favorito:</b> {time_casa if fav == 'home' else time_fora} (Odd: {odd})\n\n"
                f"🎯 <b>Estratégia:</b> Over +0.5 Cantos no jogo"
            )
            enviar_telegram(msg_sinal)

        except Exception as e:
            print(f"Erro ao processar fixture {jogo.get('fixture', {}).get('id')}: {e}")

# --- LOOP PRINCIPAL DO BOT ---
if __name__ == "__main__":
    print("Bot de Escanteios iniciado com sucesso...")
    enviar_telegram("🤖 <b>Bot de Escanteios Inicializado!</b>")
    
    while True:
        try:
            analisar_e_fazer_entradas()
            checar_jogos_encerrados()
        except Exception as e:
            print(f"Erro no loop principal: {e}")
        
        # Pausa entre varreduras (ex: 60 segundos) para não estourar os limites de requisição da API
        time.sleep(60)
