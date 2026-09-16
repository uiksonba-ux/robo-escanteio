import os
import json
import time
import math
import re
import threading
import logging
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# FLASK / LOG
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-adaptativo")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = (
    os.getenv("FIVE_DOLLAR_API_KEY")
    or os.getenv("FIVE_DOLLAR_KEY")
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))

DEBUG_ODDS = (
    os.getenv("DEBUG_ODDS", "true").lower()
    in ("1", "true", "yes", "sim")
)


# ============================================================
# BANCAS / GALES
# ============================================================

ENTRADA_INICIAL = float(
    os.getenv("ENTRADA_INICIAL", "10")
)

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(
    os.getenv("MAX_GALES", "2")
)

TOTAL_BANCAS = int(
    os.getenv("TOTAL_BANCAS", "3")
)


# ============================================================
# ANÁLISE
# ============================================================

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "10")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "10")
)

MIN_TAXA_COMBINADA = float(
    os.getenv("MIN_TAXA_COMBINADA", "0.60")
)

MIN_TAXA_CANTOS = float(
    os.getenv("MIN_TAXA_CANTOS", "0.65")
)

MIN_TAXA_GOLS = float(
    os.getenv("MIN_TAXA_GOLS", "0.70")
)

MARGEM_CANTOS = float(
    os.getenv("MARGEM_CANTOS", "0.5")
)

MARGEM_GOLS = float(
    os.getenv("MARGEM_GOLS", "0.5")
)


# ============================================================
# ODDS
# ============================================================

ODD_MINIMA = float(
    os.getenv("ODD_MINIMA", "1.80")
)

ODD_MAXIMA = float(
    os.getenv("ODD_MAXIMA", "2.50")
)

ODD_ALVO = float(
    os.getenv("ODD_ALVO", "2.00")
)

BOOKMAKER = os.getenv(
    "BOOKMAKER",
    "bet365"
).lower()


# ============================================================
# API / RATE LIMIT
# ============================================================

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv("MAX_JOGOS_ANALISADOS_CICLO", "4")
)

API_MAX_REQUESTS_PER_MINUTE = int(
    os.getenv("API_MAX_REQUESTS_PER_MINUTE", "9")
)

API_MIN_INTERVAL_SECONDS = float(
    os.getenv("API_MIN_INTERVAL_SECONDS", "6.8")
)

API_BACKOFF_429 = int(
    os.getenv("API_BACKOFF_429", "65")
)


# ============================================================
# CACHE
# ============================================================

CACHE_FIXTURES_TTL = int(
    os.getenv("CACHE_FIXTURES_TTL", "600")
)

CACHE_HISTORICO_TTL = int(
    os.getenv("CACHE_HISTORICO_TTL", "21600")
)

CACHE_ODDS_TTL = int(
    os.getenv("CACHE_ODDS_TTL", "1800")
)


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

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE",
    "cache.json"
)


# ============================================================
# LOCKS
# ============================================================

arquivo_lock = threading.Lock()
dados_lock = threading.RLock()
api_lock = threading.Lock()


# ============================================================
# JSON
# ============================================================

def carregar_json(arquivo, padrao):
    try:
        if not os.path.exists(arquivo):
            return padrao

        with open(arquivo, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        logger.error("Erro lendo %s: %s", arquivo, e)
        return padrao


def salvar_json(arquivo, dados):
    try:
        with arquivo_lock:
            temp = arquivo + ".tmp"

            with open(temp, "w", encoding="utf-8") as f:
                json.dump(
                    dados,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(temp, arquivo)

    except Exception as e:
        logger.error("Erro salvando %s: %s", arquivo, e)


# ============================================================
# ESTADO
# ============================================================

stats = carregar_json(
    ARQUIVO_STATS,
    {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "lucro_teorico": 0.0,
        "bancas": {},
        "por_gale": {}
    }
)

sinais = carregar_json(
    ARQUIVO_SINAIS,
    []
)

cache_persistente = carregar_json(
    ARQUIVO_CACHE,
    {
        "historicos": {},
        "cursor_partidas": 0
    }
)

if not isinstance(sinais, list):
    sinais = []

stats.setdefault("total", 0)
stats.setdefault("wins", 0)
stats.setdefault("losses", 0)
stats.setdefault("lucro_teorico", 0.0)
stats.setdefault("bancas", {})
stats.setdefault("por_gale", {})

cache_persistente.setdefault("historicos", {})
cache_persistente.setdefault("cursor_partidas", 0)

for gale in range(MAX_GALES + 1):
    stats["por_gale"].setdefault(
        str(gale),
        {"wins": 0, "losses": 0}
    )

for i in range(1, TOTAL_BANCAS + 1):
    stats["bancas"].setdefault(
        str(i),
        {
            "gale": 0,
            "ativa": False,
            "wins": 0,
            "losses": 0,
            "lucro": 0.0
        }
    )


# ============================================================
# CACHE MEMÓRIA
# ============================================================

cache_fixtures = {
    "timestamp": 0,
    "data": []
}

cache_odds = {}


# ============================================================
# TEMPO
# ============================================================

def utc_agora():
    return datetime.now(timezone.utc)


def iso_agora():
    return utc_agora().isoformat()


def timestamp_agora():
    return int(utc_agora().timestamp())


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram não configurado.")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        if r.status_code != 200:
            logger.error(
                "Telegram %s | %s",
                r.status_code,
                r.text
            )
            return False

        return True

    except Exception as e:
        logger.error("Erro Telegram: %s", e)
        return False


# ============================================================
# RATE LIMIT
# ============================================================

api_request_times = deque()
ultimo_request_api = 0.0
api_backoff_until = 0.0


def esperar_limite_api():

    global ultimo_request_api

    while True:

        espera = 0.0

        with api_lock:

            agora = time.monotonic()

            if agora < api_backoff_until:
                espera = max(
                    espera,
                    api_backoff_until - agora
                )

            while (
                api_request_times
                and agora - api_request_times[0] >= 60
            ):
                api_request_times.popleft()

            if ultimo_request_api:

                diferenca = agora - ultimo_request_api

                if diferenca < API_MIN_INTERVAL_SECONDS:
                    espera = max(
                        espera,
                        API_MIN_INTERVAL_SECONDS - diferenca
                    )

            if (
                len(api_request_times)
                >= API_MAX_REQUESTS_PER_MINUTE
            ):

                espera_janela = (
                    60
                    - (agora - api_request_times[0])
                    + 0.5
                )

                espera = max(
                    espera,
                    espera_janela
                )

            if espera <= 0:

                agora = time.monotonic()

                api_request_times.append(agora)

                ultimo_request_api = agora

                return

        logger.info(
            "⏳ Rate limit: aguardando %.1fs",
            espera
        )

        time.sleep(max(0.1, espera))


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None, permitir_403=False):

    global api_backoff_until

    if not API_KEY:
        logger.error("FIVE_DOLLAR_API_KEY não configurada.")
        return None

    esperar_limite_api()

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-adaptativo/6.0"
    }

    try:

        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

    except requests.RequestException as e:

        logger.error("Erro API: %s", e)
        return None

    if r.status_code == 429:

        retry = r.headers.get("Retry-After")

        try:
            espera = int(retry)
        except (TypeError, ValueError):
            espera = API_BACKOFF_429

        with api_lock:
            api_backoff_until = max(
                api_backoff_until,
                time.monotonic() + espera
            )

        logger.error(
            "API 429 | aguardando %ss",
            espera
        )

        return None

    if r.status_code == 403 and permitir_403:
        return {"_status": 403}

    if r.status_code != 200:

        logger.error(
            "API %s | %s | endpoint=%s",
            r.status_code,
            r.text[:1500],
            endpoint
        )

        return None

    try:
        return r.json()

    except Exception:
        logger.error("JSON inválido da API.")
        return None


def extrair_data(resposta):

    if resposta is None:
        return None

    if isinstance(resposta, dict):

        if "data" in resposta:
            return resposta["data"]

        if "response" in resposta:
            return resposta["response"]

        if "result" in resposta:
            return resposta["result"]

    return resposta


# ============================================================
# FIXTURES
# ============================================================

def buscar_partidas():

    agora_cache = time.time()

    with dados_lock:

        if (
            cache_fixtures["data"]
            and agora_cache - cache_fixtures["timestamp"]
            < CACHE_FIXTURES_TTL
        ):
            logger.info(
                "📦 %d partidas via cache.",
                len(cache_fixtures["data"])
            )

            return cache_fixtures["data"]

    agora = utc_agora()

    fim = agora + timedelta(hours=JANELA_HORAS)

    params = {
        "start_time": int(agora.timestamp()),
        "end_time": int(fim.timestamp()),
        "status": "scheduled",
        "per_page": 50,
        "include": "odds"
    }

    logger.info(
        "⚽ Buscando partidas das próximas %dh...",
        JANELA_HORAS
    )

    resposta = api_get(
        "/fixtures",
        params=params,
        permitir_403=True
    )

    if (
        isinstance(resposta, dict)
        and resposta.get("_status") == 403
    ):

        params.pop("include", None)

        resposta = api_get(
            "/fixtures",
            params=params
        )

    dados = extrair_data(resposta)

    if not isinstance(dados, list):

        logger.warning(
            "Resposta de fixtures não contém lista."
        )

        return []

    with dados_lock:
        cache_fixtures["timestamp"] = agora_cache
        cache_fixtures["data"] = dados

    logger.info(
        "⚽ %d partidas encontradas.",
        len(dados)
    )

    return dados


# ============================================================
# FIXTURE / TIMES
# ============================================================

def fixture_id(jogo):

    if not isinstance(jogo, dict):
        return None

    valor = (
        jogo.get("id")
        or jogo.get("fixture_id")
        or jogo.get("match_id")
    )

    if valor is None:

        fixture = jogo.get("fixture")

        if isinstance(fixture, dict):
            valor = fixture.get("id")

    if valor is None:
        return None

    return str(valor)


def dados_times(jogo):

    teams = jogo.get("teams", {})

    if not isinstance(teams, dict):
        teams = {}

    home = teams.get("home", {})
    away = teams.get("away", {})

    if not isinstance(home, dict):
        home = {}

    if not isinstance(away, dict):
        away = {}

    home_id = (
        home.get("id")
        or jogo.get("home_team_id")
    )

    away_id = (
        away.get("id")
        or jogo.get("away_team_id")
    )

    home_name = (
        home.get("name")
        or jogo.get("home_name")
        or "Casa"
    )

    away_name = (
        away.get("name")
        or jogo.get("away_name")
        or "Fora"
    )

    return {
        "home_id": (
            str(home_id)
            if home_id is not None
            else None
        ),
        "away_id": (
            str(away_id)
            if away_id is not None
            else None
        ),
        "home": str(home_name),
        "away": str(away_name)
    }


def kickoff_ts(jogo):

    for chave in (
        "kickoff_ts",
        "start_time",
        "timestamp"
    ):

        valor = jogo.get(chave)

        if valor is not None:

            try:
                return int(valor)
            except Exception:
                pass

    for chave in (
        "kickoff_utc",
        "date"
    ):

        valor = jogo.get(chave)

        if valor:

            try:

                dt = datetime.fromisoformat(
                    str(valor).replace(
                        "Z",
                        "+00:00"
                    )
                )

                return int(dt.timestamp())

            except Exception:
                pass

    return None


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico_time(team_id):

    if not team_id:
        return []

    chave = str(team_id)

    agora = time.time()

    with dados_lock:

        item = cache_persistente[
            "historicos"
        ].get(chave)

        if item:

            idade = (
                agora
                - float(item.get("timestamp", 0))
            )

            if idade < CACHE_HISTORICO_TTL:

                dados = item.get("data", [])

                logger.info(
                    "📦 Histórico time %s: %d jogos via cache.",
                    team_id,
                    len(dados)
                )

                return dados

    logger.info(
        "📊 Buscando histórico do time %s...",
        team_id
    )

    resposta = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
            "per_page": QTD_HISTORICO_TIME
        }
    )

    dados = extrair_data(resposta)

    if not isinstance(dados, list):

        logger.warning(
            "Histórico time %s indisponível.",
            team_id
        )

        return []

    dados = dados[:QTD_HISTORICO_TIME]

    with dados_lock:

        cache_persistente[
            "historicos"
        ][chave] = {
            "timestamp": agora,
            "data": dados
        }

        salvar_json(
            ARQUIVO_CACHE,
            cache_persistente
        )

    logger.info(
        "📊 Time %s: %d jogos.",
        team_id,
        len(dados)
    )

    return dados


# ============================================================
# NÚMEROS
# ============================================================

def numero(valor):

    if valor is None:
        return None

    if isinstance(valor, bool):
        return None

    if isinstance(valor, (int, float)):

        try:
            v = float(valor)

            if math.isfinite(v):
                return v

        except Exception:
            return None

    texto = str(valor).strip()

    texto = texto.replace(",", ".")

    try:
        v = float(texto)

        if math.isfinite(v):
            return v

    except Exception:
        pass

    return None


# ============================================================
# RESULTADOS HISTÓRICOS
# ============================================================

def extrair_totais(jogo):

    if not isinstance(jogo, dict):
        return None, None

    goals = jogo.get("goals")
    corners = jogo.get("corners")

    gols = None
    cantos = None

    if isinstance(goals, dict):

        gh = numero(goals.get("home"))
        ga = numero(goals.get("away"))

        if gh is not None and ga is not None:
            gols = gh + ga

    if isinstance(corners, dict):

        ch = numero(corners.get("home"))
        ca = numero(corners.get("away"))

        if ch is not None and ca is not None:
            cantos = ch + ca

    if gols is None:
        gols = numero(jogo.get("total_goals"))

    if cantos is None:
        cantos = numero(jogo.get("total_corners"))

    return gols, cantos


def combinar_historicos(home_history, away_history):

    unicos = {}
    sem_id = 0

    for jogo in home_history + away_history:

        if not isinstance(jogo, dict):
            continue

        gols, cantos = extrair_totais(jogo)

        if gols is None or cantos is None:
            continue

        fid = (
            jogo.get("id")
            or jogo.get("fixture_id")
        )

        if fid is None:

            sem_id += 1

            fid = (
                f"sem-id-{sem_id}-"
                f"{gols}-{cantos}"
            )

        unicos[str(fid)] = {
            "gols": float(gols),
            "cantos": float(cantos)
        }

    return list(unicos.values())


def calcular_medias(partidas):

    if len(partidas) < MIN_JOGOS_HISTORICO:
        return None

    media_cantos = (
        sum(x["cantos"] for x in partidas)
        / len(partidas)
    )

    media_gols = (
        sum(x["gols"] for x in partidas)
        / len(partidas)
    )

    return {
        "jogos": len(partidas),
        "media_cantos": round(media_cantos, 2),
        "media_gols": round(media_gols, 2)
    }


# ============================================================
# LINHAS-ALVO
# ============================================================

def linha_meio_abaixo(media):

    alvo = media - MARGEM_CANTOS

    linha = (
        math.floor(alvo + 0.5)
        - 0.5
    )

    return max(0.5, linha)


def linha_meio_acima(media):

    alvo = media + MARGEM_GOLS

    linha = (
        math.ceil(alvo - 0.5)
        + 0.5
    )

    return max(0.5, linha)


# ============================================================
# AVALIAR HISTÓRICO
# ============================================================

def avaliar_linhas(
    partidas,
    linha_cantos,
    linha_gols
):

    total = len(partidas)

    if total == 0:
        return None

    cantos_ok = 0
    gols_ok = 0
    combinado_ok = 0

    for p in partidas:

        ok_cantos = (
            p["cantos"] > linha_cantos
        )

        ok_gols = (
            p["gols"] < linha_gols
        )

        if ok_cantos:
            cantos_ok += 1

        if ok_gols:
            gols_ok += 1

        if ok_cantos and ok_gols:
            combinado_ok += 1

    return {
        "total": total,
        "acertos_cantos": cantos_ok,
        "acertos_gols": gols_ok,
        "acertos_combinados": combinado_ok,
        "taxa_cantos": cantos_ok / total,
        "taxa_gols": gols_ok / total,
        "taxa_combinada": combinado_ok / total
    }


# ============================================================
# NORMALIZAÇÃO DE TEXTO
# ============================================================

def normalizar_texto(valor):

    if valor is None:
        return ""

    return (
        str(valor)
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .strip()
    )


def texto_objeto(caminho, obj):

    partes = [
        normalizar_texto(caminho)
    ]

    if isinstance(obj, dict):

        for chave, valor in obj.items():

            partes.append(
                normalizar_texto(chave)
            )

            if isinstance(
                valor,
                (str, int, float)
            ):
                partes.append(
                    normalizar_texto(valor)
                )

    return " ".join(partes)


# ============================================================
# WALK JSON
# ============================================================

def percorrer_json(obj, caminho=""):

    if isinstance(obj, dict):

        yield caminho, obj

        for chave, valor in obj.items():

            novo = (
                f"{caminho}.{chave}"
                if caminho
                else str(chave)
            )

            yield from percorrer_json(
                valor,
                novo
            )

    elif isinstance(obj, list):

        for i, valor in enumerate(obj):

            novo = f"{caminho}[{i}]"

            yield from percorrer_json(
                valor,
                novo
            )


# ============================================================
# MERCADO: IDENTIFICAÇÃO
# ============================================================

TERMOS_CANTOS = (
    "corner",
    "corners",
    "corner line",
    "corner asian",
    "total corners",
    "total corner",
    "escanteio",
    "escanteios"
)

TERMOS_GOLS = (
    "goal line",
    "total goals",
    "total goal",
    "goals",
    "goal",
    "match goals",
    "goalline"
)


def contexto_cantos(texto):

    texto = normalizar_texto(texto)

    return any(
        termo in texto
        for termo in TERMOS_CANTOS
    )


def contexto_gols(texto):

    texto = normalizar_texto(texto)

    if contexto_cantos(texto):
        return False

    return any(
        termo in texto
        for termo in TERMOS_GOLS
    )


# ============================================================
# BOOKMAKER
# ============================================================

def contem_bookmaker(texto):

    if not BOOKMAKER:
        return True

    texto = normalizar_texto(texto)

    return (
        BOOKMAKER in texto
        or BOOKMAKER.replace(" ", "") in texto.replace(" ", "")
    )


# ============================================================
# EXTRAIR LINHA DE TEXTO
# ============================================================

def extrair_linha_texto(texto, lado=None):

    texto = normalizar_texto(texto)

    padroes = []

    if lado == "over":
        padroes.extend([
            r"\bover\s+(\d+(?:\.\d+)?)",
            r"\bo\s+(\d+(?:\.\d+)?)"
        ])

    elif lado == "under":
        padroes.extend([
            r"\bunder\s+(\d+(?:\.\d+)?)",
            r"\bu\s+(\d+(?:\.\d+)?)"
        ])

    else:
        padroes.extend([
            r"\bover\s+(\d+(?:\.\d+)?)",
            r"\bunder\s+(\d+(?:\.\d+)?)"
        ])

    for padrao in padroes:

        m = re.search(
            padrao,
            texto
        )

        if m:

            try:
                return float(m.group(1))
            except Exception:
                pass

    return None


# ============================================================
# PEGAR CAMPO
# ============================================================

def primeiro_numero(obj, chaves):

    if not isinstance(obj, dict):
        return None

    for chave in chaves:

        if chave in obj:

            valor = numero(
                obj.get(chave)
            )

            if valor is not None:
                return valor

    return None


def primeiro_texto(obj, chaves):

    if not isinstance(obj, dict):
        return ""

    for chave in chaves:

        valor = obj.get(chave)

        if valor is not None:
            return str(valor)

    return ""


# ============================================================
# ADICIONAR CANDIDATO DE ODD
# ============================================================

def adicionar_linha(
    destino,
    linha,
    odd,
    bookmaker_score=0,
    origem=""
):

    linha = numero(linha)
    odd = numero(odd)

    if linha is None or odd is None:
        return

    if linha <= 0:
        return

    if odd <= 1.0 or odd > 1000:
        return

    # Futebol: linhas que nos interessam
    # normalmente ficam neste intervalo.
    if linha > 100:
        return

    chave = round(float(linha), 2)

    atual = destino.get(chave)

    candidato = {
        "linha": chave,
        "odd": round(float(odd), 4),
        "bookmaker_score": bookmaker_score,
        "origem": origem
    }

    if atual is None:
        destino[chave] = candidato
        return

    # Primeiro prefere o bookmaker desejado.
    if (
        candidato["bookmaker_score"]
        > atual["bookmaker_score"]
    ):
        destino[chave] = candidato
        return

    # Mesmo nível: fica com a maior odd.
    if (
        candidato["bookmaker_score"]
        == atual["bookmaker_score"]
        and candidato["odd"] > atual["odd"]
    ):
        destino[chave] = candidato


# ============================================================
# PARSER DIRETO:
# {"line": 8.5, "over": 1.8, "under": 2.0}
# ============================================================

def analisar_objeto_direto(
    caminho,
    obj,
    cantos,
    gols
):

    texto = texto_objeto(
        caminho,
        obj
    )

    linha = primeiro_numero(
        obj,
        (
            "line",
            "handicap",
            "total",
            "points",
            "point",
            "value"
        )
    )

    over = primeiro_numero(
        obj,
        (
            "over",
            "over_odds",
            "overOdds",
            "price_over",
            "over_price",
            "odds_over"
        )
    )

    under = primeiro_numero(
        obj,
        (
            "under",
            "under_odds",
            "underOdds",
            "price_under",
            "under_price",
            "odds_under"
        )
    )

    book_score = (
        1 if contem_bookmaker(texto)
        else 0
    )

    if linha is not None:

        if contexto_cantos(texto):

            if over is not None:

                adicionar_linha(
                    cantos,
                    linha,
                    over,
                    book_score,
                    caminho
                )

        elif contexto_gols(texto):

            if under is not None:

                adicionar_linha(
                    gols,
                    linha,
                    under,
                    book_score,
                    caminho
                )


# ============================================================
# PARSER DE SELEÇÕES:
# {"name":"Over 8.5","price":1.80}
# ============================================================

def analisar_selecao(
    caminho,
    obj,
    cantos,
    gols
):

    texto_contexto = texto_objeto(
        caminho,
        obj
    )

    nome = primeiro_texto(
        obj,
        (
            "name",
            "label",
            "selection",
            "selection_name",
            "outcome",
            "outcome_name",
            "runner",
            "runner_name",
            "title",
            "description"
        )
    )

    nome_norm = normalizar_texto(
        nome
    )

    if not nome_norm:
        return

    odd = primeiro_numero(
        obj,
        (
            "price",
            "odd",
            "odds",
            "decimal",
            "decimal_odds",
            "value"
        )
    )

    if odd is None:
        return

    lado = None

    if "over" in nome_norm:
        lado = "over"

    elif "under" in nome_norm:
        lado = "under"

    if lado is None:
        return

    linha = primeiro_numero(
        obj,
        (
            "line",
            "handicap",
            "total",
            "points",
            "point"
        )
    )

    if linha is None:

        linha = extrair_linha_texto(
            nome_norm,
            lado
        )

    if linha is None:
        return

    book_score = (
        1
        if contem_bookmaker(
            texto_contexto
        )
        else 0
    )

    if (
        lado == "over"
        and contexto_cantos(
            texto_contexto
        )
    ):

        adicionar_linha(
            cantos,
            linha,
            odd,
            book_score,
            caminho
        )

    elif (
        lado == "under"
        and contexto_gols(
            texto_contexto
        )
    ):

        adicionar_linha(
            gols,
            linha,
            odd,
            book_score,
            caminho
        )


# ============================================================
# PARSER DE MAPAS:
#
# "8.5": {"over":1.8,"under":2.0}
# ============================================================

def analisar_mapas(
    caminho,
    obj,
    cantos,
    gols
):

    if not isinstance(obj, dict):
        return

    texto_pai = normalizar_texto(
        caminho
    )

    for chave, valor in obj.items():

        linha_chave = numero(chave)

        if linha_chave is None:
            continue

        if not isinstance(valor, dict):
            continue

        texto = (
            texto_pai
            + " "
            + texto_objeto(
                str(chave),
                valor
            )
        )

        over = primeiro_numero(
            valor,
            (
                "over",
                "over_odds",
                "price_over",
                "odds_over"
            )
        )

        under = primeiro_numero(
            valor,
            (
                "under",
                "under_odds",
                "price_under",
                "odds_under"
            )
        )

        book_score = (
            1 if contem_bookmaker(texto)
            else 0
        )

        if (
            contexto_cantos(texto)
            and over is not None
        ):

            adicionar_linha(
                cantos,
                linha_chave,
                over,
                book_score,
                caminho
            )

        elif (
            contexto_gols(texto)
            and under is not None
        ):

            adicionar_linha(
                gols,
                linha_chave,
                under,
                book_score,
                caminho
            )


# ============================================================
# EXTRAIR LINHAS DE ODDS
# ============================================================

def extrair_linhas_odds(dados):

    cantos = {}
    gols = {}

    for caminho, obj in percorrer_json(dados):

        if not isinstance(obj, dict):
            continue

        analisar_objeto_direto(
            caminho,
            obj,
            cantos,
            gols
        )

        analisar_selecao(
            caminho,
            obj,
            cantos,
            gols
        )

        analisar_mapas(
            caminho,
            obj,
            cantos,
            gols
        )

    lista_cantos = list(
        cantos.values()
    )

    lista_gols = list(
        gols.values()
    )

    lista_cantos.sort(
        key=lambda x: x["linha"]
    )

    lista_gols.sort(
        key=lambda x: x["linha"]
    )

    return lista_cantos, lista_gols


# ============================================================
# DEBUG DA ESTRUTURA DAS ODDS
# ============================================================

def diagnosticar_odds(fid, dados):

    if not DEBUG_ODDS:
        return

    try:

        raw = json.dumps(
            dados,
            ensure_ascii=False,
            separators=(",", ":")
        )

        # Evita explodir o log do Render.
        logger.info(
            "🔎 ODDS RAW fixture %s = %s",
            fid,
            raw[:12000]
        )

    except Exception as e:

        logger.error(
            "Erro no debug de odds: %s",
            e
        )


# ============================================================
# BUSCAR ODDS
# ============================================================

def buscar_odds(jogo):

    fid = fixture_id(jogo)

    if not fid:
        return None

    odds_embutidas = jogo.get("odds")

    if odds_embutidas:

        diagnosticar_odds(
            fid,
            odds_embutidas
        )

        return odds_embutidas

    agora = time.time()

    with dados_lock:

        item = cache_odds.get(fid)

        if item:

            if (
                agora - item["timestamp"]
                < CACHE_ODDS_TTL
            ):
                return item["data"]

    logger.info(
        "💰 Buscando odds fixture %s...",
        fid
    )

    resposta = api_get(
        f"/fixtures/{fid}/odds",
        params={
            "bookmakers": BOOKMAKER
        }
    )

    dados = extrair_data(resposta)

    if dados:

        diagnosticar_odds(
            fid,
            dados
        )

        with dados_lock:

            cache_odds[fid] = {
                "timestamp": agora,
                "data": dados
            }

    else:

        logger.warning(
            "⚠️ Endpoint de odds sem dados "
            "| fixture %s",
            fid
        )

    return dados


# ============================================================
# SELEÇÃO DO MERCADO
# ============================================================

def selecionar_mercado(
    partidas,
    media_cantos,
    media_gols,
    linhas_cantos,
    linhas_gols
):

    if not linhas_cantos or not linhas_gols:
        return None

    alvo_cantos = linha_meio_abaixo(
        media_cantos
    )

    alvo_gols = linha_meio_acima(
        media_gols
    )

    logger.info(
        "🎯 Média cantos %.2f → alvo Over %.1f | "
        "média gols %.2f → alvo Under %.1f",
        media_cantos,
        alvo_cantos,
        media_gols,
        alvo_gols
    )

    candidatos = []

    for mc in linhas_cantos:

        linha_cantos = mc["linha"]
        odd_cantos = mc["odd"]

        # Over precisa ficar abaixo da média.
        if linha_cantos >= media_cantos:
            continue

        for mg in linhas_gols:

            linha_gols = mg["linha"]
            odd_gols = mg["odd"]

            # Under precisa ficar acima da média.
            if linha_gols <= media_gols:
                continue

            hist = avaliar_linhas(
                partidas,
                linha_cantos,
                linha_gols
            )

            if not hist:
                continue

            if hist["taxa_cantos"] < MIN_TAXA_CANTOS:
                continue

            if hist["taxa_gols"] < MIN_TAXA_GOLS:
                continue

            if (
                hist["taxa_combinada"]
                < MIN_TAXA_COMBINADA
            ):
                continue

            odd_combinada = (
                odd_cantos * odd_gols
            )

            if (
                odd_combinada < ODD_MINIMA
                or odd_combinada > ODD_MAXIMA
            ):
                continue

            distancia_linhas = (
                abs(linha_cantos - alvo_cantos)
                + abs(linha_gols - alvo_gols)
            )

            distancia_odd = abs(
                odd_combinada - ODD_ALVO
            )

            score = (
                hist["taxa_combinada"] * 100
                + hist["taxa_cantos"] * 10
                + hist["taxa_gols"] * 10
                - distancia_linhas * 2
                - distancia_odd * 5
            )

            candidatos.append({
                "linha_cantos": linha_cantos,
                "odd_cantos": round(odd_cantos, 3),

                "linha_gols": linha_gols,
                "odd_gols": round(odd_gols, 3),

                "odd_combinada": round(
                    odd_combinada,
                    3
                ),

                "taxa_cantos": hist["taxa_cantos"],
                "taxa_gols": hist["taxa_gols"],
                "taxa_combinada": hist[
                    "taxa_combinada"
                ],

                "acertos_combinados": hist[
                    "acertos_combinados"
                ],

                "total": hist["total"],

                "origem_cantos": mc.get(
                    "origem",
                    ""
                ),

                "origem_gols": mg.get(
                    "origem",
                    ""
                ),

                "score": score
            })

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return candidatos[0]


# ============================================================
# JOGO JÁ ATIVO
# ============================================================

def jogo_ja_usado(fid):

    fid = str(fid)

    with dados_lock:

        return any(
            str(
                sinal.get("fixture_id")
            ) == fid
            and sinal.get("status") == "ATIVO"
            for sinal in sinais
        )


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():

    with dados_lock:

        return [
            banca
            for banca, dados
            in stats["bancas"].items()
            if not dados.get("ativa", False)
        ]


def valor_entrada(banca):

    gale = int(
        stats["bancas"][banca].get(
            "gale",
            0
        )
    )

    return round(
        ENTRADA_INICIAL
        * (MULTIPLICADOR_GALE ** gale),
        2
    )


# ============================================================
# LOTE ROTATIVO
# ============================================================

def selecionar_lote(partidas):

    elegiveis = []

    agora = timestamp_agora()

    for jogo in partidas:

        fid = fixture_id(jogo)

        if not fid:
            continue

        if jogo_ja_usado(fid):
            continue

        times = dados_times(jogo)

        if (
            not times["home_id"]
            or not times["away_id"]
        ):
            continue

        inicio = kickoff_ts(jogo)

        if not inicio or inicio <= agora:
            continue

        elegiveis.append(jogo)

    if not elegiveis:
        return []

    elegiveis.sort(
        key=lambda x:
            kickoff_ts(x) or 9999999999
    )

    cursor = int(
        cache_persistente.get(
            "cursor_partidas",
            0
        )
    )

    if cursor >= len(elegiveis):
        cursor = 0

    lote = []

    indice = cursor

    while (
        len(lote) < MAX_JOGOS_ANALISADOS_CICLO
        and len(lote) < len(elegiveis)
    ):

        lote.append(
            elegiveis[
                indice % len(elegiveis)
            ]
        )

        indice += 1

    cache_persistente[
        "cursor_partidas"
    ] = indice % len(elegiveis)

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    logger.info(
        "🔎 Lote %d/%d | cursor %d",
        len(lote),
        len(elegiveis),
        cursor
    )

    return lote


# ============================================================
# ANALISAR JOGO
# ============================================================

def analisar_jogo(jogo):

    fid = fixture_id(jogo)
    times = dados_times(jogo)

    logger.info(
        "🔍 %s x %s | fixture %s",
        times["home"],
        times["away"],
        fid
    )

    # --------------------------------------------------------
    # HISTÓRICO
    # --------------------------------------------------------

    hist_home = buscar_historico_time(
        times["home_id"]
    )

    hist_away = buscar_historico_time(
        times["away_id"]
    )

    partidas = combinar_historicos(
        hist_home,
        hist_away
    )

    medias = calcular_medias(
        partidas
    )

    if not medias:

        logger.info(
            "❌ Histórico insuficiente | %s x %s",
            times["home"],
            times["away"]
        )

        return None

    logger.info(
        "📊 %s x %s | %d jogos | "
        "média cantos %.2f | média gols %.2f",
        times["home"],
        times["away"],
        medias["jogos"],
        medias["media_cantos"],
        medias["media_gols"]
    )

    # --------------------------------------------------------
    # ODDS
    # --------------------------------------------------------

    dados_odds = buscar_odds(
        jogo
    )

    if not dados_odds:

        logger.info(
            "❌ Sem odds | %s x %s",
            times["home"],
            times["away"]
        )

        return None

    linhas_cantos, linhas_gols = (
        extrair_linhas_odds(
            dados_odds
        )
    )

    logger.info(
        "💰 %s x %s | "
        "linhas cantos=%s | linhas gols=%s",
        times["home"],
        times["away"],
        [
            f"{x['linha']}@{x['odd']}"
            for x in linhas_cantos
        ],
        [
            f"{x['linha']}@{x['odd']}"
            for x in linhas_gols
        ]
    )

    if not linhas_cantos:

        logger.warning(
            "❌ Parser não encontrou "
            "mercado de escanteios | %s x %s",
            times["home"],
            times["away"]
        )

        return None

    if not linhas_gols:

        logger.warning(
            "❌ Parser não encontrou "
            "mercado de gols | %s x %s",
            times["home"],
            times["away"]
        )

        return None

    # --------------------------------------------------------
    # MERCADO ADAPTATIVO
    # --------------------------------------------------------

    mercado = selecionar_mercado(
        partidas,
        medias["media_cantos"],
        medias["media_gols"],
        linhas_cantos,
        linhas_gols
    )

    if not mercado:

        logger.info(
            "❌ Linhas encontradas, mas nenhuma "
            "combinação passou nos filtros | %s x %s",
            times["home"],
            times["away"]
        )

        return None

    logger.info(
        "✅ CANDIDATO | %s x %s | "
        "Over %.1f cantos + Under %.1f gols | "
        "histórico conjunto %.1f%% | odd %.2f",
        times["home"],
        times["away"],
        mercado["linha_cantos"],
        mercado["linha_gols"],
        mercado["taxa_combinada"] * 100,
        mercado["odd_combinada"]
    )

    return {
        "fixture_id": fid,
        "home": times["home"],
        "away": times["away"],
        "inicio": kickoff_ts(jogo),

        "media_cantos": medias["media_cantos"],
        "media_gols": medias["media_gols"],
        "jogos_historico": medias["jogos"],

        "mercado": mercado,
        "score": mercado["score"]
    }


# ============================================================
# HORÁRIO BR
# ============================================================

def horario_br(ts):

    if not ts:
        return "Não informado"

    try:

        dt = (
            datetime.fromtimestamp(
                int(ts),
                timezone.utc
            )
            - timedelta(hours=3)
        )

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:
        return str(ts)


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(banca, candidato):

    mercado = candidato["mercado"]

    with dados_lock:

        gale = int(
            stats["bancas"][banca].get(
                "gale",
                0
            )
        )

        entrada = valor_entrada(
            banca
        )

        sinal = {
            "id": (
                f"{candidato['fixture_id']}-"
                f"{banca}-"
                f"{int(time.time())}"
            ),

            "fixture_id": candidato["fixture_id"],
            "home": candidato["home"],
            "away": candidato["away"],
            "inicio": candidato["inicio"],

            "banca": banca,
            "gale": gale,
            "entrada": entrada,

            "linha_cantos": mercado["linha_cantos"],
            "linha_gols": mercado["linha_gols"],

            "odd_cantos": mercado["odd_cantos"],
            "odd_gols": mercado["odd_gols"],
            "odd_combinada": mercado["odd_combinada"],

            "media_cantos": candidato["media_cantos"],
            "media_gols": candidato["media_gols"],

            "taxa_cantos": mercado["taxa_cantos"],
            "taxa_gols": mercado["taxa_gols"],
            "taxa_combinada": mercado["taxa_combinada"],

            "jogos_historico": candidato["jogos_historico"],

            "status": "ATIVO",
            "resultado": None,

            "criado_em": iso_agora(),
            "resolvido_em": None
        }

        sinais.append(sinal)

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

    texto = (
        "🚨 <b>NOVO SINAL ADAPTATIVO</b>\n\n"

        f"⚽ <b>{candidato['home']} x "
        f"{candidato['away']}</b>\n"

        f"🕒 {horario_br(candidato['inicio'])}\n\n"

        "📊 <b>HISTÓRICO</b>\n"

        f"📚 Jogos válidos: "
        f"{candidato['jogos_historico']}\n"

        f"🚩 Média de escanteios: "
        f"{candidato['media_cantos']:.2f}\n"

        f"🥅 Média de gols: "
        f"{candidato['media_gols']:.2f}\n\n"

        "🎯 <b>ENTRADA</b>\n"

        f"🚩 Over "
        f"{mercado['linha_cantos']:.1f} "
        f"escanteios\n"

        f"🥅 Under "
        f"{mercado['linha_gols']:.1f} "
        f"gols\n\n"

        "📈 <b>FREQUÊNCIA NA AMOSTRA</b>\n"

        f"🚩 Over: "
        f"{mercado['taxa_cantos'] * 100:.1f}%\n"

        f"🥅 Under: "
        f"{mercado['taxa_gols'] * 100:.1f}%\n"

        f"🎯 Ambos: "
        f"{mercado['taxa_combinada'] * 100:.1f}%\n\n"

        "💰 <b>ODDS</b>\n"

        f"🚩 {mercado['odd_cantos']:.2f}\n"
        f"🥅 {mercado['odd_gols']:.2f}\n"

        f"🔥 Combinada teórica: "
        f"{mercado['odd_combinada']:.2f}\n\n"

        f"🏦 Banca {banca}\n"
        f"🔄 Gale {gale}/{MAX_GALES}\n"
        f"💵 Entrada R$ {entrada:.2f}"
    )

    enviar_telegram(texto)

    logger.info(
        "🚨 SINAL ENVIADO | Banca %s | %s x %s",
        banca,
        candidato["home"],
        candidato["away"]
    )


# ============================================================
# RESULTADO
# ============================================================

def buscar_fixture(fid):

    resposta = api_get(
        f"/fixtures/{fid}"
    )

    dados = extrair_data(
        resposta
    )

    if isinstance(dados, list):

        if dados:
            return dados[0]

        return None

    if isinstance(dados, dict):
        return dados

    return None


def registrar_resultado(
    sinal,
    resultado,
    gols,
    cantos
):

    with dados_lock:

        banca = str(sinal["banca"])
        gale = int(sinal["gale"])
        entrada = float(sinal["entrada"])
        odd = float(sinal["odd_combinada"])

        sinal["status"] = "FINALIZADO"
        sinal["resultado"] = resultado
        sinal["gols_final"] = gols
        sinal["cantos_final"] = cantos
        sinal["resolvido_em"] = iso_agora()

        stats["total"] += 1

        stats["por_gale"].setdefault(
            str(gale),
            {"wins": 0, "losses": 0}
        )

        if resultado == "WIN":

            lucro = entrada * (odd - 1)

            stats["wins"] += 1

            stats["por_gale"][
                str(gale)
            ]["wins"] += 1

            stats["bancas"][
                banca
            ]["wins"] += 1

            stats["bancas"][
                banca
            ]["lucro"] += lucro

            # WIN -> volta para entrada inicial.
            stats["bancas"][
                banca
            ]["gale"] = 0

        else:

            lucro = -entrada

            stats["losses"] += 1

            stats["por_gale"][
                str(gale)
            ]["losses"] += 1

            stats["bancas"][
                banca
            ]["losses"] += 1

            stats["bancas"][
                banca
            ]["lucro"] += lucro

            if gale < MAX_GALES:

                stats["bancas"][
                    banca
                ]["gale"] = gale + 1

            else:

                stats["bancas"][
                    banca
                ]["gale"] = 0

        stats[
            "lucro_teorico"
        ] += lucro

        stats[
            "lucro_teorico"
        ] = round(
            stats["lucro_teorico"],
            2
        )

        stats["bancas"][
            banca
        ]["lucro"] = round(
            stats["bancas"][banca]["lucro"],
            2
        )

        stats["bancas"][
            banca
        ]["ativa"] = False

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

        total = (
            stats["wins"]
            + stats["losses"]
        )

        taxa = (
            stats["wins"] / total * 100
            if total
            else 0
        )

    emoji = (
        "✅"
        if resultado == "WIN"
        else "❌"
    )

    texto = (
        f"{emoji} <b>{resultado}</b>\n\n"

        f"⚽ {sinal['home']} x "
        f"{sinal['away']}\n\n"

        f"🚩 Over "
        f"{sinal['linha_cantos']:.1f} cantos\n"

        f"🥅 Under "
        f"{sinal['linha_gols']:.1f} gols\n\n"

        f"🚩 Cantos finais: {int(cantos)}\n"
        f"🥅 Gols finais: {int(gols)}\n\n"

        f"🏦 Banca {banca}\n"
        f"🔄 Gale {gale}/{MAX_GALES}\n"
        f"💵 R$ {entrada:.2f}\n\n"

        f"✅ Wins: {stats['wins']}\n"
        f"❌ Losses: {stats['losses']}\n"

        f"🎯 Assertividade: "
        f"{taxa:.2f}%\n"

        f"💰 P/L teórico: "
        f"R$ {stats['lucro_teorico']:.2f}"
    )

    enviar_telegram(texto)


# ============================================================
# RESOLVER SINAIS
# ============================================================

def resolver_sinais():

    with dados_lock:

        ativos = [
            s
            for s in sinais
            if s.get("status") == "ATIVO"
        ]

    if not ativos:
        return

    agora = timestamp_agora()

    for sinal in ativos:

        inicio = sinal.get("inicio")

        if (
            inicio
            and agora < int(inicio) + 90 * 60
        ):
            continue

        jogo = buscar_fixture(
            sinal["fixture_id"]
        )

        if not jogo:
            continue

        status = jogo.get("status")

        if isinstance(status, dict):

            status_texto = " ".join(
                str(v).lower()
                for v in status.values()
                if v is not None
            )

        else:

            status_texto = str(
                status or ""
            ).lower()

        finais = (
            "finished",
            "final",
            "ft",
            "completed",
            "ended"
        )

        if not any(
            termo in status_texto
            for termo in finais
        ):
            continue

        gols, cantos = extrair_totais(
            jogo
        )

        if gols is None or cantos is None:

            logger.warning(
                "Resultado incompleto fixture %s",
                sinal["fixture_id"]
            )

            continue

        ganhou_cantos = (
            cantos
            > float(sinal["linha_cantos"])
        )

        ganhou_gols = (
            gols
            < float(sinal["linha_gols"])
        )

        resultado = (
            "WIN"
            if ganhou_cantos and ganhou_gols
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
            "🏦 Todas as bancas ocupadas."
        )

        return

    partidas = buscar_partidas()

    if not partidas:

        logger.info(
            "Nenhuma partida futura."
        )

        return

    lote = selecionar_lote(
        partidas
    )

    if not lote:

        logger.info(
            "Nenhum jogo elegível."
        )

        return

    candidatos = []

    for jogo in lote:

        try:

            candidato = analisar_jogo(
                jogo
            )

            if candidato:
                candidatos.append(candidato)

        except Exception:

            logger.exception(
                "Erro analisando fixture %s",
                fixture_id(jogo)
            )

    if not candidatos:

        logger.info(
            "Nenhum candidato aprovado neste ciclo."
        )

        return

    candidatos.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    logger.info(
        "🏆 %d candidatos aprovados.",
        len(candidatos)
    )

    usados = set()

    for banca in livres:

        escolhido = None

        for candidato in candidatos:

            fid = candidato["fixture_id"]

            if fid in usados:
                continue

            if jogo_ja_usado(fid):
                continue

            escolhido = candidato
            break

        if not escolhido:
            break

        criar_sinal(
            banca,
            escolhido
        )

        usados.add(
            escolhido["fixture_id"]
        )


# ============================================================
# CICLO
# ============================================================

def executar_ciclo():

    logger.info("=" * 70)

    logger.info(
        "🤖 INICIANDO CICLO ADAPTATIVO 6.0"
    )

    try:
        resolver_sinais()
    except Exception:
        logger.exception(
            "Erro resolvendo sinais."
        )

    try:
        analisar_partidas()
    except Exception:
        logger.exception(
            "Erro analisando partidas."
        )

    logger.info(
        "🏁 CICLO FINALIZADO"
    )


def loop_robo():

    logger.info(
        "🤖 Robô adaptativo 6.0 iniciado."
    )

    time.sleep(5)

    while True:

        inicio = time.time()

        executar_ciclo()

        duracao = (
            time.time() - inicio
        )

        espera = max(
            30,
            INTERVALO_ANALISE_SEGUNDOS
            - duracao
        )

        logger.info(
            "⏰ Próximo ciclo em %.0fs.",
            espera
        )

        time.sleep(espera)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    taxa = (
        stats["wins"] / total * 100
        if total
        else 0
    )

    ativos = [
        s
        for s in sinais
        if s.get("status") == "ATIVO"
    ]

    return jsonify({
        "online": True,
        "versao": "adaptativo-6.0",

        "estrategia":
            "Over cantos + Under gols adaptativos",

        "sinais_ativos":
            len(ativos),

        "bancas_livres":
            bancas_livres(),

        "wins":
            stats["wins"],

        "losses":
            stats["losses"],

        "assertividade":
            round(taxa, 2),

        "lucro_teorico":
            stats["lucro_teorico"],

        "debug_odds":
            DEBUG_ODDS,

        "filtros": {
            "cantos":
                MIN_TAXA_CANTOS,

            "gols":
                MIN_TAXA_GOLS,

            "combinada":
                MIN_TAXA_COMBINADA,

            "odd_min":
                ODD_MINIMA,

            "odd_max":
                ODD_MAXIMA
        }
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,
        "versao": "adaptativo-6.0",
        "hora_utc": iso_agora(),

        "bancas_livres":
            bancas_livres(),

        "cursor":
            cache_persistente.get(
                "cursor_partidas",
                0
            ),

        "times_cache":
            len(
                cache_persistente.get(
                    "historicos",
                    {}
                )
            ),

        "odds_cache":
            len(cache_odds),

        "debug_odds":
            DEBUG_ODDS
    })


@app.route("/stats")
def rota_stats():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    resposta = dict(stats)

    resposta["assertividade"] = (
        round(
            stats["wins"]
            / total
            * 100,
            2
        )
        if total
        else 0
    )

    return jsonify(resposta)


@app.route("/bancas")
def rota_bancas():

    return jsonify(
        stats["bancas"]
    )


@app.route("/sinais")
def rota_sinais():

    return jsonify(sinais)


@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "versao": "adaptativo-6.0",
        "timestamp": timestamp_agora()
    })


# ============================================================
# THREAD
# ============================================================

thread_robo = None
thread_start_lock = threading.Lock()


def iniciar_thread():

    global thread_robo

    with thread_start_lock:

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
# LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
