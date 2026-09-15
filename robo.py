import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

ARQUIVO_STATS = "stats.json"

# Estratégia
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))
ODD_MIN = float(os.getenv("ODD_MIN", "1.85"))
ODD_MAX = float(os.getenv("ODD_MAX", "2.50"))

ULTIMOS_JOGOS = int(os.getenv("ULTIMOS_JOGOS", "5"))
MINIMO_AMOSTRAS = int(os.getenv("MINIMO_AMOSTRAS", "4"))

ASSERTIVIDADE_2_MIN = float(
    os.getenv("ASSERTIVIDADE_2_MIN", "70")
)

ASSERTIVIDADE_3_MIN = float(
    os.getenv("ASSERTIVIDADE_3_MIN", "60")
)

ASSERTIVIDADE_MEDIA_3_MIN = float(
    os.getenv("ASSERTIVIDADE_MEDIA_3_MIN", "65")
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

HORAS_FUTUROS = 24

MAX_CANDIDATOS = int(
    os.getenv("MAX_CANDIDATOS", "4")
)

STAKE_BASE = float(
    os.getenv("STAKE_BASE", "10")
)

# Gale independente por banca
GALES = [10, 20, 40, 80, 160]

NUM_BANCAS = 3

# Limite interno abaixo do limite da API
LIMITE_REQUISICOES_MINUTO = 9


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("ROBO")


# ============================================================
# ESTADO
# ============================================================

lock = threading.Lock()

stats = {
    "wins": 0,
    "losses": 0,
    "push": 0,

    "sinais": 0,

    "wins_2_mercados": 0,
    "losses_2_mercados": 0,

    "wins_3_mercados": 0,
    "losses_3_mercados": 0,

    "banca_1_wins": 0,
    "banca_1_losses": 0,

    "banca_2_wins": 0,
    "banca_2_losses": 0,

    "banca_3_wins": 0,
    "banca_3_losses": 0
}


if os.path.exists(ARQUIVO_STATS):

    try:

        with open(
            ARQUIVO_STATS,
            "r",
            encoding="utf-8"
        ) as f:

            carregado = json.load(f)

            if isinstance(carregado, dict):
                stats.update(carregado)

    except Exception as e:

        logger.error(
            f"Erro carregando stats: {e}"
        )


# ============================================================
# BANCAS
# ============================================================

bancas = []

for i in range(NUM_BANCAS):

    bancas.append(
        {
            "id": i + 1,
            "ocupada": False,
            "fixture_id": None,
            "sinal": None,
            "gale": 0,
            "entrada": GALES[0],
            "inicio": None,
            "resultado": None
        }
    )


# ============================================================
# CACHE
# ============================================================

historico_cache = {}

futuros_cache = {
    "timestamp": 0,
    "dados": []
}

fixture_usados = set()


# ============================================================
# CONTROLE DE REQUESTS
# ============================================================

request_times = []


def pode_fazer_request():

    agora = time.time()

    with lock:

        while request_times and (
            agora - request_times[0] > 60
        ):
            request_times.pop(0)

        if len(request_times) >= LIMITE_REQUISICOES_MINUTO:
            return False

        request_times.append(agora)

        return True


# ============================================================
# SALVAR STATS
# ============================================================

def salvar_stats():

    try:

        with lock:

            with open(
                ARQUIVO_STATS,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    stats,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

    except Exception as e:

        logger.error(
            f"Erro salvando stats: {e}"
        )


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        logger.error(
            "FIVE_DOLLAR_KEY não configurada."
        )

        return None

    if not pode_fazer_request():

        logger.warning(
            "Limite interno de requisições atingido."
        )

        return None

    headers = {
        "X-API-Key": API_KEY,
        "Accept": "application/json"
    }

    url = BASE_API + endpoint

    try:

        resposta = requests.get(
            url,
            headers=headers,
            params=params or {},
            timeout=30
        )

        if resposta.status_code == 429:

            retry = resposta.headers.get(
                "Retry-After",
                "60"
            )

            logger.warning(
                f"Rate limit. Retry-After={retry}"
            )

            return None

        if resposta.status_code != 200:

            logger.error(
                f"HTTP {resposta.status_code}: "
                f"{resposta.text[:1000]}"
            )

            return None

        return resposta.json()

    except Exception as e:

        logger.error(
            f"Erro API: {e}"
        )

        return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado."
        )

        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML"
    }

    try:

        resposta = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if resposta.status_code != 200:

            logger.error(
                f"Telegram HTTP {resposta.status_code}: "
                f"{resposta.text}"
            )

            return False

        return True

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


def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except Exception:

        return None


def inteiro(valor):

    try:

        if valor is None:
            return None

        return int(valor)

    except Exception:

        return None


def primeiro(*valores):

    for valor in valores:

        if valor is not None:
            return valor

    return None


# ============================================================
# NOME DOS TIMES
# ============================================================

def obter_nome_time(fixture, casa=True):

    if not isinstance(fixture, dict):
        return "?"

    lado = "home" if casa else "away"

    possiveis = [
        fixture.get(f"{lado}_team_name"),
        fixture.get(f"{lado}_name")
    ]

    team = fixture.get(lado)

    if isinstance(team, dict):

        possiveis.extend(
            [
                team.get("name"),
                team.get("team_name")
            ]
        )

    teams = fixture.get("teams")

    if isinstance(teams, dict):

        obj = teams.get(lado)

        if isinstance(obj, dict):

            possiveis.extend(
                [
                    obj.get("name"),
                    obj.get("team_name")
                ]
            )

    for valor in possiveis:

        if valor:
            return str(valor)

    return "?"


# ============================================================
# ID DOS TIMES
# ============================================================

def obter_id_time(fixture, casa=True):

    lado = "home" if casa else "away"

    possiveis = [
        fixture.get(f"{lado}_team_id"),
        fixture.get(f"{lado}_id")
    ]

    team = fixture.get(lado)

    if isinstance(team, dict):

        possiveis.extend(
            [
                team.get("id"),
                team.get("team_id")
            ]
        )

    teams = fixture.get("teams")

    if isinstance(teams, dict):

        obj = teams.get(lado)

        if isinstance(obj, dict):

            possiveis.extend(
                [
                    obj.get("id"),
                    obj.get("team_id")
                ]
            )

    for valor in possiveis:

        if valor is not None:
            return valor

    return None


# ============================================================
# ODDS
# ============================================================

def extrair_odds(fixture):

    if not isinstance(fixture, dict):
        return {}

    odds = fixture.get("odds")

    if isinstance(odds, dict):
        return odds

    bookmakers = fixture.get(
        "bookmakers"
    )

    if isinstance(bookmakers, list):

        for bookmaker in bookmakers:

            if not isinstance(bookmaker, dict):
                continue

            nome = str(
                bookmaker.get("slug")
                or bookmaker.get("name")
                or ""
            ).lower()

            if "bet365" in nome:

                odds = bookmaker.get("odds")

                if isinstance(odds, dict):
                    return odds

        for bookmaker in bookmakers:

            if isinstance(bookmaker, dict):

                odds = bookmaker.get(
                    "odds"
                )

                if isinstance(odds, dict):
                    return odds

    return {}


def extrair_linha_odd(
    mercado,
    linha_desejada=None,
    over=True
):

    if not isinstance(mercado, dict):
        return None

    # abertura/fechamento
    blocos = []

    if isinstance(
        mercado.get("closing"),
        dict
    ):
        blocos.append(
            mercado["closing"]
        )

    if isinstance(
        mercado.get("opening"),
        dict
    ):
        blocos.append(
            mercado["opening"]
        )

    # alguns formatos podem vir
    # diretamente no mercado
    blocos.append(mercado)

    for bloco in blocos:

        linha = primeiro(
            bloco.get("line"),
            bloco.get("value"),
            bloco.get("total")
        )

        linha_num = numero(linha)

        if (
            linha_desejada is not None
            and linha_num is not None
            and abs(
                linha_num - linha_desejada
            ) > 0.01
        ):
            continue

        if over:

            odd = primeiro(
                bloco.get("over"),
                bloco.get("over_odds"),
                bloco.get("yes")
            )

        else:

            odd = primeiro(
                bloco.get("under"),
                bloco.get("under_odds"),
                bloco.get("no")
            )

        odd = numero(odd)

        if odd and odd > 1:

            return {
                "linha": linha_num,
                "odd": odd
            }

    return None


# ============================================================
# UNDER GOLS
# ============================================================

def mercado_under_gols(fixture):

    odds = extrair_odds(fixture)

    possiveis = [
        odds.get("goal_line"),
        odds.get("goals"),
        odds.get("goal"),
        odds.get("totals")
    ]

    for mercado in possiveis:

        if not isinstance(mercado, dict):
            continue

        for linha in [1.5, 2.5]:

            resultado = extrair_linha_odd(
                mercado,
                linha,
                over=False
            )

            if resultado:

                return {
                    "tipo": "under_gols",
                    "nome": (
                        f"Under "
                        f"{resultado['linha']} gols"
                    ),
                    "linha": resultado["linha"],
                    "odd": resultado["odd"]
                }

    return None


# ============================================================
# OVER ESCANTEIOS
# ============================================================

def mercado_over_escanteios(fixture):

    odds = extrair_odds(fixture)

    possiveis = [
        odds.get("corner_line"),
        odds.get("corners"),
        odds.get("corner"),
        odds.get("corner_totals")
    ]

    for mercado in possiveis:

        if not isinstance(mercado, dict):
            continue

        # Primeiro tenta linhas mais comuns
        linhas = [
            7.5,
            8.5,
            9.5,
            10.5,
            11.5,
            12.5,
            13.5
        ]

        for linha in linhas:

            resultado = extrair_linha_odd(
                mercado,
                linha,
                over=True
            )

            if resultado:

                return {
                    "tipo": "over_escanteios",
                    "nome": (
                        f"Over "
                        f"{resultado['linha']} escanteios"
                    ),
                    "linha": resultado["linha"],
                    "odd": resultado["odd"]
                }

    return None


# ============================================================
# DUPLA CHANCE
# ============================================================

def mercado_dupla_chance(fixture):

    odds = extrair_odds(fixture)

    possiveis = [
        odds.get("double_chance"),
        odds.get("doublechance"),
        odds.get("doubleChance"),
        odds.get("result"),
        odds.get("match_result"),
        odds.get("1x2")
    ]

    for mercado in possiveis:

        if not isinstance(mercado, dict):
            continue

        # ----------------------------------------
        # 1X
        # ----------------------------------------

        um_x = primeiro(
            mercado.get("1x"),
            mercado.get("1X"),
            mercado.get("home_draw"),
            mercado.get("home_or_draw"),
            mercado.get("home_draw_odds")
        )

        if isinstance(um_x, dict):

            um_x = primeiro(
                um_x.get("odd"),
                um_x.get("odds"),
                um_x.get("price")
            )

        um_x = numero(um_x)

        if um_x and um_x > 1:

            return {
                "tipo": "dupla_chance",
                "nome": "Dupla Chance 1X",
                "seleção": "1X",
                "odd": um_x
            }

        # ----------------------------------------
        # X2
        # ----------------------------------------

        x_dois = primeiro(
            mercado.get("x2"),
            mercado.get("X2"),
            mercado.get("draw_away"),
            mercado.get("draw_or_away"),
            mercado.get("draw_away_odds")
        )

        if isinstance(x_dois, dict):

            x_dois = primeiro(
                x_dois.get("odd"),
                x_dois.get("odds"),
                x_dois.get("price")
            )

        x_dois = numero(x_dois)

        if x_dois and x_dois > 1:

            return {
                "tipo": "dupla_chance",
                "nome": "Dupla Chance X2",
                "seleção": "X2",
                "odd": x_dois
            }

        # ----------------------------------------
        # 12
        # ----------------------------------------

        um_dois = primeiro(
            mercado.get("12"),
            mercado.get("1/2"),
            mercado.get("home_away"),
            mercado.get("home_or_away"),
            mercado.get("home_away_odds")
        )

        if isinstance(um_dois, dict):

            um_dois = primeiro(
                um_dois.get("odd"),
                um_dois.get("odds"),
                um_dois.get("price")
            )

        um_dois = numero(um_dois)

        if um_dois and um_dois > 1:

            return {
                "tipo": "dupla_chance",
                "nome": "Dupla Chance 12",
                "seleção": "12",
                "odd": um_dois
            }

    return None


# ============================================================
# ESCANTEIOS DO JOGO
# ============================================================

def obter_escanteios(fixture):

    if not isinstance(fixture, dict):
        return None

    # Formatos diretos
    casa = primeiro(
        fixture.get("home_corners"),
        fixture.get("corners_home")
    )

    fora = primeiro(
        fixture.get("away_corners"),
        fixture.get("corners_away")
    )

    if casa is not None and fora is not None:

        return (
            inteiro(casa) or 0
        ) + (
            inteiro(fora) or 0
        )

    corners = fixture.get("corners")

    if isinstance(corners, dict):

        casa = primeiro(
            corners.get("home"),
            corners.get("home_corners")
        )

        fora = primeiro(
            corners.get("away"),
            corners.get("away_corners")
        )

        if casa is not None and fora is not None:

            return (
                inteiro(casa) or 0
            ) + (
                inteiro(fora) or 0
            )

        total = primeiro(
            corners.get("total"),
            corners.get("value")
        )

        if total is not None:
            return inteiro(total)

    return None


# ============================================================
# GOLS
# ============================================================

def obter_gols(fixture):

    if not isinstance(fixture, dict):
        return None

    home = primeiro(
        fixture.get("home_score"),
        fixture.get("home_goals"),
        fixture.get("goals_home")
    )

    away = primeiro(
        fixture.get("away_score"),
        fixture.get("away_goals"),
        fixture.get("goals_away")
    )

    if isinstance(
        fixture.get("scores"),
        dict
    ):

        scores = fixture["scores"]

        home = primeiro(
            home,
            scores.get("home"),
            scores.get("home_score")
        )

        away = primeiro(
            away,
            scores.get("away"),
            scores.get("away_score")
        )

    if isinstance(
        fixture.get("score"),
        dict
    ):

        score = fixture["score"]

        home = primeiro(
            home,
            score.get("home")
        )

        away = primeiro(
            away,
            score.get("away")
        )

    home = inteiro(home)
    away = inteiro(away)

    if home is None or away is None:
        return None

    return home + away


# ============================================================
# RESULTADO
# ============================================================

def obter_resultado(fixture):

    gols = obter_gols(fixture)

    if gols is None:
        return None

    home = primeiro(
        fixture.get("home_score"),
        fixture.get("home_goals")
    )

    away = primeiro(
        fixture.get("away_score"),
        fixture.get("away_goals")
    )

    home = inteiro(home)
    away = inteiro(away)

    if home is None or away is None:
        return None

    if home > away:
        return "1"

    if home < away:
        return "2"

    return "X"


# ============================================================
# HISTÓRICO DO TIME
# ============================================================

def buscar_historico_time(team_id):

    if not team_id:
        return []

    chave = str(team_id)

    agora = time.time()

    cache = historico_cache.get(chave)

    if cache:

        if agora - cache["timestamp"] < HISTORICO_TTL:

            return cache["dados"]

    params = {
        "status": "finished",
        "per_page": 20,
        "include": "stats"
    }

    data = api_get(
        f"/teams/{team_id}/fixtures",
        params
    )

    if not data:

        return (
            cache["dados"]
            if cache
            else []
        )

    if isinstance(data, dict):

        jogos = data.get(
            "data",
            []
        )

    else:

        jogos = data

    if not isinstance(jogos, list):

        jogos = []

    jogos_validos = []

    for jogo in jogos:

        if not isinstance(jogo, dict):
            continue

        if obter_gols(jogo) is None:
            continue

        jogos_validos.append(jogo)

        if len(jogos_validos) >= ULTIMOS_JOGOS:
            break

    historico_cache[chave] = {
        "timestamp": agora,
        "dados": jogos_validos
    }

    return jogos_validos


# ============================================================
# ASSERTIVIDADE HISTÓRICA
# ============================================================

def calcular_historico(
    jogos,
    tipo,
    linha,
    selecao=None
):

    if not jogos:
        return None

    acertos = 0
    amostras = 0

    for jogo in jogos:

        gols = obter_gols(jogo)
        cantos = obter_escanteios(jogo)

        if tipo == "under_gols":

            if gols is None:
                continue

            amostras += 1

            if gols < linha:
                acertos += 1

        elif tipo == "over_escanteios":

            if cantos is None:
                continue

            amostras += 1

            if cantos > linha:
                acertos += 1

        elif tipo == "dupla_chance":

            resultado = obter_resultado(jogo)

            if resultado is None:
                continue

            if selecao == "1X":

                valido = resultado in [
                    "1",
                    "X"
                ]

            elif selecao == "X2":

                valido = resultado in [
                    "X",
                    "2"
                ]

            elif selecao == "12":

                valido = resultado in [
                    "1",
                    "2"
                ]

            else:

                continue

            amostras += 1

            if valido:
                acertos += 1

    if amostras < MINIMO_AMOSTRAS:

        return None

    percentual = (
        acertos /
        amostras
    ) * 100

    return {
        "acertos": acertos,
        "amostras": amostras,
        "percentual": percentual
    }


# ============================================================
# ANALISAR MERCADO
# ============================================================

def analisar_mercado(
    mercado,
    fixture,
    historico_casa,
    historico_fora
):

    tipo = mercado["tipo"]

    linha = mercado.get("linha")

    selecao = mercado.get(
        "seleção"
    )

    h1 = calcular_historico(
        historico_casa,
        tipo,
        linha,
        selecao
    )

    h2 = calcular_historico(
        historico_fora,
        tipo,
        linha,
        selecao
    )

    resultados = [
        x for x in [h1, h2]
        if x is not None
    ]

    if not resultados:

        return None

    total_acertos = sum(
        x["acertos"]
        for x in resultados
    )

    total_amostras = sum(
        x["amostras"]
        for x in resultados
    )

    if total_amostras < MINIMO_AMOSTRAS:

        return None

    percentual = (
        total_acertos /
        total_amostras
    ) * 100

    novo = dict(mercado)

    novo["assertividade"] = percentual

    novo["acertos"] = total_acertos

    novo["amostras"] = total_amostras

    return novo


# ============================================================
# COMBINAÇÕES
# ============================================================

def combinar(mercados):

    resultado = []

    n = len(mercados)

    # ----------------------------------------
    # Combinações de 2
    # ----------------------------------------

    for i in range(n):

        for j in range(i + 1, n):

            a = mercados[i]
            b = mercados[j]

            tipos = {
                a["tipo"],
                b["tipo"]
            }

            if len(tipos) != 2:
                continue

            if (
                a["assertividade"]
                < ASSERTIVIDADE_2_MIN
            ):
                continue

            if (
                b["assertividade"]
                < ASSERTIVIDADE_2_MIN
            ):
                continue

            odd = (
                a["odd"] *
                b["odd"]
            )

            if (
                odd < ODD_MIN
                or odd > ODD_MAX
            ):
                continue

            media = (
                a["assertividade"] +
                b["assertividade"]
            ) / 2

            resultado.append(
                {
                    "mercados": [a, b],
                    "odd": odd,
                    "assertividade": media,
                    "quantidade": 2
                }
            )

    # ----------------------------------------
    # Combinação de 3
    # ----------------------------------------

    if n >= 3:

        for i in range(n):

            for j in range(i + 1, n):

                for k in range(j + 1, n):

                    grupo = [
                        mercados[i],
                        mercados[j],
                        mercados[k]
                    ]

                    tipos = {
                        x["tipo"]
                        for x in grupo
                    }

                    if len(tipos) != 3:
                        continue

                    if any(
                        x["assertividade"]
                        < ASSERTIVIDADE_3_MIN
                        for x in grupo
                    ):
                        continue

                    media = sum(
                        x["assertividade"]
                        for x in grupo
                    ) / 3

                    if (
                        media
                        < ASSERTIVIDADE_MEDIA_3_MIN
                    ):
                        continue

                    odd = 1

                    for x in grupo:

                        odd *= x["odd"]

                    if (
                        odd < ODD_MIN
                        or odd > ODD_MAX
                    ):
                        continue

                    resultado.append(
                        {
                            "mercados": grupo,
                            "odd": odd,
                            "assertividade": media,
                            "quantidade": 3
                        }
                    )

    resultado.sort(
        key=lambda x: (
            x["assertividade"],
            -abs(
                x["odd"] - ODD_ALVO
            )
        ),
        reverse=True
    )

    return resultado


# ============================================================
# BUSCAR JOGOS FUTUROS
# ============================================================

def buscar_futuros():

    global futuros_cache

    agora = time.time()

    if (
        agora -
        futuros_cache["timestamp"]
        < FUTUROS_TTL
    ):

        return futuros_cache["dados"]

    # IMPORTANTE:
    # A API exige Unix timestamp em segundos.
    inicio = int(agora)

    fim = int(
        agora +
        (
            HORAS_FUTUROS *
            60 *
            60
        )
    )

    params = {
        "start_time": inicio,
        "end_time": fim,
        "status": "scheduled",
        "include": "odds",
        "per_page": 100
    }

    logger.info(
        f"Buscando jogos futuros: "
        f"{inicio} -> {fim}"
    )

    data = api_get(
        "/fixtures",
        params
    )

    if not data:

        logger.warning(
            "API não retornou jogos futuros."
        )

        return futuros_cache["dados"]

    if isinstance(data, dict):

        jogos = data.get(
            "data",
            []
        )

    else:

        jogos = data

    if not isinstance(jogos, list):

        jogos = []

    futuros_cache = {
        "timestamp": agora,
        "dados": jogos
    }

    logger.info(
        f"Jogos futuros encontrados: "
        f"{len(jogos)}"
    )

    return jogos


# ============================================================
# GERAR SINAL
# ============================================================

def gerar_sinal(fixture):

    fixture_id = (
        fixture.get("id")
        or fixture.get("fixture_id")
    )

    if not fixture_id:
        return None

    if fixture_id in fixture_usados:

        return None

    home_id = obter_id_time(
        fixture,
        True
    )

    away_id = obter_id_time(
        fixture,
        False
    )

    if not home_id or not away_id:

        logger.info(
            f"Fixture {fixture_id}: "
            f"sem IDs dos times."
        )

        return None

    casa = obter_nome_time(
        fixture,
        True
    )

    fora = obter_nome_time(
        fixture,
        False
    )

    historico_casa = (
        buscar_historico_time(
            home_id
        )
    )

    historico_fora = (
        buscar_historico_time(
            away_id
        )
    )

    if not historico_casa or not historico_fora:

        logger.info(
            f"{casa} x {fora}: "
            f"histórico insuficiente."
        )

        return None

    mercados_brutos = []

    under = mercado_under_gols(
        fixture
    )

    if under:

        mercados_brutos.append(
            under
        )

    cantos = mercado_over_escanteios(
        fixture
    )

    if cantos:

        mercados_brutos.append(
            cantos
        )

    dupla = mercado_dupla_chance(
        fixture
    )

    if dupla:

        mercados_brutos.append(
            dupla
        )

    if len(mercados_brutos) < 2:

        logger.info(
            f"{casa} x {fora}: "
            f"menos de 2 mercados com odds."
        )

        return None

    mercados_analisados = []

    for mercado in mercados_brutos:

        analise = analisar_mercado(
            mercado,
            fixture,
            historico_casa,
            historico_fora
        )

        if analise:

            mercados_analisados.append(
                analise
            )

    if len(mercados_analisados) < 2:

        logger.info(
            f"{casa} x {fora}: "
            f"assertividade insuficiente."
        )

        return None

    combinacoes = combinar(
        mercados_analisados
    )

    if not combinacoes:

        logger.info(
            f"{casa} x {fora}: "
            f"nenhuma combinação passou."
        )

        return None

    melhor = combinacoes[0]

    sinal = {
        "fixture_id": fixture_id,
        "home": casa,
        "away": fora,
        "odd": melhor["odd"],
        "assertividade": melhor[
            "assertividade"
        ],
        "quantidade": melhor[
            "quantidade"
        ],
        "mercados": melhor[
            "mercados"
        ],
        "criado_em": datetime.now(
            timezone.utc
        ).isoformat()
    }

    return sinal


# ============================================================
# FORMATAR SINAL
# ============================================================

def formatar_sinal(
    sinal,
    banca_id,
    gale=0,
    entrada=None
):

    if entrada is None:

        entrada = GALES[gale]

    linhas = []

    linhas.append(
        "🚨 <b>NOVO SINAL</b>"
    )

    linhas.append("")

    linhas.append(
        f"⚽ <b>{sinal['home']}</b> "
        f"x "
        f"<b>{sinal['away']}</b>"
    )

    linhas.append("")

    linhas.append(
        f"🏦 Banca: <b>{banca_id}</b>"
    )

    linhas.append(
        f"🎯 Mercados: "
        f"<b>{sinal['quantidade']}</b>"
    )

    linhas.append("")

    for mercado in sinal["mercados"]:

        linhas.append(
            f"• {mercado['nome']}"
        )

        linhas.append(
            f"  📊 "
            f"{mercado['assertividade']:.1f}%"
        )

        linhas.append(
            f"  💰 Odd: "
            f"{mercado['odd']:.2f}"
        )

    linhas.append("")

    linhas.append(
        f"🔥 Odd combinada: "
        f"<b>{sinal['odd']:.2f}</b>"
    )

    linhas.append(
        f"📈 Assertividade: "
        f"<b>{sinal['assertividade']:.1f}%</b>"
    )

    linhas.append(
        f"💵 Entrada: "
        f"<b>R$ {entrada:.2f}</b>"
    )

    if gale > 0:

        linhas.append(
            f"🔄 <b>GALE {gale}</b>"
        )

    return "\n".join(linhas)


# ============================================================
# BANCA LIVRE
# ============================================================

def obter_banca_livre():

    with lock:

        for banca in bancas:

            if not banca["ocupada"]:

                return banca

    return None


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(sinal):

    banca = obter_banca_livre()

    if not banca:

        logger.info(
            "Todas as 3 bancas estão ocupadas."
        )

        return False

    with lock:

        banca["ocupada"] = True

        banca["fixture_id"] = (
            sinal["fixture_id"]
        )

        banca["sinal"] = sinal

        banca["gale"] = 0

        banca["entrada"] = GALES[0]

        banca["inicio"] = time.time()

        banca["resultado"] = None

        stats["sinais"] += 1

    fixture_usados.add(
        sinal["fixture_id"]
    )

    salvar_stats()

    mensagem = formatar_sinal(
        sinal,
        banca["id"],
        0,
        GALES[0]
    )

    logger.info(
        f"Sinal enviado | "
        f"Banca {banca['id']} | "
        f"{sinal['home']} x "
        f"{sinal['away']} | "
        f"odd {sinal['odd']:.2f}"
    )

    telegram(
        mensagem
    )

    return True


# ============================================================
# BUSCAR FIXTURE ATUAL
# ============================================================

def buscar_fixture(fixture_id):

    data = api_get(
        f"/fixtures/{fixture_id}",
        {
            "include": "odds,events,stats"
        }
    )

    if not data:
        return None

    if isinstance(data, dict):

        if isinstance(
            data.get("data"),
            dict
        ):
            return data["data"]

        return data

    return None


# ============================================================
# VERIFICAR RESULTADO DO SINAL
# ============================================================

def avaliar_sinal(
    sinal,
    fixture
):

    if not fixture:
        return None

    gols = obter_gols(
        fixture
    )

    cantos = obter_escanteios(
        fixture
    )

    resultado = obter_resultado(
        fixture
    )

    if gols is None:
        return None

    if resultado is None:
        return None

    for mercado in sinal["mercados"]:

        tipo = mercado["tipo"]

        if tipo == "under_gols":

            linha = mercado["linha"]

            if not (
                gols < linha
            ):

                return "LOSS"

        elif tipo == "over_escanteios":

            if cantos is None:
                return None

            linha = mercado["linha"]

            if not (
                cantos > linha
            ):

                return "LOSS"

        elif tipo == "dupla_chance":

            selecao = mercado[
                "seleção"
            ]

            if selecao == "1X":

                valido = resultado in [
                    "1",
                    "X"
                ]

            elif selecao == "X2":

                valido = resultado in [
                    "X",
                    "2"
                ]

            elif selecao == "12":

                valido = resultado in [
                    "1",
                    "2"
                ]

            else:

                return None

            if not valido:

                return "LOSS"

    return "WIN"


# ============================================================
# RESOLVER BANCA
# ============================================================

def resolver_banca(banca):

    fixture_id = banca.get(
        "fixture_id"
    )

    if not fixture_id:
        return

    fixture = buscar_fixture(
        fixture_id
    )

    if not fixture:
        return

    resultado = avaliar_sinal(
        banca["sinal"],
        fixture
    )

    if resultado is None:
        return

    banca_id = banca["id"]

    # ========================================================
    # WIN
    # ========================================================

    if resultado == "WIN":

        with lock:

            stats["wins"] += 1

            stats[
                f"banca_{banca_id}_wins"
            ] += 1

            quantidade = banca[
                "sinal"
            ]["quantidade"]

            if quantidade == 2:

                stats[
                    "wins_2_mercados"
                ] += 1

            elif quantidade == 3:

                stats[
                    "wins_3_mercados"
                ] += 1

        mensagem = (
            "✅ <b>WIN</b>\n\n"
            f"🏦 Banca: <b>{banca_id}</b>\n"
            f"⚽ "
            f"<b>{banca['sinal']['home']}</b> "
            f"x "
            f"<b>{banca['sinal']['away']}</b>\n"
            f"🔄 Gale utilizado: "
            f"<b>{banca['gale']}</b>\n"
            f"💵 Entrada: "
            f"<b>R$ {banca['entrada']:.2f}</b>\n\n"
            "♻️ Banca resetada para "
            "<b>R$ 10,00</b>."
        )

        telegram(
            mensagem
        )

        logger.info(
            f"WIN | Banca {banca_id}"
        )

        with lock:

            banca["ocupada"] = False

            banca["fixture_id"] = None

            banca["sinal"] = None

            banca["gale"] = 0

            banca["entrada"] = GALES[0]

            banca["inicio"] = None

            banca["resultado"] = "WIN"

        salvar_stats()

        return

    # ========================================================
    # LOSS
    # ========================================================

    with lock:

        stats["losses"] += 1

        stats[
            f"banca_{banca_id}_losses"
        ] += 1

        quantidade = banca[
            "sinal"
        ]["quantidade"]

        if quantidade == 2:

            stats[
                "losses_2_mercados"
            ] += 1

        elif quantidade == 3:

            stats[
                "losses_3_mercados"
            ] += 1

    proximo_gale = (
        banca["gale"] + 1
    )

    # Ainda possui Gale
    if proximo_gale < len(GALES):

        banca["gale"] = proximo_gale

        banca["entrada"] = (
            GALES[proximo_gale]
        )

        mensagem = formatar_sinal(
            banca["sinal"],
            banca_id,
            proximo_gale,
            GALES[proximo_gale]
        )

        telegram(
            "❌ <b>LOSS</b>\n\n"
            f"🏦 Banca: <b>{banca_id}</b>\n"
            f"⚽ "
            f"<b>{banca['sinal']['home']}</b> "
            f"x "
            f"<b>{banca['sinal']['away']}</b>\n\n"
            + mensagem
        )

        logger.info(
            f"LOSS | Banca {banca_id} | "
            f"indo para Gale {proximo_gale}"
        )

        salvar_stats()

        return

    # ========================================================
    # ESTOURO DO GALE
    # ========================================================

    mensagem = (
        "🛑 <b>LOSS MÁXIMO</b>\n\n"
        f"🏦 Banca: <b>{banca_id}</b>\n"
        f"⚽ "
        f"<b>{banca['sinal']['home']}</b> "
        f"x "
        f"<b>{banca['sinal']['away']}</b>\n"
        f"💥 Gale máximo atingido: "
        f"<b>{banca['gale']}</b>\n"
        f"💰 Última entrada: "
        f"<b>R$ {banca['entrada']:.2f}</b>"
    )

    telegram(
        mensagem
    )

    logger.warning(
        f"LOSS MÁXIMO | Banca {banca_id}"
    )

    with lock:

        banca["ocupada"] = False

        banca["fixture_id"] = None

        banca["sinal"] = None

        banca["gale"] = 0

        banca["entrada"] = GALES[0]

        banca["inicio"] = None

        banca["resultado"] = "LOSS"

    salvar_stats()


# ============================================================
# LOOP DOS RESULTADOS
# ============================================================

def loop_resultados():

    logger.info(
        "Thread de resultados iniciada."
    )

    while True:

        try:

            ocupadas = []

            with lock:

                for banca in bancas:

                    if banca["ocupada"]:

                        ocupadas.append(
                            dict(banca)
                        )

            for banca in ocupadas:

                try:

                    resolver_banca(
                        banca
                    )

                except Exception as e:

                    logger.exception(
                        f"Erro resolvendo banca "
                        f"{banca['id']}: {e}"
                    )

                time.sleep(1)

        except Exception as e:

            logger.exception(
                f"Erro no loop de resultados: {e}"
            )

        time.sleep(
            INTERVALO_RESULTADOS
        )


# ============================================================
# LOOP DE ANÁLISE
# ============================================================

def loop_analise():

    logger.info(
        "Thread de análise iniciada."
    )

    while True:

        try:

            # Verifica se existe banca livre
            banca_livre = (
                obter_banca_livre()
            )

            if not banca_livre:

                logger.info(
                    "3 bancas ocupadas. "
                    "Aguardando resolução."
                )

                time.sleep(
                    INTERVALO_ANALISE
                )

                continue

            jogos = buscar_futuros()

            if not jogos:

                logger.info(
                    "Nenhum jogo futuro."
                )

                time.sleep(
                    INTERVALO_ANALISE
                )

                continue

            candidatos = []

            for fixture in jogos:

                if not isinstance(
                    fixture,
                    dict
                ):
                    continue

                fixture_id = (
                    fixture.get("id")
                    or fixture.get(
                        "fixture_id"
                    )
                )

                if not fixture_id:
                    continue

                if fixture_id in fixture_usados:
                    continue

                try:

                    sinal = gerar_sinal(
                        fixture
                    )

                    if sinal:

                        candidatos.append(
                            sinal
                        )

                except Exception as e:

                    logger.exception(
                        f"Erro analisando "
                        f"fixture {fixture_id}: "
                        f"{e}"
                    )

                if len(candidatos) >= MAX_CANDIDATOS:
                    break

            candidatos.sort(
                key=lambda x: (
                    x["assertividade"],
                    -abs(
                        x["odd"] -
                        ODD_ALVO
                    )
                ),
                reverse=True
            )

            enviados = 0

            for sinal in candidatos:

                if enviados >= NUM_BANCAS:
                    break

                if not obter_banca_livre():
                    break

                if enviar_sinal(sinal):

                    enviados += 1

        except Exception as e:

            logger.exception(
                f"Erro no loop de análise: {e}"
            )

        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# ASSERTIVIDADE REAL
# ============================================================

def assertividade_real():

    with lock:

        wins = stats["wins"]

        losses = stats["losses"]

    total = wins + losses

    if total == 0:

        return 0

    return (
        wins /
        total
    ) * 100


# ============================================================
# EXPOSIÇÃO
# ============================================================

def exposicao_atual():

    total = 0

    with lock:

        for banca in bancas:

            if banca["ocupada"]:

                total += banca[
                    "entrada"
                ]

    return total


# ============================================================
# STATUS
# ============================================================

@app.route("/")
def home():

    return jsonify(
        {
            "status": "online",
            "bot": "robo-combinado",
            "estrategia": [
                "Under gols",
                "Over escanteios",
                "Dupla Chance"
            ],
            "odd_min": ODD_MIN,
            "odd_max": ODD_MAX,
            "assertividade": (
                f"{assertividade_real():.2f}%"
            ),
            "bancas": NUM_BANCAS,
            "exposicao_atual": (
                exposicao_atual()
            )
        }
    )


# ============================================================
# /STATUS
# ============================================================

@app.route("/status")
def status():

    with lock:

        bancas_status = []

        for banca in bancas:

            item = {
                "id": banca["id"],
                "ocupada": banca["ocupada"],
                "fixture_id": banca["fixture_id"],
                "gale": banca["gale"],
                "entrada": banca["entrada"]
            }

            if banca["sinal"]:

                item["jogo"] = (
                    f"{banca['sinal']['home']} "
                    f"x "
                    f"{banca['sinal']['away']}"
                )

            bancas_status.append(
                item
            )

        resposta = {
            "stats": stats,
            "assertividade_real": (
                assertividade_real()
            ),
            "bancas": bancas_status,
            "exposicao_atual": (
                exposicao_atual()
            ),
            "jogos_futuros_cache": len(
                futuros_cache["dados"]
            ),
            "historicos_cache": len(
                historico_cache
            )
        }

    return jsonify(
        resposta
    )


# ============================================================
# /STATS
# ============================================================

@app.route("/stats")
def endpoint_stats():

    with lock:

        resposta = dict(stats)

    resposta[
        "assertividade_real"
    ] = assertividade_real()

    resposta[
        "total_resolvidos"
    ] = (
        stats["wins"] +
        stats["losses"]
    )

    return jsonify(
        resposta
    )


# ============================================================
# /BANCAS
# ============================================================

@app.route("/bancas")
def endpoint_bancas():

    with lock:

        resposta = []

        for banca in bancas:

            item = dict(banca)

            if item["sinal"]:

                item["sinal"] = {
                    "fixture_id":
                        item["sinal"][
                            "fixture_id"
                        ],
                    "home":
                        item["sinal"][
                            "home"
                        ],
                    "away":
                        item["sinal"][
                            "away"
                        ],
                    "odd":
                        item["sinal"][
                            "odd"
                        ],
                    "assertividade":
                        item["sinal"][
                            "assertividade"
                        ]
                }

            resposta.append(
                item
            )

    return jsonify(
        resposta
    )


# ============================================================
# /PING
# ============================================================

@app.route("/ping")
def ping():

    return jsonify(
        {
            "pong": True,
            "timestamp": time.time()
        }
    )


# ============================================================
# INICIAR THREADS
# ============================================================

def iniciar_threads():

    t1 = threading.Thread(
        target=loop_analise,
        daemon=True,
        name="ANALISE"
    )

    t2 = threading.Thread(
        target=loop_resultados,
        daemon=True,
        name="RESULTADOS"
    )

    t1.start()

    t2.start()

    logger.info(
        "Threads iniciadas."
    )


# ============================================================
# START
# ============================================================

iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
        )
