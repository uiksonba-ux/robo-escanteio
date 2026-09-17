import os
import json
import time
import html
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# ROBÔ DE ESCANTEIOS V16
#
# ESTRATÉGIA:
#   UNDER 6.0 ESCANTEIOS HT
#   +
#   OVER 7.0 ESCANTEIOS FT
#
# HISTÓRICO:
#   20 jogos do mandante
#   20 jogos do visitante
#   remove jogos duplicados
#
# FILTROS:
#   Under HT >= 70%
#   Over FT >= 70%
#   Combinação >= 70%
#
# BET365:
#   precisa existir mercado REAL de cantos HT
#   precisa existir mercado REAL de cantos FT
#
# SEM FILTRO PELO VALOR DA ODD
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v16")


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

PORT = int(
    os.getenv("PORT", "10000")
)


# ============================================================
# LINHAS
# ============================================================

LINHA_UNDER_CANTOS_HT = float(
    os.getenv(
        "LINHA_UNDER_CANTOS_HT",
        "6.0"
    )
)

LINHA_OVER_CANTOS_FT = float(
    os.getenv(
        "LINHA_OVER_CANTOS_FT",
        "7.0"
    )
)


# ============================================================
# TAXAS
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv(
        "MIN_TAXA_UNDER_HT",
        "0.70"
    )
)

MIN_TAXA_OVER_FT = float(
    os.getenv(
        "MIN_TAXA_OVER_FT",
        "0.70"
    )
)

MIN_TAXA_COMBINADA = float(
    os.getenv(
        "MIN_TAXA_COMBINADA",
        "0.70"
    )
)


# ============================================================
# HISTÓRICO
# ============================================================

QTD_HISTORICO_TIME = int(
    os.getenv(
        "QTD_HISTORICO_TIME",
        "20"
    )
)

MIN_JOGOS_HISTORICO = int(
    os.getenv(
        "MIN_JOGOS_HISTORICO",
        "20"
    )
)


# ============================================================
# JOGOS FUTUROS
# ============================================================

JANELA_HORAS = int(
    os.getenv(
        "JANELA_HORAS",
        "24"
    )
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv(
        "INTERVALO_ANALISE_SEGUNDOS",
        "900"
    )
)

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv(
        "MAX_JOGOS_ANALISADOS_CICLO",
        "4"
    )
)


# ============================================================
# GESTÃO
# ============================================================

TOTAL_BANCAS = int(
    os.getenv(
        "TOTAL_BANCAS",
        "3"
    )
)

ENTRADA_INICIAL = float(
    os.getenv(
        "ENTRADA_INICIAL",
        "10"
    )
)

MULTIPLICADOR_GALE = float(
    os.getenv(
        "MULTIPLICADOR_GALE",
        "2"
    )
)

MAX_GALES = int(
    os.getenv(
        "MAX_GALES",
        "2"
    )
)


# ============================================================
# RATE LIMIT
# ============================================================

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

# Curto porque mercado pode aparecer perto do jogo.
CACHE_MERCADO_TTL = int(
    os.getenv(
        "CACHE_MERCADO_TTL",
        "600"
    )
)


# ============================================================
# ARQUIVOS V16
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS_V16",
    "stats_v16.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS_V16",
    "sinais_v16.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE_V16",
    "cache_v16.json"
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

if API_KEY:

    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-cantos-v16/1.0"
    })


# ============================================================
# LOCKS
# ============================================================

lock_dados = threading.RLock()
lock_api = threading.Lock()

stop_event = threading.Event()

historico_requisicoes = deque()

ultima_requisicao = 0.0


# ============================================================
# RATE LIMIT
# ============================================================

def aguardar_rate_limit():

    global ultima_requisicao

    with lock_api:

        agora = time.time()

        while (
            historico_requisicoes
            and
            agora - historico_requisicoes[0] >= 60
        ):
            historico_requisicoes.popleft()

        if (
            len(historico_requisicoes)
            >= API_MAX_REQUESTS_PER_MINUTE
        ):

            espera = (
                60
                - (
                    agora
                    - historico_requisicoes[0]
                )
                + 1
            )

            logger.info(
                "⏳ Rate limit interno. Esperando %.1fs.",
                espera
            )

            time.sleep(
                max(1, espera)
            )

        agora = time.time()

        intervalo = (
            agora
            - ultima_requisicao
        )

        if intervalo < API_MIN_INTERVAL_SECONDS:

            time.sleep(
                API_MIN_INTERVAL_SECONDS
                - intervalo
            )

        agora = time.time()

        historico_requisicoes.append(
            agora
        )

        ultima_requisicao = agora


# ============================================================
# API
# ============================================================

def api_get(
    endpoint,
    params=None,
    tentativas=3
):

    if not API_KEY:

        logger.error(
            "❌ API KEY não configurada."
        )

        return None

    url = f"{BASE_API}{endpoint}"

    for tentativa in range(
        1,
        tentativas + 1
    ):

        aguardar_rate_limit()

        try:

            resposta = session.get(
                url,
                params=params,
                timeout=30
            )

            if resposta.status_code == 429:

                logger.warning(
                    "⚠️ API 429. Esperando %ss.",
                    API_BACKOFF_429
                )

                time.sleep(
                    API_BACKOFF_429
                )

                continue

            if resposta.status_code == 403:

                logger.error(
                    "❌ API 403: %s",
                    resposta.text[:300]
                )

                return None

            if resposta.status_code == 400:

                logger.error(
                    "❌ API 400: %s",
                    resposta.text[:300]
                )

                return None

            resposta.raise_for_status()

            return resposta.json()

        except requests.RequestException as erro:

            logger.error(
                "❌ API %s | tentativa %s/%s | %s",
                endpoint,
                tentativa,
                tentativas,
                erro
            )

            if tentativa < tentativas:
                time.sleep(5)

        except ValueError:

            logger.error(
                "❌ Resposta JSON inválida: %s",
                endpoint
            )

            return None

    return None


# ============================================================
# JSON
# ============================================================

def carregar_json(
    caminho,
    padrao
):

    if not os.path.exists(caminho):
        return padrao

    try:

        with open(
            caminho,
            "r",
            encoding="utf-8"
        ) as arquivo:

            return json.load(
                arquivo
            )

    except Exception as erro:

        logger.error(
            "❌ Erro lendo %s: %s",
            caminho,
            erro
        )

        return padrao


def salvar_json(
    caminho,
    dados
):

    temporario = (
        caminho
        + ".tmp"
    )

    try:

        with open(
            temporario,
            "w",
            encoding="utf-8"
        ) as arquivo:

            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporario,
            caminho
        )

    except Exception as erro:

        logger.error(
            "❌ Erro salvando %s: %s",
            caminho,
            erro
        )


# ============================================================
# ESTADO
# ============================================================

stats = carregar_json(
    ARQUIVO_STATS,
    {
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "total_resolvidos": 0,
        "por_gale": {}
    }
)

stats.setdefault(
    "por_gale",
    {}
)


sinais = carregar_json(
    ARQUIVO_SINAIS,
    []
)


cache_persistente = carregar_json(
    ARQUIVO_CACHE,
    {
        "historicos": {},
        "mercados": {}
    }
)

cache_persistente.setdefault(
    "historicos",
    {}
)

cache_persistente.setdefault(
    "mercados",
    {}
)


cache_fixtures = {
    "timestamp": 0,
    "dados": []
}


cursor_fixture = 0


# ============================================================
# UTILIDADES
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


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def extrair_lista(dados):

    if dados is None:
        return []

    if isinstance(
        dados,
        list
    ):
        return dados

    if not isinstance(
        dados,
        dict
    ):
        return []

    for chave in (
        "data",
        "fixtures",
        "results",
        "response"
    ):

        valor = dados.get(
            chave
        )

        if isinstance(
            valor,
            list
        ):
            return valor

        if isinstance(
            valor,
            dict
        ):

            for sub in (
                "data",
                "fixtures",
                "results",
                "response"
            ):

                resultado = valor.get(
                    sub
                )

                if isinstance(
                    resultado,
                    list
                ):
                    return resultado

    return []


def percentual(
    quantidade,
    total
):

    if total <= 0:
        return 0.0

    return (
        quantidade
        / total
    )


def percentual_texto(valor):

    return (
        f"{valor * 100:.1f}%"
    )


# ============================================================
# BANCAS
# ============================================================

def criar_bancas():

    return [
        {
            "id": i,
            "ocupada": False,
            "fixture_id": None,
            "gale": 0,
            "entrada": ENTRADA_INICIAL
        }

        for i in range(
            1,
            TOTAL_BANCAS + 1
        )
    ]


bancas = criar_bancas()


def reconstruir_bancas():

    for sinal in sinais:

        if sinal.get(
            "status"
        ) != "PENDENTE":

            continue

        banca_id = sinal.get(
            "banca"
        )

        for banca in bancas:

            if banca["id"] == banca_id:

                banca["ocupada"] = True

                banca["fixture_id"] = str(
                    sinal["fixture_id"]
                )

                banca["gale"] = int(
                    sinal.get(
                        "gale",
                        0
                    )
                )

                banca["entrada"] = float(
                    sinal.get(
                        "entrada",
                        ENTRADA_INICIAL
                    )
                )


reconstruir_bancas()


def obter_banca_livre():

    for banca in bancas:

        if not banca[
            "ocupada"
        ]:

            return banca

    return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(
    mensagem
):

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        logger.warning(
            "⚠️ Telegram não configurado."
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        resposta = requests.post(
            url,
            data={
                "chat_id":
                    CHAT_ID,

                "text":
                    mensagem,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True
            },
            timeout=20
        )

        if not resposta.ok:

            logger.error(
                "❌ Telegram: %s",
                resposta.text[:500]
            )

        return resposta.ok

    except Exception as erro:

        logger.error(
            "❌ Telegram: %s",
            erro
        )

        return False


# ============================================================
# FIXTURES FUTUROS
# ============================================================

def buscar_fixtures():

    agora = time.time()

    if (
        cache_fixtures["dados"]
        and
        agora
        - cache_fixtures["timestamp"]
        < CACHE_FIXTURES_TTL
    ):

        return cache_fixtures[
            "dados"
        ]

    inicio = datetime.now(
        timezone.utc
    )

    fim = inicio + timedelta(
        hours=JANELA_HORAS
    )

    dados = api_get(
        "/fixtures",
        params={
            "start_time":
                int(
                    inicio.timestamp()
                ),

            "end_time":
                int(
                    fim.timestamp()
                ),

            "status":
                "scheduled",

            "per_page":
                100
        }
    )

    jogos = extrair_lista(
        dados
    )

    cache_fixtures[
        "timestamp"
    ] = agora

    cache_fixtures[
        "dados"
    ] = jogos

    logger.info(
        "📅 %s jogos nas próximas %sh.",
        len(jogos),
        JANELA_HORAS
    )

    return jogos


# ============================================================
# TIMES
# ============================================================

def extrair_times(
    fixture
):

    if not isinstance(
        fixture,
        dict
    ):

        return (
            None,
            None,
            "Casa",
            "Fora"
        )

    teams = fixture.get(
        "teams",
        {}
    )

    if not isinstance(
        teams,
        dict
    ):

        teams = {}

    home = teams.get(
        "home",
        {}
    )

    away = teams.get(
        "away",
        {}
    )

    home_id = None
    away_id = None

    home_nome = "Casa"
    away_nome = "Fora"

    if isinstance(
        home,
        dict
    ):

        home_id = (
            home.get("id")
            or home.get("team_id")
        )

        home_nome = (
            home.get("name")
            or home.get("team_name")
            or "Casa"
        )

    if isinstance(
        away,
        dict
    ):

        away_id = (
            away.get("id")
            or away.get("team_id")
        )

        away_nome = (
            away.get("name")
            or away.get("team_name")
            or "Fora"
        )

    return (
        home_id,
        away_id,
        str(home_nome),
        str(away_nome)
    )


# ============================================================
# BET365 - EXTRAIR BOOKMAKER
# ============================================================

def extrair_bet365(
    resposta
):

    if not isinstance(
        resposta,
        dict
    ):

        return None

    data = resposta.get(
        "data"
    )

    if not isinstance(
        data,
        dict
    ):

        return None

    bookmakers = data.get(
        "bookmakers"
    )

    if not isinstance(
        bookmakers,
        list
    ):

        return None

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):

            continue

        slug = str(
            bookmaker.get(
                "slug",
                ""
            )
        ).lower()

        if slug == "bet365":

            return bookmaker

    return None


# ============================================================
# VALIDAR MERCADO REAL
#
# Não basta existir a chave.
#
# Precisamos encontrar:
#   line
#   +
#   over
#   +
#   under
#
# em opening / closing / inplay.
# ============================================================

def validar_linha_real(
    mercado
):

    if not isinstance(
        mercado,
        dict
    ):

        return False, None

    # Preferimos a linha mais atual.
    # Para jogo futuro:
    # closing = preço pré-jogo mais recente.
    #
    # Depois opening.
    # inplay fica como fallback.

    for fase in (
        "closing",
        "opening",
        "inplay"
    ):

        item = mercado.get(
            fase
        )

        if not isinstance(
            item,
            dict
        ):

            continue

        linha = numero(
            item.get(
                "line"
            )
        )

        over = numero(
            item.get(
                "over"
            )
        )

        under = numero(
            item.get(
                "under"
            )
        )

        if (
            linha is not None
            and
            over is not None
            and
            under is not None
            and
            over > 1
            and
            under > 1
        ):

            return True, {
                "fase":
                    fase,

                "linha":
                    linha,

                "over":
                    over,

                "under":
                    under
            }

    return False, None


# ============================================================
# CONSULTA EXATA DO MERCADO BET365
# ============================================================

def consultar_mercado_bet365(
    fixture_id,
    market,
    chave_esperada
):

    resposta = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "bookmakers":
                "bet365",

            "market":
                market
        }
    )

    bookmaker = extrair_bet365(
        resposta
    )

    if not bookmaker:

        return {
            "disponivel":
                False,

            "motivo":
                "Bet365 não retornada",

            "detalhes":
                None
        }

    odds = bookmaker.get(
        "odds"
    )

    if not isinstance(
        odds,
        dict
    ):

        return {
            "disponivel":
                False,

            "motivo":
                "objeto odds ausente",

            "detalhes":
                None
        }

    mercado = odds.get(
        chave_esperada
    )

    if not isinstance(
        mercado,
        dict
    ):

        return {
            "disponivel":
                False,

            "motivo":
                f"{chave_esperada} ausente",

            "detalhes":
                None
        }

    disponivel, detalhes = (
        validar_linha_real(
            mercado
        )
    )

    if not disponivel:

        return {
            "disponivel":
                False,

            "motivo":
                "sem linha + over + under válidos",

            "detalhes":
                None
        }

    return {
        "disponivel":
            True,

        "motivo":
            "ok",

        "detalhes":
            detalhes
    }


# ============================================================
# VERIFICAR BET365 HT + FT
#
# FT:
# market=corner
# chave=corner_line
#
# HT:
# market=corner_half
# chave=corner_line_half
# ============================================================

def verificar_mercados_bet365(
    fixture_id
):

    chave_cache = str(
        fixture_id
    )

    agora = time.time()

    cache = cache_persistente[
        "mercados"
    ].get(
        chave_cache
    )

    if cache:

        idade = (
            agora
            - cache.get(
                "timestamp",
                0
            )
        )

        if idade < CACHE_MERCADO_TTL:

            logger.info(
                "📦 Mercado Bet365 em cache | %s",
                fixture_id
            )

            return cache.get(
                "dados",
                {}
            )


    # ========================================================
    # FT
    # ========================================================

    logger.info(
        "🎰 Bet365 FT | fixture %s",
        fixture_id
    )

    ft = consultar_mercado_bet365(
        fixture_id,
        "corner",
        "corner_line"
    )


    # Se nem FT existe, economizamos uma requisição.
    if not ft[
        "disponivel"
    ]:

        resultado = {
            "ft": False,
            "ht": False,
            "ft_detalhes": None,
            "ht_detalhes": None,
            "motivo":
                f"FT: {ft['motivo']}"
        }

        cache_persistente[
            "mercados"
        ][chave_cache] = {
            "timestamp":
                agora,

            "dados":
                resultado
        }

        salvar_json(
            ARQUIVO_CACHE,
            cache_persistente
        )

        logger.info(
            "⛔ Bet365 fixture %s | "
            "sem mercado real FT.",
            fixture_id
        )

        return resultado


    # ========================================================
    # HT
    # ========================================================

    logger.info(
        "🎰 Bet365 HT | fixture %s",
        fixture_id
    )

    ht = consultar_mercado_bet365(
        fixture_id,
        "corner_half",
        "corner_line_half"
    )


    resultado = {
        "ft":
            bool(
                ft["disponivel"]
            ),

        "ht":
            bool(
                ht["disponivel"]
            ),

        "ft_detalhes":
            ft.get(
                "detalhes"
            ),

        "ht_detalhes":
            ht.get(
                "detalhes"
            ),

        "motivo":
            "ok"
            if (
                ft["disponivel"]
                and
                ht["disponivel"]
            )
            else (
                f"HT: {ht['motivo']}"
            )
    }


    cache_persistente[
        "mercados"
    ][chave_cache] = {
        "timestamp":
            agora,

        "dados":
            resultado
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )


    logger.info(
        "🎰 BET365 %s | "
        "FT=%s | HT=%s",
        fixture_id,
        resultado["ft"],
        resultado["ht"]
    )

    return resultado


# ============================================================
# HISTÓRICO TIME
# ============================================================

def buscar_historico_time(
    team_id
):

    chave = str(
        team_id
    )

    agora = time.time()

    item = cache_persistente[
        "historicos"
    ].get(
        chave
    )

    if item:

        idade = (
            agora
            - item.get(
                "timestamp",
                0
            )
        )

        if idade < CACHE_HISTORICO_TTL:

            return item.get(
                "dados",
                []
            )

    logger.info(
        "📚 Buscando %s jogos do time %s.",
        QTD_HISTORICO_TIME,
        team_id
    )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status":
                "finished",

            "per_page":
                QTD_HISTORICO_TIME
        }
    )

    jogos = extrair_lista(
        dados
    )

    jogos = jogos[
        :QTD_HISTORICO_TIME
    ]


    cache_persistente[
        "historicos"
    ][chave] = {
        "timestamp":
            agora,

        "dados":
            jogos
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    return jogos


# ============================================================
# ID HISTÓRICO
# ============================================================

def obter_id_jogo(
    jogo
):

    if not isinstance(
        jogo,
        dict
    ):

        return None

    fixture_id = (
        jogo.get("id")
        or
        jogo.get("fixture_id")
    )

    if fixture_id is not None:

        return str(
            fixture_id
        )

    fixture = jogo.get(
        "fixture"
    )

    if isinstance(
        fixture,
        dict
    ):

        fixture_id = fixture.get(
            "id"
        )

        if fixture_id is not None:

            return str(
                fixture_id
            )

    return None


# ============================================================
# 20 + 20 SEM DUPLICADOS
# ============================================================

def combinar_historicos(
    home,
    away
):

    resultado = []

    vistos = set()

    for jogo in (
        list(home)
        + list(away)
    ):

        fixture_id = obter_id_jogo(
            jogo
        )

        if fixture_id:

            if fixture_id in vistos:
                continue

            vistos.add(
                fixture_id
            )

        resultado.append(
            jogo
        )

    return resultado


# ============================================================
# EXTRAIR CANTOS
# ============================================================

def extrair_cantos(
    jogo
):

    if not isinstance(
        jogo,
        dict
    ):

        return None

    corners = jogo.get(
        "corners"
    )

    if not isinstance(
        corners,
        dict
    ):

        return None

    ft_home = numero(
        corners.get(
            "home"
        )
    )

    ft_away = numero(
        corners.get(
            "away"
        )
    )

    ht_home = numero(
        corners.get(
            "half_home"
        )
    )

    ht_away = numero(
        corners.get(
            "half_away"
        )
    )

    if (
        ft_home is None
        or
        ft_away is None
        or
        ht_home is None
        or
        ht_away is None
    ):

        return None

    return {
        "ht":
            ht_home
            + ht_away,

        "ft":
            ft_home
            + ft_away
    }


# ============================================================
# RESULTADOS
# ============================================================

def resultado_under_ht(
    cantos
):

    if cantos < LINHA_UNDER_CANTOS_HT:
        return "WIN"

    if cantos == LINHA_UNDER_CANTOS_HT:
        return "VOID"

    return "LOSS"


def resultado_over_ft(
    cantos
):

    if cantos > LINHA_OVER_CANTOS_FT:
        return "WIN"

    if cantos == LINHA_OVER_CANTOS_FT:
        return "VOID"

    return "LOSS"


# ============================================================
# HISTÓRICO
#
# CORREÇÃO V16:
#
# Percentual é calculado sobre TODOS os jogos válidos.
#
# Exemplo:
# 14 WIN
# 3 VOID
# 3 LOSS
#
# Taxa WIN = 14/20 = 70%
#
# VOID NÃO É WIN.
# VOID NÃO SOME DO DENOMINADOR.
# ============================================================

def analisar_historico(
    jogos
):

    registros = []

    ignorados = 0

    for jogo in jogos:

        cantos = extrair_cantos(
            jogo
        )

        if not cantos:

            ignorados += 1
            continue

        under = resultado_under_ht(
            cantos["ht"]
        )

        over = resultado_over_ft(
            cantos["ft"]
        )

        registros.append({
            "ht":
                cantos["ht"],

            "ft":
                cantos["ft"],

            "under":
                under,

            "over":
                over
        })


    total = len(
        registros
    )

    if total == 0:
        return None


    # ========================================================
    # UNDER
    # ========================================================

    under_wins = sum(
        r["under"] == "WIN"
        for r in registros
    )

    under_voids = sum(
        r["under"] == "VOID"
        for r in registros
    )

    under_losses = sum(
        r["under"] == "LOSS"
        for r in registros
    )

    taxa_under = percentual(
        under_wins,
        total
    )


    # ========================================================
    # OVER
    # ========================================================

    over_wins = sum(
        r["over"] == "WIN"
        for r in registros
    )

    over_voids = sum(
        r["over"] == "VOID"
        for r in registros
    )

    over_losses = sum(
        r["over"] == "LOSS"
        for r in registros
    )

    taxa_over = percentual(
        over_wins,
        total
    )


    # ========================================================
    # COMBINAÇÃO
    # ========================================================

    combinada_wins = 0
    combinada_voids = 0
    combinada_losses = 0

    for r in registros:

        if (
            r["under"] == "LOSS"
            or
            r["over"] == "LOSS"
        ):

            combinada_losses += 1

        elif (
            r["under"] == "VOID"
            or
            r["over"] == "VOID"
        ):

            combinada_voids += 1

        else:

            combinada_wins += 1


    taxa_combinada = percentual(
        combinada_wins,
        total
    )


    media_ht = (
        sum(
            r["ht"]
            for r in registros
        )
        / total
    )

    media_ft = (
        sum(
            r["ft"]
            for r in registros
        )
        / total
    )


    return {
        "total":
            total,

        "ignorados":
            ignorados,

        "under_wins":
            under_wins,

        "under_voids":
            under_voids,

        "under_losses":
            under_losses,

        "taxa_under":
            taxa_under,

        "over_wins":
            over_wins,

        "over_voids":
            over_voids,

        "over_losses":
            over_losses,

        "taxa_over":
            taxa_over,

        "combinada_wins":
            combinada_wins,

        "combinada_voids":
            combinada_voids,

        "combinada_losses":
            combinada_losses,

        "taxa_combinada":
            taxa_combinada,

        "media_ht":
            media_ht,

        "media_ft":
            media_ft
    }


# ============================================================
# FILTROS
# ============================================================

def passou_filtros(
    analise
):

    if not analise:
        return False

    if (
        analise["total"]
        < MIN_JOGOS_HISTORICO
    ):

        logger.info(
            "⛔ Amostra %s < mínimo %s.",
            analise["total"],
            MIN_JOGOS_HISTORICO
        )

        return False

    if (
        analise["taxa_under"]
        < MIN_TAXA_UNDER_HT
    ):

        return False

    if (
        analise["taxa_over"]
        < MIN_TAXA_OVER_FT
    ):

        return False

    if (
        analise["taxa_combinada"]
        < MIN_TAXA_COMBINADA
    ):

        return False

    return True


# ============================================================
# JÁ USADO
# ============================================================

def fixture_ja_utilizado(
    fixture_id
):

    fixture_id = str(
        fixture_id
    )

    return any(
        str(
            sinal.get(
                "fixture_id"
            )
        )
        == fixture_id

        for sinal in sinais
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(
    fixture,
    analise,
    mercados,
    banca
):

    fixture_id = str(
        fixture.get(
            "id"
        )
    )

    (
        home_id,
        away_id,
        home,
        away
    ) = extrair_times(
        fixture
    )


    entrada = (
        ENTRADA_INICIAL
        *
        (
            MULTIPLICADOR_GALE
            ** banca["gale"]
        )
    )


    sinal = {
        "fixture_id":
            fixture_id,

        "home_id":
            home_id,

        "away_id":
            away_id,

        "home":
            home,

        "away":
            away,

        "bookmaker":
            "bet365",

        "mercado":
            (
                "Under 6 HT "
                "+ Over 7 FT"
            ),

        "linha_under_ht":
            LINHA_UNDER_CANTOS_HT,

        "linha_over_ft":
            LINHA_OVER_CANTOS_FT,

        "taxa_under":
            round(
                analise[
                    "taxa_under"
                ] * 100,
                2
            ),

        "taxa_over":
            round(
                analise[
                    "taxa_over"
                ] * 100,
                2
            ),

        "taxa_combinada":
            round(
                analise[
                    "taxa_combinada"
                ] * 100,
                2
            ),

        "amostra":
            analise["total"],

        "bet365_ft":
            mercados.get(
                "ft_detalhes"
            ),

        "bet365_ht":
            mercados.get(
                "ht_detalhes"
            ),

        "banca":
            banca["id"],

        "gale":
            banca["gale"],

        "entrada":
            entrada,

        "status":
            "PENDENTE",

        "resultado":
            None,

        "criado_em":
            agora_iso()
    }


    with lock_dados:

        sinais.append(
            sinal
        )

        banca[
            "ocupada"
        ] = True

        banca[
            "fixture_id"
        ] = fixture_id

        banca[
            "entrada"
        ] = entrada

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )


    ht_market = mercados.get(
        "ht_detalhes",
        {}
    ) or {}

    ft_market = mercados.get(
        "ft_detalhes",
        {}
    ) or {}


    mensagem = (

        "🚨 <b>SINAL V16</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(home)}"
        f" x "
        f"{html.escape(away)}"
        f"</b>\n\n"


        "1️⃣ <b>ESCANTEIOS 1º TEMPO</b>\n"

        f"📉 Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f}\n"

        f"📊 Histórico: "
        f"<b>"
        f"{percentual_texto(analise['taxa_under'])}"
        f"</b>\n"

        f"📈 Média HT: "
        f"{analise['media_ht']:.2f}\n"

        f"✅ {analise['under_wins']} WIN | "
        f"⚪ {analise['under_voids']} VOID | "
        f"❌ {analise['under_losses']} LOSS\n\n"


        "2️⃣ <b>ESCANTEIOS JOGO INTEIRO</b>\n"

        f"📈 Over "
        f"{LINHA_OVER_CANTOS_FT:.1f}\n"

        f"📊 Histórico: "
        f"<b>"
        f"{percentual_texto(analise['taxa_over'])}"
        f"</b>\n"

        f"📈 Média FT: "
        f"{analise['media_ft']:.2f}\n"

        f"✅ {analise['over_wins']} WIN | "
        f"⚪ {analise['over_voids']} VOID | "
        f"❌ {analise['over_losses']} LOSS\n\n"


        "🔥 <b>COMBINAÇÃO</b>\n"

        f"Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f} HT "
        f"+ Over "
        f"{LINHA_OVER_CANTOS_FT:.1f} FT\n"

        f"📊 Ocorrência conjunta: "
        f"<b>"
        f"{percentual_texto(analise['taxa_combinada'])}"
        f"</b>\n"

        f"✅ {analise['combinada_wins']} WIN | "
        f"⚪ {analise['combinada_voids']} VOID | "
        f"❌ {analise['combinada_losses']} LOSS\n\n"


        "🎰 <b>BET365 CONFIRMADA</b>\n"

        f"✅ Mercado HT real disponível\n"
        f"↳ Linha atual API: "
        f"{ht_market.get('linha', '-')}\n"

        f"✅ Mercado FT real disponível\n"
        f"↳ Linha atual API: "
        f"{ft_market.get('linha', '-')}\n\n"


        "📚 <b>HISTÓRICO</b>\n"

        f"Jogos válidos: "
        f"{analise['total']}\n"

        f"Jogos ignorados: "
        f"{analise['ignorados']}\n"

        f"Exigência mínima: "
        f"{MIN_JOGOS_HISTORICO}\n\n"


        "💰 <b>GESTÃO</b>\n"

        f"🏦 Banca: "
        f"{banca['id']}\n"

        f"🔄 Gale: "
        f"{banca['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )


    telegram(
        mensagem
    )


    logger.info(
        "🚨 SINAL V16 | %s x %s",
        home,
        away
    )


# ============================================================
# FIXTURE INDIVIDUAL
# ============================================================

def buscar_fixture(
    fixture_id
):

    dados = api_get(
        f"/fixtures/{fixture_id}"
    )

    if not isinstance(
        dados,
        dict
    ):

        return None

    data = dados.get(
        "data"
    )

    if isinstance(
        data,
        dict
    ):

        return data

    response = dados.get(
        "response"
    )

    if (
        isinstance(
            response,
            list
        )
        and response
    ):

        return response[0]

    return dados


def fixture_finalizado(
    fixture
):

    status = str(
        fixture.get(
            "status",
            ""
        )
    ).lower()

    return status in (
        "finished",
        "ft",
        "completed",
        "ended"
    )


# ============================================================
# BANCA
# ============================================================

def liberar_banca(
    banca,
    resultado
):

    if resultado == "WIN":

        banca[
            "gale"
        ] = 0

    elif resultado == "LOSS":

        if (
            banca["gale"]
            < MAX_GALES
        ):

            banca[
                "gale"
            ] += 1

        else:

            banca[
                "gale"
            ] = 0

    # PUSH mantém Gale.

    banca[
        "ocupada"
    ] = False

    banca[
        "fixture_id"
    ] = None

    banca[
        "entrada"
    ] = (
        ENTRADA_INICIAL
        *
        (
            MULTIPLICADOR_GALE
            ** banca["gale"]
        )
    )


# ============================================================
# RESOLVER
# ============================================================

def resolver_sinal(
    sinal,
    fixture
):

    cantos = extrair_cantos(
        fixture
    )

    if not cantos:

        logger.warning(
            "⚠️ Fixture %s sem cantos.",
            sinal["fixture_id"]
        )

        return


    under = resultado_under_ht(
        cantos["ht"]
    )

    over = resultado_over_ft(
        cantos["ft"]
    )


    if (
        under == "LOSS"
        or
        over == "LOSS"
    ):

        resultado = "LOSS"

    elif (
        under == "VOID"
        or
        over == "VOID"
    ):

        resultado = "PUSH"

    else:

        resultado = "WIN"


    sinal[
        "status"
    ] = "RESOLVIDO"

    sinal[
        "resultado"
    ] = resultado

    sinal[
        "resultado_under_ht"
    ] = under

    sinal[
        "resultado_over_ft"
    ] = over

    sinal[
        "cantos_ht_final"
    ] = cantos["ht"]

    sinal[
        "cantos_ft_final"
    ] = cantos["ft"]

    sinal[
        "resolvido_em"
    ] = agora_iso()


    if resultado == "WIN":

        stats[
            "wins"
        ] = (
            stats.get(
                "wins",
                0
            )
            + 1
        )

    elif resultado == "LOSS":

        stats[
            "losses"
        ] = (
            stats.get(
                "losses",
                0
            )
            + 1
        )

    else:

        stats[
            "pushes"
        ] = (
            stats.get(
                "pushes",
                0
            )
            + 1
        )


    stats[
        "total_resolvidos"
    ] = (
        stats.get(
            "total_resolvidos",
            0
        )
        + 1
    )


    gale = str(
        sinal.get(
            "gale",
            0
        )
    )

    stats[
        "por_gale"
    ].setdefault(
        gale,
        {
            "wins": 0,
            "losses": 0,
            "pushes": 0
        }
    )


    if resultado == "WIN":

        stats[
            "por_gale"
        ][gale][
            "wins"
        ] += 1

    elif resultado == "LOSS":

        stats[
            "por_gale"
        ][gale][
            "losses"
        ] += 1

    else:

        stats[
            "por_gale"
        ][gale][
            "pushes"
        ] += 1


    banca = next(
        (
            b
            for b in bancas
            if b["id"]
            == sinal["banca"]
        ),
        None
    )


    if banca:

        liberar_banca(
            banca,
            resultado
        )


    salvar_json(
        ARQUIVO_SINAIS,
        sinais
    )

    salvar_json(
        ARQUIVO_STATS,
        stats
    )


    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "⚪"
    }.get(
        resultado,
        "ℹ️"
    )


    mensagem = (

        f"{emoji} "
        f"<b>{resultado} V16</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"

        f"1️⃣ Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f} HT\n"

        f"🚩 Cantos HT: "
        f"{cantos['ht']:.0f}\n"

        f"Resultado: "
        f"<b>{under}</b>\n\n"

        f"2️⃣ Over "
        f"{LINHA_OVER_CANTOS_FT:.1f} FT\n"

        f"🚩 Cantos FT: "
        f"{cantos['ft']:.0f}\n"

        f"Resultado: "
        f"<b>{over}</b>\n\n"

        f"🔥 Resultado: "
        f"<b>{resultado}</b>\n\n"

        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale usado: "
        f"{sinal['gale']}"
    )


    telegram(
        mensagem
    )


# ============================================================
# RESULTADOS PENDENTES
# ============================================================

def verificar_resultados():

    pendentes = [
        sinal
        for sinal in sinais
        if sinal.get(
            "status"
        ) == "PENDENTE"
    ]

    for sinal in pendentes:

        fixture = buscar_fixture(
            sinal[
                "fixture_id"
            ]
        )

        if not fixture:
            continue

        if not fixture_finalizado(
            fixture
        ):
            continue

        resolver_sinal(
            sinal,
            fixture
        )


# ============================================================
# ANALISAR JOGO
# ============================================================

def analisar_fixture(
    fixture
):

    fixture_id = fixture.get(
        "id"
    )

    if fixture_id is None:
        return

    if fixture_ja_utilizado(
        fixture_id
    ):
        return


    (
        home_id,
        away_id,
        home,
        away
    ) = extrair_times(
        fixture
    )


    if (
        not home_id
        or
        not away_id
    ):

        return


    logger.info(
        "🔎 V16 | %s x %s",
        home,
        away
    )


    # ========================================================
    # PRIMEIRO BET365
    # ========================================================

    mercados = verificar_mercados_bet365(
        fixture_id
    )


    if not mercados.get(
        "ft",
        False
    ):

        logger.info(
            "⛔ %s x %s | "
            "sem cantos FT reais Bet365.",
            home,
            away
        )

        return


    if not mercados.get(
        "ht",
        False
    ):

        logger.info(
            "⛔ %s x %s | "
            "sem cantos HT reais Bet365.",
            home,
            away
        )

        return


    # ========================================================
    # HISTÓRICO
    # ========================================================

    historico_home = buscar_historico_time(
        home_id
    )

    historico_away = buscar_historico_time(
        away_id
    )


    historico = combinar_historicos(
        historico_home,
        historico_away
    )


    analise = analisar_historico(
        historico
    )


    if not analise:
        return


    logger.info(
        "📊 %s x %s | "
        "Jogos=%s | "
        "Under=%.1f%% | "
        "Over=%.1f%% | "
        "Comb=%.1f%%",
        home,
        away,
        analise["total"],
        analise["taxa_under"] * 100,
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100
    )


    # ========================================================
    # 70%
    # ========================================================

    if not passou_filtros(
        analise
    ):

        logger.info(
            "⛔ REPROVADO | "
            "%s x %s",
            home,
            away
        )

        return


    banca = obter_banca_livre()

    if not banca:

        return


    logger.info(
        "🔥 APROVADO V16 | "
        "%s x %s",
        home,
        away
    )


    enviar_sinal(
        fixture,
        analise,
        mercados,
        banca
    )


# ============================================================
# CICLO
# ============================================================

def ciclo():

    global cursor_fixture

    logger.info(
        "========================================"
    )

    logger.info(
        "🔄 CICLO V16"
    )


    try:

        verificar_resultados()


        if obter_banca_livre() is None:

            logger.info(
                "🏦 Todas as bancas ocupadas."
            )

            return


        fixtures = buscar_fixtures()


        if not fixtures:

            logger.info(
                "📭 Sem fixtures."
            )

            return


        total = len(
            fixtures
        )


        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )


        for _ in range(
            quantidade
        ):

            if obter_banca_livre() is None:
                break

            if cursor_fixture >= total:
                cursor_fixture = 0

            fixture = fixtures[
                cursor_fixture
            ]

            cursor_fixture += 1

            try:

                analisar_fixture(
                    fixture
                )

            except Exception:

                logger.exception(
                    "❌ Erro analisando jogo."
                )


    except Exception:

        logger.exception(
            "❌ Erro no ciclo."
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "🤖 ROBÔ V16 INICIADO"
    )

    logger.info(
        "📊 Under mínimo: %.1f%%",
        MIN_TAXA_UNDER_HT * 100
    )

    logger.info(
        "📊 Over mínimo: %.1f%%",
        MIN_TAXA_OVER_FT * 100
    )

    logger.info(
        "🔥 Combinada mínima: %.1f%%",
        MIN_TAXA_COMBINADA * 100
    )

    logger.info(
        "📚 Mínimo jogos válidos: %s",
        MIN_JOGOS_HISTORICO
    )


    while not stop_event.is_set():

        ciclo()

        stop_event.wait(
            INTERVALO_ANALISE_SEGUNDOS
        )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    wins = stats.get(
        "wins",
        0
    )

    losses = stats.get(
        "losses",
        0
    )

    total = (
        wins
        + losses
    )

    taxa = (
        wins / total * 100
        if total
        else 0
    )


    return jsonify({
        "status":
            "online",

        "versao":
            "V16",

        "estrategia":
            "Under 6 HT + Over 7 FT",

        "bookmaker":
            "Bet365",

        "wins":
            wins,

        "losses":
            losses,

        "pushes":
            stats.get(
                "pushes",
                0
            ),

        "assertividade":
            round(
                taxa,
                2
            ),

        "bancas":
            bancas
    })


# ============================================================
# STATUS DETALHADO
#
# IMPORTANTE:
# aqui você consegue conferir se o Render
# realmente carregou os valores certos.
# ============================================================

@app.route("/status")
def status():

    return jsonify({
        "online":
            True,

        "versao":
            "V16",

        "bookmaker":
            "bet365",

        "mercado_ht_api":
            "corner_half",

        "mercado_ft_api":
            "corner",

        "linha_under_ht":
            LINHA_UNDER_CANTOS_HT,

        "linha_over_ft":
            LINHA_OVER_CANTOS_FT,

        "min_taxa_under_ht":
            MIN_TAXA_UNDER_HT,

        "min_taxa_over_ft":
            MIN_TAXA_OVER_FT,

        "min_taxa_combinada":
            MIN_TAXA_COMBINADA,

        "qtd_historico_time":
            QTD_HISTORICO_TIME,

        "min_jogos_historico":
            MIN_JOGOS_HISTORICO,

        "janela_horas":
            JANELA_HORAS,

        "max_jogos_por_ciclo":
            MAX_JOGOS_ANALISADOS_CICLO,

        "total_bancas":
            TOTAL_BANCAS,

        "entrada_inicial":
            ENTRADA_INICIAL,

        "max_gales":
            MAX_GALES,

        "cache_mercado_ttl":
            CACHE_MERCADO_TTL,

        "api_max_requests_minuto":
            API_MAX_REQUESTS_PER_MINUTE
    })


@app.route("/stats")
def rota_stats():

    return jsonify(
        stats
    )


@app.route("/bancas")
def rota_bancas():

    return jsonify(
        bancas
    )


@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais[-100:]
    )


@app.route("/health")
def health():

    return jsonify({
        "status":
            "ok",

        "version":
            "V16"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v16"
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
    
