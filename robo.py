import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

ARQUIVO_STATS = "stats.json"

# Estratégia
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))
ODD_MIN = float(os.getenv("ODD_MIN", "1.85"))
ODD_MAX = float(os.getenv("ODD_MAX", "2.50"))

ULTIMOS_JOGOS = int(os.getenv("ULTIMOS_JOGOS", "5"))
MINIMO_AMOSTRAS = int(os.getenv("MINIMO_AMOSTRAS", "4"))

ASSERTIVIDADE_2_MIN = float(
    os.getenv("ASSERTIVIDADE_2_MIN", "70")
)

ASSERTIVIDADE_3_MIN = float(
    os.getenv("ASSERTIVIDADE_3_MIN", "60")
)

ASSERTIVIDADE_MEDIA_3_MIN = float(
    os.getenv("ASSERTIVIDADE_MEDIA_3_MIN", "65")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "300")
)

INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "180")
)

HISTORICO_TTL = int(
    os.getenv("HISTORICO_TTL", "1800")
)

FUTUROS_TTL = int(
    os.getenv("FUTUROS_TTL", "300")
)

HORAS_FUTUROS = 24

MAX_CANDIDATOS = int(
    os.getenv("MAX_CANDIDATOS", "4")
)

STAKE_BASE = float(
    os.getenv("STAKE_BASE", "10")
)

# Gale independente por banca
GALES = [10, 20, 40, 80, 160]

NUM_BANCAS = 3

# Limite interno abaixo do limite da API
LIMITE_REQUISICOES_MINUTO = 9


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("ROBO")


# ============================================================
# ESTADO
# ============================================================

lock = threading.Lock()

stats = {
    "wins": 0,
    "losses": 0,
    "push": 0,

    "sinais": 0,

    "wins_2_mercados": 0,
    "losses_2_mercados": 0,

    "wins_3_mercados": 0,
    "losses_3_mercados": 0,

    "banca_1_wins": 0,
    "banca_1_losses": 0,

    "banca_2_wins": 0,
    "banca_2_losses": 0,

    "banca_3_wins": 0,
    "banca_3_losses": 0
}


if os.path.exists(ARQUIVO_STATS):

    try:

        with open(
            ARQUIVO_STATS,
            "r",
            encoding="utf-8"
        ) as f:

            carregado = json.load(f)

            if isinstance(carregado, dict):
                stats.update(carregado)

    except Exception as e:

        logger.error(
            f"Erro carregando stats: {e}"
        )


# ============================================================
# BANCAS
# ============================================================

bancas = []

for i in range(NUM_BANCAS):

    bancas.append(
        {
            "id": i + 1,
            "ocupada": False,
            "fixture_id": None,
            "sinal": None,
            "gale": 0,
            "entrada": GALES[0],
            "inicio": None,
            "resultado": None
        }
    )


# ============================================================
# CACHE
# ============================================================

historico_cache = {}

futuros_cache = {
    "timestamp": 0,
    "dados": []
}

fixture_usados = set()


# ============================================================
# CONTROLE DE REQUESTS
# ============================================================

request_times = []


def pode_fazer_request():

    agora = time.time()

    with lock:

        while request_times and (
            agora - request_times[0] > 60
        ):
            request_times
