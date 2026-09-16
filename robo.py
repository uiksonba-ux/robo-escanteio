import os
import json
import time
import threading
import logging
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY") or os.getenv("FIVE_DOLLAR_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# ESTRATÉGIA
# ============================================================

ENTRADA_INICIAL = float(os.getenv("ENTRADA_INICIAL", "10"))

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(
    os.getenv("MAX_GALES", "2")
)

TOTAL_BANCAS = int(
    os.getenv("TOTAL_BANCAS", "3")
)

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

ODD_MINIMA = float(
    os.getenv("ODD_MINIMA", "1.80")
)

ODD_MAXIMA = float(
    os.getenv("ODD_MAXIMA", "2.50")
)

ODD_ALVO = float(
    os.getenv("ODD_ALVO", "2.00")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "5")
)

MIN_MEDIA_ESCANTEIOS = float(
    os.getenv("MIN_MEDIA_ESCANTEIOS", "8.5")
)

MAX_MEDIA_GOLS = float(
    os.getenv("MAX_MEDIA_GOLS", "4.0")
)

MIN_PROBABILIDADE_HISTORICA = float(
    os.getenv("MIN_PROBABILIDADE_HISTORICA", "0.62")
)

MAX_JOGOS_ODDS_POR_CICLO = int(
    os.getenv("MAX_JOGOS_ODDS_POR_CICLO", "5")
)


# ============================================================
# RATE LIMIT
# ============================================================

# API permite 10/min.
# Vamos trabalhar com 9/min para deixar margem.

API_MAX_REQUESTS_PER_MINUTE = int(
    os.getenv("API_MAX_REQUESTS_PER_MINUTE", "9")
)

API_MIN_INTERVAL_SECONDS = float(
    os.getenv("API_MIN_INTERVAL_SECONDS", "6.7")
)

API_BACKOFF_429 = int(
    os.getenv("API_BACKOFF_429", "65")
)

api_request_times = deque()

api_rate_lock = threading.Lock()

ultimo_request_api = 0.0

api_backoff_until = 0.0


# ============================================================
# CACHE
# ============================================================

CACHE_FIXTURES_TTL = int(
    os.getenv("CACHE_FIXTURES_TTL", "600")
)

CACHE_ODDS_TTL = int(
    os.getenv("CACHE_ODDS_TTL", "1800")
)

cache_fixtures = {
    "timestamp": 0,
    "data": []
}

cache_odds = {}

cache_lock = threading.Lock()


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS",
    "stats.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS",
    "sinais.json"
)

arquivo_lock = threading.Lock()


# ============================================================
# UTILIDADES JSON
# ============================================================

def carregar_json(arquivo, padrao):

    if not os.path.exists(arquivo):
        return padrao

    try:
        with open(
            arquivo,
            "r",
            encoding="utf-8"
        ) as f:

            dados = json.load(f)

            return dados

    except Exception as e:

        logger.error(
            "Erro lendo %s: %s",
            arquivo,
            e
        )

        return padrao


def salvar_json(arquivo, dados):

    try:

        with arquivo_lock:

            temp = arquivo + ".tmp"

            with open(
                temp,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    dados,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(temp, arquivo)

    except Exception as e:

        logger.error(
            "Erro salvando %s: %s",
            arquivo,
            e
        )


# ============================================================
# STATS
# ============================================================

stats_padrao = {

    "total": 0,

    "wins": 0,

    "losses": 0,

    "lucro_teorico": 0.0,

    "por_gale": {

        "0": {
            "wins": 0,
            "losses": 0
        },

        "1": {
            "wins": 0,
            "losses": 0
        },

        "2": {
            "wins": 0,
            "losses": 0
        }
    },

    "bancas": {}
}


stats = carregar_json(
    ARQUIVO_STATS,
    stats_padrao
)


# ============================================================
# NORMALIZAR STATS ANTIGOS
# ============================================================

stats.setdefault("total", 0)
stats.setdefault("wins", 0)
stats.setdefault("losses", 0)
stats.setdefault("lucro_teorico", 0.0)
stats.setdefault("por_gale", {})
stats.setdefault("bancas", {})

for g in range(MAX_GALES + 1):

    chave = str(g)

    stats["por_gale"].setdefault(
        chave,
        {
            "wins": 0,
            "losses": 0
        }
    )


# ============================================================
# BANCAS
# ============================================================

for i in range(1, TOTAL_BANCAS + 1):

    chave = str(i)

    stats["bancas"].setdefault(
        chave,
        {
            "gale": 0,
            "ativa": False,
            "wins": 0,
            "losses": 0,
            "lucro": 0.0
        }
    )


# ============================================================
# SINAIS
# ============================================================

sinais = carregar_json(
    ARQUIVO_SINAIS,
    []
)

if not isinstance(sinais, list):
    sinais = []


# ============================================================
# TEMPO
# ============================================================

def utc_agora():

    return datetime.now(timezone.utc)


def iso_agora():

    return utc_agora().isoformat()


def timestamp_agora():

    return int(utc_agora().timestamp())


def converter_timestamp(valor):

    if valor is None:
        return None

    try:

        if isinstance(valor, (int, float)):

            # Caso venha em milissegundos
            if valor > 100000000000:
                valor = valor / 1000

            return int(valor)

        texto = str(valor).strip()

        if texto.isdigit():

            valor_int = int(texto)

            if valor_int > 100000000000:
                valor_int = int(valor_int / 1000)

            return valor_int

        dt = datetime.fromisoformat(
            texto.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return int(dt.timestamp())

    except Exception:

        return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        if response.status_code != 200:

            logger.error(
                "Telegram %s | %s",
                response.status_code,
                response.text
            )

            return False

        return True

    except Exception as e:

        logger.error(
            "Erro Telegram: %s",
            e
        )

        return False


# ============================================================
# RATE LIMITER
# ============================================================

def esperar_limite_api():

    global ultimo_request_api
    global api_backoff_until

    while True:

        espera = 0.0

        with api_rate_lock:

            agora = time.monotonic()

            # Backoff causado por 429
            if agora < api_backoff_until:

                espera = max(
                    espera,
                    api_backoff_until - agora
                )

            # Remove requests com mais de 60 segundos
            while (
                api_request_times
                and agora - api_request_times[0] >= 60
            ):

                api_request_times.popleft()

            # Intervalo mínimo entre requests
            if ultimo_request_api > 0:

                desde_ultima = (
                    agora - ultimo_request_api
                )

                if (
                    desde_ultima
                    < API_MIN_INTERVAL_SECONDS
                ):

                    espera = max(
                        espera,
                        API_MIN_INTERVAL_SECONDS
                        - desde_ultima
                    )

            # Máximo de requests na janela de 60s
            if (
                len(api_request_times)
                >= API_MAX_REQUESTS_PER_MINUTE
            ):

                tempo_restante = (
                    60
                    - (
                        agora
                        - api_request_times[0]
                    )
                    + 0.5
                )

                espera = max(
                    espera,
                    tempo_restante
                )

            if espera <= 0:

                agora = time.monotonic()

                api_request_times.append(
                    agora
                )

                ultimo_request_api = agora

                return

        logger.info(
            "Rate limit API: aguardando %.1fs...",
            espera
        )

        time.sleep(
            max(espera, 0.1)
        )


# ============================================================
# API GET
# ============================================================

def api_get(endpoint, params=None):

    global api_backoff_until

    if not API_KEY:

        logger.error(
            "FIVE_DOLLAR_API_KEY não configurada."
        )

        return None

    esperar_limite_api()

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    headers = {

        "Authorization": f"Bearer {API_KEY}",

        "Accept": "application/json",

        "User-Agent": "robo-combinado/3.0"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

    except requests.RequestException as e:

        logger.error(
            "Erro conexão API: %s",
            e
        )

        return None

    # ========================================================
    # RATE LIMIT
    # ========================================================

    if response.status_code == 429:

        retry_after = response.headers.get(
            "Retry-After"
        )

        try:

            espera = int(retry_after)

        except (TypeError, ValueError):

            espera = API_BACKOFF_429

        with api_rate_lock:

            api_backoff_until = max(
                api_backoff_until,
                time.monotonic() + espera
            )

        logger.error(
            "API 429 | limite atingido. "
            "Backoff de %ss | %s",
            espera,
            response.text
        )

        return None

    # ========================================================
    # OUTROS ERROS
    # ========================================================

    if response.status_code != 200:

        logger.error(
            "API %s | %s",
            response.status_code,
            response.text
        )

        return None

    try:

        return response.json()

    except Exception:

        logger.error(
            "Resposta inválida da API: %s",
            response.text[:500]
        )

        return None


# ============================================================
# EXTRAIR DATA DA API
# ============================================================

def extrair_data(resposta):

    if resposta is None:
        return None

    if isinstance(resposta, list):
        return resposta

    if not isinstance(resposta, dict):
        return resposta

    if "data" in resposta:
        return resposta["data"]

    return resposta


# ============================================================
# FIXTURES - CACHE
# ============================================================

def buscar_partidas():

    agora_cache = time.time()

    with cache_lock:

        if (
            cache_fixtures["data"]
            and (
                agora_cache
                - cache_fixtures["timestamp"]
            )
            < CACHE_FIXTURES_TTL
        ):

            logger.info(
                "Usando cache de partidas."
            )

            return cache_fixtures["data"]

    agora = utc_agora()

    fim = agora + timedelta(
        hours=JANELA_HORAS
    )

    # IMPORTANTE:
    # A API exige UNIX timestamp UTC em segundos.

    start_timestamp = int(
        agora.timestamp()
    )

    end_timestamp = int(
        fim.timestamp()
    )

    params = {

        "start_time": start_timestamp,

        "end_time": end_timestamp,

        "status": "scheduled",

        "per_page": 100,

        "include": "odds"
    }

    logger.info(
        "Buscando partidas: %s -> %s",
        start_timestamp,
        end_timestamp
    )

    resposta = api_get(
        "/fixtures",
        params=params
    )

    dados = extrair_data(
        resposta
    )

    if isinstance(dados, dict):

        for chave in [
            "fixtures",
            "matches",
            "results"
        ]:

            if isinstance(
                dados.get(chave),
                list
            ):

                dados = dados[chave]

                break

    if not isinstance(dados, list):

        logger.warning(
            "Nenhuma lista de partidas retornada."
        )

        return []

    with cache_lock:

        cache_fixtures["timestamp"] = (
            agora_cache
        )

        cache_fixtures["data"] = dados

    logger.info(
        "%s partidas encontradas.",
        len(dados)
    )

    return dados


# ============================================================
# ID DO JOGO
# ============================================================

def pegar_id_jogo(jogo):

    if not isinstance(jogo, dict):
        return None

    for chave in [
        "id",
        "fixture_id",
        "match_id"
    ]:

        if jogo.get(chave) is not None:
            return str(jogo[chave])

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        if fixture.get("id") is not None:
            return str(
                fixture["id"]
            )

    return None


# ============================================================
# TIMES
# ============================================================

def pegar_nome_times(jogo):

    casa = None
    fora = None

    if not isinstance(jogo, dict):
        return "Casa", "Fora"

    # Formato teams
    teams = jogo.get("teams")

    if isinstance(teams, dict):

        home = teams.get("home")
        away = teams.get("away")

        if isinstance(home, dict):

            casa = (
                home.get("name")
                or home.get("team_name")
            )

        elif isinstance(home, str):

            casa = home

        if isinstance(away, dict):

            fora = (
                away.get("name")
                or away.get("team_name")
            )

        elif isinstance(away, str):

            fora = away

    # Outros formatos
    casa = (
        casa
        or jogo.get("home_name")
        or jogo.get("home_team")
        or jogo.get("home")
    )

    fora = (
        fora
        or jogo.get("away_name")
        or jogo.get("away_team")
        or jogo.get("away")
    )

    if isinstance(casa, dict):

        casa = (
            casa.get("name")
            or casa.get("team_name")
        )

    if isinstance(fora, dict):

        fora = (
            fora.get("name")
            or fora.get("team_name")
        )

    return (
        str(casa or "Casa"),
        str(fora or "Fora")
    )


# ============================================================
# HORÁRIO DO JOGO
# ============================================================

def pegar_inicio_jogo(jogo):

    campos = [

        "start_time",
        "timestamp",
        "kickoff",
        "date",
        "start_at"
    ]

    for campo in campos:

        if jogo.get(campo) is not None:

            ts = converter_timestamp(
                jogo.get(campo)
            )

            if ts:
                return ts

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for campo in campos:

            if fixture.get(campo) is not None:

                ts = converter_timestamp(
                    fixture.get(campo)
                )

                if ts:
                    return ts

    return None


# ============================================================
# CACHE DE ODDS
# ============================================================

def buscar_odds(jogo):

    fixture_id = pegar_id_jogo(jogo)

    if not fixture_id:
        return None

    # Primeiro tenta odds já presentes
    # no retorno de /fixtures.

    odds_embutidas = jogo.get("odds")

    if odds_embutidas:

        return odds_embutidas

    agora = time.time()

    with cache_lock:

        item = cache_odds.get(
            fixture_id
        )

        if item:

            idade = (
                agora
                - item["timestamp"]
            )

            if idade < CACHE_ODDS_TTL:

                logger.info(
                    "Odds cache fixture %s",
                    fixture_id
                )

                return item["data"]

    resposta = api_get(
        f"/fixtures/{fixture_id}/odds"
    )

    dados = extrair_data(
        resposta
    )

    if dados is not None:

        with cache_lock:

            cache_odds[
                fixture_id
            ] = {

                "timestamp": agora,

                "data": dados
            }

    return dados


# ============================================================
# UTILIDADE NÚMERO
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (TypeError, ValueError):

        return None


# ============================================================
# BUSCAR MERCADO RECURSIVAMENTE
# ============================================================

def percorrer_objeto(obj):

    if isinstance(obj, dict):

        yield obj

        for valor in obj.values():

            yield from percorrer_objeto(
                valor
            )

    elif isinstance(obj, list):

        for item in obj:

            yield from percorrer_objeto(
                item
            )


# ============================================================
# ENCONTRAR ODD OVER 8.5 CORNERS
# ============================================================

def encontrar_odd_escanteios(odds):

    candidatos = []

    for obj in percorrer_objeto(odds):

        texto = " ".join(
            str(v).lower()
            for v in obj.values()
            if isinstance(
                v,
                (str, int, float)
            )
        )

        line = numero(
            obj.get("line")
            or obj.get("handicap")
            or obj.get("total")
        )

        odd_over = numero(
            obj.get("over")
            or obj.get("over_odds")
            or obj.get("price_over")
        )

        if (
            line is not None
            and abs(line - 8.5) < 0.01
            and odd_over
            and (
                "corner" in texto
                or "corners" in texto
                or "escante" in texto
            )
        ):

            candidatos.append(
                odd_over
            )

    # Formato específico comum
    if isinstance(odds, dict):

        for chave in [
            "corner",
            "corners",
            "corner_line",
            "corner_asian"
        ]:

            mercado = odds.get(chave)

            if isinstance(mercado, dict):

                for periodo in [
                    "closing",
                    "opening",
                    "pre_match",
                    "prematch"
                ]:

                    dados = mercado.get(
                        periodo
                    )

                    if isinstance(
                        dados,
                        dict
                    ):

                        line = numero(
                            dados.get("line")
                        )

                        over = numero(
                            dados.get("over")
                        )

                        if (
                            line is not None
                            and abs(
                                line - 8.5
                            ) < 0.01
                            and over
                        ):

                            candidatos.append(
                                over
                            )

    if not candidatos:
        return None

    return candidatos[0]


# ============================================================
# ENCONTRAR ODD UNDER 4.5 GOALS
# ============================================================

def encontrar_odd_gols(odds):

    candidatos = []

    for obj in percorrer_objeto(odds):

        texto = " ".join(
            str(v).lower()
            for v in obj.values()
            if isinstance(
                v,
                (str, int, float)
            )
        )

        line = numero(
            obj.get("line")
            or obj.get("handicap")
            or obj.get("total")
        )

        odd_under = numero(
            obj.get("under")
            or obj.get("under_odds")
            or obj.get("price_under")
        )

        if (
            line is not None
            and abs(line - 4.5) < 0.01
            and odd_under
            and (
                "goal" in texto
                or "goalline" in texto
                or "goals" in texto
            )
            and "corner" not in texto
        ):

            candidatos.append(
                odd_under
            )

    if isinstance(odds, dict):

        for chave in [
            "goal_line",
            "goalline",
            "goals",
            "total_goals"
        ]:

            mercado = odds.get(chave)

            if isinstance(
                mercado,
                dict
            ):

                for periodo in [
                    "closing",
                    "opening",
                    "pre_match",
                    "prematch"
                ]:

                    dados = mercado.get(
                        periodo
                    )

                    if isinstance(
                        dados,
                        dict
                    ):

                        line = numero(
                            dados.get("line")
                        )

                        under = numero(
                            dados.get("under")
                        )

                        if (
                            line is not None
                            and abs(
                                line - 4.5
                            ) < 0.01
                            and under
                        ):

                            candidatos.append(
                                under
                            )

    if not candidatos:
        return None

    return candidatos[0]


# ============================================================
# HISTÓRICO
# ============================================================

def extrair_historico(jogo):

    campos = [

        "history",

        "recent_matches",

        "last_matches",

        "form",

        "historical_matches"
    ]

    encontrados = []

    for campo in campos:

        valor = jogo.get(campo)

        if isinstance(valor, list):

            encontrados.extend(
                valor
            )

        elif isinstance(valor, dict):

            for sub in valor.values():

                if isinstance(sub, list):

                    encontrados.extend(
                        sub
                    )

    return encontrados


# ============================================================
# EXTRAIR GOLS / CANTOS HISTÓRICOS
# ============================================================

def extrair_totais_partida(partida):

    if not isinstance(partida, dict):
        return None, None

    gols_total = None
    cantos_total = None

    goals = partida.get("goals")

    if isinstance(goals, dict):

        home = numero(
            goals.get("home")
        )

        away = numero(
            goals.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            gols_total = home + away

    corners = partida.get("corners")

    if isinstance(corners, dict):

        home = numero(
            corners.get("home")
        )

        away = numero(
            corners.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            cantos_total = home + away

    if gols_total is None:

        gols_total = numero(
            partida.get("total_goals")
        )

    if cantos_total is None:

        cantos_total = numero(
            partida.get("total_corners")
        )

    return (
        gols_total,
        cantos_total
    )


# ============================================================
# ESTATÍSTICAS HISTÓRICAS
# ============================================================

def calcular_historico(jogo):

    historico = extrair_historico(
        jogo
    )

    validos = []

    for partida in historico:

        gols, cantos = (
            extrair_totais_partida(
                partida
            )
        )

        if (
            gols is not None
            and cantos is not None
        ):

            validos.append(
                {
                    "gols": gols,
                    "cantos": cantos
                }
            )

    total = len(validos)

    if total < MIN_JOGOS_HISTORICO:

        return None

    soma_gols = sum(
        p["gols"]
        for p in validos
    )

    soma_cantos = sum(
        p["cantos"]
        for p in validos
    )

    media_gols = (
        soma_gols / total
    )

    media_cantos = (
        soma_cantos / total
    )

    acertos = sum(
        1
        for p in validos
        if (
            p["cantos"] >= 9
            and p["gols"] <= 4
        )
    )

    probabilidade = (
        acertos / total
    )

    return {

        "jogos": total,

        "media_gols": round(
            media_gols,
            2
        ),

        "media_cantos": round(
            media_cantos,
            2
        ),

        "acertos": acertos,

        "probabilidade": round(
            probabilidade,
            4
        )
    }


# ============================================================
# FILTRO HISTÓRICO
# ============================================================

def historico_aprovado(hist):

    if hist is None:
        return False

    if (
        hist["media_cantos"]
        < MIN_MEDIA_ESCANTEIOS
    ):
        return False

    if (
        hist["media_gols"]
        > MAX_MEDIA_GOLS
    ):
        return False

    if (
        hist["probabilidade"]
        < MIN_PROBABILIDADE_HISTORICA
    ):
        return False

    return True


# ============================================================
# JOGO JÁ ATIVO
# ============================================================

def jogo_ja_usado(fixture_id):

    fixture_id = str(
        fixture_id
    )

    for sinal in sinais:

        if (
            str(
                sinal.get(
                    "fixture_id"
                )
            )
            == fixture_id
            and sinal.get("status")
            == "ATIVO"
        ):

            return True

    return False


# ============================================================
# CANDIDATO
# ============================================================

def analisar_jogo(jogo):

    fixture_id = pegar_id_jogo(
        jogo
    )

    if not fixture_id:
        return None

    if jogo_ja_usado(
        fixture_id
    ):
        return None

    inicio = pegar_inicio_jogo(
        jogo
    )

    if inicio:

        agora = timestamp_agora()

        if inicio <= agora:
            return None

        limite = (
            agora
            + JANELA_HORAS * 3600
        )

        if inicio > limite:
            return None

    # Histórico real
    hist = calcular_historico(
        jogo
    )

    if not historico_aprovado(
        hist
    ):

        return None

    odds = buscar_odds(
        jogo
    )

    if odds is None:
        return None

    odd_cantos = (
        encontrar_odd_escanteios(
            odds
        )
    )

    odd_gols = (
        encontrar_odd_gols(
            odds
        )
    )

    if (
        odd_cantos is None
        or odd_gols is None
    ):

        return None

    # Odd combinada TEÓRICA
    # É produto das duas odds individuais.
    #
    # A odd real oferecida pela casa pode
    # ser diferente.

    odd_combinada = (
        odd_cantos
        * odd_gols
    )

    if (
        odd_combinada
        < ODD_MINIMA
        or odd_combinada
        > ODD_MAXIMA
    ):

        return None

    casa, fora = pegar_nome_times(
        jogo
    )

    # Quanto maior a probabilidade histórica
    # e mais perto da odd alvo, maior o score.

    distancia_odd = abs(
        odd_combinada
        - ODD_ALVO
    )

    score = (
        hist["probabilidade"] * 100
        - distancia_odd * 10
    )

    return {

        "fixture_id": fixture_id,

        "home": casa,

        "away": fora,

        "inicio": inicio,

        "odd_cantos": round(
            odd_cantos,
            3
        ),

        "odd_gols": round(
            odd_gols,
            3
        ),

        "odd_combinada": round(
            odd_combinada,
            3
        ),

        "historico": hist,

        "score": round(
            score,
            3
        )
    }


# ============================================================
# BANCAS LIVRES
# ============================================================

def bancas_livres():

    livres = []

    for numero_banca, dados in (
        stats["bancas"].items()
    ):

        if not dados.get(
            "ativa",
            False
        ):

            livres.append(
                numero_banca
            )

    return livres


# ============================================================
# VALOR ENTRADA
# ============================================================

def calcular_entrada(banca):

    gale = int(
        stats["bancas"][banca].get(
            "gale",
            0
        )
    )

    valor = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** gale
        )
    )

    return round(
        valor,
        2
    )


# ============================================================
# FORMATAR HORÁRIO
# ============================================================

def formatar_horario(timestamp):

    if not timestamp:
        return "Não informado"

    try:

        dt = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc
        )

        # Brasil -03:00
        dt_br = dt - timedelta(
            hours=3
        )

        return dt_br.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(
            timestamp
        )


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    banca,
    candidato
):

    gale = int(
        stats["bancas"][banca].get(
            "gale",
            0
        )
    )

    entrada = calcular_entrada(
        banca
    )

    sinal = {

        "id": (
            f"{candidato['fixture_id']}"
            f"-{banca}-"
            f"{int(time.time())}"
        ),

        "fixture_id": (
            candidato["fixture_id"]
        ),

        "home": (
            candidato["home"]
        ),

        "away": (
            candidato["away"]
        ),

        "inicio": (
            candidato["inicio"]
        ),

        "banca": banca,

        "gale": gale,

        "entrada": entrada,

        "odd_cantos": (
            candidato["odd_cantos"]
        ),

        "odd_gols": (
            candidato["odd_gols"]
        ),

        "odd_combinada": (
            candidato[
                "odd_combinada"
            ]
        ),

        "historico": (
            candidato["historico"]
        ),

        "status": "ATIVO",

        "resultado": None,

        "criado_em": iso_agora(),

        "resolvido_em": None
    }

    sinais.append(
        sinal
    )

    stats["bancas"][banca][
        "ativa"
    ] = True

    salvar_json(
        ARQUIVO_SINAIS,
        sinais
    )

    salvar_json(
        ARQUIVO_STATS,
        stats
    )

    hist = candidato[
        "historico"
    ]

    mensagem = (
        "🚨 <b>NOVO SINAL</b>\n\n"
        f"⚽ <b>{candidato['home']} x "
        f"{candidato['away']}</b>\n\n"
        f"🕒 {formatar_horario(candidato['inicio'])}\n\n"
        "📊 <b>Mercados</b>\n"
        "• Over 8.5 escanteios\n"
        "• Under 4.5 gols\n\n"
        f"💰 Odd cantos: {candidato['odd_cantos']}\n"
        f"💰 Odd gols: {candidato['odd_gols']}\n"
        f"🔥 Odd combinada teórica: "
        f"{candidato['odd_combinada']}\n\n"
        f"🏦 Banca: {banca}\n"
        f"🔄 Gale: {gale}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}\n\n"
        "📈 <b>Histórico encontrado</b>\n"
        f"Jogos: {hist['jogos']}\n"
        f"Média cantos: {hist['media_cantos']}\n"
        f"Média gols: {hist['media_gols']}\n"
        f"Acertos combinados: {hist['acertos']}\n"
        f"Taxa histórica: "
        f"{hist['probabilidade'] * 100:.1f}%"
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "SINAL | %s x %s | "
        "Banca %s | Gale %s | R$ %.2f",
        candidato["home"],
        candidato["away"],
        banca,
        gale,
        entrada
    )


# ============================================================
# STATUS FINAL
# ============================================================

def status_final(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return False

    valores = []

    for chave in [
        "status",
        "state",
        "fixture_status"
    ]:

        if jogo.get(chave) is not None:

            valores.append(
                str(
                    jogo.get(chave)
                ).lower()
            )

    fixture = jogo.get(
        "fixture"
    )

    if isinstance(
        fixture,
        dict
    ):

        status = fixture.get(
            "status"
        )

        if isinstance(
            status,
            dict
        ):

            valores.extend(
                str(v).lower()
                for v in status.values()
                if v is not None
            )

        elif status is not None:

            valores.append(
                str(status).lower()
            )

    texto = " ".join(
        valores
    )

    finais = [

        "finished",
        "final",
        "ft",
        "ended",
        "completed"
    ]

    return any(
        palavra in texto
        for palavra in finais
    )


# ============================================================
# PEGAR PLACAR FINAL
# ============================================================

def pegar_resultado_final(jogo):

    gols_total = None
    cantos_total = None

    goals = jogo.get(
        "goals"
    )

    if isinstance(
        goals,
        dict
    ):

        home = numero(
            goals.get("home")
        )

        away = numero(
            goals.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            gols_total = (
                home + away
            )

    corners = jogo.get(
        "corners"
    )

    if isinstance(
        corners,
        dict
    ):

        home = numero(
            corners.get("home")
        )

        away = numero(
            corners.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            cantos_total = (
                home + away
            )

    return (
        gols_total,
        cantos_total
    )


# ============================================================
# REGISTRAR RESULTADO
# ============================================================

def registrar_resultado(
    sinal,
    resultado,
    gols,
    cantos
):

    banca = str(
        sinal["banca"]
    )

    gale = int(
        sinal["gale"]
    )

    entrada = float(
        sinal["entrada"]
    )

    odd = float(
        sinal["odd_combinada"]
    )

    sinal["status"] = (
        "FINALIZADO"
    )

    sinal["resultado"] = (
        resultado
    )

    sinal["gols_final"] = (
        gols
    )

    sinal["cantos_final"] = (
        cantos
    )

    sinal["resolvido_em"] = (
        iso_agora()
    )

    stats["total"] += 1

    chave_gale = str(
        gale
    )

    stats["por_gale"].setdefault(
        chave_gale,
        {
            "wins": 0,
            "losses": 0
        }
    )

    # ========================================================
    # WIN
    # ========================================================

    if resultado == "WIN":

        lucro = (
            entrada
            * (odd - 1)
        )

        stats["wins"] += 1

        stats["lucro_teorico"] += (
            lucro
        )

        stats["por_gale"][
            chave_gale
        ]["wins"] += 1

        stats["bancas"][
            banca
        ]["wins"] += 1

        stats["bancas"][
            banca
        ]["lucro"] += lucro

        # WIN reseta sequência
        stats["bancas"][
            banca
        ]["gale"] = 0

        stats["bancas"][
            banca
        ]["ativa"] = False

    # ========================================================
    # LOSS
    # ========================================================

    else:

        lucro = -entrada

        stats["losses"] += 1

        stats["lucro_teorico"] += (
            lucro
        )

        stats["por_gale"][
            chave_gale
        ]["losses"] += 1

        stats["bancas"][
            banca
        ]["losses"] += 1

        stats["bancas"][
            banca
        ]["lucro"] += lucro

        # Próximo Gale
        if gale < MAX_GALES:

            stats["bancas"][
                banca
            ]["gale"] = (
                gale + 1
            )

        else:

            # Perdeu Gale máximo
            # reinicia sequência
            stats["bancas"][
                banca
            ]["gale"] = 0

        stats["bancas"][
            banca
        ]["ativa"] = False

    stats["lucro_teorico"] = round(
        stats["lucro_teorico"],
        2
    )

    stats["bancas"][
        banca
    ]["lucro"] = round(
        stats["bancas"][
            banca
        ]["lucro"],
        2
    )

    salvar_json(
        ARQUIVO_SINAIS,
        sinais
    )

    salvar_json(
        ARQUIVO_STATS,
        stats
    )

    total = stats[
        "wins"
    ] + stats[
        "losses"
    ]

    taxa = (
        stats["wins"]
        / total
        * 100
        if total
        else 0
    )

    emoji = (
        "✅"
        if resultado == "WIN"
        else "❌"
    )

    mensagem = (
        f"{emoji} <b>{resultado}</b>\n\n"
        f"⚽ {sinal['home']} x "
        f"{sinal['away']}\n\n"
        f"🥅 Gols: {int(gols)}\n"
        f"🚩 Escanteios: {int(cantos)}\n\n"
        f"🏦 Banca: {banca}\n"
        f"🔄 Gale: {gale}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}\n\n"
        f"📊 Wins: {stats['wins']}\n"
        f"📊 Losses: {stats['losses']}\n"
        f"🎯 Assertividade: {taxa:.2f}%\n"
        f"💰 Resultado teórico: "
        f"R$ {stats['lucro_teorico']:.2f}"
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "%s | %s x %s | "
        "Gols %.0f | Cantos %.0f",
        resultado,
        sinal["home"],
        sinal["away"],
        gols,
        cantos
    )


# ============================================================
# RESOLVER SINAIS
# ============================================================

def resolver_sinais():

    ativos = [

        s
        for s in sinais
        if s.get("status")
        == "ATIVO"
    ]

    if not ativos:
        return

    agora = timestamp_agora()

    for sinal in ativos:

        inicio = sinal.get(
            "inicio"
        )

        # Não gasta request antes
        # do início da partida.

        if (
            inicio
            and agora < inicio
        ):

            continue

        fixture_id = sinal.get(
            "fixture_id"
        )

        if not fixture_id:
            continue

        logger.info(
            "Verificando resultado fixture %s",
            fixture_id
        )

        resposta = api_get(
            f"/fixtures/{fixture_id}"
        )

        dados = extrair_data(
            resposta
        )

        if not dados:
            continue

        if isinstance(
            dados,
            list
        ):

            if not dados:
                continue

            jogo = dados[0]

        elif isinstance(
            dados,
            dict
        ):

            if isinstance(
                dados.get("fixture"),
                dict
            ) and len(dados) == 1:

                jogo = dados["fixture"]

            else:

                jogo = dados

        else:
            continue

        if not status_final(
            jogo
        ):

            continue

        gols, cantos = (
            pegar_resultado_final(
                jogo
            )
        )

        if (
            gols is None
            or cantos is None
        ):

            logger.warning(
                "Fixture %s terminou, "
                "mas gols/cantos não disponíveis.",
                fixture_id
            )

            continue

        # Over 8.5 corners = 9+
        # Under 4.5 goals = máximo 4

        venceu_cantos = (
            cantos >= 9
        )

        venceu_gols = (
            gols <= 4
        )

        resultado = (
            "WIN"
            if (
                venceu_cantos
                and venceu_gols
            )
            else "LOSS"
        )

        registrar_resultado(
            sinal,
            resultado,
            gols,
            cantos
        )


# ============================================================
# ANALISAR PARTIDAS
# ============================================================

def analisar_partidas():

    livres = bancas_livres()

    if not livres:

        logger.info(
            "3 bancas ocupadas. "
            "Aguardando resultados."
        )

        return

    partidas = buscar_partidas()

    if not partidas:

        logger.info(
            "Nenhuma partida disponível."
        )

        return

    # Primeiro damos preferência
    # a jogos que possuem histórico.

    pre_selecionados = []

    for jogo in partidas:

        fixture_id = pegar_id_jogo(
            jogo
        )

        if not fixture_id:
            continue

        if jogo_ja_usado(
            fixture_id
        ):
            continue

        hist = calcular_historico(
            jogo
        )

        if hist is None:
            continue

        if not historico_aprovado(
            hist
        ):
            continue

        pre_selecionados.append(
            jogo
        )

    logger.info(
        "%s jogos passaram pelo "
        "filtro histórico.",
        len(pre_selecionados)
    )

    # Limita quantidade de chamadas /odds
    pre_selecionados = (
        pre_selecionados[
            :MAX_JOGOS_ODDS_POR_CICLO
        ]
    )

    candidatos = []

    for jogo in pre_selecionados:

        candidato = analisar_jogo(
            jogo
        )

        if candidato:

            candidatos.append(
                candidato
            )

    if not candidatos:

        logger.info(
            "Nenhum candidato aprovado "
            "neste ciclo."
        )

        return

    candidatos.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    # ========================================================
    # PREENCHE TODAS AS BANCAS LIVRES
    # ========================================================

    usados = set()

    for banca in livres:

        candidato_escolhido = None

        for candidato in candidatos:

            fixture_id = (
                candidato[
                    "fixture_id"
                ]
            )

            if fixture_id in usados:
                continue

            if jogo_ja_usado(
                fixture_id
            ):
                continue

            candidato_escolhido = (
                candidato
            )

            break

        if not candidato_escolhido:
            break

        criar_sinal(
            banca,
            candidato_escolhido
        )

        usados.add(
            candidato_escolhido[
                "fixture_id"
            ]
        )


# ============================================================
# CICLO PRINCIPAL
# ============================================================

def executar_ciclo():

    logger.info(
        "=" * 60
    )

    logger.info(
        "INICIANDO CICLO"
    )

    try:

        # Primeiro verifica sinais antigos
        resolver_sinais()

    except Exception:

        logger.exception(
            "Erro ao resolver sinais."
        )

    try:

        # Depois preenche bancas liberadas
        analisar_partidas()

    except Exception:

        logger.exception(
            "Erro na análise."
        )

    logger.info(
        "CICLO FINALIZADO"
    )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "Robô iniciado."
    )

    logger.info(
        "API limitada internamente a "
        "%s requests/min.",
        API_MAX_REQUESTS_PER_MINUTE
    )

    # Pequena espera para o Gunicorn
    # terminar inicialização.

    time.sleep(5)

    while True:

        inicio = time.time()

        try:

            executar_ciclo()

        except Exception:

            logger.exception(
                "Erro geral no ciclo."
            )

        duracao = (
            time.time() - inicio
        )

        espera = max(
            30,
            INTERVALO_ANALISE_SEGUNDOS
            - duracao
        )

        logger.info(
            "Próximo ciclo em %.0fs.",
            espera
        )

        time.sleep(
            espera
        )


# ============================================================
# ENDPOINT PRINCIPAL
# ============================================================

@app.route("/")
def home():

    total = (
        stats.get("wins", 0)
        + stats.get("losses", 0)
    )

    taxa = (
        stats.get("wins", 0)
        / total
        * 100
        if total
        else 0
    )

    ativos = len(
        [
            s
            for s in sinais
            if s.get("status")
            == "ATIVO"
        ]
    )

    return jsonify({

        "status": "online",

        "robo": (
            "Over 8.5 cantos + "
            "Under 4.5 gols"
        ),

        "bancas": TOTAL_BANCAS,

        "sinais_ativos": ativos,

        "wins": stats.get(
            "wins",
            0
        ),

        "losses": stats.get(
            "losses",
            0
        ),

        "assertividade": round(
            taxa,
            2
        ),

        "lucro_teorico": round(
            stats.get(
                "lucro_teorico",
                0
            ),
            2
        ),

        "api_max_requests_min": (
            API_MAX_REQUESTS_PER_MINUTE
        )
    })


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    ativos = [

        s
        for s in sinais
        if s.get("status")
        == "ATIVO"
    ]

    return jsonify({

        "online": True,

        "hora_utc": (
            iso_agora()
        ),

        "sinais_ativos": (
            len(ativos)
        ),

        "bancas_livres": (
            bancas_livres()
        ),

        "rate_limit": {

            "max_por_minuto":
                API_MAX_REQUESTS_PER_MINUTE,

            "intervalo_minimo":
                API_MIN_INTERVAL_SECONDS
        }
    })


# ============================================================
# STATS
# ============================================================

@app.route("/stats")
def rota_stats():

    total = (
        stats.get("wins", 0)
        + stats.get("losses", 0)
    )

    taxa = (
        stats.get("wins", 0)
        / total
        * 100
        if total
        else 0
    )

    resposta = dict(
        stats
    )

    resposta[
        "assertividade"
    ] = round(
        taxa,
        2
    )

    return jsonify(
        resposta
    )


# ============================================================
# BANCAS
# ============================================================

@app.route("/bancas")
def rota_bancas():

    return jsonify(
        stats.get(
            "bancas",
            {}
        )
    )


# ============================================================
# SINAIS
# ============================================================

@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "ok": True,

        "timestamp": (
            timestamp_agora()
        )
    })


# ============================================================
# INICIAR THREAD
# ============================================================

thread_robo = None
thread_lock = threading.Lock()


def iniciar_thread():

    global thread_robo

    with thread_lock:

        if (
            thread_robo is None
            or not thread_robo.is_alive()
        ):

            thread_robo = threading.Thread(
                target=loop_robo,
                daemon=True,
                name="robo-thread"
            )

            thread_robo.start()

            logger.info(
                "Thread do robô iniciada."
            )


iniciar_thread()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
