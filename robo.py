import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Gestão
STAKE_BASE = float(os.getenv("STAKE_BASE", "10"))

GALES = [
    10,
    20,
    40,
    80,
    160
]

NUM_BANCAS = 3


# ============================================================
# ODDS
# ============================================================

ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))
ODD_MIN = float(os.getenv("ODD_MIN", "1.85"))
ODD_MAX = float(os.getenv("ODD_MAX", "2.50"))


# ============================================================
# HISTÓRICO
# ============================================================

ULTIMOS_JOGOS = int(os.getenv("ULTIMOS_JOGOS", "5"))

MINIMO_AMOSTRAS = int(
    os.getenv("MINIMO_AMOSTRAS", "4")
)


# ============================================================
# ASSERTIVIDADE
# ============================================================

ASSERTIVIDADE_2_MIN = float(
    os.getenv("ASSERTIVIDADE_2_MIN", "70")
)

ASSERTIVIDADE_3_MIN = float(
    os.getenv("ASSERTIVIDADE_3_MIN", "60")
)

ASSERTIVIDADE_MEDIA_3_MIN = float(
    os.getenv("ASSERTIVIDADE_MEDIA_3_MIN", "65")
)


# ============================================================
# INTERVALOS
# ============================================================

HORAS_FUTUROS = float(
    os.getenv("HORAS_FUTUROS", "24")
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

MAX_CANDIDATOS = int(
    os.getenv("MAX_CANDIDATOS", "4")
)


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_STATS = "stats.json"
ARQUIVO_CACHE = "historico_cache.json"


# ============================================================
# LOCK
# ============================================================

lock = threading.Lock()


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("ROBO")


# ============================================================
# ESTATÍSTICAS
# ============================================================

stats_default = {
    "wins": 0,
    "losses": 0,
    "push": 0,
    "sinais": 0,

    "banca1_wins": 0,
    "banca1_losses": 0,

    "banca2_wins": 0,
    "banca2_losses": 0,

    "banca3_wins": 0,
    "banca3_losses": 0
}


if os.path.exists(ARQUIVO_STATS):

    try:

        with open(
            ARQUIVO_STATS,
            "r",
            encoding="utf-8"
        ) as f:

            stats = json.load(f)

        for chave, valor in stats_default.items():

            if chave not in stats:
                stats[chave] = valor

    except Exception as e:

        logger.error(
            f"Erro carregando stats: {e}"
        )

        stats = stats_default.copy()

else:

    stats = stats_default.copy()


# ============================================================
# BANCAS
# ============================================================

bancas = {}

for i in range(1, NUM_BANCAS + 1):

    bancas[i] = {

        "status": "livre",

        "fixture_id": None,

        "sinal": None,

        "gale": 0,

        "entrada": GALES[0],

        "numero_sinal": 0
    }


# ============================================================
# CACHE
# ============================================================

historico_cache = {}

futuros_cache = {
    "timestamp": 0,
    "dados": []
}


fixture_usados = set()

api_last_error = ""

api_calls = []

thread_started = False


# ============================================================
# STATS
# ============================================================

def salvar_stats():

    try:

        with open(
            ARQUIVO_STATS,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                stats,
                f,
                indent=2,
                ensure_ascii=False
            )

    except Exception as e:

        logger.error(
            f"Erro salvando stats: {e}"
        )


# ============================================================
# CACHE
# ============================================================

def salvar_cache_historico():

    try:

        with open(
            ARQUIVO_CACHE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                historico_cache,
                f,
                indent=2,
                ensure_ascii=False
            )

    except Exception as e:

        logger.error(
            f"Erro salvando cache: {e}"
        )


def carregar_cache_historico():

    global historico_cache

    if not os.path.exists(
        ARQUIVO_CACHE
    ):
        return

    try:

        with open(
            ARQUIVO_CACHE,
            "r",
            encoding="utf-8"
        ) as f:

            historico_cache = json.load(f)

    except Exception as e:

        logger.error(
            f"Erro carregando cache: {e}"
        )

        historico_cache = {}


carregar_cache_historico()


# ============================================================
# CONTROLE DE REQUESTS
# ============================================================

def pode_fazer_request():

    global api_calls

    agora = time.time()

    api_calls = [
        t for t in api_calls
        if agora - t < 60
    ]

    # Margem de segurança abaixo do limite da API
    if len(api_calls) >= 9:
        return False

    api_calls.append(agora)

    return True


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    global api_last_error

    if not API_KEY:

        api_last_error = (
            "FIVE_DOLLAR_KEY não configurada"
        )

        logger.error(api_last_error)

        return None

    if not pode_fazer_request():

        api_last_error = (
            "Limite interno de requests/minuto atingido"
        )

        logger.warning(api_last_error)

        return None

    url = (
        BASE_API.rstrip("/")
        + "/"
        + endpoint.lstrip("/")
    )

    headers = {
        "X-API-Key": API_KEY,
        "Accept": "application/json"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            params=params or {},
            timeout=25
        )

        if response.status_code == 200:

            api_last_error = ""

            return response.json()

        if response.status_code == 429:

            retry = response.headers.get(
                "Retry-After",
                "60"
            )

            api_last_error = (
                f"Rate limit. Retry-After={retry}"
            )

            logger.warning(
                api_last_error
            )

            return None

        api_last_error = (
            f"HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

        logger.error(api_last_error)

        return None

    except Exception as e:

        api_last_error = str(e)

        logger.error(
            f"Erro API: {e}"
        )

        return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(text):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado"
        )

        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if response.status_code == 200:
            return True

        logger.error(
            f"Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text}"
        )

        return False

    except Exception as e:

        logger.error(
            f"Erro Telegram: {e}"
        )

        return False


# ============================================================
# UTILIDADES
# ============================================================

def agora_utc():

    return datetime.now(
        timezone.utc
    )


def parse_data(valor):

    if not valor:
        return None

    try:

        if isinstance(valor, datetime):
            return valor

        valor = str(valor)

        valor = valor.replace(
            "Z",
            "+00:00"
        )

        return datetime.fromisoformat(
            valor
        )

    except Exception:

        return None


def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except Exception:

        return None


def nome_time(team):

    if not isinstance(team, dict):
        return "?"

    return (
        team.get("name")
        or team.get("display_name")
        or team.get("short_name")
        or "?"
    )


def get_team_id(team):

    if not isinstance(team, dict):
        return None

    return (
        team.get("id")
        or team.get("team_id")
    )


def get_fixture_id(fixture):

    if not isinstance(fixture, dict):
        return None

    return (
        fixture.get("id")
        or fixture.get("fixture_id")
    )


# ============================================================
# TIMES DO JOGO
# ============================================================

def obter_times_fixture(fixture):

    home = (
        fixture.get("home_team")
        or fixture.get("home")
        or {}
    )

    away = (
        fixture.get("away_team")
        or fixture.get("away")
        or {}
    )

    return home, away


# ============================================================
# GOLS
# ============================================================

def obter_gols(fixture):

    home = fixture.get(
        "home_score"
    )

    away = fixture.get(
        "away_score"
    )

    if isinstance(home, dict):

        home = (
            home.get("current")
            or home.get("display")
            or home.get("total")
            or home.get("goals")
        )

    if isinstance(away, dict):

        away = (
            away.get("current")
            or away.get("display")
            or away.get("total")
            or away.get("goals")
        )

    home = numero(home)
    away = numero(away)

    if home is None or away is None:

        score = fixture.get("score")

        if isinstance(score, dict):

            home = numero(
                score.get("home")
            )

            away = numero(
                score.get("away")
            )

    if home is None or away is None:
        return None

    return home, away


# ============================================================
# ESCANTEIOS
# ============================================================

def obter_escanteios(fixture):

    home = fixture.get(
        "home_corners"
    )

    away = fixture.get(
        "away_corners"
    )

    if home is None:

        home = fixture.get(
            "corners_home"
        )

    if away is None:

        away = fixture.get(
            "corners_away"
        )

    if home is None or away is None:

        corners = fixture.get(
            "corners"
        )

        if isinstance(corners, dict):

            home = numero(
                corners.get("home")
            )

            away = numero(
                corners.get("away")
            )

    home = numero(home)
    away = numero(away)

    if home is None or away is None:
        return None

    return home + away


# ============================================================
# BOOKMAKER / ODDS
# ============================================================

def encontrar_bookmaker(fixture):

    odds = fixture.get("odds")

    if isinstance(odds, dict):

        return odds

    bookmakers = fixture.get(
        "bookmakers"
    )

    if not isinstance(
        bookmakers,
        list
    ):

        return {}

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome = str(
            bookmaker.get("slug")
            or bookmaker.get("name")
            or ""
        ).lower()

        if "bet365" in nome:

            resultado = bookmaker.get(
                "odds",
                {}
            )

            if isinstance(
                resultado,
                dict
            ):

                return resultado

    if bookmakers:

        primeiro = bookmakers[0]

        if isinstance(
            primeiro,
            dict
        ):

            resultado = primeiro.get(
                "odds",
                {}
            )

            if isinstance(
                resultado,
                dict
            ):

                return resultado

    return {}


# ============================================================
# ODDS - EXTRAÇÃO
# ============================================================

def obter_bloco_odd(mercado):

    if not isinstance(
        mercado,
        dict
    ):
        return None

    for chave in [
        "closing",
        "opening",
        "inplay"
    ]:

        bloco = mercado.get(
            chave
        )

        if isinstance(
            bloco,
            dict
        ):

            return bloco

    return None


def extrair_linha(mercado):

    bloco = obter_bloco_odd(
        mercado
    )

    if not bloco:
        return None

    return numero(
        bloco.get("line")
    )


def extrair_odd(mercado, campos):

    if not isinstance(
        mercado,
        dict
    ):
        return None

    if isinstance(
        campos,
        str
    ):
        campos = [campos]

    for periodo in [
        "closing",
        "opening",
        "inplay"
    ]:

        bloco = mercado.get(
            periodo
        )

        if not isinstance(
            bloco,
            dict
        ):
            continue

        for campo in campos:

            valor = numero(
                bloco.get(campo)
            )

            if valor is not None and valor > 1:

                return valor

    return None


# ============================================================
# HISTÓRICO DO TIME
# ============================================================

def buscar_historico_time(team_id):

    if not team_id:
        return []

    chave = str(team_id)

    agora = time.time()

    cache = historico_cache.get(
        chave
    )

    if isinstance(
        cache,
        dict
    ):

        timestamp = cache.get(
            "timestamp",
            0
        )

        if (
            agora - timestamp
            < HISTORICO_TTL
        ):

            return cache.get(
                "jogos",
                []
            )

    data = api_get(
        f"/teams/{team_id}/fixtures",
        {
            "status": "finished",
            "per_page": 20,
            "include": "stats,odds"
        }
    )

    if not data:
        return []

    if isinstance(
        data,
        dict
    ):

        jogos = data.get(
            "data",
            []
        )

    else:

        jogos = data

    if not isinstance(
        jogos,
        list
    ):

        return []

    jogos = sorted(
        jogos,
        key=lambda x: (
            parse_data(
                x.get("start_time")
                or x.get("date")
            )
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    jogos = jogos[
        :ULTIMOS_JOGOS
    ]

    historico_cache[chave] = {
        "timestamp": agora,
        "jogos": jogos
    }

    salvar_cache_historico()

    return jogos


# ============================================================
# RESULTADO DO JOGO
# ============================================================

def resultado_jogo(
    fixture,
    team_id
):

    home, away = obter_times_fixture(
        fixture
    )

    home_id = get_team_id(home)
    away_id = get_team_id(away)

    gols = obter_gols(
        fixture
    )

    if gols is None:
        return None

    gh, ga = gols

    if team_id == home_id:

        if gh > ga:
            return "1"

        if gh == ga:
            return "X"

        return "2"

    if team_id == away_id:

        if ga > gh:
            return "2"

        if gh == ga:
            return "X"

        return "1"

    return None


# ============================================================
# TAXA UNDER GOLS
# ============================================================

def taxa_under_gols(
    jogos,
    linha
):

    resultados = []

    for jogo in jogos:

        gols = obter_gols(
            jogo
        )

        if gols is None:
            continue

        total = (
            gols[0]
            + gols[1]
        )

        resultados.append(
            total < linha
        )

    if len(resultados) < MINIMO_AMOSTRAS:
        return None

    return (
        sum(resultados)
        / len(resultados)
        * 100
    )


# ============================================================
# TAXA OVER ESCANTEIOS
# ============================================================

def taxa_over_corners(
    jogos,
    linha
):

    resultados = []

    for jogo in jogos:

        corners = obter_escanteios(
            jogo
        )

        if corners is None:
            continue

        resultados.append(
            corners > linha
        )

    if len(resultados) < MINIMO_AMOSTRAS:
        return None

    return (
        sum(resultados)
        / len(resultados)
        * 100
    )


# ============================================================
# TAXA RESULTADO
# ============================================================

def taxa_resultado(
    jogos,
    team_id,
    tipo
):

    resultados = []

    for jogo in jogos:

        resultado = resultado_jogo(
            jogo,
            team_id
        )

        if resultado is None:
            continue

        if tipo == "1X":

            acertou = (
                resultado
                in ["1", "X"]
            )

        elif tipo == "X2":

            acertou = (
                resultado
                in ["X", "2"]
            )

        elif tipo == "12":

            acertou = (
                resultado
                in ["1", "2"]
            )

        elif tipo == "1":

            acertou = (
                resultado == "1"
            )

        elif tipo == "X":

            acertou = (
                resultado == "X"
            )

        elif tipo == "2":

            acertou = (
                resultado == "2"
            )

        else:

            continue

        resultados.append(
            acertou
        )

    if len(resultados) < MINIMO_AMOSTRAS:
        return None

    return (
        sum(resultados)
        / len(resultados)
        * 100
    )


# ============================================================
# MERCADO DE RESULTADO
# ============================================================

def encontrar_resultado_mercado(
    odds
):

    nomes = [
        "double_chance",
        "doublechance",
        "1x2",
        "match_result",
        "result"
    ]

    for nome in nomes:

        mercado = odds.get(
            nome
        )

        if isinstance(
            mercado,
            dict
        ):

            return mercado

    return None


# ============================================================
# GERAR MERCADOS
# ============================================================

def gerar_mercados(
    fixture,
    historico_home,
    historico_away,
    home_id,
    away_id
):

    mercados = []

    odds = encontrar_bookmaker(
        fixture
    )

    if not odds:
        return mercados


    # ========================================================
    # UNDER GOLS
    # ========================================================

    goal_line = odds.get(
        "goal_line"
    )

    if isinstance(
        goal_line,
        dict
    ):

        linha = extrair_linha(
            goal_line
        )

        # SOMENTE LINHAS ATÉ 2.5
        if (
            linha is not None
            and linha > 0
            and linha <= 2.5
            and linha % 1 == 0.5
        ):

            odd = extrair_odd(
                goal_line,
                [
                    "under"
                ]
            )

            if odd:

                taxa_h = taxa_under_gols(
                    historico_home,
                    linha
                )

                taxa_a = taxa_under_gols(
                    historico_away,
                    linha
                )

                taxas = [
                    x
                    for x in [
                        taxa_h,
                        taxa_a
                    ]
                    if x is not None
                ]

                if taxas:

                    taxa = (
                        sum(taxas)
                        / len(taxas)
                    )

                    mercados.append({

                        "tipo":
                            "under_gols",

                        "nome":
                            f"Under {linha:.1f} gols",

                        "linha":
                            linha,

                        "odd":
                            odd,

                        "taxa":
                            taxa,

                        "amostras":
                            len(taxas)
                    })


    # ========================================================
    # OVER ESCANTEIOS
    # ========================================================

    corner_line = odds.get(
        "corner_line"
    )

    if isinstance(
        corner_line,
        dict
    ):

        linha = extrair_linha(
            corner_line
        )

        if (
            linha is not None
            and linha > 0
            and linha % 1 == 0.5
        ):

            odd = extrair_odd(
                corner_line,
                [
                    "over"
                ]
            )

            if odd:

                taxa_h = taxa_over_corners(
                    historico_home,
                    linha
                )

                taxa_a = taxa_over_corners(
                    historico_away,
                    linha
                )

                taxas = [
                    x
                    for x in [
                        taxa_h,
                        taxa_a
                    ]
                    if x is not None
                ]

                if taxas:

                    taxa = (
                        sum(taxas)
                        / len(taxas)
                    )

                    mercados.append({

                        "tipo":
                            "over_corners",

                        "nome":
                            f"Over {linha:.1f} escanteios",

                        "linha":
                            linha,

                        "odd":
                            odd,

                        "taxa":
                            taxa,

                        "amostras":
                            len(taxas)
                    })


    # ========================================================
    # DUPLA CHANCE
    # ========================================================

    resultado = encontrar_resultado_mercado(
        odds
    )

    if isinstance(
        resultado,
        dict
    ):

        possibilidades = [

            (
                "1X",
                [
                    "1x",
                    "home_draw",
                    "home_or_draw"
                ]
            ),

            (
                "X2",
                [
                    "x2",
                    "draw_away",
                    "draw_or_away"
                ]
            ),

            (
                "12",
                [
                    "12",
                    "home_away",
                    "home_or_away"
                ]
            )
        ]

        for nome, campos in possibilidades:

            odd = extrair_odd(
                resultado,
                campos
            )

            if not odd:
                continue

            taxa_h = taxa_resultado(
                historico_home,
                home_id,
                nome
            )

            taxa_a = taxa_resultado(
                historico_away,
                away_id,
                nome
            )

            taxas = [
                x
                for x in [
                    taxa_h,
                    taxa_a
                ]
                if x is not None
            ]

            if not taxas:
                continue

            taxa = (
                sum(taxas)
                / len(taxas)
            )

            mercados.append({

                "tipo":
                    "resultado",

                "nome":
                    nome,

                "linha":
                    None,

                "odd":
                    odd,

                "taxa":
                    taxa,

                "amostras":
                    len(taxas)
            })


    return mercados


# ============================================================
# COMBINAÇÕES
# ============================================================

def gerar_combinacoes(
    mercados
):

    combinacoes = []

    quantidade = len(
        mercados
    )


    # ========================================================
    # 2 MERCADOS
    # ========================================================

    for i in range(
        quantidade
    ):

        for j in range(
            i + 1,
            quantidade
        ):

            a = mercados[i]
            b = mercados[j]

            if a["tipo"] == b["tipo"]:
                continue

            odd = (
                a["odd"]
                * b["odd"]
            )

            taxa = (
                a["taxa"]
                + b["taxa"]
            ) / 2

            if not (
                ODD_MIN
                <= odd
                <= ODD_MAX
            ):
                continue

            if (
                a["taxa"]
                < ASSERTIVIDADE_2_MIN
            ):
                continue

            if (
                b["taxa"]
                < ASSERTIVIDADE_2_MIN
            ):
                continue

            combinacoes.append({

                "mercados": [
                    a,
                    b
                ],

                "odd_combinada":
                    odd,

                "assertividade":
                    taxa,

                "quantidade":
                    2
            })


    # ========================================================
    # 3 MERCADOS
    # ========================================================

    for i in range(
        quantidade
    ):

        for j in range(
            i + 1,
            quantidade
        ):

            for k in range(
                j + 1,
                quantidade
            ):

                lista = [
                    mercados[i],
                    mercados[j],
                    mercados[k]
                ]

                tipos = [
                    x["tipo"]
                    for x in lista
                ]

                if len(set(tipos)) != 3:
                    continue

                odd = 1.0

                for mercado in lista:

                    odd *= mercado[
                        "odd"
                    ]

                taxa = (
                    sum(
                        x["taxa"]
                        for x in lista
                    )
                    / 3
                )

                if not (
                    ODD_MIN
                    <= odd
                    <= ODD_MAX
                ):
                    continue

                if any(
                    x["taxa"]
                    < ASSERTIVIDADE_3_MIN
                    for x in lista
                ):
                    continue

                if (
                    taxa
                    < ASSERTIVIDADE_MEDIA_3_MIN
                ):
                    continue

                combinacoes.append({

                    "mercados":
                        lista,

                    "odd_combinada":
                        odd,

                    "assertividade":
                        taxa,

                    "quantidade":
                        3
                })


    # ========================================================
    # MELHOR ODD = MAIS PRÓXIMA DE 2.00
    # ========================================================

    combinacoes.sort(

        key=lambda x: (

            abs(
                x["odd_combinada"]
                - ODD_ALVO
            ),

            -x["assertividade"],

            -x["quantidade"]
        )
    )

    return combinacoes


# ============================================================
# ANALISAR JOGO
# ============================================================

def analisar_jogo(
    fixture
):

    fixture_id = get_fixture_id(
        fixture
    )

    if not fixture_id:
        return None

    home, away = obter_times_fixture(
        fixture
    )

    home_id = get_team_id(
        home
    )

    away_id = get_team_id(
        away
    )

    if not home_id or not away_id:
        return None

    historico_home = buscar_historico_time(
        home_id
    )

    historico_away = buscar_historico_time(
        away_id
    )

    if not historico_home:
        return None

    if not historico_away:
        return None

    mercados = gerar_mercados(

        fixture,

        historico_home,

        historico_away,

        home_id,

        away_id
    )

    if len(mercados) < 2:
        return None

    combinacoes = gerar_combinacoes(
        mercados
    )

    if not combinacoes:
        return None

    melhor = combinacoes[0]

    league = fixture.get(
        "league"
    )

    if isinstance(
        league,
        dict
    ):

        league = league.get(
            "name"
        )

    return {

        "fixture_id":
            fixture_id,

        "home":
            nome_time(home),

        "away":
            nome_time(away),

        "league":
            league,

        "start_time":
            fixture.get(
                "start_time"
            )
            or fixture.get(
                "date"
            ),

        "mercados":
            melhor["mercados"],

        "odd_combinada":
            melhor["odd_combinada"],

        "assertividade":
            melhor["assertividade"],

        "quantidade":
            melhor["quantidade"]
    }


# ============================================================
# JOGOS FUTUROS
# ============================================================

def buscar_futuros():

    global futuros_cache

    agora = time.time()

    if (
        agora
        - futuros_cache["timestamp"]
        < FUTUROS_TTL
    ):

        return futuros_cache["dados"]

    inicio = agora_utc()

    fim = (
        inicio
        + timedelta(
            hours=HORAS_FUTUROS
        )
    )

    params = {

        "start_time":
            inicio.isoformat(),

        "end_time":
            fim.isoformat(),

        "status":
            "scheduled",

        "include":
            "odds",

        "per_page":
            100
    }

    data = api_get(
        "/fixtures",
        params
    )

    if not data:

        return futuros_cache[
            "dados"
        ]

    if isinstance(
        data,
        dict
    ):

        jogos = data.get(
            "data",
            []
        )

    else:

        jogos = data

    if not isinstance(
        jogos,
        list
    ):

        jogos = []

    futuros_cache = {

        "timestamp":
            agora,

        "dados":
            jogos
    }

    return jogos


# ============================================================
# ASSERTIVIDADE GERAL
# ============================================================

def assertividade_geral():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    if total <= 0:
        return 0.0

    return (
        stats["wins"]
        / total
        * 100
    )


# ============================================================
# HORÁRIO BRASIL
# ============================================================

def horario_br(data):

    dt = parse_data(
        data
    )

    if not dt:
        return "N/D"

    try:

        dt = dt.astimezone(
            timezone(
                timedelta(
                    hours=-3
                )
            )
        )

        return dt.strftime(
            "%d/%m %H:%M"
        )

    except Exception:

        return str(data)


# ============================================================
# FORMATAR SINAL
# ============================================================

def formatar_sinal(
    banca_id,
    sinal
):

    numero_sinal = (
        bancas[banca_id][
            "numero_sinal"
        ]
        + 1
    )

    bancas[banca_id][
        "numero_sinal"
    ] = numero_sinal

    odd = sinal[
        "odd_combinada"
    ]

    assertividade = sinal[
        "assertividade"
    ]

    gale_atual = bancas[
        banca_id
    ][
        "gale"
    ]

    entrada = GALES[
        gale_atual
    ]

    linhas = [

        (
            f"🚨 <b>BANCA "
            f"{banca_id} — NOVO "
            f"SINAL #{numero_sinal}</b>"
        ),

        "",

        (
            f"⚽ <b>{sinal['home']} "
            f"x {sinal['away']}</b>"
        )
    ]

    if sinal.get(
        "league"
    ):

        linhas.append(
            f"🏆 {sinal['league']}"
        )

    linhas.append(
        f"⏰ {horario_br(sinal.get('start_time'))}"
    )

    linhas += [

        "",

        "🎯 <b>ENTRADA COMBINADA</b>"
    ]

    for mercado in sinal[
        "mercados"
    ]:

        linhas.append(

            f"• {mercado['nome']} "
            f"— Odd {mercado['odd']:.2f}"
        )

        linhas.append(

            f"  📊 Histórico: "
            f"{mercado['taxa']:.1f}%"
        )

    linhas += [

        "",

        (
            f"💰 <b>ODD COMBINADA: "
            f"{odd:.2f}</b>"
        ),

        (
            f"🎯 Alvo geral: "
            f"{ODD_ALVO:.2f}"
        ),

        (
            f"📊 <b>Assertividade histórica: "
            f"{assertividade:.1f}%</b>"
        ),

        "",

        (
            f"💵 <b>GESTÃO — "
            f"BANCA {banca_id}</b>"
        ),

        (
            f"Entrada atual: "
            f"R$ {entrada:.2f}"
        ),

        (
            f"Gale 1: "
            f"R$ {GALES[1]:.2f}"
        ),

        (
            f"Gale 2: "
            f"R$ {GALES[2]:.2f}"
        ),

        (
            f"Gale 3: "
            f"R$ {GALES[3]:.2f}"
        ),

        (
            f"Gale 4: "
            f"R$ {GALES[4]:.2f}"
        )
    ]

    return "\n".join(
        linhas
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(
    banca_id,
    sinal
):

    texto = formatar_sinal(
        banca_id,
        sinal
    )

    if not telegram(
        texto
    ):

        return False

    bancas[banca_id][
        "status"
    ] = "ocupada"

    bancas[banca_id][
        "fixture_id"
    ] = sinal[
        "fixture_id"
    ]

    bancas[banca_id][
        "sinal"
    ] = sinal

    bancas[banca_id][
        "entrada"
    ] = GALES[
        bancas[banca_id]["gale"]
    ]

    stats[
        "sinais"
    ] += 1

    salvar_stats()

    fixture_usados.add(
        str(
            sinal[
                "fixture_id"
            ]
        )
    )

    logger.info(
        f"Banca {banca_id}: "
        f"sinal enviado"
    )

    return True


# ============================================================
# RESOLUÇÃO DOS MERCADOS
# ============================================================

def resolver_mercado(
    mercado,
    fixture,
    home_id
):

    tipo = mercado[
        "tipo"
    ]

    linha = mercado.get(
        "linha"
    )


    # --------------------------------------------------------
    # UNDER GOLS
    # --------------------------------------------------------

    if tipo == "under_gols":

        gols = obter_gols(
            fixture
        )

        if gols is None:
            return None

        total = (
            gols[0]
            + gols[1]
        )

        return total < linha


    # --------------------------------------------------------
    # OVER ESCANTEIOS
    # --------------------------------------------------------

    if tipo == "over_corners":

        corners = obter_escanteios(
            fixture
        )

        if corners is None:
            return None

        return corners > linha


    # --------------------------------------------------------
    # RESULTADO / DUPLA CHANCE
    # --------------------------------------------------------

    if tipo == "resultado":

        resultado = resultado_jogo(
            fixture,
            home_id
        )

        if resultado is None:
            return None

        nome = mercado[
            "nome"
        ]

        if nome == "1X":

            return resultado in [
                "1",
                "X"
            ]

        if nome == "X2":

            return resultado in [
                "X",
                "2"
            ]

        if nome == "12":

            return resultado in [
                "1",
                "2"
            ]

    return None


# ============================================================
# RESOLVER BANCA
# ============================================================

def resolver_banca(
    banca_id
):

    banca = bancas[
        banca_id
    ]

    if banca[
        "status"
    ] != "ocupada":

        return

    fixture_id = banca[
        "fixture_id"
    ]

    if not fixture_id:
        return

    data = api_get(

        f"/fixtures/{fixture_id}",

        {
            "include":
                "odds,events,stats"
        }
    )

    if not data:
        return

    if isinstance(
        data,
        dict
    ):

        fixture = data.get(
            "data"
        )

    else:

        fixture = data

    if not isinstance(
        fixture,
        dict
    ):

        return

    status = str(
        fixture.get(
            "status",
            ""
        )
    ).lower()

    if status not in [
        "finished",
        "completed"
    ]:

        return

    sinal = banca[
        "sinal"
    ]

    home, away = obter_times_fixture(
        fixture
    )

    home_id = get_team_id(
        home
    )

    away_id = get_team_id(
        away
    )

    if not home_id or not away_id:
        return

    resultados = []

    for mercado in sinal[
        "mercados"
    ]:

        resultado = resolver_mercado(

            mercado,

            fixture,

            home_id
        )

        if resultado is None:
            return

        resultados.append(
            resultado
        )

    venceu = all(
        resultados
    )

    gols = obter_gols(
        fixture
    )

    if gols:

        placar = (
            f"{int(gols[0])}"
            f"x"
            f"{int(gols[1])}"
        )

    else:

        placar = "N/D"


    # ========================================================
    # WIN
    # ========================================================

    if venceu:

        stats[
            "wins"
        ] += 1

        stats[
            f"banca{banca_id}_wins"
        ] += 1

        texto = (

            f"✅ <b>WIN — "
            f"BANCA {banca_id}</b>\n\n"

            f"⚽ {sinal['home']} "
            f"x {sinal['away']}\n"

            f"📊 Placar: {placar}\n\n"

            f"💰 Odd: "
            f"{sinal['odd_combinada']:.2f}\n"

            f"💵 Entrada: "
            f"R$ {banca['entrada']:.2f}\n"

            f"🔄 Ciclo resetado "
            f"para R$ {GALES[0]:.2f}\n\n"

            f"📈 Assertividade do robô: "
            f"{assertividade_geral():.1f}%"
        )

        telegram(
            texto
        )

        banca[
            "status"
        ] = "livre"

        banca[
            "fixture_id"
        ] = None

        banca[
            "sinal"
        ] = None

        banca[
            "gale"
        ] = 0

        banca[
            "entrada"
        ] = GALES[0]


    # ========================================================
    # LOSS
    # ========================================================

    else:

        stats[
            "losses"
        ] += 1

        stats[
            f"banca{banca_id}_losses"
        ] += 1

        gale_atual = banca[
            "gale"
        ]

        proximo_gale = (
            gale_atual + 1
        )


        # ----------------------------------------------------
        # GALE MÁXIMO
        # ----------------------------------------------------

        if (
            proximo_gale
            >= len(GALES)
        ):

            texto = (

                f"❌ <b>LOSS — "
                f"BANCA {banca_id}</b>\n\n"

                f"⚽ {sinal['home']} "
                f"x {sinal['away']}\n"

                f"📊 Placar: {placar}\n\n"

                f"🔴 <b>GALE MÁXIMO "
                f"ATINGIDO</b>\n"

                f"💵 Ciclo: "
                f"R$ {sum(GALES):.2f}\n\n"

                f"🔄 Novo ciclo: "
                f"R$ {GALES[0]:.2f}\n\n"

                f"📈 Assertividade do robô: "
                f"{assertividade_geral():.1f}%"
            )

            telegram(
                texto
            )

            banca[
                "status"
            ] = "livre"

            banca[
                "fixture_id"
            ] = None

            banca[
                "sinal"
            ] = None

            banca[
                "gale"
            ] = 0

            banca[
                "entrada"
            ] = GALES[0]


        # ----------------------------------------------------
        # PRÓXIMO GALE
        # ----------------------------------------------------

        else:

            banca[
                "gale"
            ] = proximo_gale

            banca[
                "entrada"
            ] = GALES[
                proximo_gale
            ]

            texto = (

                f"❌ <b>LOSS — "
                f"BANCA {banca_id}</b>\n\n"

                f"⚽ {sinal['home']} "
                f"x {sinal['away']}\n"

                f"📊 Placar: {placar}\n\n"

                f"🔴 Próxima entrada: "
                f"Gale {proximo_gale}\n"

                f"💵 Valor: "
                f"R$ {GALES[proximo_gale]:.2f}\n\n"

                f"📈 Assertividade do robô: "
                f"{assertividade_geral():.1f}%"
            )

            telegram(
                texto
            )

            banca[
                "status"
            ] = "livre"

            banca[
                "fixture_id"
            ] = None

            banca[
                "sinal"
            ] = None


    salvar_stats()


# ============================================================
# PROCURAR SINAIS
# ============================================================

def procurar_sinais():

    futuros = buscar_futuros()

    if not futuros:
        return

    livres = [

        i

        for i in range(
            1,
            NUM_BANCAS + 1
        )

        if bancas[i][
            "status"
        ] == "livre"
    ]

    if not livres:
        return

    candidatos = []

    for fixture in futuros:

        fixture_id = get_fixture_id(
            fixture
        )

        if not fixture_id:
            continue

        fixture_key = str(
            fixture_id
        )

        if fixture_key in fixture_usados:
            continue

        try:

            sinal = analisar_jogo(
                fixture
            )

            if sinal:

                candidatos.append(
                    sinal
                )

        except Exception as e:

            logger.exception(
                f"Erro analisando "
                f"{fixture_id}: {e}"
            )


    if not candidatos:
        return


    # Melhor odd próxima de 2.00
    candidatos.sort(

        key=lambda x: (

            abs(
                x["odd_combinada"]
                - ODD_ALVO
            ),

            -x["assertividade"]
        )
    )


    candidatos = candidatos[
        :MAX_CANDIDATOS
    ]


    for banca_id, sinal in zip(
        livres,
        candidatos
    ):

        enviar_sinal(
            banca_id,
            sinal
        )


# ============================================================
# LOOP DE ANÁLISE
# ============================================================

def loop_analise():

    while True:

        try:

            procurar_sinais()

        except Exception as e:

            logger.exception(
                f"Erro loop análise: {e}"
            )

        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# LOOP DE RESULTADOS
# ============================================================

def loop_resultados():

    while True:

        try:

            for banca_id in range(
                1,
                NUM_BANCAS + 1
            ):

                try:

                    resolver_banca(
                        banca_id
                    )

                except Exception as e:

                    logger.exception(
                        f"Erro resolvendo "
                        f"Banca {banca_id}: "
                        f"{e}"
                    )

        except Exception as e:

            logger.exception(
                f"Erro loop resultados: {e}"
            )

        time.sleep(
            INTERVALO_RESULTADOS
        )


# ============================================================
# THREADS
# ============================================================

def iniciar_threads():

    global thread_started

    if thread_started:
        return

    thread_started = True

    threading.Thread(
        target=loop_analise,
        daemon=True
    ).start()

    threading.Thread(
        target=loop_resultados,
        daemon=True
    ).start()

    logger.info(
        "Threads iniciadas"
    )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "status":
            "online",

        "robo":
            "Football Signals Bot",

        "odd_alvo":
            ODD_ALVO,

        "odd_min":
            ODD_MIN,

        "odd_max":
            ODD_MAX,

        "ultimos_jogos":
            ULTIMOS_JOGOS,

        "bancas":
            NUM_BANCAS,

        "api":
            bool(API_KEY),

        "telegram":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),

        "assertividade":
            f"{assertividade_geral():.2f}%"
    })


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    return jsonify({

        "status":
            "online",

        "stats":
            stats,

        "assertividade":
            assertividade_geral(),

        "bancas":
            bancas,

        "cache_times":
            len(
                historico_cache
            ),

        "futuros_cache":
            len(
                futuros_cache["dados"]
            ),

        "api_last_error":
            api_last_error,

        "odd_alvo":
            ODD_ALVO,

        "odd_min":
            ODD_MIN,

        "odd_max":
            ODD_MAX,

        "historico_por_time":
            ULTIMOS_JOGOS,

        "minimo_amostras":
            MINIMO_AMOSTRAS,

        "historico_ttl":
            HISTORICO_TTL
    })


# ============================================================
# STATS
# ============================================================

@app.route("/stats")
def rota_stats():

    return jsonify({

        **stats,

        "assertividade":
            assertividade_geral()
    })


# ============================================================
# BANCAS
# ============================================================

@app.route("/bancas")
def rota_bancas():

    return jsonify(
        bancas
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "ok":
            True,

        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


# ============================================================
# INICIAR
# ============================================================

iniciar_threads()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
)
