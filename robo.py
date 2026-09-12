import requests
import time
import random
from datetime import datetime

TOKEN = "8933202267:AAG0f_Aggve3LmWoGu23ZMPn1qBKO2RFoy0"
CHAT_ID = "8863811629"

def enviar(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

print("ROBO 24H LIGADO")
enviar("✅ <b>ROBÔ ESCANTEIO 24H ONLINE NO RENDER!</b>\n\nNunca mais desliga!")

jogos = ["Flamengo x Palmeiras", "Real Madrid x Barcelona", "Man City x Arsenal", "Corinthians x São Paulo"]

while True:
    time.sleep(1800)
    jogo = random.choice(jogos)
    msg = f"""
🚩 <b>SINAL DE ESCANTEIO!</b>

⚽ Jogo: {jogo}
📊 7 escanteios | Pressão total
⏰ {random.randint(75,88)} min

👉 <b>ENTRADA: +1 Escanteio</b>
💰 Odd: 1.85

ENTRA RÁPIDO!
"""
    enviar(msg)
