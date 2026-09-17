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
# ROBÔ V11
# UNDER ESCANTEIOS 1º TEMPO + OVER ESCANTEIOS JOGO INTEIRO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-v11-cantos")


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

BOOKMAKER = os.getenv(
    "BOOKMAKER",
    "bet365"
).lower()


# ============================================================
# FILTROS DA ESTRATÉGIA
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv("MIN_TAXA_UNDER_HT", "0.50")
)

MIN_TAXA_OVER_FT = float(
    os.getenv("MIN_TAXA_OVER_FT", "0.50")
)

MIN_TAXA_COMBINADA = float(
    os.getenv("MIN_TAXA_COMBINADA", "0.50")
)

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "10")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "10")
)


# ============================================================
# JANELA / CICLO
# ============================================================

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv("MAX_JOGOS_ANALISADOS_CICLO", "4")
)


# ============================================================
# GESTÃO DE BANCAS / GALE
# ============================================================

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


# ============================================================
# RATE LIMIT
# ============================================================

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
#
# Arquivos V11 separados para não misturar estatísticas
# do teste anterior V10.
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS_V11",
    "stats_v11.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS_V11",
    "sinais_v11.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE_V11",
    "cache_v11.json"
)


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-cantos-v11"
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

            time.sleep(max(1, espera))

        agora = time.time()

        intervalo = agora - ultima_requisicao

        if intervalo < API_MIN_INTERVAL_SECONDS:

            time.sleep(
                API_MIN_INTERVAL_SECONDS - intervalo
            )

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
            "❌ Erro lendo %s: %s",
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

        os.replace(temporario, caminho)

    except Exception as e:

        logger.error(
            "❌ Erro salvando %s: %s",
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
            "0": {
                "wins": 0,
                "losses": 0,
                "pushes": 0
            },
            "1": {
                "wins": 0,
                "losses": 0,
                "pushes": 0
            },
            "2": {
                "wins": 0,
                "losses": 0,
                "pushes": 0
            }
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
        for i in range(1, TOTAL_BANCAS + 1)
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
                "❌ Telegram: %s",
                resposta.text[:500]
            )

        return resposta.ok

    except Exception as e:

        logger.error(
            "❌ Erro Telegram: %s",
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
# HISTÓRICO DOS TIMES
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
# EXTRAIR ESCANTEIOS
#
# Estrutura confirmada:
#
# corners = {
#     "home": 6,
#     "away": 3,
#     "half_home": 2,
#     "half_away": 3
# }
#
# HT = half_home + half_away
# FT = home + away
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(jogo, dict):

        return (
            None,
            None
        )

    corners = jogo.get("corners")

    if not isinstance(corners, dict):

        return (
            None,
            None
        )

    home = numero(
        corners.get("home")
    )

    away = numero(
        corners.get("away")
    )

    half_home = numero(
        corners.get("half_home")
    )

    half_away = numero(
        corners.get("half_away")
    )

    cantos_ft = None
    cantos_ht = None

    if (
        home is not None
        and away is not None
    ):

        cantos_ft = (
            home + away
        )

    if (
        half_home is not None
        and half_away is not None
    ):

        cantos_ht = (
            half_home + half_away
        )

    return (
        cantos_ht,
        cantos_ft
    )


# ============================================================
# COMBINAR HISTÓRICOS
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

        if not isinstance(jogo, dict):

            descartados += 1
            continue

        fid = jogo.get("id")

        (
            cantos_ht,
            cantos_ft
        ) = extrair_cantos(jogo)

        if (
            fid is None
            or cantos_ht is None
            or cantos_ft is None
        ):

            descartados += 1
            continue

        unicos[str(fid)] = {
            "id": str(fid),
            "cantos_ht": cantos_ht,
            "cantos_ft": cantos_ft
        }

    partidas = list(
        unicos.values()
    )

    logger.info(
        "📚 Histórico | "
        "válidos=%s | descartados=%s",
        len(partidas),
        descartados
    )

    return partidas


# ============================================================
# MÉDIAS
# ============================================================

def calcular_medias(partidas):

    if not partidas:

        return (
            None,
            None
        )

    media_ht = (
        sum(
            p["cantos_ht"]
            for p in partidas
        )
        / len(partidas)
    )

    media_ft = (
        sum(
            p["cantos_ft"]
            for p in partidas
        )
        / len(partidas)
    )

    return (
        media_ht,
        media_ft
    )


# ============================================================
# CLASSIFICAR UMA LINHA ASIÁTICA
# ============================================================

def resultado_under(valor, linha):

    if valor < linha:
        return "WIN"

    if valor > linha:
        return "LOSS"

    return "VOID"


def resultado_over(valor, linha):

    if valor > linha:
        return "WIN"

    if valor < linha:
        return "LOSS"

    return "VOID"


# ============================================================
# HISTÓRICO DA ESTRATÉGIA
# ============================================================

def avaliar_historico(
    partidas,
    linha_under_ht,
    linha_over_ft
):

    under_wins = 0
    under_losses = 0
    under_voids = 0

    over_wins = 0
    over_losses = 0
    over_voids = 0

    combinada_wins = 0
    combinada_losses = 0
    combinada_pushes = 0

    for partida in partidas:

        r_under = resultado_under(
            partida["cantos_ht"],
            linha_under_ht
        )

        r_over = resultado_over(
            partida["cantos_ft"],
            linha_over_ft
        )

        if r_under == "WIN":
            under_wins += 1

        elif r_under == "LOSS":
            under_losses += 1

        else:
            under_voids += 1

        if r_over == "WIN":
            over_wins += 1

        elif r_over == "LOSS":
            over_losses += 1

        else:
            over_voids += 1

        # Liquidação histórica da combinação:
        #
        # LOSS em qualquer perna = LOSS
        # WIN + WIN = WIN
        # WIN + VOID = WIN
        # VOID + WIN = WIN
        # VOID + VOID = PUSH

        if (
            r_under == "LOSS"
            or r_over == "LOSS"
        ):

            combinada_losses += 1

        elif (
            r_under == "VOID"
            and r_over == "VOID"
        ):

            combinada_pushes += 1

        else:

            combinada_wins += 1

    under_decididos = (
        under_wins
        + under_losses
    )

    over_decididos = (
        over_wins
        + over_losses
    )

    combinada_decididos = (
        combinada_wins
        + combinada_losses
    )

    taxa_under = (
        under_wins / under_decididos
        if under_decididos
        else 0
    )

    taxa_over = (
        over_wins / over_decididos
        if over_decididos
        else 0
    )

    taxa_combinada = (
        combinada_wins
        / combinada_decididos
        if combinada_decididos
        else 0
    )

    return {
        "total": len(partidas),

        "under_wins": under_wins,
        "under_losses": under_losses,
        "under_voids": under_voids,
        "taxa_under": taxa_under,

        "over_wins": over_wins,
        "over_losses": over_losses,
        "over_voids": over_voids,
        "taxa_over": taxa_over,

        "combinada_wins": combinada_wins,
        "combinada_losses": combinada_losses,
        "combinada_pushes": combinada_pushes,
        "taxa_combinada": taxa_combinada
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

    # Pré-jogo:
    # tenta closing primeiro;
    # se não houver, usa opening.
    for estagio in (
        "closing",
        "opening"
    ):

        dados = mercado.get(
            estagio
        )

        if not isinstance(dados, dict):
            continue

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

    return None


# ============================================================
# EXTRAIR OS DOIS MERCADOS
#
# Confirmados:
#
# corner_line_half -> UNDER HT
# corner_line      -> OVER FT
# ============================================================

def extrair_mercados(dados):

    bookmaker = encontrar_bookmaker(
        dados
    )

    if not bookmaker:

        logger.info(
            "⛔ Bookmaker não encontrado."
        )

        return None

    odds = bookmaker.get(
        "odds",
        bookmaker
    )

    if not isinstance(odds, dict):

        logger.info(
            "⛔ Estrutura de odds inválida."
        )

        return None

    mercado_ht = odds.get(
        "corner_line_half"
    )

    mercado_ft = odds.get(
        "corner_line"
    )

    logger.info(
        "🎯 CANTOS HT RAW: %s",
        mercado_ht
    )

    logger.info(
        "🎯 CANTOS FT RAW: %s",
        mercado_ft
    )

    under_ht = escolher_estagio_odd(
        mercado_ht,
        "under"
    )

    over_ft = escolher_estagio_odd(
        mercado_ft,
        "over"
    )

    if not under_ht:

        logger.info(
            "⛔ Sem Under HT utilizável."
        )

        return None

    if not over_ft:

        logger.info(
            "⛔ Sem Over FT utilizável."
        )

        return None

    logger.info(
        "🎯 MERCADOS | "
        "Under %.2f HT @ %s | "
        "Over %.2f FT @ %s",
        under_ht["line"],
        under_ht.get("odd"),
        over_ft["line"],
        over_ft.get("odd")
    )

    return {
        "under_ht": under_ht,
        "over_ft": over_ft
    }


# ============================================================
# SELEÇÃO
# ============================================================

def selecionar_mercado(
    partidas,
    media_ht,
    media_ft,
    odds_data
):

    mercados = extrair_mercados(
        odds_data
    )

    if not mercados:
        return None

    linha_under_ht = (
        mercados["under_ht"]["line"]
    )

    linha_over_ft = (
        mercados["over_ft"]["line"]
    )

    odd_under_ht = (
        mercados["under_ht"].get("odd")
    )

    odd_over_ft = (
        mercados["over_ft"].get("odd")
    )

    historico = avaliar_historico(
        partidas,
        linha_under_ht,
        linha_over_ft
    )

    logger.info(
        "📊 HISTÓRICO %s jogos | "
        "Under HT %.1f%% | "
        "Over FT %.1f%% | "
        "Combinada %.1f%%",
        historico["total"],
        historico["taxa_under"] * 100,
        historico["taxa_over"] * 100,
        historico["taxa_combinada"] * 100
    )

    logger.info(
        "📉 UNDER HT %.2f | "
        "WIN %s | VOID %s | LOSS %s",
        linha_under_ht,
        historico["under_wins"],
        historico["under_voids"],
        historico["under_losses"]
    )

    logger.info(
        "📈 OVER FT %.2f | "
        "WIN %s | VOID %s | LOSS %s",
        linha_over_ft,
        historico["over_wins"],
        historico["over_voids"],
        historico["over_losses"]
    )

    logger.info(
        "🔥 COMBINADA | "
        "WIN %s | PUSH %s | LOSS %s",
        historico["combinada_wins"],
        historico["combinada_pushes"],
        historico["combinada_losses"]
    )

    if (
        historico["taxa_under"]
        < MIN_TAXA_UNDER_HT
    ):

        logger.info(
            "⛔ Under HT abaixo de %.0f%%.",
            MIN_TAXA_UNDER_HT * 100
        )

        return None

    if (
        historico["taxa_over"]
        < MIN_TAXA_OVER_FT
    ):

        logger.info(
            "⛔ Over FT abaixo de %.0f%%.",
            MIN_TAXA_OVER_FT * 100
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

    odd_combinada = None

    if (
        odd_under_ht is not None
        and odd_over_ft is not None
    ):

        odd_combinada = (
            odd_under_ht
            * odd_over_ft
        )

    logger.info(
        "✅ COMBINAÇÃO APROVADA | "
        "Under %.2f HT + Over %.2f FT | "
        "%.1f%% histórico conjunto",
        linha_under_ht,
        linha_over_ft,
        historico["taxa_combinada"] * 100
    )

    return {
        "linha_under_ht":
            linha_under_ht,

        "linha_over_ft":
            linha_over_ft,

        "odd_under_ht":
            odd_under_ht,

        "odd_over_ft":
            odd_over_ft,

        "odd_combinada":
            odd_combinada,

        "media_ht":
            media_ht,

        "media_ft":
            media_ft,

        "historico":
            historico
    }


# ============================================================
# FIXTURE JÁ USADO
# ============================================================

def fixture_ja_utilizado(
    fixture_id
):

    fid = str(fixture_id)

    return any(
        str(
            sinal.get("fixture_id")
        ) == fid
        for sinal in sinais
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

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
    ) = extrair_times(
        fixture
    )

    entrada = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** banco["gale"]
        )
    )

    historico = selecao[
        "historico"
    ]

    sinal = {
        "fixture_id":
            fixture_id,

        "home":
            home,

        "away":
            away,

        "home_id":
            home_id,

        "away_id":
            away_id,

        "mercado":
            "Under cantos HT + Over cantos FT",

        "linha_under_ht":
            selecao["linha_under_ht"],

        "linha_over_ft":
            selecao["linha_over_ft"],

        "odd_under_ht":
            selecao.get("odd_under_ht"),

        "odd_over_ft":
            selecao.get("odd_over_ft"),

        "odd_combinada":
            selecao.get("odd_combinada"),

        "media_ht":
            round(
                selecao["media_ht"],
                2
            ),

        "media_ft":
            round(
                selecao["media_ft"],
                2
            ),

        "taxa_under_ht":
            round(
                historico["taxa_under"]
                * 100,
                2
            ),

        "taxa_over_ft":
            round(
                historico["taxa_over"]
                * 100,
                2
            ),

        "taxa_combinada":
            round(
                historico["taxa_combinada"]
                * 100,
                2
            ),

        "amostra":
            historico["total"],

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

    odd_under = numero(
        selecao.get("odd_under_ht")
    )

    odd_over = numero(
        selecao.get("odd_over_ft")
    )

    odd_comb = numero(
        selecao.get("odd_combinada")
    )

    odd_under_txt = (
        f"{odd_under:.3f}"
        if odd_under is not None
        else "N/D"
    )

    odd_over_txt = (
        f"{odd_over:.3f}"
        if odd_over is not None
        else "N/D"
    )

    odd_comb_txt = (
        f"{odd_comb:.2f}"
        if odd_comb is not None
        else "N/D"
    )

    mensagem = (
        "🚨 <b>SINAL V11 - CANTOS</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x {html.escape(away)}</b>\n\n"

        "1️⃣ <b>1º TEMPO</b>\n"
        f"📉 Under {selecao['linha_under_ht']:.2f} escanteios\n"
        f"💰 Odd: {odd_under_txt}\n"
        f"📊 Média HT: {selecao['media_ht']:.2f}\n"
        f"✅ Histórico: "
        f"{historico['taxa_under'] * 100:.1f}%\n"
        f"WIN/VOID/LOSS: "
        f"{historico['under_wins']}/"
        f"{historico['under_voids']}/"
        f"{historico['under_losses']}\n\n"

        "2️⃣ <b>JOGO INTEIRO</b>\n"
        f"📈 Over {selecao['linha_over_ft']:.2f} escanteios\n"
        f"💰 Odd: {odd_over_txt}\n"
        f"📊 Média FT: {selecao['media_ft']:.2f}\n"
        f"✅ Histórico: "
        f"{historico['taxa_over'] * 100:.1f}%\n"
        f"WIN/VOID/LOSS: "
        f"{historico['over_wins']}/"
        f"{historico['over_voids']}/"
        f"{historico['over_losses']}\n\n"

        "🔥 <b>COMBINAÇÃO</b>\n"
        f"📊 Histórico conjunto: "
        f"{historico['taxa_combinada'] * 100:.1f}%\n"
        f"📚 Amostra: {historico['total']} jogos\n"
        f"💰 Odd combinada informativa: "
        f"{odd_comb_txt}\n\n"

        f"🏦 Banca: {banco['id']}\n"
        f"🔄 Gale: {banco['gale']}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}"
    )

    telegram(mensagem)

    logger.info(
        "🚨 SINAL V11 | %s x %s | "
        "Under %.2f HT + Over %.2f FT | "
        "Combinada %.1f%%",
        home,
        away,
        selecao["linha_under_ht"],
        selecao["linha_over_ft"],
        historico["taxa_combinada"] * 100
    )


# ============================================================
# BUSCAR FIXTURE
# ============================================================

def buscar_fixture(
    fixture_id
):

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
# BANCA / GALE
# ============================================================

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

    # PUSH mantém o Gale atual.

    banco["ocupada"] = False
    banco["fixture_id"] = None

    banco["entrada"] = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** banco["gale"]
        )
    )


# ============================================================
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal,
    fixture
):

    (
        cantos_ht,
        cantos_ft
    ) = extrair_cantos(
        fixture
    )

    if (
        cantos_ht is None
        or cantos_ft is None
    ):

        logger.warning(
            "⚠️ Fixture %s terminou "
            "sem dados de cantos HT/FT.",
            sinal["fixture_id"]
        )

        return

    linha_under_ht = float(
        sinal["linha_under_ht"]
    )

    linha_over_ft = float(
        sinal["linha_over_ft"]
    )

    resultado_ht = resultado_under(
        cantos_ht,
        linha_under_ht
    )

    resultado_ft = resultado_over(
        cantos_ft,
        linha_over_ft
    )

    # --------------------------------------------------------
    # RESULTADO DA COMBINAÇÃO
    # --------------------------------------------------------

    if (
        resultado_ht == "LOSS"
        or resultado_ft == "LOSS"
    ):

        resultado = "LOSS"

    elif (
        resultado_ht == "VOID"
        and resultado_ft == "VOID"
    ):

        resultado = "PUSH"

    else:

        resultado = "WIN"

    sinal["status"] = "RESOLVIDO"

    sinal["resultado"] = resultado

    sinal["resultado_under_ht"] = (
        resultado_ht
    )

    sinal["resultado_over_ft"] = (
        resultado_ft
    )

    sinal["cantos_ht_final"] = (
        cantos_ht
    )

    sinal["cantos_ft_final"] = (
        cantos_ft
    )

    sinal["resolvido_em"] = (
        agora_iso()
    )

    entrada = float(
        sinal["entrada"]
    )

    odd_under = numero(
        sinal.get("odd_under_ht")
    )

    odd_over = numero(
        sinal.get("odd_over_ft")
    )

    odd_combinada = numero(
        sinal.get("odd_combinada")
    )

    odd_liquidacao = None

    if resultado == "WIN":

        if (
            resultado_ht == "WIN"
            and resultado_ft == "WIN"
        ):

            odd_liquidacao = odd_combinada

        elif (
            resultado_ht == "WIN"
            and resultado_ft == "VOID"
        ):

            odd_liquidacao = odd_under

        elif (
            resultado_ht == "VOID"
            and resultado_ft == "WIN"
        ):

            odd_liquidacao = odd_over

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
            "losses": 0,
            "pushes": 0
        }

    if resultado == "WIN":

        stats[
            "por_gale"
        ][gale]["wins"] += 1

    elif resultado == "LOSS":

        stats[
            "por_gale"
        ][gale]["losses"] += 1

    else:

        stats[
            "por_gale"
        ][gale]["pushes"] = (
            stats[
                "por_gale"
            ][gale].get(
                "pushes",
                0
            )
            + 1
        )

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
        f"{emoji} <b>{resultado} - V11</b>\n\n"

        f"⚽ <b>{html.escape(sinal['home'])}"
        f" x {html.escape(sinal['away'])}</b>\n\n"

        "1️⃣ <b>UNDER HT</b>\n"
        f"Under {linha_under_ht:.2f}\n"
        f"Cantos HT: {cantos_ht:.0f}\n"
        f"Resultado: {resultado_ht}\n\n"

        "2️⃣ <b>OVER FT</b>\n"
        f"Over {linha_over_ft:.2f}\n"
        f"Cantos FT: {cantos_ft:.0f}\n"
        f"Resultado: {resultado_ft}\n\n"

        f"🏦 Banca: {sinal['banca']}\n"
        f"🔄 Gale: {sinal['gale']}"
    )

    telegram(mensagem)

    logger.info(
        "%s | %s x %s | "
        "HT %.0f (%s) | "
        "FT %.0f (%s)",
        resultado,
        sinal["home"],
        sinal["away"],
        cantos_ht,
        resultado_ht,
        cantos_ft,
        resultado_ft
    )


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    pendentes = [
        sinal
        for sinal in sinais
        if sinal.get("status")
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
# ANALISAR FIXTURE
# ============================================================

def analisar_fixture(
    fixture
):

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
    ) = extrair_times(
        fixture
    )

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
        "🔎 V11 | Analisando %s x %s",
        home,
        away
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
            "%s jogos.",
            len(partidas)
        )

        return

    (
        media_ht,
        media_ft
    ) = calcular_medias(
        partidas
    )

    logger.info(
        "📊 %s x %s | "
        "Média HT %.2f | "
        "Média FT %.2f | "
        "Amostra %s",
        home,
        away,
        media_ht,
        media_ft,
        len(partidas)
    )

    odds_data = buscar_odds(
        fixture_id
    )

    if not odds_data:

        logger.info(
            "⛔ %s x %s sem odds.",
            home,
            away
        )

        return

    selecao = selecionar_mercado(
        partidas,
        media_ht,
        media_ft,
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
        "🔄 Iniciando ciclo V11"
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
        "🤖 Robô V11 iniciado."
    )

    logger.info(
        "🚩 UNDER CANTOS HT + OVER CANTOS FT"
    )

    logger.info(
        "📊 Filtros | "
        "Under HT %.0f%% | "
        "Over FT %.0f%% | "
        "Combinada %.0f%%",
        MIN_TAXA_UNDER_HT * 100,
        MIN_TAXA_OVER_FT * 100,
        MIN_TAXA_COMBINADA * 100
    )

    logger.info(
        "🏦 %s bancas | "
        "Entrada R$ %.2f | "
        "%s Gales",
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
            "V11",

        "estrategia":
            "Under cantos HT + Over cantos FT",

        "mercado_under_ht":
            "corner_line_half",

        "mercado_over_ft":
            "corner_line",

        "taxa_under_ht_minima":
            MIN_TAXA_UNDER_HT,

        "taxa_over_ft_minima":
            MIN_TAXA_OVER_FT,

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

        "lucro_teorico":
            stats.get(
                "lucro_teorico",
                0
            ),

        "bancas":
            bancas
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,
        "versao": "V11",
        "under_ht": "corner_line_half",
        "over_ft": "corner_line",
        "min_under_ht": MIN_TAXA_UNDER_HT,
        "min_over_ft": MIN_TAXA_OVER_FT,
        "min_combinada": MIN_TAXA_COMBINADA,
        "odd_como_filtro": False
    })


@app.route("/stats")
def rota_stats():

    resposta = dict(stats)

    resposta["assertividade"] = taxa(
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
        "version": "V11"
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
