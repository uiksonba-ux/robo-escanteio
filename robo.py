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
# ROBÔ V20
#
# 1 JOGO = 1 SINAL = 3 MERCADOS
#
# 1) UNDER ESCANTEIOS HT
# 2) OVER ESCANTEIOS FT
# 3) UNDER GOLS FT
#
# REGRAS:
# - Linha vem da API Bet365
# - Histórico testa exatamente a linha recebida
# - Cada mercado precisa atingir a taxa mínima
# - Ocorrência conjunta é apenas informativa
# - 3 bancas independentes
# - Gale 0 / Gale 1 / Gale 2
# - Resolução automática
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v20")


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
# FILTROS
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv("MIN_TAXA_UNDER_HT", "0.70")
)

MIN_TAXA_OVER_FT = float(
    os.getenv("MIN_TAXA_OVER_FT", "0.70")
)

MIN_TAXA_UNDER_GOLS = float(
    os.getenv("MIN_TAXA_UNDER_GOLS", "0.70")
)

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "20")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "20")
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

CACHE_MERCADO_TTL = int(
    os.getenv("CACHE_MERCADO_TTL", "600")
)


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_STATS = "stats_v20.json"
ARQUIVO_SINAIS = "sinais_v20.json"
ARQUIVO_BANCAS = "bancas_v20.json"
ARQUIVO_CACHE = "cache_v20.json"


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

if API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-v20/1.0"
    })


lock_dados = threading.RLock()
lock_api = threading.Lock()

stop_event = threading.Event()

historico_requisicoes = deque()

ultima_requisicao = 0.0

cache_fixtures = {
    "timestamp": 0,
    "dados": []
}

cursor_fixture = 0


# ============================================================
# JSON
# ============================================================

def carregar_json(caminho, padrao):

    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as erro:

        logger.error(
            "Erro lendo %s: %s",
            caminho,
            erro
        )

        return padrao


def salvar_json(caminho, dados):

    temporario = caminho + ".tmp"

    try:

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
            caminho
        )

    except Exception as erro:

        logger.error(
            "Erro salvando %s: %s",
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


# ============================================================
# UTILIDADES
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (TypeError, ValueError):
        return None


def pct(valor):

    return f"{valor * 100:.1f}%"


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def taxa(wins, total):

    if total <= 0:
        return 0.0

    return wins / total


def extrair_lista(dados):

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

            for sub in (
                "data",
                "fixtures",
                "results",
                "response"
            ):

                lista = valor.get(sub)

                if isinstance(lista, list):
                    return lista

    return []


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
                - (
                    agora
                    - historico_requisicoes[0]
                )
                + 1
            )

            logger.info(
                "Rate limit interno: %.1fs",
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
            "FIVE_DOLLAR_API_KEY não configurada"
        )

        return None

    url = (
        f"{BASE_API}{endpoint}"
    )

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
                    "429 recebido. "
                    "Aguardando %ss",
                    API_BACKOFF_429
                )

                time.sleep(
                    API_BACKOFF_429
                )

                continue

            if resposta.status_code in (
                400,
                401,
                403,
                404
            ):

                logger.error(
                    "API %s | %s",
                    resposta.status_code,
                    resposta.text[:500]
                )

                return None

            resposta.raise_for_status()

            return resposta.json()

        except requests.RequestException as erro:

            logger.error(
                "API %s tentativa %s/%s: %s",
                endpoint,
                tentativa,
                tentativas,
                erro
            )

            if tentativa < tentativas:
                time.sleep(5)

        except ValueError:

            logger.error(
                "JSON inválido em %s",
                endpoint
            )

            return None

    return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado"
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

    except Exception as erro:

        logger.error(
            "Telegram: %s",
            erro
        )

        return False


# ============================================================
# BANCAS
# ============================================================

def bancas_padrao():

    return [
        {
            "id": i,
            "ocupada": False,
            "sinal_id": None,
            "gale": 0
        }
        for i in range(
            1,
            TOTAL_BANCAS + 1
        )
    ]


bancas = carregar_json(
    ARQUIVO_BANCAS,
    bancas_padrao()
)


ids_existentes = {
    b.get("id")
    for b in bancas
}


for i in range(
    1,
    TOTAL_BANCAS + 1
):

    if i not in ids_existentes:

        bancas.append({
            "id": i,
            "ocupada": False,
            "sinal_id": None,
            "gale": 0
        })


def salvar_bancas():

    salvar_json(
        ARQUIVO_BANCAS,
        bancas
    )


def obter_banca_livre():

    for banca in bancas:

        if not banca.get(
            "ocupada",
            False
        ):
            return banca

    return None


def valor_entrada(gale):

    return (
        ENTRADA_INICIAL
        *
        (
            MULTIPLICADOR_GALE
            ** gale
        )
    )


# ============================================================
# ESTATÍSTICAS
# ============================================================

def estatisticas_grupo():

    wins = int(
        stats.get("wins", 0)
    )

    losses = int(
        stats.get("losses", 0)
    )

    pushes = int(
        stats.get("pushes", 0)
    )

    decisoes = (
        wins + losses
    )

    assertividade = (
        wins / decisoes
        if decisoes
        else 0.0
    )

    por_gale = {}

    for gale in range(
        MAX_GALES + 1
    ):

        dados = (
            stats
            .get("por_gale", {})
            .get(str(gale), {})
        )

        w = int(
            dados.get("wins", 0)
        )

        l = int(
            dados.get("losses", 0)
        )

        p = int(
            dados.get("pushes", 0)
        )

        decisoes_gale = (
            w + l
        )

        por_gale[str(gale)] = {
            "wins": w,
            "losses": l,
            "pushes": p,
            "taxa": (
                w / decisoes_gale
                if decisoes_gale
                else 0.0
            )
        }

    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "assertividade": assertividade,
        "resolvidos":
            wins + losses + pushes,
        "por_gale": por_gale
    }


def texto_assertividade():

    dados = estatisticas_grupo()

    decisoes = (
        dados["wins"]
        + dados["losses"]
    )

    geral = (
        pct(
            dados["assertividade"]
        )
        if decisoes
        else "Aguardando resultados"
    )

    linhas = [
        "━━━━━━━━━━━━━━━━━━",
        "📊 <b>ASSERTIVIDADE V20</b>",
        "",
        f"🎯 Geral: <b>{geral}</b>",
        (
            f"✅ {dados['wins']} WIN | "
            f"❌ {dados['losses']} LOSS | "
            f"⚪ {dados['pushes']} VOID"
        ),
        ""
    ]

    for gale in range(
        MAX_GALES + 1
    ):

        g = dados[
            "por_gale"
        ][str(gale)]

        total = (
            g["wins"]
            + g["losses"]
        )

        valor = (
            pct(g["taxa"])
            if total
            else "—"
        )

        nome = (
            "Sem Gale"
            if gale == 0
            else f"Gale {gale}"
        )

        linhas.append(
            f"🔹 {nome}: {valor}"
        )

    linhas.extend([
        "",
        (
            "📌 Sinais resolvidos: "
            f"{dados['resolvidos']}"
        ),
        "━━━━━━━━━━━━━━━━━━"
    ])

    return "\n".join(
        linhas
    )


# ============================================================
# TIMES
# ============================================================

def extrair_times(fixture):

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
# FIXTURES
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
                int(inicio.timestamp()),

            "end_time":
                int(fim.timestamp()),

            "status":
                "scheduled",

            "per_page":
                100
        }
    )

    jogos = extrair_lista(
        dados
    )

    def chave(jogo):

        if not isinstance(
            jogo,
            dict
        ):
            return 9999999999

        return (
            jogo.get("kickoff_ts")
            or jogo.get("start_time")
            or 9999999999
        )

    jogos.sort(
        key=chave
    )

    cache_fixtures[
        "timestamp"
    ] = agora

    cache_fixtures[
        "dados"
    ] = jogos

    logger.info(
        "%s fixtures encontrados",
        len(jogos)
    )

    return jogos


# ============================================================
# BET365
# ============================================================

def extrair_bet365(resposta):

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


def extrair_linha_mercado(
    mercado
):

    if not isinstance(
        mercado,
        dict
    ):
        return None

    # Pré-jogo:
    # closing > opening > inplay
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
            item.get("line")
        )

        over = numero(
            item.get("over")
        )

        under = numero(
            item.get("under")
        )

        if linha is None:
            continue

        if over is None:
            continue

        if under is None:
            continue

        if over <= 1:
            continue

        if under <= 1:
            continue

        return {
            "fase": fase,
            "linha": linha,
            "over": over,
            "under": under
        }

    return None


def consultar_mercado_bet365(
    fixture_id,
    market,
    chave
):

    resposta = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "bookmakers": "bet365",
            "market": market
        }
    )

    bookmaker = extrair_bet365(
        resposta
    )

    if not bookmaker:
        return None

    odds = bookmaker.get(
        "odds"
    )

    if not isinstance(
        odds,
        dict
    ):
        return None

    mercado = odds.get(
        chave
    )

    return extrair_linha_mercado(
        mercado
    )


# ============================================================
# 3 MERCADOS DA V20
# ============================================================

def verificar_mercados_bet365(
    fixture_id
):

    chave_cache = str(
        fixture_id
    )

    agora = time.time()

    item = (
        cache_persistente[
            "mercados"
        ].get(chave_cache)
    )

    if item:

        idade = (
            agora
            - item.get(
                "timestamp",
                0
            )
        )

        if idade < CACHE_MERCADO_TTL:

            return item.get(
                "dados",
                {}
            )

    # ========================================================
    # 1 - ESCANTEIOS HT
    # ========================================================

    cantos_ht = consultar_mercado_bet365(
        fixture_id,
        "corner_half",
        "corner_line_half"
    )

    if not cantos_ht:

        resultado = {
            "cantos_ht": None,
            "cantos_ft": None,
            "gols_ft": None
        }

    else:

        # ====================================================
        # 2 - ESCANTEIOS FT
        # ====================================================

        cantos_ft = consultar_mercado_bet365(
            fixture_id,
            "corner",
            "corner_line"
        )

        if not cantos_ft:

            resultado = {
                "cantos_ht":
                    cantos_ht,

                "cantos_ft":
                    None,

                "gols_ft":
                    None
            }

        else:

            # ================================================
            # 3 - GOLS FT
            # ================================================

            gols_ft = consultar_mercado_bet365(
                fixture_id,
                "goalline",
                "goal_line"
            )

            resultado = {
                "cantos_ht":
                    cantos_ht,

                "cantos_ft":
                    cantos_ft,

                "gols_ft":
                    gols_ft
            }

    cache_persistente[
        "mercados"
    ][chave_cache] = {
        "timestamp": agora,
        "dados": resultado
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    return resultado


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico_time(
    team_id
):

    chave = str(
        team_id
    )

    agora = time.time()

    item = (
        cache_persistente[
            "historicos"
        ].get(chave)
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

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status":
                "finished",

            "order":
                "desc",

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
        "timestamp": agora,
        "dados": jogos
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    return jogos


def obter_id_jogo(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return None

    fixture_id = (
        jogo.get("id")
        or jogo.get("fixture_id")
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
# EXTRAÇÃO DE ESCANTEIOS
# ============================================================

def extrair_cantos(jogo):

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
        corners.get("home")
    )

    ft_away = numero(
        corners.get("away")
    )

    ht_home = numero(
        corners.get("half_home")
    )

    ht_away = numero(
        corners.get("half_away")
    )

    if (
        ft_home is None
        or ft_away is None
        or ht_home is None
        or ht_away is None
    ):
        return None

    return {
        "ht":
            ht_home + ht_away,

        "ft":
            ft_home + ft_away
    }


# ============================================================
# EXTRAÇÃO DE GOLS
# ============================================================

def extrair_gols(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return None

    # --------------------------------------------------------
    # FORMATO 1:
    # goals = {"home": 2, "away": 1}
    # --------------------------------------------------------

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

            return home + away

    # --------------------------------------------------------
    # FORMATO 2:
    # score.fulltime.home / away
    # --------------------------------------------------------

    score = jogo.get(
        "score"
    )

    if isinstance(
        score,
        dict
    ):

        fulltime = score.get(
            "fulltime"
        )

        if isinstance(
            fulltime,
            dict
        ):

            home = numero(
                fulltime.get("home")
            )

            away = numero(
                fulltime.get("away")
            )

            if (
                home is not None
                and away is not None
            ):

                return home + away

    # --------------------------------------------------------
    # FORMATO 3:
    # scores.fulltime
    # --------------------------------------------------------

    scores = jogo.get(
        "scores"
    )

    if isinstance(
        scores,
        dict
    ):

        fulltime = scores.get(
            "fulltime"
        )

        if isinstance(
            fulltime,
            dict
        ):

            home = numero(
                fulltime.get("home")
            )

            away = numero(
                fulltime.get("away")
            )

            if (
                home is not None
                and away is not None
            ):

                return home + away

    return None


# ============================================================
# EXTRAIR REGISTRO COMPLETO
# ============================================================

def extrair_registro(
    jogo
):

    cantos = extrair_cantos(
        jogo
    )

    gols = extrair_gols(
        jogo
    )

    if not cantos:
        return None

    if gols is None:
        return None

    return {
        "cantos_ht":
            cantos["ht"],

        "cantos_ft":
            cantos["ft"],

        "gols_ft":
            gols
    }


# ============================================================
# RESULTADOS INDIVIDUAIS
# ============================================================

def resultado_under(
    valor,
    linha
):

    if valor < linha:
        return "WIN"

    if valor == linha:
        return "VOID"

    return "LOSS"


def resultado_over(
    valor,
    linha
):

    if valor > linha:
        return "WIN"

    if valor == linha:
        return "VOID"

    return "LOSS"


# ============================================================
# ANÁLISE HISTÓRICA V20
# ============================================================

def analisar_historico(
    jogos,
    linha_cantos_ht,
    linha_cantos_ft,
    linha_gols_ft
):

    registros = []

    for jogo in jogos:

        registro = extrair_registro(
            jogo
        )

        if registro:

            registros.append(
                registro
            )

    total = len(
        registros
    )

    if total == 0:
        return None


    # ========================================================
    # MÉDIAS
    # ========================================================

    media_cantos_ht = (
        sum(
            r["cantos_ht"]
            for r in registros
        )
        / total
    )

    media_cantos_ft = (
        sum(
            r["cantos_ft"]
            for r in registros
        )
        / total
    )

    media_gols_ft = (
        sum(
            r["gols_ft"]
            for r in registros
        )
        / total
    )


    # ========================================================
    # CONTADORES
    # ========================================================

    under_ht_wins = 0
    under_ht_voids = 0
    under_ht_losses = 0

    over_ft_wins = 0
    over_ft_voids = 0
    over_ft_losses = 0

    under_gols_wins = 0
    under_gols_voids = 0
    under_gols_losses = 0

    conjunta_wins = 0
    conjunta_voids = 0
    conjunta_losses = 0


    # ========================================================
    # BACKTEST
    # ========================================================

    for registro in registros:

        r_under_ht = resultado_under(
            registro["cantos_ht"],
            linha_cantos_ht
        )

        r_over_ft = resultado_over(
            registro["cantos_ft"],
            linha_cantos_ft
        )

        r_under_gols = resultado_under(
            registro["gols_ft"],
            linha_gols_ft
        )


        # UNDER CANTOS HT

        if r_under_ht == "WIN":
            under_ht_wins += 1

        elif r_under_ht == "VOID":
            under_ht_voids += 1

        else:
            under_ht_losses += 1


        # OVER CANTOS FT

        if r_over_ft == "WIN":
            over_ft_wins += 1

        elif r_over_ft == "VOID":
            over_ft_voids += 1

        else:
            over_ft_losses += 1


        # UNDER GOLS FT

        if r_under_gols == "WIN":
            under_gols_wins += 1

        elif r_under_gols == "VOID":
            under_gols_voids += 1

        else:
            under_gols_losses += 1


        # ====================================================
        # CONJUNTA
        # ====================================================

        resultados = [
            r_under_ht,
            r_over_ft,
            r_under_gols
        ]

        if "LOSS" in resultados:

            conjunta_losses += 1

        elif "VOID" in resultados:

            conjunta_voids += 1

        else:

            conjunta_wins += 1


    return {

        "total":
            total,

        "linha_cantos_ht":
            linha_cantos_ht,

        "linha_cantos_ft":
            linha_cantos_ft,

        "linha_gols_ft":
            linha_gols_ft,


        "media_cantos_ht":
            media_cantos_ht,

        "media_cantos_ft":
            media_cantos_ft,

        "media_gols_ft":
            media_gols_ft,


        # UNDER HT

        "under_ht_wins":
            under_ht_wins,

        "under_ht_voids":
            under_ht_voids,

        "under_ht_losses":
            under_ht_losses,

        "taxa_under_ht":
            taxa(
                under_ht_wins,
                total
            ),


        # OVER FT

        "over_ft_wins":
            over_ft_wins,

        "over_ft_voids":
            over_ft_voids,

        "over_ft_losses":
            over_ft_losses,

        "taxa_over_ft":
            taxa(
                over_ft_wins,
                total
            ),


        # UNDER GOLS

        "under_gols_wins":
            under_gols_wins,

        "under_gols_voids":
            under_gols_voids,

        "under_gols_losses":
            under_gols_losses,

        "taxa_under_gols":
            taxa(
                under_gols_wins,
                total
            ),


        # CONJUNTA

        "conjunta_wins":
            conjunta_wins,

        "conjunta_voids":
            conjunta_voids,

        "conjunta_losses":
            conjunta_losses,

        "taxa_conjunta":
            taxa(
                conjunta_wins,
                total
            )
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
        return False

    if (
        analise["taxa_under_ht"]
        < MIN_TAXA_UNDER_HT
    ):
        return False

    if (
        analise["taxa_over_ft"]
        < MIN_TAXA_OVER_FT
    ):
        return False

    if (
        analise["taxa_under_gols"]
        < MIN_TAXA_UNDER_GOLS
    ):
        return False

    return True


# ============================================================
# JÁ UTILIZADO
# ============================================================

def fixture_ja_usado(
    fixture_id
):

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
        ):
            return True

    return False


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    fixture,
    analise,
    mercados
):

    banca = obter_banca_livre()

    if not banca:

        logger.info(
            "Sem banca livre"
        )

        return False


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


    gale = int(
        banca.get(
            "gale",
            0
        )
    )


    entrada = valor_entrada(
        gale
    )


    sinal_id = (
        f"{int(time.time())}-"
        f"{fixture_id}"
    )


    sinal = {

        "id":
            sinal_id,

        "versao":
            "V20",

        "status":
            "PENDENTE",

        "resultado":
            None,

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


        # ====================================================
        # LINHAS
        # ====================================================

        "linha_cantos_ht":
            analise[
                "linha_cantos_ht"
            ],

        "linha_cantos_ft":
            analise[
                "linha_cantos_ft"
            ],

        "linha_gols_ft":
            analise[
                "linha_gols_ft"
            ],


        # ====================================================
        # MÉDIAS
        # ====================================================

        "media_cantos_ht":
            analise[
                "media_cantos_ht"
            ],

        "media_cantos_ft":
            analise[
                "media_cantos_ft"
            ],

        "media_gols_ft":
            analise[
                "media_gols_ft"
            ],


        # ====================================================
        # TAXAS
        # ====================================================

        "taxa_under_ht":
            analise[
                "taxa_under_ht"
            ],

        "taxa_over_ft":
            analise[
                "taxa_over_ft"
            ],

        "taxa_under_gols":
            analise[
                "taxa_under_gols"
            ],

        "taxa_conjunta":
            analise[
                "taxa_conjunta"
            ],


        # ====================================================
        # CONTAGENS
        # ====================================================

        "under_ht_wins":
            analise[
                "under_ht_wins"
            ],

        "under_ht_voids":
            analise[
                "under_ht_voids"
            ],

        "under_ht_losses":
            analise[
                "under_ht_losses"
            ],


        "over_ft_wins":
            analise[
                "over_ft_wins"
            ],

        "over_ft_voids":
            analise[
                "over_ft_voids"
            ],

        "over_ft_losses":
            analise[
                "over_ft_losses"
            ],


        "under_gols_wins":
            analise[
                "under_gols_wins"
            ],

        "under_gols_voids":
            analise[
                "under_gols_voids"
            ],

        "under_gols_losses":
            analise[
                "under_gols_losses"
            ],


        "conjunta_wins":
            analise[
                "conjunta_wins"
            ],

        "conjunta_voids":
            analise[
                "conjunta_voids"
            ],

        "conjunta_losses":
            analise[
                "conjunta_losses"
            ],


        "amostra":
            analise[
                "total"
            ],


        # ====================================================
        # MERCADOS BET365
        # ====================================================

        "bet365_cantos_ht":
            mercados[
                "cantos_ht"
            ],

        "bet365_cantos_ft":
            mercados[
                "cantos_ft"
            ],

        "bet365_gols_ft":
            mercados[
                "gols_ft"
            ],


        # ====================================================
        # GESTÃO
        # ====================================================

        "banca":
            banca["id"],

        "gale":
            gale,

        "entrada":
            entrada,

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
            "sinal_id"
        ] = sinal_id

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )

        salvar_bancas()


    enviar_sinal(
        sinal
    )

    return True


# ============================================================
# TELEGRAM - SINAL
# ============================================================

def enviar_sinal(
    sinal
):

    ht = (
        sinal.get(
            "bet365_cantos_ht"
        )
        or {}
    )

    ft = (
        sinal.get(
            "bet365_cantos_ft"
        )
        or {}
    )

    gols = (
        sinal.get(
            "bet365_gols_ft"
        )
        or {}
    )


    mensagem = (

        "🚨 <b>SINAL V20 — "
        "1 JOGO / 3 MERCADOS</b>\n\n"


        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"


        "1️⃣ <b>ESCANTEIOS HT</b>\n"

        f"🎯 Under "
        f"<b>{sinal['linha_cantos_ht']:.1f}</b>\n"

        "🎰 Linha API Bet365\n"

        f"💵 Odd Under: "
        f"{ht.get('under', '-')}\n"

        f"📊 Média histórica: "
        f"{sinal['media_cantos_ht']:.2f}\n"

        f"📈 Assertividade: "
        f"<b>{pct(sinal['taxa_under_ht'])}</b>\n"

        f"✅ {sinal['under_ht_wins']} WIN | "
        f"⚪ {sinal['under_ht_voids']} VOID | "
        f"❌ {sinal['under_ht_losses']} LOSS\n\n"


        "2️⃣ <b>ESCANTEIOS FT</b>\n"

        f"🎯 Over "
        f"<b>{sinal['linha_cantos_ft']:.1f}</b>\n"

        "🎰 Linha API Bet365\n"

        f"💵 Odd Over: "
        f"{ft.get('over', '-')}\n"

        f"📊 Média histórica: "
        f"{sinal['media_cantos_ft']:.2f}\n"

        f"📈 Assertividade: "
        f"<b>{pct(sinal['taxa_over_ft'])}</b>\n"

        f"✅ {sinal['over_ft_wins']} WIN | "
        f"⚪ {sinal['over_ft_voids']} VOID | "
        f"❌ {sinal['over_ft_losses']} LOSS\n\n"


        "3️⃣ <b>GOLS FT</b>\n"

        f"🎯 Under "
        f"<b>{sinal['linha_gols_ft']:.1f}</b>\n"

        "🎰 Linha API Bet365\n"

        f"💵 Odd Under: "
        f"{gols.get('under', '-')}\n"

        f"📊 Média histórica: "
        f"{sinal['media_gols_ft']:.2f}\n"

        f"📈 Assertividade: "
        f"<b>{pct(sinal['taxa_under_gols'])}</b>\n"

        f"✅ {sinal['under_gols_wins']} WIN | "
        f"⚪ {sinal['under_gols_voids']} VOID | "
        f"❌ {sinal['under_gols_losses']} LOSS\n\n"


        "━━━━━━━━━━━━━━━━━━\n"

        "🔥 <b>COMBINAÇÃO</b>\n\n"

        f"Under "
        f"{sinal['linha_cantos_ht']:.1f} "
        f"escanteios HT\n"

        f"+ Over "
        f"{sinal['linha_cantos_ft']:.1f} "
        f"escanteios FT\n"

        f"+ Under "
        f"{sinal['linha_gols_ft']:.1f} "
        f"gols FT\n\n"


        f"📊 Ocorrência conjunta histórica: "
        f"<b>{pct(sinal['taxa_conjunta'])}</b>\n"

        f"📚 Amostra: "
        f"{sinal['amostra']} jogos\n"

        "ℹ️ Conjunta apenas informativa\n\n"


        "💰 <b>GESTÃO</b>\n"

        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale: "
        f"{sinal['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {sinal['entrada']:.2f}\n\n"


        + texto_assertividade()
    )


    telegram(
        mensagem
    )


# ============================================================
# ANALISAR FIXTURE
# ============================================================

def analisar_fixture(
    fixture
):

    fixture_id = fixture.get(
        "id"
    )

    if fixture_id is None:
        return


    if fixture_ja_usado(
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
            "REPROVADO %s x %s | "
            "times sem ID",
            home,
            away
        )

        return


    logger.info(
        "V20 analisando %s x %s",
        home,
        away
    )


    # ========================================================
    # 1 - CONSULTA OS 3 MERCADOS
    # ========================================================

    mercados = verificar_mercados_bet365(
        fixture_id
    )


    cantos_ht = mercados.get(
        "cantos_ht"
    )

    cantos_ft = mercados.get(
        "cantos_ft"
    )

    gols_ft = mercados.get(
        "gols_ft"
    )


    if not cantos_ht:

        logger.info(
            "REPROVADO %s x %s | "
            "sem mercado escanteios HT",
            home,
            away
        )

        return


    if not cantos_ft:

        logger.info(
            "REPROVADO %s x %s | "
            "sem mercado escanteios FT",
            home,
            away
        )

        return


    if not gols_ft:

        logger.info(
            "REPROVADO %s x %s | "
            "sem mercado gols FT",
            home,
            away
        )

        return


    # ========================================================
    # 2 - LINHAS DA API BET365
    # ========================================================

    linha_cantos_ht = numero(
        cantos_ht.get("linha")
    )

    linha_cantos_ft = numero(
        cantos_ft.get("linha")
    )

    linha_gols_ft = numero(
        gols_ft.get("linha")
    )


    if (
        linha_cantos_ht is None
        or linha_cantos_ft is None
        or linha_gols_ft is None
    ):

        logger.info(
            "REPROVADO %s x %s | "
            "linha inválida",
            home,
            away
        )

        return


    logger.info(
        "LINHAS API | %s x %s | "
        "Cantos HT %.1f | "
        "Cantos FT %.1f | "
        "Gols FT %.1f",
        home,
        away,
        linha_cantos_ht,
        linha_cantos_ft,
        linha_gols_ft
    )


    # ========================================================
    # 3 - HISTÓRICO
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


    # ========================================================
    # 4 - TESTA AS 3 LINHAS
    # ========================================================

    analise = analisar_historico(
        historico,
        linha_cantos_ht,
        linha_cantos_ft,
        linha_gols_ft
    )


    if not analise:

        logger.info(
            "REPROVADO %s x %s | "
            "sem histórico válido",
            home,
            away
        )

        return


    logger.info(
        "%s x %s | "
        "N=%s | "
        "Under cantos HT %.1f%% | "
        "Over cantos FT %.1f%% | "
        "Under gols %.1f%% | "
        "Conjunta %.1f%%",
        home,
        away,
        analise["total"],
        analise["taxa_under_ht"] * 100,
        analise["taxa_over_ft"] * 100,
        analise["taxa_under_gols"] * 100,
        analise["taxa_conjunta"] * 100
    )


    # ========================================================
    # 5 - FILTRO 70% INDIVIDUAL
    # ========================================================

    if not passou_filtros(
        analise
    ):

        logger.info(
            "REPROVADO V20 | "
            "%s x %s | "
            "HT %.1f%% | "
            "FT %.1f%% | "
            "Gols %.1f%%",
            home,
            away,
            analise["taxa_under_ht"] * 100,
            analise["taxa_over_ft"] * 100,
            analise["taxa_under_gols"] * 100
        )

        return


    # ========================================================
    # 6 - CRIA SINAL
    # ========================================================

    logger.info(
        "APROVADO V20 | %s x %s",
        home,
        away
    )


    criar_sinal(
        fixture,
        analise,
        mercados
    )


# ============================================================
# BUSCAR FIXTURE INDIVIDUAL
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
        isinstance(response, list)
        and response
    ):
        return response[0]


    return dados


# ============================================================
# FINALIZADO
# ============================================================

def fixture_finalizado(
    fixture
):

    if not isinstance(
        fixture,
        dict
    ):
        return False


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
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal,
    fixture
):

    registro = extrair_registro(
        fixture
    )

    if not registro:
        return None


    r1 = resultado_under(
        registro["cantos_ht"],
        float(
            sinal[
                "linha_cantos_ht"
            ]
        )
    )


    r2 = resultado_over(
        registro["cantos_ft"],
        float(
            sinal[
                "linha_cantos_ft"
            ]
        )
    )


    r3 = resultado_under(
        registro["gols_ft"],
        float(
            sinal[
                "linha_gols_ft"
            ]
        )
    )


    pernas = [
        r1,
        r2,
        r3
    ]


    # ========================================================
    # LIQUIDAÇÃO
    #
    # LOSS em qualquer perna = LOSS
    #
    # VOID:
    # - a perna é anulada
    # - se as demais forem WIN, o sinal é WIN
    #
    # se TODAS forem VOID = PUSH
    # ========================================================

    if "LOSS" in pernas:

        resultado_final = "LOSS"

    else:

        ativas = [
            r
            for r in pernas
            if r != "VOID"
        ]

        if not ativas:

            resultado_final = "PUSH"

        elif all(
            r == "WIN"
            for r in ativas
        ):

            resultado_final = "WIN"

        else:

            resultado_final = "LOSS"


    return {

        "resultado":
            resultado_final,

        "under_cantos_ht":
            r1,

        "over_cantos_ft":
            r2,

        "under_gols_ft":
            r3,

        "cantos_ht":
            registro[
                "cantos_ht"
            ],

        "cantos_ft":
            registro[
                "cantos_ft"
            ],

        "gols_ft":
            registro[
                "gols_ft"
            ]
    }


# ============================================================
# LIBERAR BANCA
# ============================================================

def liberar_banca(
    banca,
    resultado
):

    if resultado == "WIN":

        banca["gale"] = 0


    elif resultado == "LOSS":

        gale_atual = int(
            banca.get(
                "gale",
                0
            )
        )

        if gale_atual < MAX_GALES:

            banca["gale"] = (
                gale_atual + 1
            )

        else:

            banca["gale"] = 0


    # PUSH mantém Gale

    banca["ocupada"] = False
    banca["sinal_id"] = None

    salvar_bancas()


# ============================================================
# REGISTRAR STATS
# ============================================================

def registrar_resultado_stats(
    sinal,
    resultado
):

    if resultado == "WIN":

        stats["wins"] = (
            int(
                stats.get(
                    "wins",
                    0
                )
            )
            + 1
        )


    elif resultado == "LOSS":

        stats["losses"] = (
            int(
                stats.get(
                    "losses",
                    0
                )
            )
            + 1
        )


    else:

        stats["pushes"] = (
            int(
                stats.get(
                    "pushes",
                    0
                )
            )
            + 1
        )


    stats[
        "total_resolvidos"
    ] = (
        int(
            stats.get(
                "total_resolvidos",
                0
            )
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
        ][gale]["wins"] += 1


    elif resultado == "LOSS":

        stats[
            "por_gale"
        ][gale]["losses"] += 1


    else:

        stats[
            "por_gale"
        ][gale]["pushes"] += 1


    salvar_json(
        ARQUIVO_STATS,
        stats
    )


# ============================================================
# RESULTADO TELEGRAM
# ============================================================

def enviar_resultado(
    sinal,
    resultado
):

    final = resultado[
        "resultado"
    ]


    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "⚪"
    }.get(
        final,
        "ℹ️"
    )


    mensagem = (

        f"{emoji} <b>RESULTADO V20 — "
        f"{final}</b>\n\n"


        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"


        "1️⃣ <b>ESCANTEIOS HT</b>\n"

        f"🎯 Under "
        f"{sinal['linha_cantos_ht']:.1f}\n"

        f"🚩 Resultado: "
        f"{resultado['cantos_ht']:.0f} cantos\n"

        f"📌 <b>"
        f"{resultado['under_cantos_ht']}"
        f"</b>\n\n"


        "2️⃣ <b>ESCANTEIOS FT</b>\n"

        f"🎯 Over "
        f"{sinal['linha_cantos_ft']:.1f}\n"

        f"🚩 Resultado: "
        f"{resultado['cantos_ft']:.0f} cantos\n"

        f"📌 <b>"
        f"{resultado['over_cantos_ft']}"
        f"</b>\n\n"


        "3️⃣ <b>GOLS FT</b>\n"

        f"🎯 Under "
        f"{sinal['linha_gols_ft']:.1f}\n"

        f"⚽ Resultado: "
        f"{resultado['gols_ft']:.0f} gols\n"

        f"📌 <b>"
        f"{resultado['under_gols_ft']}"
        f"</b>\n\n"


        "━━━━━━━━━━━━━━━━━━\n"

        f"🔥 Resultado do sinal: "
        f"<b>{final}</b>\n\n"


        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale usado: "
        f"{sinal['gale']}\n\n"


        + texto_assertividade()
    )


    telegram(
        mensagem
    )


# ============================================================
# VERIFICAR RESULTADOS
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


        resultado = resolver_sinal(
            sinal,
            fixture
        )


        if not resultado:

            logger.info(
                "Fixture %s finalizado "
                "mas sem dados suficientes",
                sinal[
                    "fixture_id"
                ]
            )

            continue


        final = resultado[
            "resultado"
        ]


        with lock_dados:

            sinal["status"] = (
                "RESOLVIDO"
            )

            sinal["resultado"] = (
                final
            )

            sinal[
                "detalhes_resultado"
            ] = resultado

            sinal[
                "resolvido_em"
            ] = agora_iso()


            registrar_resultado_stats(
                sinal,
                final
            )


            banca = next(
                (
                    b
                    for b in bancas
                    if b.get("id")
                    == sinal.get("banca")
                ),
                None
            )


            if banca:

                liberar_banca(
                    banca,
                    final
                )


            salvar_json(
                ARQUIVO_SINAIS,
                sinais
            )


        enviar_resultado(
            sinal,
            resultado
        )


# ============================================================
# CICLO
# ============================================================

def ciclo():

    global cursor_fixture


    logger.info(
        "======================================"
    )

    logger.info(
        "CICLO V20 | Bancas livres=%s",
        sum(
            1
            for b in bancas
            if not b.get(
                "ocupada",
                False
            )
        )
    )


    try:

        # ====================================================
        # PRIMEIRO RESOLVE SINAIS
        # ====================================================

        verificar_resultados()


        # ====================================================
        # SEM BANCA = NÃO CRIA NOVOS SINAIS
        # ====================================================

        if obter_banca_livre() is None:

            logger.info(
                "Todas as bancas ocupadas"
            )

            return


        # ====================================================
        # BUSCA PARTIDAS
        # ====================================================

        fixtures = buscar_fixtures()


        if not fixtures:

            logger.info(
                "Nenhum fixture agendado"
            )

            return


        total = len(
            fixtures
        )


        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )


        # ====================================================
        # ANALISA
        # ====================================================

        for _ in range(
            quantidade
        ):

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
                    "Erro analisando fixture"
                )


            if obter_banca_livre() is None:

                break


    except Exception:

        logger.exception(
            "Erro no ciclo V20"
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "======================================"
    )

    logger.info(
        "ROBÔ V20 INICIADO"
    )

    logger.info(
        "1 JOGO / 3 MERCADOS"
    )

    logger.info(
        "Under cantos HT >= %.1f%%",
        MIN_TAXA_UNDER_HT * 100
    )

    logger.info(
        "Over cantos FT >= %.1f%%",
        MIN_TAXA_OVER_FT * 100
    )

    logger.info(
        "Under gols FT >= %.1f%%",
        MIN_TAXA_UNDER_GOLS * 100
    )

    logger.info(
        "Linhas = API Bet365"
    )

    logger.info(
        "======================================"
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

    grupo = estatisticas_grupo()

    return jsonify({

        "status":
            "online",

        "versao":
            "V20",

        "estrategia":
            "1 jogo / 3 mercados",

        "mercados": [
            "Under escanteios HT",
            "Over escanteios FT",
            "Under gols FT"
        ],

        "fonte_linhas":
            "Bet365 API",

        "taxa_min_under_cantos_ht":
            MIN_TAXA_UNDER_HT,

        "taxa_min_over_cantos_ft":
            MIN_TAXA_OVER_FT,

        "taxa_min_under_gols_ft":
            MIN_TAXA_UNDER_GOLS,

        "historico_por_time":
            QTD_HISTORICO_TIME,

        "amostra_minima":
            MIN_JOGOS_HISTORICO,

        "wins":
            grupo["wins"],

        "losses":
            grupo["losses"],

        "pushes":
            grupo["pushes"],

        "assertividade":
            round(
                grupo[
                    "assertividade"
                ] * 100,
                2
            ),

        "bancas":
            bancas
    })


@app.route("/status")
def status():

    return jsonify({

        "online":
            True,

        "versao":
            "V20",

        "jogos_por_sinal":
            1,

        "mercados_por_sinal":
            3,

        "mercados": {
            "1":
                "Under escanteios HT",

            "2":
                "Over escanteios FT",

            "3":
                "Under gols FT"
        },

        "fonte_linhas":
            "Bet365 API",

        "taxas_minimas": {
            "under_cantos_ht":
                MIN_TAXA_UNDER_HT,

            "over_cantos_ft":
                MIN_TAXA_OVER_FT,

            "under_gols_ft":
                MIN_TAXA_UNDER_GOLS
        },

        "historico_por_time":
            QTD_HISTORICO_TIME,

        "amostra_minima":
            MIN_JOGOS_HISTORICO,

        "janela_horas":
            JANELA_HORAS
    })


@app.route("/stats")
def rota_stats():

    return jsonify(
        estatisticas_grupo()
    )


@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais[-100:]
    )


@app.route("/bancas")
def rota_bancas():

    return jsonify(
        bancas
    )


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "version": "V20"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v20"
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
