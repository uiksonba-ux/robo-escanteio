import threading
import time
import requests
from flask import Flask

app = Flask(__name__)

# Estrutura de Estatísticas e Entradas Pendentes
stats = {
    "wins": 0,
    "losses": 0
}

# Guarda jogos em andamento que tiveram entrada enviada
entradas_pendentes = {}

def calcular_taxa():
    total = stats["wins"] + stats["losses"]
    if total == 0:
        return 0.0
    return round((stats["wins"] / total) * 100, 2)

@app.route('/')
def home():
    return f"Robo Online - Wins: {stats['wins']} | Losses: {stats['losses']} | Taxa: {calcular_taxa()}%"

# Servidor web para manter a aplicação online (Render / Replit)
threading.Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': 10000}, daemon=True).start()

# --- CONFIGURAÇÕES DE TELEGRAM E API ---
TELEGRAM_TOKEN = "8933202267:AAG0f_Aggve3LmwoGu23ZMPn1qBKD2RFoy0"
CHAT_ID = "8863811629"
RAPIDAPI_KEY = "82010ba2c58cf9a791512c38bcbc44e8"

HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
}

# IDs dos times favoritos na API-Football (Exemplos: Flamengo, Palmeiras, Real Madrid, Corinthians, etc.)
FAVORITOS_IDS = [127, 126, 50, 49, 541, 121, 131]

def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

def buscar_jogos_ao_vivo():
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"live": "all"}
    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        return response.json().get("response", [])
    except Exception as e:
        print(f"Erro ao buscar jogos: {e}")
        return []

def obter_cantos(fixture_id):
    """Obtém o número atual de escanteios da partida"""
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures/statistics"
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        dados = response.json().get("response", [])
        
        total_cantos = 0
        for time_stat in dados:
            for stat in time_stat.get("statistics", []):
                if stat.get("type") == "Corner Kicks":
                    val = stat.get("value")
                    total_cantos += val if val is not None else 0
        return total_cantos
    except Exception as e:
        print(f"Erro ao obter estatísticas de escanteios: {e}")
        return None

def checar_jogos_encerrados():
    """Verifica entradas enviadas anteriormente para atualizar Wins/Losses quando a partida terminar"""
    for fixture_id in list(entradas_pendentes.keys()):
        url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
        params = {"id": fixture_id}
        try:
            response = requests.get(url, headers=HEADERS, params=params, timeout=10)
            dados = response.json().get("response", [])
            if not dados:
                continue
            
            jogo = dados[0]
            status = jogo["fixture"]["status"]["short"]

            # Status de final de jogo: FT, AET ou PEN
            if status in ["FT", "AET", "PEN"]:
                cantos_finais = obter_cantos(fixture_id)
                nome_jogo = entradas_pendentes[fixture_id]["nome"]
                
                if cantos_finais is not None:
                    # Regra de Win: Over 8.5 precisa de 9 ou mais cantos
                    if cantos_finais >= 9:
                        stats["wins"] += 1
                        resultado_str = "✅ <b>GREEN / WIN!</b>"
                    else:
                        stats["losses"] += 1
                        resultado_str = "❌ <b>RED / LOSS!</b>"

                    taxa = calcular_taxa()
                    total_jogos = stats["wins"] + stats["losses"]

                    msg_resultado = f"""{resultado_str}

⚽ <b>Jogo:</b> {nome_jogo}
🚩 <b>Escanteios Finais:</b> {cantos_finais}
🎯 <b>Entrada:</b> Over 8.5 escanteios

📊 <b>DESEMPENHO ATUALIZADO:</b>
✅ Wins: {stats['wins']}
❌ Losses: {stats['losses']}
📈 Assertividade: <b>{taxa}%</b> ({stats['wins']}/{total_jogos})"""

                    enviar_telegram(msg_resultado)
                    del entradas_pendentes[fixture_id]

        except Exception as e:
            print(f"Erro ao verificar jogo encerrado {fixture_id}: {e}")

print("🤖 ROBÔ 24H LIGADO - MODULO AO VIVO ATIVADO")
enviar_telegram("<b>🤖 ROBÔ ATUALIZADO!</b>\nMonitorando partidas em tempo real (20-60 min)...")

while True:
    jogos = buscar_jogos_ao_vivo()

    for jogo in jogos:
        fixture_id = jogo["fixture"]["id"]
        tempo = jogo["fixture"]["status"]["elapsed"]

        if tempo is None or fixture_id in entradas_pendentes:
            continue

        # 1. Filtro de Tempo (Entre 20 e 60 minutos)
        if 20 <= tempo <= 60:
            home_team = jogo["teams"]["home"]
            away_team = jogo["teams"]["away"]
            
            gols_casa = jogo["goals"]["home"] or 0
            gols_fora = jogo["goals"]["away"] or 0

            fav_casa = home_team["id"] in FAVORITOS_IDS
            fav_fora = away_team["id"] in FAVORITOS_IDS

            motivo = None
            if fav_casa:
                if gols_casa == gols_fora:
                    motivo = f"Favorito ({home_team['name']}) Empatando"
                elif gols_fora - gols_casa == 1:
                    motivo = f"Favorito ({home_team['name']}) Perdendo por 1"
            elif fav_fora:
                if gols_casa == gols_fora:
                    motivo = f"Favorito ({away_team['name']}) Empatando"
                elif gols_casa - gols_fora == 1:
                    motivo = f"Favorito ({away_team['name']}) Perdendo por 1"

            # 2. Se atendeu o critério do favorito, checa escanteios
            if motivo:
                cantos_atuais = obter_cantos(fixture_id)

                # Regra: Poucos cantos (3 ou menos no momento)
                if cantos_atuais is not None and cantos_atuais <= 3:
                    nome_partida = f"{home_team['name']} x {away_team['name']}"

                    msg = f"""🔥 <b>ODD ALTA DETECTADA (JOGO REAL)!</b> 🔥

⚽ <b>Jogo:</b> {nome_partida}
⏰ <b>Tempo:</b> {tempo} min
📊 <b>Placar:</b> {gols_casa}x{gols_fora}
🎯 <b>Situação:</b> {motivo}
🚩 <b>Escanteios Agora:</b> {cantos_atuais}

<b>👉 ENTRADA: OVER 8.5 escanteios FT</b>
💰 <b>ODD Sugerida:</b> 3.50+ (ALTA)
💡 <b>Motivo:</b> Pressão do favorito até o fim com poucos cantos na partida.

📊 <i>Assertividade Atual: {calcular_taxa()}% ({stats['wins']}W / {stats['losses']}L)</i>"""

                    enviar_telegram(msg)
                    entradas_pendentes[fixture_id] = {"nome": nome_partida}

    # Verifica se jogos anteriormente enviados terminaram para atualizar placar de GREEN/RED
    checar_jogos_encerrados()

    # Intervalo de 3 minutos para cada consulta
    time.sleep(180)
