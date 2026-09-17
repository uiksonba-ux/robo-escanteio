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
# ROBÔ V10.0
# UNDER ESCANTEIOS - 1º TEMPO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v10-under-cantos-ht")


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
# ESTRATÉGIA UNDER CANTOS HT
# ============================================================

MIN_TAXA_CANTOS_HT = float(
    os.getenv("MIN_TAXA_CANTOS_HT", "0.50")
)

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "10")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "10")
)


# ============================================================
# JANELA
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
# GESTÃO
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
# API / RATE LIMIT
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
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS",
    "stats_ht.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS",
    "sinais_ht.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE",
    "cache_ht.json"
)


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-under-cantos-ht-v10"
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
                API_MIN_INTERVAL_SECONDS
                - intervalo
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

        os.replace(
            temporario,
            caminho
        )

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
# EXTRAÇÃO DOS ESCANTEIOS DO 1º TEMPO
#
# FORMATO JÁ CONFIRMADO:
#
# corners = {
#     "home": 6,
#     "away": 3,
#     "half_home": 2,
#     "half_away": 3
# }
#
# HT = half_home + half_away
# ============================================================

def extrair_cantos_ht(jogo):

    if not isinstance(jogo, dict):
        return None

    corners = jogo.get("corners")

    if not isinstance(corners, dict):
        return None

    half_home = numero(
        corners.get("half_home")
    )

    half_away = numero(
        corners.get("half_away")
    )

    if (
        half_home is None
        or half_away is None
    ):
        return None

    return (
        half_home
        + half_away
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

        cantos_ht = extrair_cantos_ht(
            jogo
        )

        if (
            fid is None
            or cantos_ht is None
        ):

            descartados += 1
            continue

        unicos[str(fid)] = {
            "id": str(fid),
            "cantos_ht": cantos_ht
        }

    partidas = list(
        unicos.values()
    )

    logger.info(
        "📚 Histórico HT | "
        "válidos=%s | descartados=%s",
        len(partidas),
        descartados
    )

    return partidas


# ============================================================
# MÉDIA HT
# ============================================================

def media_cantos_ht(partidas):

    if not partidas:
        return None

    return (
        sum(
            p["cantos_ht"]
            for p in partidas
        )
        / len(partidas)
    )


# ============================================================
# AVALIAR UNDER HISTÓRICO
#
# Under 4.5:
# 0,1,2,3,4 = WIN
#
# Under 4.0:
# 0,1,2,3 = WIN
# 4 = VOID
# 5+ = LOSS
#
# Taxa usada no filtro:
# WIN / (WIN + LOSS)
# VOID fica separado.
# ============================================================

def avaliar_under_ht(
    partidas,
    linha
):

    wins = 0
    losses = 0
    voids = 0

    for partida in partidas:

        cantos = partida[
            "cantos_ht"
        ]

        if cantos < linha:
            wins += 1

        elif cantos > linha:
            losses += 1

        else:
            voids += 1

    decididos = (
        wins
        + losses
    )

    taxa_under = (
        wins / decididos
        if decididos > 0
        else 0
    )

    return {
        "total": len(partidas),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "decididos": decididos,
        "taxa_under": taxa_under
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


# ============================================================
# ESCOLHER CLOSING -> OPENING
# ============================================================

def escolher_estagio_odd(
    mercado,
    lado
):

    if not isinstance(
        mercado,
        dict
    ):
        return None

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
# EXTRAIR UNDER CANTOS HT
#
# CAMPO CONFIRMADO NO RAW:
# corner_line_half
# ============================================================

def extrair_under_cantos_ht(
    dados
):

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

    mercado = odds.get(
        "corner_line_half"
    )

    logger.info(
        "🎯 CANTOS HT RAW: %s",
        mercado
    )

    if not mercado:

        logger.info(
            "⛔ Sem corner_line_half."
        )

        return None

    under = escolher_estagio_odd(
        mercado,
        "under"
    )

    if not under:

        logger.info(
            "⛔ Sem linha Under HT utilizável."
        )

        return None

    logger.info(
        "🎯 UNDER HT | "
        "Under %.2f cantos @ %s | %s",
        under["line"],
        under.get("odd"),
        under["estagio"]
    )

    return under


# ============================================================
# SELEÇÃO
# ============================================================

def selecionar_under_ht(
    partidas,
    media_ht,
    odds_data
):

    mercado = extrair_under_cantos_ht(
        odds_data
    )

    if not mercado:
        return None

    linha = mercado["line"]
    odd = mercado.get("odd")

    historico = avaliar_under_ht(
        partidas,
        linha
    )

    logger.info(
        "📉 UNDER HT %.2f | "
        "média HT %.2f | "
        "WIN %s | VOID %s | LOSS %s | "
        "taxa %.1f%%",
        linha,
        media_ht,
        historico["wins"],
        historico["voids"],
        historico["losses"],
        historico["taxa_under"] * 100
    )

    if (
        historico["decididos"]
        == 0
    ):

        logger.info(
            "⛔ Sem resultados decididos "
            "para esta linha."
        )

        return None

    if (
        historico["taxa_under"]
        < MIN_TAXA_CANTOS_HT
    ):

        logger.info(
            "⛔ Under HT abaixo de %.0f%%.",
            MIN_TAXA_CANTOS_HT * 100
        )

        return None

    logger.info(
        "✅ UNDER HT APROVADO | "
        "%.1f%% histórico.",
        historico["taxa_under"] * 100
    )

    return {
        "linha": linha,
        "odd": odd,
        "estagio": mercado["estagio"],
        "media_ht": media_ht,
        "historico": historico
    }


# ============================================================
# FIXTURE JÁ UTILIZADO
# ============================================================

def fixture_ja_utilizado(fixture_id):

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
        "fixture_id": fixture_id,

        "home": home,
        "away": away,

        "home_id": home_id,
        "away_id": away_id,

        "mercado":
            "Under escanteios 1º tempo",

        "linha_cantos_ht":
            selecao["linha"],

        "odd":
            selecao.get("odd"),

        "estagio_odd":
            selecao["estagio"],

        "media_cantos_ht":
            round(
                selecao["media_ht"],
                2
            ),

        "taxa_under":
            round(
                historico["taxa_under"]
                * 100,
                2
            ),

        "historico_wins":
            historico["wins"],

        "historico_voids":
            historico["voids"],

        "historico_losses":
            historico["losses"],

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

    odd = numero(
        selecao.get("odd")
    )

    odd_txt = (
        f"{odd:.3f}"
        if odd is not None
        else "N/D"
    )

    mensagem = (
        "🚨 <b>SINAL V10 - UNDER HT</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x {html.escape(away)}</b>\n\n"

        "🚩 <b>ESCANTEIOS - 1º TEMPO</b>\n"

        f"📉 Under "
        f"{selecao['linha']:.2f}\n"

        f"💰 Odd: "
        f"{odd_txt}\n\n"

        f"📊 Média de cantos HT: "
        f"{selecao['media_ht']:.2f}\n"

        f"✅ Under histórico: "
        f"{historico['taxa_under'] * 100:.1f}%\n"

        f"🟢 WIN: "
        f"{historico['wins']}\n"

        f"↩️ VOID: "
        f"{historico['voids']}\n"

        f"🔴 LOSS: "
        f"{historico['losses']}\n"

        f"📚 Amostra: "
        f"{historico['total']} jogos\n\n"

        f"🏦 Banca: "
        f"{banco['id']}\n"

        f"🔄 Gale: "
        f"{banco['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )

    telegram(mensagem)

    logger.info(
        "🚨 SINAL UNDER HT | "
        "%s x %s | "
        "Under %.2f @ %s | "
        "%.1f%% histórico",
        home,
        away,
        selecao["linha"],
        odd_txt,
        historico["taxa_under"] * 100
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


# ============================================================
# LIBERAR BANCA
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

    # VOID mantém Gale atual

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

    cantos_ht = extrair_cantos_ht(
        fixture
    )

    if cantos_ht is None:

        logger.warning(
            "⚠️ Fixture %s terminou "
            "sem cantos HT utilizáveis.",
            sinal["fixture_id"]
        )

        return

    linha = float(
        sinal["linha_cantos_ht"]
    )

    if cantos_ht < linha:

        resultado = "WIN"

    elif cantos_ht > linha:

        resultado = "LOSS"

    else:

        resultado = "PUSH"

    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado

    sinal["cantos_ht_final"] = (
        cantos_ht
    )

    sinal["resolvido_em"] = (
        agora_iso()
    )

    entrada = float(
        sinal["entrada"]
    )

    odd = numero(
        sinal.get("odd")
    )

    lucro = 0.0

    if resultado == "WIN":

        stats["wins"] = (
            stats.get("wins", 0)
            + 1
        )

        if odd is not None:

            lucro = (
                entrada
                * (odd - 1)
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
        f"{emoji} <b>{resultado} - UNDER HT</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"

        f"🚩 Under "
        f"{linha:.2f} escanteios HT\n"

        f"📊 Cantos no 1º tempo: "
        f"{cantos_ht:.0f}\n\n"

        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale: "
        f"{sinal['gale']}"
    )

    telegram(mensagem)

    logger.info(
        "%s | %s x %s | "
        "Under %.2f HT | "
        "cantos HT %.0f",
        resultado,
        sinal["home"],
        sinal["away"],
        linha,
        cantos_ht
    )


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

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
# ANALISAR FIXTURE
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
        "🔎 Analisando UNDER HT | "
        "%s x %s",
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
            "⛔ Histórico HT insuficiente: "
            "%s jogos.",
            len(partidas)
        )

        return

    media_ht = media_cantos_ht(
        partidas
    )

    logger.info(
        "📊 %s x %s | "
        "média cantos HT %.2f | "
        "amostra %s",
        home,
        away,
        media_ht,
        len(partidas)
    )

    odds_data = buscar_odds(
        fixture_id
    )

    if not odds_data:

        logger.info(
            "⛔ Sem odds para %s x %s.",
            home,
            away
        )

        return

    selecao = selecionar_under_ht(
        partidas,
        media_ht,
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
        "🔄 Iniciando ciclo V10 UNDER HT"
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
        "🤖 Robô V10 UNDER HT iniciado."
    )

    logger.info(
        "🚩 Estratégia: "
        "UNDER ESCANTEIOS NO 1º TEMPO"
    )

    logger.info(
        "📊 Taxa histórica mínima: %.0f%%",
        MIN_TAXA_CANTOS_HT * 100
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
            "V10.0",

        "estrategia":
            "Under escanteios 1º tempo",

        "mercado_api":
            "corner_line_half",

        "lado":
            "under",

        "taxa_minima":
            MIN_TAXA_CANTOS_HT,

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

        "total_resolvidos":
            stats.get(
                "total_resolvidos",
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
        "versao": "V10.0",
        "mercado": "corner_line_half",
        "selecao": "UNDER",
        "taxa_minima": MIN_TAXA_CANTOS_HT,
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
        "status": "ok",
        "version": "V10.0"
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
