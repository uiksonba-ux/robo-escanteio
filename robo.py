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
# ROBÔ V9.3
# OVER ESCANTEIOS + OVER GOLS
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-v9.3")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))

# Estratégia
MARGEM_CANTOS = float(
    os.getenv("MARGEM_CANTOS", "0.5")
)

MARGEM_GOLS = float(
    os.getenv("MARGEM_GOLS", "0.5")
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

# Histórico
QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "10")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "10")
)

# Janela
JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv("MAX_JOGOS_ANALISADOS_CICLO", "4")
)

# Gestão
TOTAL_BANCAS = int(
    os.getenv("TOTAL_BANCAS", "3")
)

ENTRADA_INICIAL = float(
    os.getenv("ENTRADA_INICIAL", "10")
)

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(
    os.getenv("MAX_GALES", "2")
)

# API
API_MAX_REQUESTS_PER_MINUTE = int(
    os.getenv("API_MAX_REQUESTS_PER_MINUTE", "9")
)

API_MIN_INTERVAL_SECONDS = float(
    os.getenv("API_MIN_INTERVAL_SECONDS", "6.8")
)

API_BACKOFF_429 = int(
    os.getenv("API_BACKOFF_429", "65")
)

# Cache
CACHE_FIXTURES_TTL = int(
    os.getenv("CACHE_FIXTURES_TTL", "600")
)

CACHE_HISTORICO_TTL = int(
    os.getenv("CACHE_HISTORICO_TTL", "21600")
)

CACHE_ODDS_TTL = int(
    os.getenv("CACHE_ODDS_TTL", "1800")
)

BOOKMAKER = os.getenv(
    "BOOKMAKER",
    "bet365"
).lower()

DEBUG_ODDS = (
    os.getenv("DEBUG_ODDS", "true").lower()
    == "true"
)

# Arquivos
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
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-over-v9.3"
})


# ============================================================
# LOCKS / RATE LIMIT
# ============================================================

lock_dados = threading.RLock()
lock_api = threading.Lock()
stop_event = threading.Event()

historico_requisicoes = deque()
ultima_requisicao = 0.0


def aguardar_rate_limit():

    global ultima_requisicao

    with lock_api:

        agora = time.time()

        while (
            historico_requisicoes
            and agora - historico_requisicoes[0] >= 60
        ):
            historico_requisicoes.popleft()

        if (
            len(historico_requisicoes)
            >= API_MAX_REQUESTS_PER_MINUTE
        ):

            espera = (
                60
                - (agora - historico_requisicoes[0])
                + 1
            )

            logger.info(
                "⏳ Rate limit: aguardando %.1fs",
                espera
            )

            time.sleep(max(espera, 1))

        agora = time.time()

        intervalo = agora - ultima_requisicao

        if intervalo < API_MIN_INTERVAL_SECONDS:

            espera = (
                API_MIN_INTERVAL_SECONDS
                - intervalo
            )

            time.sleep(espera)

        agora = time.time()

        historico_requisicoes.append(agora)
        ultima_requisicao = agora


def api_get(endpoint, params=None, tentativas=3):

    url = f"{BASE_API}{endpoint}"

    for tentativa in range(1, tentativas + 1):

        aguardar_rate_limit()

        try:

            resposta = session.get(
                url,
                params=params,
                timeout=30
            )

            if resposta.status_code == 429:

                logger.warning(
                    "⚠️ API 429. Aguardando %ss.",
                    API_BACKOFF_429
                )

                time.sleep(API_BACKOFF_429)
                continue

            resposta.raise_for_status()

            return resposta.json()

        except requests.RequestException as e:

            logger.error(
                "❌ API %s | tentativa %s/%s | %s",
                endpoint,
                tentativa,
                tentativas,
                e
            )

            if tentativa < tentativas:
                time.sleep(5)

    return None


# ============================================================
# JSON
# ============================================================

def carregar_json(caminho, padrao):

    if not os.path.exists(caminho):
        return padrao

    try:

        with open(
            caminho,
            "r",
            encoding="utf-8"
        ) as arquivo:

            return json.load(arquivo)

    except Exception as e:

        logger.error(
            "Erro lendo %s: %s",
            caminho,
            e
        )

        return padrao


def salvar_json(caminho, dados):

    temporario = caminho + ".tmp"

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

    except Exception as e:

        logger.error(
            "Erro salvando %s: %s",
            caminho,
            e
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
        "lucro_teorico": 0.0,
        "por_gale": {
            "0": {"wins": 0, "losses": 0},
            "1": {"wins": 0, "losses": 0},
            "2": {"wins": 0, "losses": 0}
        }
    }
)

sinais = carregar_json(
    ARQUIVO_SINAIS,
    []
)

cache_persistente = carregar_json(
    ARQUIVO_CACHE,
    {
        "historicos": {}
    }
)

cache_fixtures = {
    "timestamp": 0,
    "dados": []
}

cache_odds = {}

cursor_fixture = 0


# ============================================================
# UTILITÁRIOS
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (TypeError, ValueError):
        return None


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def taxa(wins, losses):

    total = wins + losses

    if total == 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def extrair_lista(dados):

    if dados is None:
        return []

    if isinstance(dados, list):
        return dados

    if not isinstance(dados, dict):
        return []

    for chave in (
        "data",
        "fixtures",
        "results",
        "response"
    ):

        valor = dados.get(chave)

        if isinstance(valor, list):
            return valor

        if isinstance(valor, dict):

            for subchave in (
                "data",
                "fixtures",
                "results"
            ):

                subvalor = valor.get(subchave)

                if isinstance(subvalor, list):
                    return subvalor

    return []


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

        if sinal.get("status") != "PENDENTE":
            continue

        banco_id = sinal.get("banca")

        for banco in bancas:

            if banco["id"] == banco_id:

                banco["ocupada"] = True

                banco["fixture_id"] = str(
                    sinal["fixture_id"]
                )

                banco["gale"] = int(
                    sinal.get("gale", 0)
                )

                banco["entrada"] = float(
                    sinal.get(
                        "entrada",
                        ENTRADA_INICIAL
                    )
                )


reconstruir_bancas()


def banco_livre():

    for banco in bancas:

        if not banco["ocupada"]:
            return banco

    return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

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
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if not resposta.ok:

            logger.error(
                "Telegram: %s",
                resposta.text[:500]
            )

        return resposta.ok

    except Exception as e:

        logger.error(
            "Erro Telegram: %s",
            e
        )

        return False


# ============================================================
# FIXTURES
# ============================================================

def buscar_fixtures():

    agora = time.time()

    if (
        cache_fixtures["dados"]
        and agora - cache_fixtures["timestamp"]
        < CACHE_FIXTURES_TTL
    ):

        logger.info(
            "📦 Fixtures em cache: %s jogos.",
            len(cache_fixtures["dados"])
        )

        return cache_fixtures["dados"]

    inicio = datetime.now(timezone.utc)

    fim = inicio + timedelta(
        hours=JANELA_HORAS
    )

    dados = api_get(
        "/fixtures",
        params={
            "start_time": int(inicio.timestamp()),
            "end_time": int(fim.timestamp()),
            "status": "scheduled",
            "per_page": 100
        }
    )

    jogos = extrair_lista(dados)

    logger.info(
        "📅 %s jogos encontrados nas próximas %sh.",
        len(jogos),
        JANELA_HORAS
    )

    cache_fixtures["timestamp"] = agora
    cache_fixtures["dados"] = jogos

    return jogos


# ============================================================
# TIMES
# Estrutura confirmada no RAW da API
# ============================================================

def extrair_times(fixture):

    if not isinstance(fixture, dict):

        return (
            None,
            None,
            "Casa",
            "Fora"
        )

    teams = fixture.get("teams", {})

    if not isinstance(teams, dict):
        teams = {}

    home = teams.get("home", {})
    away = teams.get("away", {})

    home_id = None
    away_id = None

    home_nome = "Casa"
    away_nome = "Fora"

    if isinstance(home, dict):

        home_id = (
            home.get("id")
            or home.get("team_id")
        )

        home_nome = (
            home.get("name")
            or home.get("team_name")
            or "Casa"
        )

    if isinstance(away, dict):

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
# HISTÓRICO
# ============================================================

def buscar_historico_time(team_id):

    chave = str(team_id)
    agora = time.time()

    item = cache_persistente[
        "historicos"
    ].get(chave)

    if item:

        idade = (
            agora
            - item.get("timestamp", 0)
        )

        if idade < CACHE_HISTORICO_TTL:

            jogos = item.get(
                "dados",
                []
            )

            logger.info(
                "📦 Time %s: %s históricos no cache.",
                team_id,
                len(jogos)
            )

            return jogos

    logger.info(
        "📊 Buscando histórico do time %s...",
        team_id
    )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "per_page": QTD_HISTORICO_TIME
        }
    )

    jogos = extrair_lista(dados)

    jogos = jogos[
        :QTD_HISTORICO_TIME
    ]

    logger.info(
        "📊 Time %s: %s históricos obtidos.",
        team_id,
        len(jogos)
    )

    cache_persistente[
        "historicos"
    ][chave] = {
        "timestamp": agora,
        "dados": jogos
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    return jogos


# ============================================================
# EXTRAÇÃO DOS RESULTADOS
#
# CONFIRMADO NO LOG:
#
# goals = {
#     "home": 3,
#     "away": 2
# }
#
# corners = {
#     "home": 6,
#     "away": 3,
#     "half_home": 2,
#     "half_away": 3
# }
#
# half_home / half_away NÃO são somados.
# ============================================================

def extrair_totais(jogo):

    if not isinstance(jogo, dict):

        return (
            None,
            None
        )

    gols = None
    cantos = None

    # ========================================================
    # GOLS - FORMATO CONFIRMADO
    # ========================================================

    goals = jogo.get("goals")

    if isinstance(goals, dict):

        home_goals = numero(
            goals.get("home")
        )

        away_goals = numero(
            goals.get("away")
        )

        if (
            home_goals is not None
            and away_goals is not None
        ):

            gols = (
                home_goals
                + away_goals
            )

    # Fallback
    if gols is None:

        home_score = numero(
            jogo.get("home_score")
        )

        away_score = numero(
            jogo.get("away_score")
        )

        if (
            home_score is not None
            and away_score is not None
        ):

            gols = (
                home_score
                + away_score
            )

    # Fallback score {}
    if gols is None:

        score = jogo.get("score")

        if isinstance(score, dict):

            home_score = numero(
                score.get("home")
            )

            away_score = numero(
                score.get("away")
            )

            if (
                home_score is not None
                and away_score is not None
            ):

                gols = (
                    home_score
                    + away_score
                )

    # ========================================================
    # CANTOS - FORMATO CONFIRMADO
    # ========================================================

    corners = jogo.get("corners")

    if isinstance(corners, dict):

        home_corners = numero(
            corners.get("home")
        )

        away_corners = numero(
            corners.get("away")
        )

        if (
            home_corners is not None
            and away_corners is not None
        ):

            cantos = (
                home_corners
                + away_corners
            )

    # Fallback
    if cantos is None:

        home_corners = numero(
            jogo.get("home_corners")
        )

        away_corners = numero(
            jogo.get("away_corners")
        )

        if (
            home_corners is not None
            and away_corners is not None
        ):

            cantos = (
                home_corners
                + away_corners
            )

    return (
        gols,
        cantos
    )


# ============================================================
# HISTÓRICO COMBINADO
# ============================================================

def combinar_historicos(
    home_history,
    away_history
):

    unicos = {}
    descartados = 0

    for jogo in (
        home_history
        + away_history
    ):

        fid = jogo.get("id")

        gols, cantos = extrair_totais(
            jogo
        )

        if (
            fid is None
            or gols is None
            or cantos is None
        ):

            descartados += 1
            continue

        unicos[str(fid)] = {
            "id": str(fid),
            "gols": gols,
            "cantos": cantos
        }

    partidas = list(
        unicos.values()
    )

    logger.info(
        "📚 Histórico combinado | "
        "válidos=%s | descartados=%s",
        len(partidas),
        descartados
    )

    return partidas


def medias_historicas(partidas):

    if not partidas:

        return (
            None,
            None
        )

    media_cantos = sum(
        p["cantos"]
        for p in partidas
    ) / len(partidas)

    media_gols = sum(
        p["gols"]
        for p in partidas
    ) / len(partidas)

    return (
        media_cantos,
        media_gols
    )


# ============================================================
# FREQUÊNCIA HISTÓRICA
# ============================================================

def avaliar_linhas(
    partidas,
    linha_cantos,
    linha_gols
):

    total = len(partidas)

    if total == 0:

        return {
            "total": 0,
            "taxa_cantos": 0,
            "taxa_gols": 0,
            "taxa_combinada": 0
        }

    cantos_ok = 0
    gols_ok = 0
    combinado_ok = 0

    for partida in partidas:

        ok_cantos = (
            partida["cantos"]
            > linha_cantos
        )

        ok_gols = (
            partida["gols"]
            > linha_gols
        )

        if ok_cantos:
            cantos_ok += 1

        if ok_gols:
            gols_ok += 1

        if ok_cantos and ok_gols:
            combinado_ok += 1

    return {
        "total":
            total,

        "taxa_cantos":
            cantos_ok / total,

        "taxa_gols":
            gols_ok / total,

        "taxa_combinada":
            combinado_ok / total
    }


# ============================================================
# ODDS
# ============================================================

def buscar_odds(fixture_id):

    chave = str(fixture_id)
    agora = time.time()

    item = cache_odds.get(chave)

    if (
        item
        and agora - item["timestamp"]
        < CACHE_ODDS_TTL
    ):

        return item["dados"]

    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "bookmakers": BOOKMAKER
        }
    )

    if dados is not None:

        cache_odds[chave] = {
            "timestamp": agora,
            "dados": dados
        }

    return dados


def encontrar_bookmaker(dados):

    if not isinstance(dados, dict):
        return None

    bookmakers = dados.get(
        "bookmakers"
    )

    if isinstance(bookmakers, list):

        for bookmaker in bookmakers:

            nome = str(
                bookmaker.get(
                    "name",
                    ""
                )
            ).lower()

            if BOOKMAKER in nome:
                return bookmaker

        if bookmakers:
            return bookmakers[0]

    if isinstance(
        dados.get("odds"),
        dict
    ):

        return dados

    data = dados.get("data")

    if isinstance(data, dict):

        return encontrar_bookmaker(
            data
        )

    return None


def escolher_estagio_odd(
    mercado,
    lado
):

    if not isinstance(mercado, dict):
        return None

    # Pré-jogo somente
    for estagio in (
        "closing",
        "opening"
    ):

        dados = mercado.get(
            estagio
        )

        if isinstance(dados, dict):

            linha = numero(
                dados.get("line")
            )

            odd = numero(
                dados.get(lado)
            )

            if linha is not None:

                return {
                    "estagio": estagio,
                    "line": linha,
                    "odd": odd
                }

        linha = numero(dados)

        if linha is not None:

            return {
                "estagio": estagio,
                "line": linha,
                "odd": None
            }

    return None


def extrair_linhas_odds(dados):

    bookmaker = encontrar_bookmaker(
        dados
    )

    if not bookmaker:
        return None

    odds = bookmaker.get(
        "odds",
        bookmaker
    )

    if not isinstance(odds, dict):
        return None

    mercado_cantos = odds.get(
        "corner_line"
    )

    mercado_gols = odds.get(
        "goal_line"
    )

    cantos = escolher_estagio_odd(
        mercado_cantos,
        "over"
    )

    gols = escolher_estagio_odd(
        mercado_gols,
        "over"
    )

    if DEBUG_ODDS:

        logger.info(
            "🎯 CANTOS RAW: %s",
            mercado_cantos
        )

        logger.info(
            "🎯 GOLS RAW: %s",
            mercado_gols
        )

    if not cantos or not gols:
        return None

    logger.info(
        "🎯 Mercado | "
        "Over %.2f cantos @ %s | "
        "Over %.2f gols @ %s",
        cantos["line"],
        cantos.get("odd"),
        gols["line"],
        gols.get("odd")
    )

    return {
        "cantos": cantos,
        "gols": gols
    }


# ============================================================
# SELEÇÃO
# ============================================================

def selecionar_mercado(
    partidas,
    media_cantos,
    media_gols,
    odds_data
):

    linhas = extrair_linhas_odds(
        odds_data
    )

    if not linhas:

        logger.info(
            "⛔ Sem linhas utilizáveis."
        )

        return None

    linha_cantos = (
        linhas["cantos"]["line"]
    )

    linha_gols = (
        linhas["gols"]["line"]
    )

    odd_cantos = (
        linhas["cantos"].get("odd")
    )

    odd_gols = (
        linhas["gols"].get("odd")
    )

    # ========================================================
    # MARGEM DOS CANTOS
    # linha precisa estar 0.5 ou mais abaixo da média
    # ========================================================

    folga_cantos = (
        media_cantos
        - linha_cantos
    )

    if folga_cantos < MARGEM_CANTOS:

        logger.info(
            "⛔ CANTOS | Over %.2f | "
            "média %.2f | "
            "folga %.2f | mínimo %.2f",
            linha_cantos,
            media_cantos,
            folga_cantos,
            MARGEM_CANTOS
        )

        return None

    # ========================================================
    # MARGEM DOS GOLS
    # linha precisa estar 0.5 ou mais abaixo da média
    # ========================================================

    folga_gols = (
        media_gols
        - linha_gols
    )

    if folga_gols < MARGEM_GOLS:

        logger.info(
            "⛔ GOLS | Over %.2f | "
            "média %.2f | "
            "folga %.2f | mínimo %.2f",
            linha_gols,
            media_gols,
            folga_gols,
            MARGEM_GOLS
        )

        return None

    # ========================================================
    # FREQUÊNCIAS
    # ========================================================

    historico = avaliar_linhas(
        partidas,
        linha_cantos,
        linha_gols
    )

    logger.info(
        "📈 %s jogos | "
        "Cantos %.1f%% | "
        "Gols %.1f%% | "
        "Combinada %.1f%%",
        historico["total"],
        historico["taxa_cantos"] * 100,
        historico["taxa_gols"] * 100,
        historico["taxa_combinada"] * 100
    )

    if (
        historico["taxa_cantos"]
        < MIN_TAXA_CANTOS
    ):

        logger.info(
            "⛔ Cantos abaixo de %.0f%%.",
            MIN_TAXA_CANTOS * 100
        )

        return None

    if (
        historico["taxa_gols"]
        < MIN_TAXA_GOLS
    ):

        logger.info(
            "⛔ Gols abaixo de %.0f%%.",
            MIN_TAXA_GOLS * 100
        )

        return None

    if (
        historico["taxa_combinada"]
        < MIN_TAXA_COMBINADA
    ):

        logger.info(
            "⛔ Combinada abaixo de %.0f%%.",
            MIN_TAXA_COMBINADA * 100
        )

        return None

    # Odd NÃO filtra
    odd_combinada = None

    if (
        odd_cantos is not None
        and odd_gols is not None
    ):

        odd_combinada = (
            odd_cantos
            * odd_gols
        )

    return {
        "linha_cantos": linha_cantos,
        "linha_gols": linha_gols,

        "odd_cantos": odd_cantos,
        "odd_gols": odd_gols,
        "odd_combinada": odd_combinada,

        "media_cantos": media_cantos,
        "media_gols": media_gols,

        "folga_cantos": folga_cantos,
        "folga_gols": folga_gols,

        "historico": historico
    }


# ============================================================
# SINAIS
# ============================================================

def fixture_ja_utilizado(fixture_id):

    fid = str(fixture_id)

    return any(
        str(
            sinal.get("fixture_id")
        ) == fid
        for sinal in sinais
    )


def enviar_sinal(
    fixture,
    selecao,
    banco
):

    fixture_id = str(
        fixture.get("id")
    )

    (
        home_id,
        away_id,
        home,
        away
    ) = extrair_times(fixture)

    entrada = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** banco["gale"]
        )
    )

    odd_combinada = selecao.get(
        "odd_combinada"
    )

    sinal = {
        "fixture_id": fixture_id,

        "home": home,
        "away": away,

        "home_id": home_id,
        "away_id": away_id,

        "mercado":
            "Over cantos + Over gols",

        "linha_cantos":
            selecao["linha_cantos"],

        "linha_gols":
            selecao["linha_gols"],

        "media_cantos":
            round(
                selecao["media_cantos"],
                2
            ),

        "media_gols":
            round(
                selecao["media_gols"],
                2
            ),

        "folga_cantos":
            round(
                selecao["folga_cantos"],
                2
            ),

        "folga_gols":
            round(
                selecao["folga_gols"],
                2
            ),

        "taxa_cantos":
            round(
                selecao[
                    "historico"
                ]["taxa_cantos"] * 100,
                2
            ),

        "taxa_gols":
            round(
                selecao[
                    "historico"
                ]["taxa_gols"] * 100,
                2
            ),

        "taxa_combinada":
            round(
                selecao[
                    "historico"
                ]["taxa_combinada"] * 100,
                2
            ),

        "amostra":
            selecao["historico"]["total"],

        "odd_cantos":
            selecao.get("odd_cantos"),

        "odd_gols":
            selecao.get("odd_gols"),

        "odd_combinada":
            odd_combinada,

        "banca":
            banco["id"],

        "gale":
            banco["gale"],

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

        sinais.append(sinal)

        banco["ocupada"] = True
        banco["fixture_id"] = fixture_id
        banco["entrada"] = entrada

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )

    if odd_combinada is not None:

        odd_txt = (
            f"{odd_combinada:.2f}"
        )

    else:

        odd_txt = "N/D"

    mensagem = (
        "🚨 <b>SINAL V9.3</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x {html.escape(away)}</b>\n\n"

        "🚩 <b>OVER ESCANTEIOS</b>\n"
        f"Over {selecao['linha_cantos']:.2f}\n"
        f"Média: {selecao['media_cantos']:.2f}\n"
        f"Margem: {selecao['folga_cantos']:.2f}\n"
        f"Histórico: "
        f"{selecao['historico']['taxa_cantos'] * 100:.1f}%\n\n"

        "⚽ <b>OVER GOLS</b>\n"
        f"Over {selecao['linha_gols']:.2f}\n"
        f"Média: {selecao['media_gols']:.2f}\n"
        f"Margem: {selecao['folga_gols']:.2f}\n"
        f"Histórico: "
        f"{selecao['historico']['taxa_gols'] * 100:.1f}%\n\n"

        "🔥 <b>COMBINADA</b>\n"
        f"Histórico conjunto: "
        f"{selecao['historico']['taxa_combinada'] * 100:.1f}%\n"
        f"Amostra: "
        f"{selecao['historico']['total']} jogos\n\n"

        f"💰 Odd informativa: {odd_txt}\n\n"

        f"🏦 Banca: {banco['id']}\n"
        f"🔄 Gale: "
        f"{banco['gale']}/{MAX_GALES}\n"
        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )

    telegram(mensagem)

    logger.info(
        "🚨 SINAL | %s x %s | "
        "Over %.2f cantos + "
        "Over %.2f gols | "
        "Histórico combinado %.1f%%",
        home,
        away,
        selecao["linha_cantos"],
        selecao["linha_gols"],
        selecao[
            "historico"
        ]["taxa_combinada"] * 100
    )


# ============================================================
# RESULTADOS
# ============================================================

def buscar_fixture(fixture_id):

    dados = api_get(
        f"/fixtures/{fixture_id}"
    )

    if not isinstance(dados, dict):
        return None

    data = dados.get("data")

    if isinstance(data, dict):
        return data

    response = dados.get(
        "response"
    )

    if (
        isinstance(response, list)
        and response
    ):

        return response[0]

    return dados


def fixture_finalizado(fixture):

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


def liberar_banca(
    banco,
    resultado
):

    if resultado == "WIN":

        banco["gale"] = 0

    elif resultado == "LOSS":

        if banco["gale"] < MAX_GALES:

            banco["gale"] += 1

        else:

            banco["gale"] = 0

    # PUSH mantém o nível
    banco["ocupada"] = False
    banco["fixture_id"] = None

    banco["entrada"] = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** banco["gale"]
        )
    )


def resolver_sinal(
    sinal,
    fixture
):

    gols, cantos = extrair_totais(
        fixture
    )

    if (
        gols is None
        or cantos is None
    ):

        logger.warning(
            "⚠️ Fixture %s finalizado, "
            "mas sem gols/cantos utilizáveis.",
            sinal["fixture_id"]
        )

        return

    linha_cantos = float(
        sinal["linha_cantos"]
    )

    linha_gols = float(
        sinal["linha_gols"]
    )

    # ========================================================
    # LIQUIDAÇÃO DAS PERNAS
    # ========================================================

    if cantos > linha_cantos:

        resultado_cantos = "WIN"

    elif cantos == linha_cantos:

        resultado_cantos = "VOID"

    else:

        resultado_cantos = "LOSS"

    if gols > linha_gols:

        resultado_gols = "WIN"

    elif gols == linha_gols:

        resultado_gols = "VOID"

    else:

        resultado_gols = "LOSS"

    # ========================================================
    # RESULTADO DA COMBINADA
    #
    # LOSS em qualquer perna = LOSS
    # WIN + WIN = WIN
    # WIN + VOID = WIN da perna restante
    # VOID + VOID = PUSH
    # ========================================================

    if (
        resultado_cantos == "LOSS"
        or resultado_gols == "LOSS"
    ):

        resultado = "LOSS"

    elif (
        resultado_cantos == "WIN"
        and resultado_gols == "WIN"
    ):

        resultado = "WIN"

    elif (
        resultado_cantos == "VOID"
        and resultado_gols == "VOID"
    ):

        resultado = "PUSH"

    else:

        # Uma perna ganhou e a outra anulou.
        resultado = "WIN"

    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado

    sinal["resultado_cantos"] = (
        resultado_cantos
    )

    sinal["resultado_gols"] = (
        resultado_gols
    )

    sinal["gols_final"] = gols
    sinal["cantos_final"] = cantos

    sinal["resolvido_em"] = (
        agora_iso()
    )

    entrada = float(
        sinal["entrada"]
    )

    odd_cantos = numero(
        sinal.get("odd_cantos")
    )

    odd_gols = numero(
        sinal.get("odd_gols")
    )

    odd_combinada = numero(
        sinal.get("odd_combinada")
    )

    # Odd efetivamente liquidada
    odd_liquidacao = None

    if resultado == "WIN":

        if (
            resultado_cantos == "WIN"
            and resultado_gols == "WIN"
        ):

            odd_liquidacao = (
                odd_combinada
            )

        elif (
            resultado_cantos == "WIN"
            and resultado_gols == "VOID"
        ):

            odd_liquidacao = (
                odd_cantos
            )

        elif (
            resultado_cantos == "VOID"
            and resultado_gols == "WIN"
        ):

            odd_liquidacao = (
                odd_gols
            )

    lucro = 0.0

    if resultado == "WIN":

        stats["wins"] = (
            stats.get("wins", 0)
            + 1
        )

        if odd_liquidacao is not None:

            lucro = (
                entrada
                * (odd_liquidacao - 1)
            )

    elif resultado == "LOSS":

        stats["losses"] = (
            stats.get("losses", 0)
            + 1
        )

        lucro = -entrada

    else:

        stats["pushes"] = (
            stats.get("pushes", 0)
            + 1
        )

    stats["total_resolvidos"] = (
        stats.get(
            "total_resolvidos",
            0
        )
        + 1
    )

    stats["lucro_teorico"] = round(
        stats.get(
            "lucro_teorico",
            0
        )
        + lucro,
        2
    )

    gale = str(
        sinal.get("gale", 0)
    )

    if gale not in stats["por_gale"]:

        stats["por_gale"][gale] = {
            "wins": 0,
            "losses": 0
        }

    if resultado == "WIN":

        stats[
            "por_gale"
        ][gale]["wins"] += 1

    elif resultado == "LOSS":

        stats[
            "por_gale"
        ][gale]["losses"] += 1

    banco = next(
        (
            b
            for b in bancas
            if b["id"]
            == sinal["banca"]
        ),
        None
    )

    if banco:

        liberar_banca(
            banco,
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
        "PUSH": "↩️"
    }.get(
        resultado,
        ""
    )

    mensagem = (
        f"{emoji} <b>{resultado}</b>\n\n"

        f"⚽ <b>{html.escape(sinal['home'])}"
        f" x {html.escape(sinal['away'])}</b>\n\n"

        f"🚩 Over {linha_cantos:.2f} cantos\n"
        f"Final: {cantos:.0f}\n"
        f"Perna: {resultado_cantos}\n\n"

        f"⚽ Over {linha_gols:.2f} gols\n"
        f"Final: {gols:.0f}\n"
        f"Perna: {resultado_gols}\n\n"

        f"🏦 Banca: {sinal['banca']}\n"
        f"🔄 Gale: {sinal['gale']}"
    )

    telegram(mensagem)

    logger.info(
        "%s | %s x %s | "
        "cantos %.0f (%s) | "
        "gols %.0f (%s)",
        resultado,
        sinal["home"],
        sinal["away"],
        cantos,
        resultado_cantos,
        gols,
        resultado_gols
    )


def verificar_resultados():

    pendentes = [
        s
        for s in sinais
        if s.get("status")
        == "PENDENTE"
    ]

    for sinal in pendentes:

        fixture = buscar_fixture(
            sinal["fixture_id"]
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

def analisar_fixture(fixture):

    fixture_id = fixture.get("id")

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
    ) = extrair_times(fixture)

    if (
        not home_id
        or not away_id
    ):

        logger.info(
            "⛔ %s x %s sem IDs.",
            home,
            away
        )

        return

    logger.info(
        "🔎 Analisando %s x %s | "
        "home_id=%s | away_id=%s",
        home,
        away,
        home_id,
        away_id
    )

    home_history = (
        buscar_historico_time(
            home_id
        )
    )

    away_history = (
        buscar_historico_time(
            away_id
        )
    )

    partidas = combinar_historicos(
        home_history,
        away_history
    )

    if (
        len(partidas)
        < MIN_JOGOS_HISTORICO
    ):

        logger.info(
            "⛔ Histórico insuficiente: "
            "%s jogos válidos.",
            len(partidas)
        )

        return

    (
        media_cantos,
        media_gols
    ) = medias_historicas(
        partidas
    )

    logger.info(
        "📊 %s x %s | "
        "média cantos %.2f | "
        "média gols %.2f | "
        "amostra %s",
        home,
        away,
        media_cantos,
        media_gols,
        len(partidas)
    )

    odds_data = buscar_odds(
        fixture_id
    )

    if not odds_data:

        logger.info(
            "⛔ %s x %s sem odds/linhas.",
            home,
            away
        )

        return

    selecao = selecionar_mercado(
        partidas,
        media_cantos,
        media_gols,
        odds_data
    )

    if not selecao:
        return

    banco = banco_livre()

    if not banco:

        logger.info(
            "🏦 Todas as bancas ocupadas."
        )

        return

    enviar_sinal(
        fixture,
        selecao,
        banco
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
        "🔄 Iniciando ciclo V9.3"
    )

    try:

        verificar_resultados()

        if banco_livre() is None:

            logger.info(
                "🏦 As %s bancas estão ocupadas.",
                TOTAL_BANCAS
            )

            return

        fixtures = buscar_fixtures()

        if not fixtures:

            logger.info(
                "Nenhum fixture disponível."
            )

            return

        total = len(fixtures)

        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )

        logger.info(
            "🔎 Analisando até %s de %s jogos.",
            quantidade,
            total
        )

        for _ in range(quantidade):

            if banco_livre() is None:
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
                    "❌ Erro analisando fixture."
                )

    except Exception:

        logger.exception(
            "❌ Erro geral no ciclo."
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "🤖 Robô V9.3 iniciado."
    )

    logger.info(
        "⚽ Estratégia: "
        "OVER CANTOS + OVER GOLS"
    )

    logger.info(
        "📏 Margens: "
        "cantos %.2f | gols %.2f",
        MARGEM_CANTOS,
        MARGEM_GOLS
    )

    logger.info(
        "📊 Filtros: "
        "cantos %.0f%% | "
        "gols %.0f%% | "
        "combinada %.0f%%",
        MIN_TAXA_CANTOS * 100,
        MIN_TAXA_GOLS * 100,
        MIN_TAXA_COMBINADA * 100
    )

    logger.info(
        "🏦 Bancas: %s | "
        "Entrada R$ %.2f | "
        "Máximo %s Gales",
        TOTAL_BANCAS,
        ENTRADA_INICIAL,
        MAX_GALES
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

    return jsonify({
        "status":
            "online",

        "versao":
            "V9.3",

        "estrategia":
            "Over cantos + Over gols",

        "margem_cantos":
            MARGEM_CANTOS,

        "margem_gols":
            MARGEM_GOLS,

        "taxa_cantos_minima":
            MIN_TAXA_CANTOS,

        "taxa_gols_minima":
            MIN_TAXA_GOLS,

        "taxa_combinada_minima":
            MIN_TAXA_COMBINADA,

        "odd_como_filtro":
            False,

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
            taxa(
                wins,
                losses
            ),

        "bancas":
            bancas
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,
        "versao": "V9.3",
        "mercado_cantos": "OVER",
        "mercado_gols": "OVER",
        "margem_cantos": MARGEM_CANTOS,
        "margem_gols": MARGEM_GOLS,
        "odd_como_filtro": False
    })


@app.route("/stats")
def rota_stats():

    resposta = dict(stats)

    resposta[
        "assertividade"
    ] = taxa(
        stats.get("wins", 0),
        stats.get("losses", 0)
    )

    return jsonify(resposta)


@app.route("/bancas")
def rota_bancas():

    return jsonify(bancas)


@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais[-100:]
    )


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "version": "V9.3"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
)
