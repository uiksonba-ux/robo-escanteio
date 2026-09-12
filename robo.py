from flask import Flask
import threading
app = Flask(__name__)
@app.route('/')
def home():
    return "Robo Online - Cacador Odd Alta"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

import requests, time, random
from datetime import datetime

TOKEN = "8933202267:AAG0f_Aggve3LmWoGu23ZMPn1qBKO2RFoy0"
CHAT_ID = "8863811629"

def enviar(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

print("ROBO 24H LIGADO - MODO ODD ALTA")
enviar("✅ <b>ROBÔ ATUALIZADO! Agora é CAÇADOR DE ODD ALTA!</b>\n\nAguardando favorito empatando/perdendo por 1 gol com poucos escanteios...")

# Lista com favorito definido
jogos_base = [
    {"nome": "Flamengo x Palmeiras", "fav": "casa"},
    {"nome": "Real Madrid x Barcelona", "fav": "casa"},
    {"nome": "Man City x Arsenal", "fav": "casa"},
    {"nome": "Corinthians x São Paulo", "fav": "casa"},
    {"nome": "Bayern x Dortmund", "fav": "casa"},
]

while True:
    time.sleep(300) # verifica a cada 5 min
    
    jogo = random.choice(jogos_base)
    
    # SIMULAÇÃO DE PLACAR REAL (depois trocamos por API real)
    # Aqui simula placar onde favorito tá empatando ou perdendo de 1
    situacoes = [
        (0,0, "EMPATANDO"), # 0x0
        (1,1, "EMPATANDO"), # 1x1
        (0,1, "PERDENDO por 1"), # perde de 1
        (1,2, "PERDENDO por 1"),
    ]
    casa, fora, motivo = random.choice(situacoes)
    
    # Inverte se favorito for fora
    if jogo["fav"] == "fora":
        casa, fora = fora, casa
    
    tempo = random.randint(28, 85)
    cantos = random.randint(1, 3) # SÓ 2 OU 3 CANTO - ODD ALTA!

    # REGRA FINAL: Favorito empatando/perdendo de 1 + poucos cantos
    if cantos <= 3 and tempo >= 25:
        msg = f"""
🔥 <b>ODD ALTA DETECTADA!</b> 🔥

⚽ Jogo: {jogo['nome']}
⏰ Tempo: {tempo} min
📊 Placar: {casa}x{fora}
🎯 Favorito: {motivo}
📍 Escanteios agora: {cantos}

<b>👉 ENTRADA: OVER 8.5 escanteios FT</b>
💰 ODD: 3.50+ (ALTA)
🧠 Motivo: Favorito precisa do resultado, pressão total até o fim!

ENTRA COM STAKE BAIXA - É PRA FORRAR!
"""
        enviar(msg)
