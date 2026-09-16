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

# Frequência mínima da combinação escolhida
MIN_TAXA_COMBINADA = float(
    os.getenv("MIN_TAXA_COMBINADA", "0.60")
)

# Frequência mínima individual
MIN_TAXA_CANTOS = float(
    os.getenv("MIN_TAXA_CANTOS", "0.65")
)

MIN_TAXA_GOLS = float(
    os.getenv("MIN_TAXA_GOLS", "0.70")
)

# Distância da linha em relação à média
# Ex:
# média cantos 7.0 -> alvo Over 6.5
# média gols 6.0 -> alvo Under 6.5

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
# API
# ============================================================

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv(
        "MAX_JOGOS_ANALISADOS_CICLO",
        "4"
    )
)

API_MAX_REQUESTS_PER_MINUTE = int(
    os.getenv(
        "API_MAX_REQUESTS_PER_MINUTE",
        "9"
    )
)

API_MIN_INTERVAL_SECONDS = float(
    os.getenv(
        "API_MIN_INTERVAL_SECONDS",
        "6.8"
    )
)

API_BACKOFF_429 = int(
    os.getenv(
        "API_BACKOFF_429",
        "65"
    )
)


# ============================================================
# CACHE
# ============================================================

CACHE_FIXTURES_TTL = int(
    os.getenv(
        "CACHE_FIXTURES_TTL",
        "600"
    )
)

CACHE_HISTORICO_TTL = int(
    os.getenv(
        "CACHE_HISTORICO_TTL",
        "21600"
    )
)

CACHE_ODDS_TTL = int(
    os.getenv(
        "CACHE_ODDS_TTL",
        "1800"
    )
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

            os.replace(
                temp,
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

if not isinstance(sinais, list):
    sinais = []

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

for gale in range(
    MAX_GALES + 1
):

    stats["por_gale"].setdefault(
        str(gale),
        {
            "wins": 0,
            "losses": 0
        }
    )

for i in range(
    1,
    TOTAL_BANCAS + 1
):

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
    return datetime.now(
        timezone.utc
    )


def iso_agora():
    return utc_agora().isoformat()


def timestamp_agora():
    return int(
        utc_agora().timestamp()
    )


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

        r = requests.post(
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

        if r.status_code != 200:

            logger.error(
                "Telegram %s | %s",
                r.status_code,
                r.text
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
                and
                agora - api_request_times[0]
                >= 60
            ):

                api_request_times.popleft()

            if ultimo_request_api:

                diferenca = (
                    agora
                    - ultimo_request_api
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
# API GET
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
        "Authorization": (
            f"Bearer {API_KEY}"
        ),
        "Accept": "application/json",
        "User-Agent": (
            "robo-adaptativo/5.0"
        )
    }

    try:

        r = requests.get(
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

    if r.status_code == 429:

        retry = r.headers.get(
            "Retry-After"
        )

        try:

            espera = int(retry)

        except (TypeError, ValueError):

            espera = API_BACKOFF_429

        with api_lock:

            api_backoff_until = max(
                api_backoff_until,
                time.monotonic()
                + espera
            )

        logger.error(
            "API 429 | backoff %ss",
            espera
        )

        return None

    if (
        r.status_code == 403
        and permitir_403
    ):

        return {
            "_status": 403
        }

    if r.status_code != 200:

        logger.error(
            "API %s | %s",
            r.status_code,
            r.text[:1000]
        )

        return None

    try:

        return r.json()

    except Exception:

        logger.error(
            "JSON inválido da API."
        )

        return None


def extrair_data(resposta):

    if resposta is None:
        return None

    if (
        isinstance(resposta, dict)
        and "data" in resposta
    ):

        return resposta["data"]

    return resposta


# ============================================================
# FIXTURES
# ============================================================

def buscar_partidas():

    agora_cache = time.time()

    with dados_lock:

        if (
            cache_fixtures["data"]
            and
            agora_cache
            - cache_fixtures["timestamp"]
            < CACHE_FIXTURES_TTL
        ):

            logger.info(
                "📦 %d partidas via cache.",
                len(
                    cache_fixtures["data"]
                )
            )

            return cache_fixtures[
                "data"
            ]

    agora = utc_agora()

    fim = agora + timedelta(
        hours=JANELA_HORAS
    )

    params = {
        "start_time": int(
            agora.timestamp()
        ),
        "end_time": int(
            fim.timestamp()
        ),
        "status": "scheduled",
        "per_page": 50,
        "include": "odds"
    }

    logger.info(
        "Buscando partidas das "
        "próximas %dh...",
        JANELA_HORAS
    )

    resposta = api_get(
        "/fixtures",
        params=params,
        permitir_403=True
    )

    if (
        isinstance(resposta, dict)
        and resposta.get("_status")
        == 403
    ):

        params.pop(
            "include",
            None
        )

        resposta = api_get(
            "/fixtures",
            params=params
        )

    dados = extrair_data(
        resposta
    )

    if not isinstance(
        dados,
        list
    ):

        logger.warning(
            "Nenhuma lista de partidas."
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
# FIXTURE / TIMES
# ============================================================

def fixture_id(jogo):

    if not isinstance(
        jogo,
        dict
    ):
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

        if isinstance(
            fixture,
            dict
        ):

            valor = fixture.get(
                "id"
            )

    if valor is None:
        return None

    return str(valor)


def dados_times(jogo):

    teams = jogo.get(
        "teams",
        {}
    )

    home = teams.get(
        "home",
        {}
    ) if isinstance(
        teams,
        dict
    ) else {}

    away = teams.get(
        "away",
        {}
    ) if isinstance(
        teams,
        dict
    ) else {}

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

    for chave in [
        "kickoff_ts",
        "start_time",
        "timestamp"
    ]:

        valor = jogo.get(chave)

        if valor is not None:

            try:
                return int(valor)
            except Exception:
                pass

    for chave in [
        "kickoff_utc",
        "date"
    ]:

        valor = jogo.get(chave)

        if valor:

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
# HISTÓRICO DO TIME
# ============================================================

def buscar_historico_time(
    team_id
):

    if not team_id:
        return []

    chave = str(team_id)

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

    if not isinstance(
        dados,
        list
    ):

        logger.warning(
            "Histórico time %s "
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
# EXTRAÇÃO DE GOLS / CANTOS
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (
        TypeError,
        ValueError
    ):

        return None


def extrair_totais(jogo):

    if not isinstance(
        jogo,
        dict
    ):

        return None, None

    goals = jogo.get(
        "goals"
    )

    corners = jogo.get(
        "corners"
    )

    gols = None
    cantos = None

    if isinstance(
        goals,
        dict
    ):

        gh = numero(
            goals.get("home")
        )

        ga = numero(
            goals.get("away")
        )

        if (
            gh is not None
            and ga is not None
        ):

            gols = gh + ga

    if isinstance(
        corners,
        dict
    ):

        ch = numero(
            corners.get("home")
        )

        ca = numero(
            corners.get("away")
        )

        if (
            ch is not None
            and ca is not None
        ):

            cantos = ch + ca

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
# UNIR HISTÓRICOS
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

        if not isinstance(
            jogo,
            dict
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
            continue

        fid = (
            jogo.get("id")
            or jogo.get(
                "fixture_id"
            )
        )

        if fid is None:

            contador_sem_id += 1

            fid = (
                f"sem-id-"
                f"{contador_sem_id}-"
                f"{gols}-{cantos}"
            )

        unicos[
            str(fid)
        ] = {
            "gols": gols,
            "cantos": cantos
        }

    return list(
        unicos.values()
    )


# ============================================================
# MÉDIAS
# ============================================================

def calcular_medias(
    partidas
):

    if (
        len(partidas)
        < MIN_JOGOS_HISTORICO
    ):

        return None

    media_cantos = sum(
        p["cantos"]
        for p in partidas
    ) / len(partidas)

    media_gols = sum(
        p["gols"]
        for p in partidas
    ) / len(partidas)

    return {
        "jogos": len(partidas),
        "media_cantos": round(
            media_cantos,
            2
        ),
        "media_gols": round(
            media_gols,
            2
        )
    }


# ============================================================
# LINHA ALVO BASEADA NA MÉDIA
# ============================================================

def linha_meio_abaixo(
    media
):
    """
    Ex:
    média 7.0  -> 6.5
    média 7.4  -> 6.5
    média 7.8  -> 7.5
    média 10.3 -> 9.5
    """

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


def linha_meio_acima(
    media
):
    """
    Ex:
    média 6.0 -> 6.5
    média 5.2 -> 5.5
    média 2.7 -> 3.5
    """

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
# FREQUÊNCIA DE UMA COMBINAÇÃO
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

    taxa_cantos = (
        acertos_cantos
        / total
    )

    taxa_gols = (
        acertos_gols
        / total
    )

    taxa_combinada = (
        acertos_combinados
        / total
    )

    return {
        "total": total,

        "acertos_cantos":
            acertos_cantos,

        "acertos_gols":
            acertos_gols,

        "acertos_combinados":
            acertos_combinados,

        "taxa_cantos":
            taxa_cantos,

        "taxa_gols":
            taxa_gols,

        "taxa_combinada":
            taxa_combinada
    }


# ============================================================
# PERCORRER JSON
# ============================================================

def percorrer_json(
    obj,
    caminho=""
):

    if isinstance(
        obj,
        dict
    ):

        yield caminho, obj

        for chave, valor in (
            obj.items()
        ):

            novo = (
                f"{caminho}.{chave}"
                if caminho
                else str(chave)
            )

            yield from percorrer_json(
                valor,
                novo
            )

    elif isinstance(
        obj,
        list
    ):

        for i, valor in (
            enumerate(obj)
        ):

            novo = (
                f"{caminho}[{i}]"
            )

            yield from percorrer_json(
                valor,
                novo
            )


# ============================================================
# IDENTIFICAR TIPO DO MERCADO
# ============================================================

def texto_objeto(
    caminho,
    obj
):

    partes = [
        str(caminho)
    ]

    if isinstance(
        obj,
        dict
    ):

        for chave, valor in (
            obj.items()
        ):

            if isinstance(
                valor,
                (
                    str,
                    int,
                    float
                )
            ):

                partes.append(
                    str(chave)
                )

                partes.append(
                    str(valor)
                )

    return " ".join(
        partes
    ).lower()


def eh_mercado_cantos(
    texto
):

    termos = [
        "corner",
        "corners",
        "corner_line",
        "corner_asian",
        "escante",
        "escanteio"
    ]

    return any(
        termo in texto
        for termo in termos
    )


def eh_mercado_gols(
    texto
):

    termos = [
        "goal_line",
        "goalline",
        "total_goals",
        "goals",
        "goal"
    ]

    return (
        any(
            termo in texto
            for termo in termos
        )
        and
        not eh_mercado_cantos(
            texto
        )
    )


# ============================================================
# EXTRAIR LINHAS DE ODDS
# ============================================================

def extrair_linhas_odds(
    dados
):

    cantos = {}
    gols = {}

    for caminho, obj in (
        percorrer_json(
            dados
        )
    ):

        if not isinstance(
            obj,
            dict
        ):
            continue

        texto = texto_objeto(
            caminho,
            obj
        )

        linha = numero(
            obj.get("line")
        )

        if linha is None:

            linha = numero(
                obj.get("handicap")
            )

        if linha is None:

            linha = numero(
                obj.get("total")
            )

        over = numero(
            obj.get("over")
        )

        if over is None:

            over = numero(
                obj.get(
                    "over_odds"
                )
            )

        if over is None:

            over = numero(
                obj.get(
                    "price_over"
                )
            )

        under = numero(
            obj.get("under")
        )

        if under is None:

            under = numero(
                obj.get(
                    "under_odds"
                )
            )

        if under is None:

            under = numero(
                obj.get(
                    "price_under"
                )
            )

        if linha is None:
            continue

        if (
            eh_mercado_cantos(
                texto
            )
            and over is not None
            and over > 1
        ):

            # Se houver repetição da
            # mesma linha, mantemos
            # a maior odd encontrada.

            atual = cantos.get(
                linha
            )

            if (
                atual is None
                or over > atual
            ):

                cantos[
                    linha
                ] = over

        if (
            eh_mercado_gols(
                texto
            )
            and under is not None
            and under > 1
        ):

            atual = gols.get(
                linha
            )

            if (
                atual is None
                or under > atual
            ):

                gols[
                    linha
                ] = under

    lista_cantos = [
        {
            "linha": float(linha),
            "odd": float(odd)
        }
        for linha, odd
        in cantos.items()
    ]

    lista_gols = [
        {
            "linha": float(linha),
            "odd": float(odd)
        }
        for linha, odd
        in gols.items()
    ]

    lista_cantos.sort(
        key=lambda x:
            x["linha"]
    )

    lista_gols.sort(
        key=lambda x:
            x["linha"]
    )

    return (
        lista_cantos,
        lista_gols
    )


# ============================================================
# ODDS
# ============================================================

def buscar_odds(jogo):

    fid = fixture_id(
        jogo
    )

    if not fid:
        return None

    odds_embutidas = (
        jogo.get("odds")
    )

    if odds_embutidas:

        return odds_embutidas

    agora = time.time()

    with dados_lock:

        item = cache_odds.get(
            fid
        )

        if item:

            if (
                agora
                - item["timestamp"]
                < CACHE_ODDS_TTL
            ):

                return item[
                    "data"
                ]

    logger.info(
        "💰 Buscando odds "
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

    if dados:

        with dados_lock:

            cache_odds[
                fid
            ] = {
                "timestamp": agora,
                "data": dados
            }

    return dados


# ============================================================
# SELECIONAR MELHOR MERCADO
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
        "🎯 Médias: cantos %.2f / "
        "gols %.2f | "
        "alvos: Over %.1f / "
        "Under %.1f",
        media_cantos,
        media_gols,
        alvo_cantos,
        alvo_gols
    )

    candidatos = []

    for mercado_cantos in (
        linhas_cantos
    ):

        linha_cantos = (
            mercado_cantos[
                "linha"
            ]
        )

        odd_cantos = (
            mercado_cantos[
                "odd"
            ]
        )

        # Regra:
        # linha de Over precisa
        # ficar abaixo da média.

        if (
            linha_cantos
            >= media_cantos
        ):
            continue

        for mercado_gols in (
            linhas_gols
        ):

            linha_gols = (
                mercado_gols[
                    "linha"
                ]
            )

            odd_gols = (
                mercado_gols[
                    "odd"
                ]
            )

            # Under acima da média.
            if (
                linha_gols
                <= media_gols
            ):
                continue

            resultado = (
                avaliar_linhas(
                    partidas,
                    linha_cantos,
                    linha_gols
                )
            )

            if not resultado:
                continue

            if (
                resultado[
                    "taxa_cantos"
                ]
                < MIN_TAXA_CANTOS
            ):
                continue

            if (
                resultado[
                    "taxa_gols"
                ]
                < MIN_TAXA_GOLS
            ):
                continue

            if (
                resultado[
                    "taxa_combinada"
                ]
                < MIN_TAXA_COMBINADA
            ):
                continue

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
                continue

            distancia_alvo_linhas = (
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

            # Prioridade:
            # 1. taxa conjunta
            # 2. taxas individuais
            # 3. proximidade da linha
            #    sugerida pela média
            # 4. odd próxima de 2.00

            score = (
                resultado[
                    "taxa_combinada"
                ] * 100
                +
                resultado[
                    "taxa_cantos"
                ] * 10
                +
                resultado[
                    "taxa_gols"
                ] * 10
                -
                distancia_alvo_linhas
                * 2
                -
                distancia_odd
                * 5
            )

            candidatos.append(
                {
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
                        resultado[
                            "taxa_cantos"
                        ],

                    "taxa_gols":
                        resultado[
                            "taxa_gols"
                        ],

                    "taxa_combinada":
                        resultado[
                            "taxa_combinada"
                        ],

                    "acertos_combinados":
                        resultado[
                            "acertos_combinados"
                        ],

                    "total":
                        resultado[
                            "total"
                        ],

                    "score":
                        score
                }
            )

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    return candidatos[0]


# ============================================================
# JOGO JÁ USADO
# ============================================================

def jogo_ja_usado(
    fid
):

    fid = str(fid)

    with dados_lock:

        return any(
            str(
                sinal.get(
                    "fixture_id"
                )
            )
            == fid
            and
            sinal.get(
                "status"
            )
            == "ATIVO"
            for sinal
            in sinais
        )


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


def valor_entrada(
    banca
):

    gale = int(
        stats["bancas"][
            banca
        ].get(
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
# SELECIONAR LOTE
# ============================================================

def selecionar_lote(
    partidas
):

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

    if (
        cursor
        >= len(elegiveis)
    ):

        cursor = 0

    lote = []

    indice = cursor

    while (
        len(lote)
        < MAX_JOGOS_ANALISADOS_CICLO
        and
        len(lote)
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
        "🔎 Lote %d/%d partidas "
        "| cursor %d",
        len(lote),
        len(elegiveis),
        cursor
    )

    return lote


# ============================================================
# ANALISAR JOGO
# ============================================================

def analisar_jogo(
    jogo
):

    fid = fixture_id(
        jogo
    )

    times = dados_times(
        jogo
    )

    logger.info(
        "🔍 %s x %s",
        times["home"],
        times["away"]
    )

    # --------------------------------------------------------
    # HISTÓRICO
    # --------------------------------------------------------

    hist_home = (
        buscar_historico_time(
            times["home_id"]
        )
    )

    hist_away = (
        buscar_historico_time(
            times["away_id"]
        )
    )

    partidas = (
        combinar_historicos(
            hist_home,
            hist_away
        )
    )

    medias = calcular_medias(
        partidas
    )

    if not medias:

        logger.info(
            "❌ Histórico insuficiente "
            "| %s x %s",
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
        "linhas cantos=%s | "
        "linhas gols=%s",
        times["home"],
        times["away"],
        [
            x["linha"]
            for x
            in linhas_cantos
        ],
        [
            x["linha"]
            for x
            in linhas_gols
        ]
    )

    if not linhas_cantos:

        logger.info(
            "❌ API não retornou "
            "linhas de cantos."
        )

        return None

    if not linhas_gols:

        logger.info(
            "❌ API não retornou "
            "linhas de gols."
        )

        return None

    # --------------------------------------------------------
    # MERCADO ADAPTATIVO
    # --------------------------------------------------------

    mercado = selecionar_mercado(
        partidas,
        medias[
            "media_cantos"
        ],
        medias[
            "media_gols"
        ],
        linhas_cantos,
        linhas_gols
    )

    if not mercado:

        logger.info(
            "❌ Nenhuma combinação "
            "adaptativa aprovada | "
            "%s x %s",
            times["home"],
            times["away"]
        )

        return None

    logger.info(
        "✅ CANDIDATO | "
        "%s x %s | "
        "Over %.1f cantos + "
        "Under %.1f gols | "
        "%.1f%% | odd %.2f",
        times["home"],
        times["away"],
        mercado[
            "linha_cantos"
        ],
        mercado[
            "linha_gols"
        ],
        mercado[
            "taxa_combinada"
        ] * 100,
        mercado[
            "odd_combinada"
        ]
    )

    return {
        "fixture_id": fid,
        "home": times["home"],
        "away": times["away"],
        "inicio": kickoff_ts(
            jogo
        ),
        "media_cantos":
            medias[
                "media_cantos"
            ],
        "media_gols":
            medias[
                "media_gols"
            ],
        "jogos_historico":
            medias[
                "jogos"
            ],
        "mercado": mercado,
        "score":
            mercado["score"]
    }


# ============================================================
# HORÁRIO
# ============================================================

def horario_br(
    ts
):

    if not ts:
        return "Não informado"

    try:

        dt = (
            datetime.fromtimestamp(
                int(ts),
                timezone.utc
            )
            - timedelta(
                hours=3
            )
        )

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:

        return str(ts)


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
            stats["bancas"][
                banca
            ].get(
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

        stats["bancas"][
            banca
        ]["ativa"] = True

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

        "📊 <b>MÉDIAS HISTÓRICAS</b>\n"

        f"🚩 Escanteios: "
        f"{candidato['media_cantos']:.2f}\n"

        f"🥅 Gols: "
        f"{candidato['media_gols']:.2f}\n"

        f"📚 Jogos válidos: "
        f"{candidato['jogos_historico']}\n\n"

        "🎯 <b>ENTRADA ESCOLHIDA</b>\n"

        f"🚩 Over "
        f"{mercado['linha_cantos']:.1f} "
        f"escanteios\n"

        f"🥅 Under "
        f"{mercado['linha_gols']:.1f} "
        f"gols\n\n"

        "📈 <b>HISTÓRICO DA LINHA</b>\n"

        f"Over cantos: "
        f"{mercado['taxa_cantos'] * 100:.1f}%\n"

        f"Under gols: "
        f"{mercado['taxa_gols'] * 100:.1f}%\n"

        f"🎯 Ambos juntos: "
        f"{mercado['taxa_combinada'] * 100:.1f}%\n\n"

        "💰 <b>ODDS</b>\n"

        f"Cantos: "
        f"{mercado['odd_cantos']:.2f}\n"

        f"Gols: "
        f"{mercado['odd_gols']:.2f}\n"

        f"🔥 Combinada teórica: "
        f"{mercado['odd_combinada']:.2f}\n\n"

        f"🏦 Banca: {banca}\n"
        f"🔄 Gale: {gale}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}\n\n"

        "ℹ️ A odd combinada é o produto "
        "das odds individuais retornadas "
        "pela API; a cotação real da múltipla "
        "na casa pode ser diferente."
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
# BUSCAR RESULTADO
# ============================================================

def buscar_fixture(
    fid
):

    resposta = api_get(
        f"/fixtures/{fid}"
    )

    dados = extrair_data(
        resposta
    )

    if isinstance(
        dados,
        list
    ):

        if dados:
            return dados[0]

        return None

    if isinstance(
        dados,
        dict
    ):

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
            sinal[
                "odd_combinada"
            ]
        )

        sinal[
            "status"
        ] = "FINALIZADO"

        sinal[
            "resultado"
        ] = resultado

        sinal[
            "gols_final"
        ] = gols

        sinal[
            "cantos_final"
        ] = cantos

        sinal[
            "resolvido_em"
        ] = iso_agora()

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

            stats["bancas"][
                banca
            ]["wins"] += 1

            stats["bancas"][
                banca
            ]["lucro"] += lucro

            # WIN:
            # volta para entrada inicial.
            stats["bancas"][
                banca
            ]["gale"] = 0

        else:

            lucro = -entrada

            stats["losses"] += 1

            stats[
                "por_gale"
            ][str(gale)][
                "losses"
            ] += 1

            stats["bancas"][
                banca
            ]["losses"] += 1

            stats["bancas"][
                banca
            ]["lucro"] += lucro

            # LOSS:
            # próxima oportunidade
            # usa o próximo Gale.

            if gale < MAX_GALES:

                stats["bancas"][
                    banca
                ]["gale"] = (
                    gale + 1
                )

            else:

                # perdeu Gale 2:
                # reinicia sequência.
                stats["bancas"][
                    banca
                ]["gale"] = 0

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

        stats["bancas"][
            banca
        ]["lucro"] = round(
            stats["bancas"][
                banca
            ]["lucro"],
            2
        )

        # Libera a banca.
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

        f"🎯 Over "
        f"{sinal['linha_cantos']:.1f} "
        f"escanteios\n"

        f"🎯 Under "
        f"{sinal['linha_gols']:.1f} "
        f"gols\n\n"

        f"🚩 Escanteios finais: "
        f"{int(cantos)}\n"

        f"🥅 Gols finais: "
        f"{int(gols)}\n\n"

        f"🏦 Banca: {banca}\n"

        f"🔄 Gale: "
        f"{gale}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}\n\n"

        f"✅ Wins: "
        f"{stats['wins']}\n"

        f"❌ Losses: "
        f"{stats['losses']}\n"

        f"🎯 Assertividade: "
        f"{taxa:.2f}%\n"

        f"💰 P/L teórico: "
        f"R$ "
        f"{stats['lucro_teorico']:.2f}"
    )

    enviar_telegram(
        texto
    )

    logger.info(
        "%s | %s x %s | "
        "%s cantos | %s gols",
        resultado,
        sinal["home"],
        sinal["away"],
        cantos,
        gols
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
            )
            == "ATIVO"
        ]

    if not ativos:
        return

    agora = timestamp_agora()

    for sinal in ativos:

        inicio = sinal.get(
            "inicio"
        )

        # Evita consulta antes de
        # aproximadamente 90 min.
        if (
            inicio
            and
            agora
            < int(inicio)
            + 90 * 60
        ):

            continue

        jogo = buscar_fixture(
            sinal[
                "fixture_id"
            ]
        )

        if not jogo:
            continue

        status = jogo.get(
            "status"
        )

        if isinstance(
            status,
            dict
        ):

            status_texto = " ".join(
                str(v).lower()
                for v in status.values()
                if v is not None
            )

        else:

            status_texto = str(
                status or ""
            ).lower()

        finais = [
            "finished",
            "final",
            "ft",
            "completed",
            "ended"
        ]

        if not any(
            termo in status_texto
            for termo in finais
        ):

            logger.info(
                "⏳ Fixture %s "
                "ainda não finalizado.",
                sinal[
                    "fixture_id"
                ]
            )

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
                "Fixture %s terminou "
                "mas resultado incompleto.",
                sinal[
                    "fixture_id"
                ]
            )

            continue

        linha_cantos = float(
            sinal[
                "linha_cantos"
            ]
        )

        linha_gols = float(
            sinal[
                "linha_gols"
            ]
        )

        ganhou_cantos = (
            cantos
            > linha_cantos
        )

        ganhou_gols = (
            gols
            < linha_gols
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
            "🏦 3 bancas ocupadas."
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

            candidato = (
                analisar_jogo(
                    jogo
                )
            )

            if candidato:

                candidatos.append(
                    candidato
                )

        except Exception:

            logger.exception(
                "Erro analisando "
                "fixture %s",
                fixture_id(jogo)
            )

    if not candidatos:

        logger.info(
            "Nenhum candidato "
            "aprovado neste ciclo."
        )

        return

    candidatos.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    logger.info(
        "🏆 %d candidatos "
        "aprovados.",
        len(candidatos)
    )

    usados = set()

    for banca in livres:

        escolhido = None

        for candidato in (
            candidatos
        ):

            fid = candidato[
                "fixture_id"
            ]

            if fid in usados:
                continue

            if jogo_ja_usado(
                fid
            ):
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
        "ADAPTATIVO"
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
        "CICLO FINALIZADO"
    )


def loop_robo():

    logger.info(
        "🤖 Robô adaptativo iniciado."
    )

    logger.info(
        "API: máximo interno "
        "%d req/min.",
        API_MAX_REQUESTS_PER_MINUTE
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
            "Próximo ciclo em %.0fs.",
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

    taxa = (
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
        )
        == "ATIVO"
    ]

    return jsonify(
        {
            "online": True,

            "versao":
                "adaptativo-5.0",

            "estrategia":
                "Over cantos + "
                "Under gols adaptativos",

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
                    taxa,
                    2
                ),

            "lucro_teorico":
                stats[
                    "lucro_teorico"
                ],

            "filtro": {
                "taxa_cantos_min":
                    MIN_TAXA_CANTOS,

                "taxa_gols_min":
                    MIN_TAXA_GOLS,

                "taxa_combinada_min":
                    MIN_TAXA_COMBINADA,

                "odd_min":
                    ODD_MINIMA,

                "odd_max":
                    ODD_MAXIMA
            }
        }
    )


@app.route("/status")
def status():

    return jsonify(
        {
            "online": True,
            "hora_utc":
                iso_agora(),
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
                len(
                    cache_odds
                )
        }
    )


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

    return jsonify(
        {
            "ok": True,
            "versao":
                "adaptativo-5.0",
            "timestamp":
                timestamp_agora()
        }
    )


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
            or
            not thread_robo.is_alive()
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
# LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
