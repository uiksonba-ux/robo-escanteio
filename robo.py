import os
import json
import time
import math
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

logger = logging.getLogger("robo-adaptativo-v7")


# ============================================================
# CONFIGURAÇÃO
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


# ============================================================
# DEBUG
# ============================================================

DEBUG_ODDS = (
    os.getenv("DEBUG_ODDS", "true")
    .strip()
    .lower()
    in ("1", "true", "yes", "sim", "on")
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

MIN_TAXA_CANTOS = float(
    os.getenv("MIN_TAXA_CANTOS", "0.65")
)

MIN_TAXA_GOLS = float(
    os.getenv("MIN_TAXA_GOLS", "0.70")
)

MIN_TAXA_COMBINADA = float(
    os.getenv("MIN_TAXA_COMBINADA", "0.60")
)


# ============================================================
# LINHAS ADAPTATIVAS
# ============================================================

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
).strip().lower()


# ============================================================
# RATE LIMIT
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

        with open(
            arquivo,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

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

            temporario = arquivo + ".tmp"

            with open(
                temporario,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    dados,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(
                temporario,
                arquivo
            )

    except Exception as e:

        logger.error(
            "Erro salvando %s: %s",
            arquivo,
            e
        )


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


if not isinstance(stats, dict):
    stats = {}

if not isinstance(sinais, list):
    sinais = []

if not isinstance(cache_persistente, dict):
    cache_persistente = {}


stats.setdefault("total", 0)
stats.setdefault("wins", 0)
stats.setdefault("losses", 0)
stats.setdefault("lucro_teorico", 0.0)
stats.setdefault("bancas", {})
stats.setdefault("por_gale", {})

cache_persistente.setdefault(
    "historicos",
    {}
)

cache_persistente.setdefault(
    "cursor_partidas",
    0
)


for gale in range(MAX_GALES + 1):

    stats["por_gale"].setdefault(
        str(gale),
        {
            "wins": 0,
            "losses": 0
        }
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
# CACHE EM MEMÓRIA
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

    return datetime.now(
        timezone.utc
    )


def timestamp_agora():

    return int(
        utc_agora().timestamp()
    )


def iso_agora():

    return utc_agora().isoformat()


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

            resultado = float(valor)

            if math.isfinite(resultado):
                return resultado

        except Exception:
            return None

    try:

        texto = (
            str(valor)
            .strip()
            .replace(",", ".")
        )

        resultado = float(texto)

        if math.isfinite(resultado):
            return resultado

    except Exception:
        pass

    return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado."
        )

        return False

    try:

        resposta = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/sendMessage"
            ),
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        if resposta.status_code != 200:

            logger.error(
                "Telegram %s | %s",
                resposta.status_code,
                resposta.text[:1000]
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

                diferenca = (
                    agora - ultimo_request_api
                )

                if (
                    diferenca
                    < API_MIN_INTERVAL_SECONDS
                ):

                    espera = max(
                        espera,
                        API_MIN_INTERVAL_SECONDS
                        - diferenca
                    )

            if (
                len(api_request_times)
                >= API_MAX_REQUESTS_PER_MINUTE
            ):

                espera_janela = (
                    60
                    - (
                        agora
                        - api_request_times[0]
                    )
                    + 0.5
                )

                espera = max(
                    espera,
                    espera_janela
                )

            if espera <= 0:

                agora = time.monotonic()

                api_request_times.append(
                    agora
                )

                ultimo_request_api = agora

                return

        logger.info(
            "⏳ Rate limit: aguardando %.1fs",
            espera
        )

        time.sleep(
            max(0.1, espera)
        )


# ============================================================
# API
# ============================================================

def api_get(
    endpoint,
    params=None,
    permitir_403=False
):

    global api_backoff_until

    if not API_KEY:

        logger.error(
            "FIVE_DOLLAR_API_KEY não configurada."
        )

        return None

    esperar_limite_api()

    url = (
        f"{BASE_API}/"
        f"{endpoint.lstrip('/')}"
    )

    headers = {
        "Authorization":
            f"Bearer {API_KEY}",

        "Accept":
            "application/json",

        "User-Agent":
            "robo-adaptativo/7.0"
    }

    try:

        resposta = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

    except requests.RequestException as e:

        logger.error(
            "Erro API: %s",
            e
        )

        return None

    if resposta.status_code == 429:

        retry_after = resposta.headers.get(
            "Retry-After"
        )

        try:

            espera = int(
                retry_after
            )

        except (TypeError, ValueError):

            espera = API_BACKOFF_429

        with api_lock:

            api_backoff_until = max(
                api_backoff_until,
                time.monotonic() + espera
            )

        logger.error(
            "⚠️ API 429 | aguardando %ss",
            espera
        )

        return None

    if (
        resposta.status_code == 403
        and permitir_403
    ):

        return {
            "_status": 403
        }

    if resposta.status_code != 200:

        logger.error(
            "API %s | endpoint=%s | %s",
            resposta.status_code,
            endpoint,
            resposta.text[:1500]
        )

        return None

    try:

        return resposta.json()

    except Exception:

        logger.error(
            "JSON inválido retornado pela API."
        )

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
            and (
                agora_cache
                - cache_fixtures["timestamp"]
            ) < CACHE_FIXTURES_TTL
        ):

            logger.info(
                "📦 %d partidas via cache.",
                len(
                    cache_fixtures["data"]
                )
            )

            return cache_fixtures["data"]

    agora = utc_agora()

    fim = (
        agora
        + timedelta(
            hours=JANELA_HORAS
        )
    )

    params = {
        "start_time":
            int(agora.timestamp()),

        "end_time":
            int(fim.timestamp()),

        "status":
            "scheduled",

        "per_page":
            100
    }

    logger.info(
        "⚽ Buscando partidas "
        "das próximas %dh...",
        JANELA_HORAS
    )

    resposta = api_get(
        "/fixtures",
        params=params
    )

    dados = extrair_data(
        resposta
    )

    if not isinstance(dados, list):

        logger.warning(
            "Resposta de fixtures "
            "não contém uma lista."
        )

        return []

    with dados_lock:

        cache_fixtures[
            "timestamp"
        ] = agora_cache

        cache_fixtures[
            "data"
        ] = dados

    logger.info(
        "⚽ %d partidas encontradas.",
        len(dados)
    )

    return dados


# ============================================================
# FIXTURE ID
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

        fixture = jogo.get(
            "fixture"
        )

        if isinstance(fixture, dict):

            valor = fixture.get(
                "id"
            )

    if valor is None:
        return None

    return str(valor)


# ============================================================
# TIMES
# ============================================================

def dados_times(jogo):

    teams = jogo.get(
        "teams",
        {}
    )

    if not isinstance(teams, dict):
        teams = {}

    home = teams.get(
        "home",
        {}
    )

    away = teams.get(
        "away",
        {}
    )

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
        "home_id":
            str(home_id)
            if home_id is not None
            else None,

        "away_id":
            str(away_id)
            if away_id is not None
            else None,

        "home":
            str(home_name),

        "away":
            str(away_name)
    }


# ============================================================
# HORÁRIO DA PARTIDA
# ============================================================

def kickoff_ts(jogo):

    for chave in (
        "kickoff_ts",
        "start_time",
        "timestamp"
    ):

        valor = jogo.get(
            chave
        )

        if valor is not None:

            try:

                return int(
                    valor
                )

            except Exception:
                pass

    for chave in (
        "kickoff_utc",
        "date"
    ):

        valor = jogo.get(
            chave
        )

        if not valor:
            continue

        try:

            dt = datetime.fromisoformat(
                str(valor).replace(
                    "Z",
                    "+00:00"
                )
            )

            return int(
                dt.timestamp()
            )

        except Exception:
            pass

    return None


# ============================================================
# HISTÓRICO DOS TIMES
# ============================================================

def buscar_historico_time(team_id):

    if not team_id:
        return []

    chave = str(
        team_id
    )

    agora = time.time()

    with dados_lock:

        item = (
            cache_persistente[
                "historicos"
            ].get(chave)
        )

        if item:

            idade = (
                agora
                - float(
                    item.get(
                        "timestamp",
                        0
                    )
                )
            )

            if (
                idade
                < CACHE_HISTORICO_TTL
            ):

                dados = item.get(
                    "data",
                    []
                )

                logger.info(
                    "📦 Histórico time %s: "
                    "%d jogos via cache.",
                    team_id,
                    len(dados)
                )

                return dados

    logger.info(
        "📊 Buscando histórico "
        "do time %s...",
        team_id
    )

    resposta = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
            "per_page":
                QTD_HISTORICO_TIME
        }
    )

    dados = extrair_data(
        resposta
    )

    if not isinstance(dados, list):

        logger.warning(
            "Histórico do time %s "
            "indisponível.",
            team_id
        )

        return []

    dados = dados[
        :QTD_HISTORICO_TIME
    ]

    with dados_lock:

        cache_persistente[
            "historicos"
        ][chave] = {
            "timestamp":
                agora,

            "data":
                dados
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
# EXTRAIR GOLS / CANTOS
# ============================================================

def extrair_totais(jogo):

    if not isinstance(jogo, dict):
        return None, None

    gols = None
    cantos = None

    goals = jogo.get(
        "goals"
    )

    corners = jogo.get(
        "corners"
    )

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

            gols = (
                home + away
            )

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

            cantos = (
                home + away
            )

    if gols is None:

        gols = numero(
            jogo.get(
                "total_goals"
            )
        )

    if cantos is None:

        cantos = numero(
            jogo.get(
                "total_corners"
            )
        )

    return gols, cantos


# ============================================================
# COMBINAR HISTÓRICOS
# ============================================================

def combinar_historicos(
    home_history,
    away_history
):

    unicos = {}

    contador_sem_id = 0

    for jogo in (
        home_history
        + away_history
    ):

        if not isinstance(jogo, dict):
            continue

        gols, cantos = (
            extrair_totais(jogo)
        )

        if (
            gols is None
            or cantos is None
        ):
            continue

        fid = (
            jogo.get("id")
            or jogo.get("fixture_id")
        )

        if fid is None:

            contador_sem_id += 1

            fid = (
                f"sem-id-"
                f"{contador_sem_id}-"
                f"{gols}-"
                f"{cantos}"
            )

        unicos[
            str(fid)
        ] = {
            "gols":
                float(gols),

            "cantos":
                float(cantos)
        }

    return list(
        unicos.values()
    )


# ============================================================
# MÉDIAS
# ============================================================

def calcular_medias(partidas):

    total = len(
        partidas
    )

    if (
        total
        < MIN_JOGOS_HISTORICO
    ):

        return None

    media_cantos = (
        sum(
            x["cantos"]
            for x in partidas
        )
        / total
    )

    media_gols = (
        sum(
            x["gols"]
            for x in partidas
        )
        / total
    )

    return {
        "jogos":
            total,

        "media_cantos":
            round(
                media_cantos,
                2
            ),

        "media_gols":
            round(
                media_gols,
                2
            )
    }


# ============================================================
# LINHAS ALVO
# ============================================================

def linha_meio_abaixo(media):

    alvo = (
        media
        - MARGEM_CANTOS
    )

    linha = (
        math.floor(
            alvo + 0.5
        )
        - 0.5
    )

    return max(
        0.5,
        linha
    )


def linha_meio_acima(media):

    alvo = (
        media
        + MARGEM_GOLS
    )

    linha = (
        math.ceil(
            alvo - 0.5
        )
        + 0.5
    )

    return max(
        0.5,
        linha
    )


# ============================================================
# FREQUÊNCIA HISTÓRICA
# ============================================================

def avaliar_linhas(
    partidas,
    linha_cantos,
    linha_gols
):

    total = len(
        partidas
    )

    if total == 0:
        return None

    acertos_cantos = 0
    acertos_gols = 0
    acertos_combinados = 0

    for partida in partidas:

        ganhou_cantos = (
            partida["cantos"]
            > linha_cantos
        )

        ganhou_gols = (
            partida["gols"]
            < linha_gols
        )

        if ganhou_cantos:
            acertos_cantos += 1

        if ganhou_gols:
            acertos_gols += 1

        if (
            ganhou_cantos
            and ganhou_gols
        ):
            acertos_combinados += 1

    return {
        "total":
            total,

        "acertos_cantos":
            acertos_cantos,

        "acertos_gols":
            acertos_gols,

        "acertos_combinados":
            acertos_combinados,

        "taxa_cantos":
            acertos_cantos / total,

        "taxa_gols":
            acertos_gols / total,

        "taxa_combinada":
            acertos_combinados / total
    }


# ============================================================
# NORMALIZAÇÃO BOOKMAKER
# ============================================================

def normalizar_nome(texto):

    return (
        str(texto or "")
        .lower()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
    )


# ============================================================
# ENCONTRAR BOOKMAKER
# ============================================================

def encontrar_bookmaker(dados):

    if not isinstance(dados, dict):
        return None

    bookmakers = dados.get(
        "bookmakers"
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return None

    desejado = normalizar_nome(
        BOOKMAKER
    )

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome = normalizar_nome(
            bookmaker.get("name")
        )

        slug = normalizar_nome(
            bookmaker.get("slug")
        )

        if (
            desejado == nome
            or desejado == slug
            or desejado in nome
            or desejado in slug
        ):

            return bookmaker

    return None


# ============================================================
# ESTÁGIO DA ODD
# ============================================================

def escolher_estagio_odd(mercado):

    if not isinstance(
        mercado,
        dict
    ):
        return None

    # Pré-jogo:
    # fechamento mais recente primeiro.
    for estagio in (
        "closing",
        "opening"
    ):

        dados = mercado.get(
            estagio
        )

        if not isinstance(
            dados,
            dict
        ):
            continue

        linha = numero(
            dados.get("line")
        )

        over = numero(
            dados.get("over")
        )

        under = numero(
            dados.get("under")
        )

        if (
            linha is not None
            and over is not None
            and under is not None
            and over > 1
            and under > 1
        ):

            return {
                "estagio":
                    estagio,

                "line":
                    linha,

                "over":
                    over,

                "under":
                    under
            }

    return None


# ============================================================
# EXTRAIR ODDS REAIS
# ============================================================

def extrair_linhas_odds(dados):

    linhas_cantos = []
    linhas_gols = []

    bookmaker = encontrar_bookmaker(
        dados
    )

    if bookmaker is None:

        logger.warning(
            "⚠️ Bookmaker %s "
            "não encontrado.",
            BOOKMAKER
        )

        return (
            linhas_cantos,
            linhas_gols
        )

    odds = bookmaker.get(
        "odds"
    )

    if not isinstance(
        odds,
        dict
    ):

        logger.warning(
            "⚠️ %s encontrado, "
            "mas sem objeto odds.",
            BOOKMAKER
        )

        return (
            linhas_cantos,
            linhas_gols
        )

    # --------------------------------------------------------
    # ESCANTEIOS
    # --------------------------------------------------------

    mercado_cantos = odds.get(
        "corner_line"
    )

    cantos = escolher_estagio_odd(
        mercado_cantos
    )

    if cantos:

        linhas_cantos.append({
            "linha":
                cantos["line"],

            "odd":
                cantos["over"],

            "lado":
                "over",

            "estagio":
                cantos["estagio"],

            "bookmaker":
                bookmaker.get(
                    "slug"
                )
                or bookmaker.get(
                    "name"
                )
                or BOOKMAKER
        })

    # --------------------------------------------------------
    # GOLS
    # --------------------------------------------------------

    mercado_gols = odds.get(
        "goal_line"
    )

    gols = escolher_estagio_odd(
        mercado_gols
    )

    if gols:

        linhas_gols.append({
            "linha":
                gols["line"],

            "odd":
                gols["under"],

            "lado":
                "under",

            "estagio":
                gols["estagio"],

            "bookmaker":
                bookmaker.get(
                    "slug"
                )
                or bookmaker.get(
                    "name"
                )
                or BOOKMAKER
        })

    return (
        linhas_cantos,
        linhas_gols
    )


# ============================================================
# DEBUG ODDS
# ============================================================

def diagnosticar_odds(
    fid,
    dados
):

    if not DEBUG_ODDS:
        return

    try:

        bookmaker = encontrar_bookmaker(
            dados
        )

        if bookmaker is None:

            logger.info(
                "🔎 ODDS fixture %s | "
                "%s não encontrada | RAW=%s",
                fid,
                BOOKMAKER,
                json.dumps(
                    dados,
                    ensure_ascii=False
                )[:5000]
            )

            return

        odds = bookmaker.get(
            "odds",
            {}
        )

        logger.info(
            "🔎 ODDS COMPLETAS fixture %s | "
            "bookmaker=%s | "
            "goal_line=%s | "
            "corner_line=%s",
            fid,
            (
                bookmaker.get("slug")
                or bookmaker.get("name")
                or BOOKMAKER
            ),
            json.dumps(
                odds.get(
                    "goal_line"
                ),
                ensure_ascii=False
            ),
            json.dumps(
                odds.get(
                    "corner_line"
                ),
                ensure_ascii=False
            )
        )

    except Exception as e:

        logger.warning(
            "Erro diagnosticando odds "
            "fixture %s: %s",
            fid,
            e
        )


# ============================================================
# BUSCAR ODDS COMPLETAS
# ============================================================

def buscar_odds(jogo):

    fid = fixture_id(
        jogo
    )

    if not fid:
        return None

    agora = time.time()

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    with dados_lock:

        item = cache_odds.get(
            fid
        )

        if item:

            idade = (
                agora
                - float(
                    item.get(
                        "timestamp",
                        0
                    )
                )
            )

            if (
                idade
                < CACHE_ODDS_TTL
            ):

                logger.info(
                    "📦 Odds completas "
                    "fixture %s via cache.",
                    fid
                )

                return item.get(
                    "data"
                )

    # ========================================================
    # MUITO IMPORTANTE:
    #
    # NÃO usamos jogo["odds"] aqui.
    #
    # A listagem de fixtures pode trazer apenas
    # a linha resumida sem preço Over/Under.
    # ========================================================

    logger.info(
        "💰 Buscando odds COMPLETAS "
        "fixture %s...",
        fid
    )

    resposta = api_get(
        f"/fixtures/{fid}/odds",
        params={
            "bookmakers":
                BOOKMAKER
        }
    )

    dados = extrair_data(
        resposta
    )

    if not dados:

        logger.warning(
            "❌ API não retornou "
            "odds completas | fixture %s",
            fid
        )

        return None

    diagnosticar_odds(
        fid,
        dados
    )

    bookmaker = encontrar_bookmaker(
        dados
    )

    if bookmaker is None:

        logger.warning(
            "❌ %s não disponível "
            "para fixture %s",
            BOOKMAKER,
            fid
        )

        return None

    with dados_lock:

        cache_odds[
            fid
        ] = {
            "timestamp":
                agora,

            "data":
                dados
        }

    return dados


# ============================================================
# SELECIONAR MERCADO
# ============================================================

def selecionar_mercado(
    partidas,
    media_cantos,
    media_gols,
    linhas_cantos,
    linhas_gols
):

    if (
        not linhas_cantos
        or not linhas_gols
    ):

        return None

    alvo_cantos = (
        linha_meio_abaixo(
            media_cantos
        )
    )

    alvo_gols = (
        linha_meio_acima(
            media_gols
        )
    )

    logger.info(
        "🎯 Alvo histórico | "
        "Over cantos %.1f | "
        "Under gols %.1f",
        alvo_cantos,
        alvo_gols
    )

    candidatos = []

    for mercado_cantos in linhas_cantos:

        linha_cantos = float(
            mercado_cantos[
                "linha"
            ]
        )

        odd_cantos = float(
            mercado_cantos[
                "odd"
            ]
        )

        # Queremos Over abaixo da média histórica.
        if (
            linha_cantos
            >= media_cantos
        ):

            logger.info(
                "⛔ Over %.2f cantos "
                "não está abaixo da média %.2f.",
                linha_cantos,
                media_cantos
            )

            continue

        for mercado_gols in linhas_gols:

            linha_gols = float(
                mercado_gols[
                    "linha"
                ]
            )

            odd_gols = float(
                mercado_gols[
                    "odd"
                ]
            )

            # Queremos Under acima da média histórica.
            if (
                linha_gols
                <= media_gols
            ):

                logger.info(
                    "⛔ Under %.2f gols "
                    "não está acima da média %.2f.",
                    linha_gols,
                    media_gols
                )

                continue

            historico = avaliar_linhas(
                partidas,
                linha_cantos,
                linha_gols
            )

            if historico is None:
                continue

            logger.info(
                "📈 Teste Over %.2f cantos "
                "+ Under %.2f gols | "
                "cantos %.1f%% | "
                "gols %.1f%% | "
                "conjunto %.1f%%",
                linha_cantos,
                linha_gols,
                historico[
                    "taxa_cantos"
                ] * 100,
                historico[
                    "taxa_gols"
                ] * 100,
                historico[
                    "taxa_combinada"
                ] * 100
            )

            if (
                historico[
                    "taxa_cantos"
                ]
                < MIN_TAXA_CANTOS
            ):
                continue

            if (
                historico[
                    "taxa_gols"
                ]
                < MIN_TAXA_GOLS
            ):
                continue

            if (
                historico[
                    "taxa_combinada"
                ]
                < MIN_TAXA_COMBINADA
            ):
                continue

            # Produto matemático das duas odds.
            # Não é garantia de ser o preço real de
            # uma múltipla do mesmo jogo na casa.
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

                logger.info(
                    "⛔ Odd teórica %.2f "
                    "fora de %.2f–%.2f.",
                    odd_combinada,
                    ODD_MINIMA,
                    ODD_MAXIMA
                )

                continue

            distancia_linhas = (
                abs(
                    linha_cantos
                    - alvo_cantos
                )
                +
                abs(
                    linha_gols
                    - alvo_gols
                )
            )

            distancia_odd = abs(
                odd_combinada
                - ODD_ALVO
            )

            score = (
                historico[
                    "taxa_combinada"
                ] * 100
                +
                historico[
                    "taxa_cantos"
                ] * 10
                +
                historico[
                    "taxa_gols"
                ] * 10
                -
                distancia_linhas * 2
                -
                distancia_odd * 5
            )

            candidatos.append({
                "linha_cantos":
                    linha_cantos,

                "odd_cantos":
                    round(
                        odd_cantos,
                        3
                    ),

                "linha_gols":
                    linha_gols,

                "odd_gols":
                    round(
                        odd_gols,
                        3
                    ),

                "odd_combinada":
                    round(
                        odd_combinada,
                        3
                    ),

                "taxa_cantos":
                    historico[
                        "taxa_cantos"
                    ],

                "taxa_gols":
                    historico[
                        "taxa_gols"
                    ],

                "taxa_combinada":
                    historico[
                        "taxa_combinada"
                    ],

                "acertos_combinados":
                    historico[
                        "acertos_combinados"
                    ],

                "total":
                    historico[
                        "total"
                    ],

                "bookmaker":
                    mercado_cantos.get(
                        "bookmaker",
                        BOOKMAKER
                    ),

                "estagio_cantos":
                    mercado_cantos.get(
                        "estagio"
                    ),

                "estagio_gols":
                    mercado_gols.get(
                        "estagio"
                    ),

                "score":
                    score
            })

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    return candidatos[0]


# ============================================================
# SINAL ATIVO PARA O JOGO
# ============================================================

def jogo_ja_usado(fid):

    fid = str(
        fid
    )

    with dados_lock:

        for sinal in sinais:

            if (
                str(
                    sinal.get(
                        "fixture_id"
                    )
                ) == fid
                and sinal.get(
                    "status"
                ) == "ATIVO"
            ):

                return True

    return False


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():

    with dados_lock:

        return [
            banca
            for banca, dados
            in stats[
                "bancas"
            ].items()
            if not dados.get(
                "ativa",
                False
            )
        ]


def valor_entrada(banca):

    gale = int(
        stats[
            "bancas"
        ][banca].get(
            "gale",
            0
        )
    )

    return round(
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** gale
        ),
        2
    )


# ============================================================
# LOTE ROTATIVO
# ============================================================

def selecionar_lote(partidas):

    elegiveis = []

    agora = timestamp_agora()

    for jogo in partidas:

        fid = fixture_id(
            jogo
        )

        if not fid:
            continue

        if jogo_ja_usado(
            fid
        ):
            continue

        times = dados_times(
            jogo
        )

        if (
            not times["home_id"]
            or not times["away_id"]
        ):
            continue

        inicio = kickoff_ts(
            jogo
        )

        if (
            not inicio
            or inicio <= agora
        ):
            continue

        elegiveis.append(
            jogo
        )

    if not elegiveis:
        return []

    elegiveis.sort(
        key=lambda x:
            kickoff_ts(x)
            or 9999999999
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
        len(lote)
        < MAX_JOGOS_ANALISADOS_CICLO
        and len(lote)
        < len(elegiveis)
    ):

        lote.append(
            elegiveis[
                indice
                % len(elegiveis)
            ]
        )

        indice += 1

    cache_persistente[
        "cursor_partidas"
    ] = (
        indice
        % len(elegiveis)
    )

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

    fid = fixture_id(
        jogo
    )

    times = dados_times(
        jogo
    )

    logger.info(
        "🔍 %s x %s | fixture %s",
        times["home"],
        times["away"],
        fid
    )

    # --------------------------------------------------------
    # HISTÓRICO
    # --------------------------------------------------------

    historico_home = (
        buscar_historico_time(
            times["home_id"]
        )
    )

    historico_away = (
        buscar_historico_time(
            times["away_id"]
        )
    )

    partidas = combinar_historicos(
        historico_home,
        historico_away
    )

    medias = calcular_medias(
        partidas
    )

    if not medias:

        logger.info(
            "❌ Histórico insuficiente | "
            "%s x %s",
            times["home"],
            times["away"]
        )

        return None

    logger.info(
        "📊 %s x %s | "
        "%d jogos | "
        "média cantos %.2f | "
        "média gols %.2f",
        times["home"],
        times["away"],
        medias["jogos"],
        medias["media_cantos"],
        medias["media_gols"]
    )

    # --------------------------------------------------------
    # ODDS COMPLETAS
    # --------------------------------------------------------

    dados_odds = buscar_odds(
        jogo
    )

    if not dados_odds:

        logger.info(
            "❌ Sem odds completas | "
            "%s x %s",
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
        "cantos=%s | gols=%s",
        times["home"],
        times["away"],
        linhas_cantos,
        linhas_gols
    )

    if not linhas_cantos:

        logger.warning(
            "❌ Sem preço REAL "
            "para Over de escanteios | "
            "%s x %s",
            times["home"],
            times["away"]
        )

        return None

    if not linhas_gols:

        logger.warning(
            "❌ Sem preço REAL "
            "para Under de gols | "
            "%s x %s",
            times["home"],
            times["away"]
        )

        return None

    # --------------------------------------------------------
    # SELEÇÃO
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
            "❌ Odds encontradas, "
            "mas combinação reprovada | "
            "%s x %s",
            times["home"],
            times["away"]
        )

        return None

    logger.info(
        "✅ CANDIDATO | %s x %s | "
        "Over %.2f cantos @ %.2f + "
        "Under %.2f gols @ %.2f | "
        "histórico conjunto %.1f%% | "
        "odd teórica %.2f",
        times["home"],
        times["away"],
        mercado["linha_cantos"],
        mercado["odd_cantos"],
        mercado["linha_gols"],
        mercado["odd_gols"],
        mercado["taxa_combinada"] * 100,
        mercado["odd_combinada"]
    )

    return {
        "fixture_id":
            fid,

        "home":
            times["home"],

        "away":
            times["away"],

        "inicio":
            kickoff_ts(jogo),

        "media_cantos":
            medias["media_cantos"],

        "media_gols":
            medias["media_gols"],

        "jogos_historico":
            medias["jogos"],

        "mercado":
            mercado,

        "score":
            mercado["score"]
    }


# ============================================================
# HORÁRIO BRASIL
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

        return str(
            ts
        )


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    banca,
    candidato
):

    mercado = candidato[
        "mercado"
    ]

    with dados_lock:

        gale = int(
            stats[
                "bancas"
            ][banca].get(
                "gale",
                0
            )
        )

        entrada = valor_entrada(
            banca
        )

        sinal = {
            "id":
                (
                    f"{candidato['fixture_id']}-"
                    f"{banca}-"
                    f"{int(time.time())}"
                ),

            "fixture_id":
                candidato[
                    "fixture_id"
                ],

            "home":
                candidato["home"],

            "away":
                candidato["away"],

            "inicio":
                candidato["inicio"],

            "banca":
                banca,

            "gale":
                gale,

            "entrada":
                entrada,

            "linha_cantos":
                mercado[
                    "linha_cantos"
                ],

            "linha_gols":
                mercado[
                    "linha_gols"
                ],

            "odd_cantos":
                mercado[
                    "odd_cantos"
                ],

            "odd_gols":
                mercado[
                    "odd_gols"
                ],

            "odd_combinada":
                mercado[
                    "odd_combinada"
                ],

            "bookmaker":
                mercado.get(
                    "bookmaker",
                    BOOKMAKER
                ),

            "media_cantos":
                candidato[
                    "media_cantos"
                ],

            "media_gols":
                candidato[
                    "media_gols"
                ],

            "taxa_cantos":
                mercado[
                    "taxa_cantos"
                ],

            "taxa_gols":
                mercado[
                    "taxa_gols"
                ],

            "taxa_combinada":
                mercado[
                    "taxa_combinada"
                ],

            "jogos_historico":
                candidato[
                    "jogos_historico"
                ],

            "status":
                "ATIVO",

            "resultado":
                None,

            "criado_em":
                iso_agora(),

            "resolvido_em":
                None
        }

        sinais.append(
            sinal
        )

        stats[
            "bancas"
        ][banca][
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
        "🚨 <b>NOVO SINAL</b>\n\n"

        f"⚽ <b>{candidato['home']} x "
        f"{candidato['away']}</b>\n"

        f"🕒 {horario_br(candidato['inicio'])}\n\n"

        "🎯 <b>COMBINADA</b>\n"

        f"🚩 Over "
        f"{mercado['linha_cantos']:.2f} "
        f"escanteios @ "
        f"{mercado['odd_cantos']:.2f}\n"

        f"🥅 Under "
        f"{mercado['linha_gols']:.2f} "
        f"gols @ "
        f"{mercado['odd_gols']:.2f}\n\n"

        f"💰 Odd combinada teórica: "
        f"{mercado['odd_combinada']:.2f}\n"

        f"🏪 Bookmaker: "
        f"{mercado.get('bookmaker', BOOKMAKER)}\n\n"

        "📊 <b>HISTÓRICO</b>\n"

        f"📚 Amostra: "
        f"{candidato['jogos_historico']} jogos\n"

        f"🚩 Média de cantos: "
        f"{candidato['media_cantos']:.2f}\n"

        f"🥅 Média de gols: "
        f"{candidato['media_gols']:.2f}\n\n"

        "📈 <b>FREQUÊNCIA NA AMOSTRA</b>\n"

        f"🚩 Over cantos: "
        f"{mercado['taxa_cantos'] * 100:.1f}%\n"

        f"🥅 Under gols: "
        f"{mercado['taxa_gols'] * 100:.1f}%\n"

        f"🎯 Ambos: "
        f"{mercado['taxa_combinada'] * 100:.1f}%\n\n"

        f"🏦 Banca {banca}\n"

        f"🔄 Gale "
        f"{gale}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )

    enviar_telegram(
        texto
    )

    logger.info(
        "🚨 SINAL ENVIADO | "
        "Banca %s | %s x %s",
        banca,
        candidato["home"],
        candidato["away"]
    )


# ============================================================
# BUSCAR FIXTURE PARA RESULTADO
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


# ============================================================
# REGISTRAR RESULTADO
# ============================================================

def registrar_resultado(
    sinal,
    resultado,
    gols,
    cantos
):

    with dados_lock:

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

        stats[
            "por_gale"
        ].setdefault(
            str(gale),
            {
                "wins": 0,
                "losses": 0
            }
        )

        if resultado == "WIN":

            lucro = (
                entrada
                * (odd - 1)
            )

            stats["wins"] += 1

            stats[
                "por_gale"
            ][str(gale)][
                "wins"
            ] += 1

            stats[
                "bancas"
            ][banca][
                "wins"
            ] += 1

            stats[
                "bancas"
            ][banca][
                "lucro"
            ] += lucro

            # WIN -> volta à entrada inicial.
            stats[
                "bancas"
            ][banca][
                "gale"
            ] = 0

        else:

            lucro = -entrada

            stats["losses"] += 1

            stats[
                "por_gale"
            ][str(gale)][
                "losses"
            ] += 1

            stats[
                "bancas"
            ][banca][
                "losses"
            ] += 1

            stats[
                "bancas"
            ][banca][
                "lucro"
            ] += lucro

            if gale < MAX_GALES:

                stats[
                    "bancas"
                ][banca][
                    "gale"
                ] = gale + 1

            else:

                # Perdeu último Gale:
                # encerra sequência e volta ao início.
                stats[
                    "bancas"
                ][banca][
                    "gale"
                ] = 0

        stats[
            "lucro_teorico"
        ] += lucro

        stats[
            "lucro_teorico"
        ] = round(
            stats[
                "lucro_teorico"
            ],
            2
        )

        stats[
            "bancas"
        ][banca][
            "lucro"
        ] = round(
            stats[
                "bancas"
            ][banca][
                "lucro"
            ],
            2
        )

        stats[
            "bancas"
        ][banca][
            "ativa"
        ] = False

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

        assertividade = (
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

    texto = (
        f"{emoji} <b>{resultado}</b>\n\n"

        f"⚽ {sinal['home']} x "
        f"{sinal['away']}\n\n"

        f"🚩 Over "
        f"{sinal['linha_cantos']:.2f} cantos\n"

        f"🥅 Under "
        f"{sinal['linha_gols']:.2f} gols\n\n"

        f"🚩 Cantos finais: "
        f"{int(cantos)}\n"

        f"🥅 Gols finais: "
        f"{int(gols)}\n\n"

        f"🏦 Banca {banca}\n"

        f"🔄 Gale "
        f"{gale}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}\n\n"

        f"✅ Wins: "
        f"{stats['wins']}\n"

        f"❌ Losses: "
        f"{stats['losses']}\n"

        f"🎯 Assertividade: "
        f"{assertividade:.2f}%\n"

        f"💰 P/L teórico: "
        f"R$ {stats['lucro_teorico']:.2f}"
    )

    enviar_telegram(
        texto
    )


# ============================================================
# STATUS FINAL
# ============================================================

def status_final(jogo):

    status = jogo.get(
        "status"
    )

    if isinstance(
        status,
        dict
    ):

        texto = " ".join(
            str(v).lower()
            for v in status.values()
            if v is not None
        )

    else:

        texto = str(
            status or ""
        ).lower()

    termos = (
        "finished",
        "final",
        "completed",
        "ended",
        "full time",
        "fulltime",
        "ft"
    )

    return any(
        termo in texto
        for termo in termos
    )


# ============================================================
# RESOLVER SINAIS
# ============================================================

def resolver_sinais():

    with dados_lock:

        ativos = [
            sinal
            for sinal in sinais
            if sinal.get(
                "status"
            ) == "ATIVO"
        ]

    if not ativos:
        return

    agora = timestamp_agora()

    for sinal in ativos:

        inicio = sinal.get(
            "inicio"
        )

        # Evita gastar request antes de haver
        # possibilidade de a partida ter terminado.
        if inicio:

            if (
                agora
                < int(inicio)
                + (90 * 60)
            ):

                continue

        jogo = buscar_fixture(
            sinal["fixture_id"]
        )

        if not jogo:
            continue

        if not status_final(
            jogo
        ):
            continue

        gols, cantos = (
            extrair_totais(
                jogo
            )
        )

        if (
            gols is None
            or cantos is None
        ):

            logger.warning(
                "⚠️ Fixture %s finalizada "
                "mas resultado de gols/cantos "
                "está incompleto.",
                sinal[
                    "fixture_id"
                ]
            )

            continue

        ganhou_cantos = (
            cantos
            > float(
                sinal[
                    "linha_cantos"
                ]
            )
        )

        ganhou_gols = (
            gols
            < float(
                sinal[
                    "linha_gols"
                ]
            )
        )

        resultado = (
            "WIN"
            if (
                ganhou_cantos
                and ganhou_gols
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
            "🏦 As 3 bancas estão ocupadas."
        )

        return

    partidas = buscar_partidas()

    if not partidas:

        logger.info(
            "Nenhuma partida futura encontrada."
        )

        return

    lote = selecionar_lote(
        partidas
    )

    if not lote:

        logger.info(
            "Nenhuma partida elegível "
            "neste ciclo."
        )

        return

    candidatos = []

    for jogo in lote:

        try:

            candidato = analisar_jogo(
                jogo
            )

            if candidato:

                candidatos.append(
                    candidato
                )

        except Exception:

            logger.exception(
                "Erro analisando fixture %s",
                fixture_id(jogo)
            )

    if not candidatos:

        logger.info(
            "Nenhum candidato aprovado "
            "neste ciclo."
        )

        return

    candidatos.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    logger.info(
        "🏆 %d candidatos aprovados.",
        len(candidatos)
    )

    fixtures_usadas = set()

    for banca in livres:

        escolhido = None

        for candidato in candidatos:

            fid = candidato[
                "fixture_id"
            ]

            if fid in fixtures_usadas:
                continue

            if jogo_ja_usado(
                fid
            ):
                continue

            escolhido = candidato
            break

        if escolhido is None:
            break

        criar_sinal(
            banca,
            escolhido
        )

        fixtures_usadas.add(
            escolhido[
                "fixture_id"
            ]
        )


# ============================================================
# CICLO
# ============================================================

def executar_ciclo():

    logger.info(
        "=" * 70
    )

    logger.info(
        "🤖 INICIANDO CICLO "
        "ADAPTATIVO V7"
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


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "🤖 Robô adaptativo V7 iniciado."
    )

    time.sleep(5)

    while True:

        inicio = time.time()

        executar_ciclo()

        duracao = (
            time.time()
            - inicio
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

        time.sleep(
            espera
        )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    assertividade = (
        stats["wins"]
        / total
        * 100
        if total
        else 0
    )

    ativos = [
        sinal
        for sinal in sinais
        if sinal.get(
            "status"
        ) == "ATIVO"
    ]

    return jsonify({
        "online":
            True,

        "versao":
            "adaptativo-v7",

        "estrategia":
            (
                "Over escanteios + "
                "Under gols adaptativos"
            ),

        "bookmaker":
            BOOKMAKER,

        "sinais_ativos":
            len(ativos),

        "bancas_livres":
            bancas_livres(),

        "wins":
            stats["wins"],

        "losses":
            stats["losses"],

        "assertividade":
            round(
                assertividade,
                2
            ),

        "lucro_teorico":
            stats[
                "lucro_teorico"
            ],

        "debug_odds":
            DEBUG_ODDS,

        "filtros": {
            "taxa_cantos":
                MIN_TAXA_CANTOS,

            "taxa_gols":
                MIN_TAXA_GOLS,

            "taxa_combinada":
                MIN_TAXA_COMBINADA,

            "odd_minima":
                ODD_MINIMA,

            "odd_maxima":
                ODD_MAXIMA,

            "odd_alvo":
                ODD_ALVO
        }
    })


@app.route("/status")
def rota_status():

    return jsonify({
        "online":
            True,

        "versao":
            "adaptativo-v7",

        "hora_utc":
            iso_agora(),

        "bookmaker":
            BOOKMAKER,

        "bancas_livres":
            bancas_livres(),

        "cursor":
            cache_persistente.get(
                "cursor_partidas",
                0
            ),

        "times_em_cache":
            len(
                cache_persistente.get(
                    "historicos",
                    {}
                )
            ),

        "odds_em_cache":
            len(
                cache_odds
            ),

        "debug_odds":
            DEBUG_ODDS
    })


@app.route("/stats")
def rota_stats():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    resposta = dict(
        stats
    )

    resposta[
        "assertividade"
    ] = (
        round(
            stats["wins"]
            / total
            * 100,
            2
        )
        if total
        else 0
    )

    return jsonify(
        resposta
    )


@app.route("/bancas")
def rota_bancas():

    return jsonify(
        stats["bancas"]
    )


@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais
    )


@app.route("/health")
def health():

    return jsonify({
        "ok":
            True,

        "versao":
            "adaptativo-v7",

        "timestamp":
            timestamp_agora()
    })


# ============================================================
# THREAD
# ============================================================

thread_robo = None

thread_start_lock = (
    threading.Lock()
)


def iniciar_thread():

    global thread_robo

    with thread_start_lock:

        if (
            thread_robo is None
            or not thread_robo.is_alive()
        ):

            thread_robo = (
                threading.Thread(
                    target=loop_robo,
                    daemon=True,
                    name="robo-thread"
                )
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
