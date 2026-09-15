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

BASE_API = os.getenv(
    "BASE_API",
    "https://" + "api.5dollarfootballapi.com/v1"
)

TELEGRAM_BASE = "https://" + "api.telegram.org"

API_KEY = os.getenv("FIVE_DOLLAR_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "10.00"))

QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))
ASSERTIVIDADE_MINIMA = float(os.getenv("ASSERTIVIDADE_MINIMA", "60"))

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "60"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "120"))

MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "20"))

# Tempo de validade do cache histórico
HISTORICO_TTL = int(os.getenv("HISTORICO_TTL", "1800"))

MONITOR_ATIVO = os.getenv(
    "MONITOR_ATIVO",
    "true"
).lower() in ("1", "true", "yes", "sim")

ARQUIVO_STATS = "stats.json"
ARQUIVO_CACHE = "historico_cache.json"


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
api_lock = threading.Lock()

stats = {
    "live_wins": 0,
    "live_losses": 0,
    "live_pushes": 0,

    "pre_wins": 0,
    "pre_losses": 0,
    "pre_pushes": 0,

    "total_sinais": 0,
    "ultima_execucao": None,
    "ultimo_historico": None,

    "historico_jogos": 0,
    "historico_combinacoes": 0,

    "api_requests": 0,
    "api_429": 0
}


pending = {}

historico_cache = {}

rate_state = {
    "remaining": None,
    "limit": None,
    "reset": 0
}

status_state = {
    "pre_em_execucao": False,
    "resultados_em_execucao": False,
    "historico_em_execucao": False,
    "ultima_mensagem": "",
    "ultimo_erro": ""
}


# ============================================================
# ARQUIVOS
# ============================================================

def carregar_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Erro lendo {caminho}: {e}")
        return padrao


def salvar_json(caminho, dados):
    temporario = caminho + ".tmp"

    try:
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(
                dados,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, caminho)

    except Exception as e:
        logger.error(f"Erro salvando {caminho}: {e}")


def carregar_estado():

    global stats
    global pending
    global historico_cache

    dados = carregar_json(ARQUIVO_STATS, {})

    if isinstance(dados, dict):
        stats.update(dados)

    dados_pending = dados.get("pending", {})

    if isinstance(dados_pending, dict):
        pending.update(dados_pending)

    historico_cache = carregar_json(
        ARQUIVO_CACHE,
        {}
    )

    if not isinstance(historico_cache, dict):
        historico_cache = {}


def salvar_estado():

    with lock:

        dados = dict(stats)

        dados["pending"] = pending

        salvar_json(
            ARQUIVO_STATS,
            dados
        )

        salvar_json(
            ARQUIVO_CACHE,
            historico_cache
        )


carregar_estado()


# ============================================================
# SESSÃO HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}" if API_KEY else "",
    "Accept": "application/json",
    "User-Agent": "Robo-Futebol-Real/4.0"
})


# ============================================================
# RATE LIMIT INTELIGENTE
# ============================================================

def interpretar_reset(valor):

    if valor is None:
        return 0

    try:
        numero = float(valor)
    except Exception:
        return 0

    agora = time.time()

    # Timestamp Unix
    if numero > 1_000_000_000:
        return numero

    # Caso seja quantidade de segundos
    return agora + max(numero, 0)


def atualizar_rate_limit(response):

    remaining = response.headers.get(
        "X-RateLimit-Remaining"
    )

    limit = response.headers.get(
        "X-RateLimit-Limit"
    )

    reset = response.headers.get(
        "X-RateLimit-Reset"
    )

    try:
        if remaining is not None:
            rate_state["remaining"] = int(float(remaining))
    except Exception:
        pass

    try:
        if limit is not None:
            rate_state["limit"] = int(float(limit))
    except Exception:
        pass

    if reset is not None:
        rate_state["reset"] = interpretar_reset(reset)


def esperar_rate_limit():

    while True:

        agora = time.time()

        remaining = rate_state.get("remaining")
        reset = rate_state.get("reset", 0)

        if (
            remaining is not None
            and remaining <= 0
            and reset > agora
        ):

            espera = reset - agora + 0.5

            logger.warning(
                f"Rate limit atingido. "
                f"Aguardando {espera:.1f}s..."
            )

            time.sleep(espera)

            continue

        break


# ============================================================
# API GET
# ============================================================

def api_get(endpoint, params=None, tentativas=4):

    if not API_KEY:
        raise RuntimeError(
            "FIVE_DOLLAR_KEY não configurada."
        )

    url = BASE_API + endpoint

    for tentativa in range(1, tentativas + 1):

        with api_lock:

            esperar_rate_limit()

            try:

                response = session.get(
                    url,
                    params=params or {},
                    timeout=45
                )

                atualizar_rate_limit(response)

                stats["api_requests"] += 1

            except requests.RequestException as e:

                logger.warning(
                    f"Erro HTTP: {e}"
                )

                if tentativa >= tentativas:
                    raise

                time.sleep(3)

                continue

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                stats["api_429"] += 1

                retry_after = response.headers.get(
                    "Retry-After"
                )

                try:
                    espera = float(retry_after)
                except Exception:
                    reset = rate_state.get("reset", 0)
                    espera = max(
                        reset - time.time() + 1,
                        5
                    )

                espera = max(espera, 1)

                logger.warning(
                    f"HTTP 429. "
                    f"Aguardando {espera:.1f}s..."
                )

                time.sleep(espera)

                continue

            # ------------------------------------------------
            # ERROS
            # ------------------------------------------------

            if response.status_code >= 400:

                texto = response.text[:1000]

                raise RuntimeError(
                    f"API HTTP {response.status_code}: "
                    f"{texto}"
                )

            try:
                return response.json()

            except Exception:

                raise RuntimeError(
                    "API retornou resposta que não é JSON."
                )

    raise RuntimeError(
        "Não foi possível consultar a API."
    )


# ============================================================
# UTILITÁRIOS JSON
# ============================================================

def numero(valor):

    if valor is None:
        return None

    try:
        return float(valor)
    except Exception:
        return None


def inteiro(valor):

    if valor is None:
        return None

    try:
        return int(valor)
    except Exception:
        try:
            return int(float(valor))
        except Exception:
            return None


def extrair_lista(payload):

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for chave in (
            "fixtures",
            "results",
            "items",
            "data"
        ):
            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def extrair_objeto(payload):

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")

    if isinstance(data, dict):

        # Fixture diretamente
        if "id" in data:
            return data

        # Alguns envelopes
        for chave in (
            "fixture",
            "result",
            "item"
        ):
            valor = data.get(chave)

            if isinstance(valor, dict):
                return valor

    return None


# ============================================================
# DATA
# ============================================================

def kickoff_timestamp(jogo):

    ts = inteiro(
        jogo.get("kickoff_ts")
    )

    if ts:
        return ts

    data = jogo.get("kickoff_utc")

    if not data:
        return None

    try:

        dt = datetime.fromisoformat(
            data.replace("Z", "+00:00")
        )

        return int(
            dt.timestamp()
        )

    except Exception:

        return None


# ============================================================
# NOME DOS TIMES
# ============================================================

def nomes_times(jogo):

    teams = jogo.get(
        "teams",
        {}
    )

    home = teams.get(
        "home",
        {}
    )

    away = teams.get(
        "away",
        {}
    )

    return (
        home.get("name", "Casa"),
        away.get("name", "Fora")
    )


# ============================================================
# ODDS
# ============================================================

def extrair_estagio(market, estagio):

    if not isinstance(market, dict):
        return None

    valor = market.get(estagio)

    if isinstance(valor, dict):
        return valor

    # Algumas respostas usam valor simples
    if valor is not None:
        return {
            "line": numero(valor)
        }

    return None


def extrair_linha_preco(market, historico=False):

    if not isinstance(market, dict):
        return None, None

    # Histórico:
    # prioridade absoluta para closing.
    if historico:

        fechamento = extrair_estagio(
            market,
            "closing"
        )

        if fechamento:

            linha = numero(
                fechamento.get("line")
            )

            over = numero(
                fechamento.get("over")
            )

            return linha, over

        return None, None

    # Futuro:
    # closing = último preço pré-jogo
    fechamento = extrair_estagio(
        market,
        "closing"
    )

    if fechamento:

        linha = numero(
            fechamento.get("line")
        )

        over = numero(
            fechamento.get("over")
        )

        if linha is not None:
            return linha, over

    abertura = extrair_estagio(
        market,
        "opening"
    )

    if abertura:

        linha = numero(
            abertura.get("line")
        )

        over = numero(
            abertura.get("over")
        )

        return linha, over

    return None, None


def extrair_mercado_de_odds(odds, chave):

    if not isinstance(odds, dict):
        return None

    # Formato direto:
    #
    # odds = {
    #   "goal_line": {...},
    #   "corner_line": {...}
    # }

    mercado = odds.get(chave)

    if isinstance(mercado, dict):
        return mercado

    # Formato com bookmaker:
    #
    # odds = {
    #   "data": {
    #       "bookmakers": [...]
    #   }
    # }

    data = odds.get("data")

    if isinstance(data, dict):

        bookmakers = data.get(
            "bookmakers",
            []
        )

        if isinstance(bookmakers, list):

            for bookmaker in bookmakers:

                if not isinstance(
                    bookmaker,
                    dict
                ):
                    continue

                book_odds = bookmaker.get(
                    "odds",
                    {}
                )

                mercado = book_odds.get(
                    chave
                )

                if isinstance(
                    mercado,
                    dict
                ):
                    return mercado

    # Formato:
    # odds.bookmakers

    bookmakers = odds.get(
        "bookmakers"
    )

    if isinstance(bookmakers, list):

        for bookmaker in bookmakers:

            if not isinstance(
                bookmaker,
                dict
            ):
                continue

            book_odds = bookmaker.get(
                "odds",
                {}
            )

            mercado = book_odds.get(
                chave
            )

            if isinstance(
                mercado,
                dict
            ):
                return mercado

    return None


def extrair_odds_fixture(jogo, historico=False):

    odds = jogo.get(
        "odds",
        {}
    )

    if not isinstance(odds, dict):
        odds = {}

    # --------------------------------------------------------
    # Formato direto
    # --------------------------------------------------------

    goal_market = extrair_mercado_de_odds(
        odds,
        "goal_line"
    )

    corner_market = extrair_mercado_de_odds(
        odds,
        "corner_line"
    )

    # --------------------------------------------------------
    # Formato antigo
    # --------------------------------------------------------

    if goal_market is None:

        wrapper = jogo.get(
            "goalline"
        )

        if isinstance(wrapper, dict):

            goal_market = extrair_mercado_de_odds(
                wrapper,
                "goal_line"
            )

    if corner_market is None:

        wrapper = jogo.get(
            "corner"
        )

        if isinstance(wrapper, dict):

            corner_market = extrair_mercado_de_odds(
                wrapper,
                "corner_line"
            )

    goal_line, goal_over = extrair_linha_preco(
        goal_market,
        historico=historico
    )

    corner_line, corner_over = extrair_linha_preco(
        corner_market,
        historico=historico
    )

    return {
        "goal_line": goal_line,
        "goal_over": goal_over,
        "corner_line": corner_line,
        "corner_over": corner_over
    }


# ============================================================
# BUSCAR ODDS COMPLETAS DE UM FIXTURE
# ============================================================

def buscar_odds_completas(fixture_id):

    try:

        payload = api_get(
            f"/fixtures/{fixture_id}/odds"
        )

        data = payload.get(
            "data",
            {}
        )

        if not isinstance(data, dict):
            return None

        # Pode vir direto
        odds = data.get(
            "odds"
        )

        if isinstance(odds, dict):
            return odds

        # Ou dentro de bookmaker
        bookmakers = data.get(
            "bookmakers",
            []
        )

        if isinstance(bookmakers, list):

            for bookmaker in bookmakers:

                if not isinstance(
                    bookmaker,
                    dict
                ):
                    continue

                if bookmaker.get(
                    "slug"
                ) != "bet365":
                    continue

                odds = bookmaker.get(
                    "odds"
                )

                if isinstance(odds, dict):
                    return odds

            # fallback primeiro bookmaker
            if bookmakers:

                odds = bookmakers[0].get(
                    "odds"
                )

                if isinstance(odds, dict):
                    return odds

    except Exception as e:

        logger.warning(
            f"Não foi possível obter odds completas "
            f"do jogo {fixture_id}: {e}"
        )

    return None


def completar_odds(jogo):

    odds = extrair_odds_fixture(
        jogo,
        historico=False
    )

    # Já temos os preços
    if (
        odds["goal_over"] is not None
        and
        odds["corner_over"] is not None
    ):
        return odds

    fixture_id = jogo.get("id")

    if not fixture_id:
        return odds

    completas = buscar_odds_completas(
        fixture_id
    )

    if not completas:
        return odds

    goal_market = completas.get(
        "goal_line"
    )

    corner_market = completas.get(
        "corner_line"
    )

    goal_line, goal_over = extrair_linha_preco(
        goal_market,
        historico=False
    )

    corner_line, corner_over = extrair_linha_preco(
        corner_market,
        historico=False
    )

    if odds["goal_line"] is None:
        odds["goal_line"] = goal_line

    if odds["corner_line"] is None:
        odds["corner_line"] = corner_line

    if odds["goal_over"] is None:
        odds["goal_over"] = goal_over

    if odds["corner_over"] is None:
        odds["corner_over"] = corner_over

    return odds


# ============================================================
# PRÓXIMOS JOGOS
# ============================================================

def buscar_proximos():

    agora = int(
        time.time()
    )

    inicio = agora + int(
        HORAS_MIN * 3600
    )

    fim = agora + int(
        HORAS_MAX * 3600
    )

    logger.info(
        f"Buscando jogos entre "
        f"{HORAS_MIN}h e {HORAS_MAX}h..."
    )

    payload = api_get(
        "/fixtures",
        params={
            "start_time": inicio,
            "end_time": fim,
            "status": "scheduled",
            "include": "odds",
            "lang": "pt",
            "per_page": 50
        }
    )

    jogos = extrair_lista(
        payload
    )

    logger.info(
        f"Jogos encontrados: {len(jogos)}"
    )

    return jogos


# ============================================================
# HISTÓRICO POR LIGA
# ============================================================

def obter_league_id(jogo):

    league = jogo.get(
        "league",
        {}
    )

    if isinstance(
        league,
        dict
    ):
        return league.get("id")

    return None


def buscar_historico_liga(
    league_id,
    nome_liga=""
):

    agora = int(
        time.time()
    )

    inicio = agora - (
        7 * 24 * 3600
    )

    cache = historico_cache.get(
        str(league_id)
    )

    if isinstance(cache, dict):

        atualizado = numero(
            cache.get("timestamp")
        )

        if (
            atualizado
            and
            time.time() - atualizado
            < HISTORICO_TTL
        ):

            logger.info(
                f"Histórico em cache: "
                f"{nome_liga or league_id}"
            )

            return cache.get(
                "jogos",
                []
            )

    logger.info(
        f"Atualizando histórico: "
        f"{nome_liga or league_id}"
    )

    jogos = []

    for pagina in range(
        1,
        MAX_PAGINAS + 1
    ):

        payload = api_get(
            f"/leagues/{league_id}/fixtures",
            params={
                "start_time": inicio,
                "end_time": agora,
                "status": "finished",
                "include": "odds",
                "lang": "pt",
                "page": pagina,
                "per_page": 50
            }
        )

        lista = extrair_lista(
            payload
        )

        if not lista:
            break

        jogos.extend(
            lista
        )

        pagination = payload.get(
            "pagination",
            {}
        )

        has_more = bool(
            pagination.get(
                "has_more",
                False
            )
        )

        if not has_more:
            break

    # --------------------------------------------------------
    # Remove duplicados
    # --------------------------------------------------------

    unicos = {}

    for jogo in jogos:

        fid = jogo.get("id")

        if fid:
            unicos[str(fid)] = jogo

    jogos = list(
        unicos.values()
    )

    historico_cache[
        str(league_id)
    ] = {
        "timestamp": time.time(),
        "nome": nome_liga,
        "jogos": jogos
    }

    salvar_json(
        ARQUIVO_CACHE,
        historico_cache
    )

    logger.info(
        f"Histórico {nome_liga or league_id}: "
        f"{len(jogos)} jogos"
    )

    return jogos


def construir_historico(jogos_futuros):

    global status_state

    status_state[
        "historico_em_execucao"
    ] = True

    try:

        ligas = {}

        for jogo in jogos_futuros:

            league_id = obter_league_id(
                jogo
            )

            if not league_id:
                continue

            league = jogo.get(
                "league",
                {}
            )

            nome = ""

            if isinstance(
                league,
                dict
            ):
                nome = league.get(
                    "name",
                    ""
                )

            ligas[str(league_id)] = {
                "id": league_id,
                "nome": nome
            }

        logger.info(
            f"Ligas para histórico: "
            f"{len(ligas)}"
        )

        historico = []

        for item in ligas.values():

            try:

                lista = buscar_historico_liga(
                    item["id"],
                    item["nome"]
                )

                historico.extend(
                    lista
                )

            except Exception as e:

                logger.error(
                    f"Erro histórico liga "
                    f"{item['id']}: {e}"
                )

        # Deduplicação
        unicos = {}

        for jogo in historico:

            fid = jogo.get("id")

            if fid:
                unicos[str(fid)] = jogo

        historico = list(
            unicos.values()
        )

        stats["historico_jogos"] = len(
            historico
        )

        stats["ultimo_historico"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        salvar_estado()

        logger.info(
            f"Histórico total: "
            f"{len(historico)} jogos"
        )

        return historico

    finally:

        status_state[
            "historico_em_execucao"
        ] = False


# ============================================================
# VALIDAÇÃO DE HISTÓRICO
# ============================================================

def extrair_resultado(jogo):

    goals = jogo.get(
        "goals",
        {}
    )

    corners = jogo.get(
        "corners",
        {}
    )

    if not isinstance(
        goals,
        dict
    ):
        return None, None

    if not isinstance(
        corners,
        dict
    ):
        return None, None

    gols_casa = inteiro(
        goals.get("home")
    )

    gols_fora = inteiro(
        goals.get("away")
    )

    cantos_casa = inteiro(
        corners.get("home")
    )

    cantos_fora = inteiro(
        corners.get("away")
    )

    if None in (
        gols_casa,
        gols_fora,
        cantos_casa,
        cantos_fora
    ):
        return None, None

    gols = (
        gols_casa
        + gols_fora
    )

    cantos = (
        cantos_casa
        + cantos_fora
    )

    return gols, cantos


def chave_combinacao(
    goal_line,
    corner_line
):

    return (
        f"Over {goal_line:g} Gols + "
        f"Over {corner_line:g} Escanteios"
    )


def resultado_combinacao(
    gols,
    cantos,
    goal_line,
    corner_line
):

    # WIN somente se os dois mercados
    # ultrapassarem suas respectivas linhas.

    if (
        gols > goal_line
        and
        cantos > corner_line
    ):
        return "WIN"

    # PUSH quando algum mercado cai
    # exatamente na linha.

    if (
        gols == goal_line
        or
        cantos == corner_line
    ):
        return "PUSH"

    return "LOSS"


def analisar_historico(
    historico
):

    combinacoes = {}

    for jogo in historico:

        gols, cantos = extrair_resultado(
            jogo
        )

        if gols is None:
            continue

        odds = extrair_odds_fixture(
            jogo,
            historico=True
        )

        goal_line = odds.get(
            "goal_line"
        )

        corner_line = odds.get(
            "corner_line"
        )

        if (
            goal_line is None
            or
            corner_line is None
        ):
            continue

        # Linhas válidas
        if not (
            0.5
            <= goal_line
            <= 6.5
        ):
            continue

        if not (
            4.5
            <= corner_line
            <= 15.5
        ):
            continue

        chave = chave_combinacao(
            goal_line,
            corner_line
        )

        resultado = resultado_combinacao(
            gols,
            cantos,
            goal_line,
            corner_line
        )

        if chave not in combinacoes:

            combinacoes[chave] = {
                "goal_line": goal_line,
                "corner_line": corner_line,
                "jogos": 0,
                "wins": 0,
                "losses": 0,
                "pushes": 0
            }

        item = combinacoes[chave]

        item["jogos"] += 1

        if resultado == "WIN":
            item["wins"] += 1

        elif resultado == "LOSS":
            item["losses"] += 1

        else:
            item["pushes"] += 1

    # --------------------------------------------------------
    # Calcula assertividade
    # --------------------------------------------------------

    qualificadas = []

    for chave, item in combinacoes.items():

        decisivos = (
            item["wins"]
            +
            item["losses"]
        )

        if decisivos <= 0:
            continue

        assertividade = (
            item["wins"]
            /
            decisivos
        ) * 100

        item["decisivos"] = decisivos

        item["assertividade"] = round(
            assertividade,
            2
        )

        if (
            decisivos >= MINIMO_HISTORICO
            and
            assertividade >= ASSERTIVIDADE_MINIMA
        ):

            qualificadas.append({
                "chave": chave,
                **item
            })

    qualificadas.sort(
        key=lambda x: (
            x["assertividade"],
            x["decisivos"]
        ),
        reverse=True
    )

    stats[
        "historico_combinacoes"
    ] = len(qualificadas)

    return {
        "todas": combinacoes,
        "qualificadas": qualificadas
    }


# ============================================================
# CANDIDATOS
# ============================================================

def preparar_candidato(jogo):

    fixture_id = jogo.get(
        "id"
    )

    if not fixture_id:
        return None

    odds = completar_odds(
        jogo
    )

    goal_line = odds.get(
        "goal_line"
    )

    corner_line = odds.get(
        "corner_line"
    )

    goal_over = odds.get(
        "goal_over"
    )

    corner_over = odds.get(
        "corner_over"
    )

    if None in (
        goal_line,
        corner_line,
        goal_over,
        corner_over
    ):
        return None

    if goal_over <= 1:
        return None

    if corner_over <= 1:
        return None

    odd_combinada = (
        goal_over
        *
        corner_over
    )

    if not (
        ODD_MIN
        <= odd_combinada
        <= ODD_MAX
    ):
        return None

    home, away = nomes_times(
        jogo
    )

    league = jogo.get(
        "league",
        {}
    )

    league_name = ""

    if isinstance(
        league,
        dict
    ):
        league_name = league.get(
            "name",
            ""
        )

    kickoff = kickoff_timestamp(
        jogo
    )

    if kickoff is None:
        return None

    return {
        "fixture_id": fixture_id,

        "home": home,
        "away": away,

        "league_id": obter_league_id(
            jogo
        ),

        "league": league_name,

        "kickoff_ts": kickoff,

        "goal_line": goal_line,
        "goal_over": goal_over,

        "corner_line": corner_line,
        "corner_over": corner_over,

        "odd_combinada": round(
            odd_combinada,
            3
        ),

        "combinacao": chave_combinacao(
            goal_line,
            corner_line
        )
    }


def criar_sinais(
    jogos,
    analise_historico
):

    qualificadas = {
        item["chave"]: item
        for item
        in analise_historico[
            "qualificadas"
        ]
    }

    candidatos = []

    for jogo in jogos:

        candidato = preparar_candidato(
            jogo
        )

        if not candidato:
            continue

        historico = qualificadas.get(
            candidato["combinacao"]
        )

        if not historico:
            continue

        candidato["historico_jogos"] = (
            historico["decisivos"]
        )

        candidato["historico_wins"] = (
            historico["wins"]
        )

        candidato["historico_losses"] = (
            historico["losses"]
        )

        candidato["historico_pushes"] = (
            historico["pushes"]
        )

        candidato["assertividade"] = (
            historico["assertividade"]
        )

        candidatos.append(
            candidato
        )

    # Prioridade:
    # 1. maior assertividade
    # 2. maior amostra
    # 3. maior odd

    candidatos.sort(
        key=lambda x: (
            x["assertividade"],
            x["historico_jogos"],
            x["odd_combinada"]
        ),
        reverse=True
    )

    return candidatos[
        :QTD_POR_RODADA
    ]


# ============================================================
# TELEGRAM
# ============================================================

def telegram_enviar(texto):

    if not TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN não configurado."
        )
        return False

    if not CHAT_ID:
        logger.error(
            "CHAT_ID não configurado."
        )
        return False

    url = (
        TELEGRAM_BASE
        +
        f"/bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if response.status_code != 200:

            logger.error(
                f"Telegram HTTP "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

            return False

        return True

    except Exception as e:

        logger.error(
            f"Erro Telegram: {e}"
        )

        return False


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def formatar_sinal(sinal):

    kickoff = datetime.fromtimestamp(
        sinal["kickoff_ts"],
        tz=timezone.utc
    )

    # Horário Brasil
    brasil = kickoff - timedelta(
        hours=3
    )

    hora = brasil.strftime(
        "%d/%m %H:%M"
    )

    return (
        "⚽ <b>NOVO SINAL</b>\n\n"

        f"🏟️ <b>{sinal['home']}</b> "
        f"x "
        f"<b>{sinal['away']}</b>\n"

        f"🏆 {sinal['league']}\n"
        f"🕐 {hora}\n\n"

        f"🎯 <b>{sinal['combinacao']}</b>\n\n"

        f"⚽ Odd gols: "
        f"<b>{sinal['goal_over']:.2f}</b>\n"

        f"🚩 Odd escanteios: "
        f"<b>{sinal['corner_over']:.2f}</b>\n"

        f"💰 Odd combinada: "
        f"<b>{sinal['odd_combinada']:.2f}</b>\n\n"

        f"📊 Assertividade histórica: "
        f"<b>{sinal['assertividade']:.1f}%</b>\n"

        f"📚 Histórico: "
        f"<b>{sinal['historico_jogos']}</b> "
        f"jogos\n"

        f"✅ Wins: {sinal['historico_wins']}\n"
        f"❌ Losses: {sinal['historico_losses']}\n"
        f"➖ Pushes: {sinal['historico_pushes']}\n\n"

        "🤖 <b>Entrada baseada em histórico real</b>"
    )


# ============================================================
# REGISTRAR SINAL
# ============================================================

def registrar_sinal(sinal):

    fixture_id = str(
        sinal["fixture_id"]
    )

    with lock:

        if fixture_id in pending:
            return False

        pending[fixture_id] = {
            **sinal,
            "criado_em": datetime.now(
                timezone.utc
            ).isoformat(),
            "resultado": None
        }

        stats["total_sinais"] += 1

    salvar_estado()

    mensagem = formatar_sinal(
        sinal
    )

    enviado = telegram_enviar(
        mensagem
    )

    if not enviado:

        logger.warning(
            f"Sinal {fixture_id} salvo, "
            f"mas Telegram não confirmou envio."
        )

    return True


# ============================================================
# RESOLVER RESULTADO
# ============================================================

def buscar_fixture(fixture_id):

    payload = api_get(
        f"/fixtures/{fixture_id}"
    )

    return extrair_objeto(
        payload
    )


def resolver_sinal(
    fixture_id,
    sinal
):

    jogo = buscar_fixture(
        fixture_id
    )

    if not jogo:
        return None

    status = str(
        jogo.get(
            "status",
            ""
        )
    ).lower()

    if status not in (
        "finished",
        "ft",
        "aet",
        "pen"
    ):
        return None

    gols, cantos = extrair_resultado(
        jogo
    )

    if gols is None:
        return None

    resultado = resultado_combinacao(
        gols,
        cantos,
        sinal["goal_line"],
        sinal["corner_line"]
    )

    return {
        "resultado": resultado,
        "gols": gols,
        "cantos": cantos
    }


def enviar_resultado(
    sinal,
    resultado
):

    if resultado["resultado"] == "WIN":

        emoji = "🟢"
        titulo = "WIN"

    elif resultado["resultado"] == "LOSS":

        emoji = "🔴"
        titulo = "LOSS"

    else:

        emoji = "🟡"
        titulo = "PUSH"

    mensagem = (
        f"{emoji} <b>RESULTADO — {titulo}</b>\n\n"

        f"🏟️ <b>{sinal['home']}</b> "
        f"x "
        f"<b>{sinal['away']}</b>\n\n"

        f"🎯 {sinal['combinacao']}\n"

        f"⚽ Gols: "
        f"<b>{resultado['gols']}</b>\n"

        f"🚩 Escanteios: "
        f"<b>{resultado['cantos']}</b>\n\n"

        f"📊 Assertividade histórica: "
        f"<b>{sinal['assertividade']:.1f}%</b>\n"

        f"💰 Odd combinada: "
        f"<b>{sinal['odd_combinada']:.2f}</b>"
    )

    telegram_enviar(
        mensagem
    )


def atualizar_estatisticas_resultado(
    resultado,
    sinal
):

    if resultado == "WIN":
        stats["pre_wins"] += 1

    elif resultado == "LOSS":
        stats["pre_losses"] += 1

    else:
        stats["pre_pushes"] += 1


# ============================================================
# CICLO DE RESULTADOS
# ============================================================

def ciclo_resultados():

    if not pending:
        return

    status_state[
        "resultados_em_execucao"
    ] = True

    try:

        ids = list(
            pending.keys()
        )

        logger.info(
            f"Verificando {len(ids)} "
            f"sinais pendentes..."
        )

        alterou = False

        for fixture_id in ids:

            sinal = pending.get(
                fixture_id
            )

            if not sinal:
                continue

            try:

                resultado = resolver_sinal(
                    fixture_id,
                    sinal
                )

                if not resultado:
                    continue

                logger.info(
                    f"Resultado "
                    f"{fixture_id}: "
                    f"{resultado['resultado']}"
                )

                enviar_resultado(
                    sinal,
                    resultado
                )

                atualizar_estatisticas_resultado(
                    resultado["resultado"],
                    sinal
                )

                with lock:

                    pending.pop(
                        fixture_id,
                        None
                    )

                alterou = True

            except Exception as e:

                logger.error(
                    f"Erro resolvendo "
                    f"{fixture_id}: {e}"
                )

        if alterou:
            salvar_estado()

    finally:

        status_state[
            "resultados_em_execucao"
        ] = False


# ============================================================
# CICLO DE SINAIS
# ============================================================

def ciclo_sinais():

    status_state[
        "pre_em_execucao"
    ] = True

    try:

        logger.info(
            "======================================"
        )

        logger.info(
            "INICIANDO ANÁLISE DE NOVOS SINAIS"
        )

        # ----------------------------------------------------
        # 1. Próximos jogos
        # ----------------------------------------------------

        jogos = buscar_proximos()

        if not jogos:

            logger.info(
                "Nenhum jogo encontrado "
                "na janela configurada."
            )

            return

        # ----------------------------------------------------
        # 2. Histórico
        # ----------------------------------------------------

        historico = construir_historico(
            jogos
        )

        if not historico:

            logger.warning(
                "Histórico vazio."
            )

            return

        # ----------------------------------------------------
        # 3. Analisar combinações
        # ----------------------------------------------------

        analise = analisar_historico(
            historico
        )

        logger.info(
            f"Combinações qualificadas: "
            f"{len(analise['qualificadas'])}"
        )

        # ----------------------------------------------------
        # 4. Criar candidatos
        # ----------------------------------------------------

        candidatos = criar_sinais(
            jogos,
            analise
        )

        logger.info(
            f"Candidatos finais: "
            f"{len(candidatos)}"
        )

        enviados = 0

        # ----------------------------------------------------
        # 5. Enviar sinais
        # ----------------------------------------------------

        for sinal in candidatos:

            fixture_id = str(
                sinal["fixture_id"]
            )

            if fixture_id in pending:
                continue

            if registrar_sinal(
                sinal
            ):
                enviados += 1

        stats["ultima_execucao"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        salvar_estado()

        status_state[
            "ultima_mensagem"
        ] = (
            f"{enviados} sinais enviados"
        )

        logger.info(
            f"Sinais enviados: {enviados}"
        )

        logger.info(
            "FINAL DA ANÁLISE"
        )

        logger.info(
            "======================================"
        )

    except Exception as e:

        status_state[
            "ultimo_erro"
        ] = str(e)

        logger.exception(
            "Erro no ciclo de sinais"
        )

    finally:

        status_state[
            "pre_em_execucao"
        ] = False


# ============================================================
# MONITOR
# ============================================================

def monitor():

    logger.info(
        "Monitor iniciado."
    )

    ultima_pre = 0
    ultima_resultados = 0

    while True:

        try:

            agora = time.time()

            # -----------------------------------------------
            # NOVOS SINAIS
            # -----------------------------------------------

            if (
                agora - ultima_pre
                >= INTERVALO_PRE
            ):

                ultima_pre = agora

                ciclo_sinais()

            # -----------------------------------------------
            # RESULTADOS
            # -----------------------------------------------

            agora = time.time()

            if (
                agora - ultima_resultados
                >= INTERVALO_RESULTADOS
            ):

                ultima_resultados = agora

                ciclo_resultados()

        except Exception as e:

            logger.exception(
                f"Erro no monitor: {e}"
            )

        time.sleep(5)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "Robo Futebol Combinado v4",
        "monitor": MONITOR_ATIVO,
        "historico_dias": 7,
        "odd_min": ODD_MIN,
        "odd_max": ODD_MAX,
        "assertividade_minima": ASSERTIVIDADE_MINIMA,
        "minimo_historico": MINIMO_HISTORICO,
        "pendentes": len(pending),
        "api_remaining": rate_state["remaining"],
        "api_limit": rate_state["limit"]
    })


@app.route("/status")
def status():

    return jsonify({
        "bot": "online",
        "monitor": MONITOR_ATIVO,

        "pending": len(
            pending
        ),

        "stats": stats,

        "rate_limit": {
            "remaining": rate_state[
                "remaining"
            ],
            "limit": rate_state[
                "limit"
            ],
            "reset": rate_state[
                "reset"
            ]
        },

        "execucao": status_state
    })


@app.route("/debug/pendentes")
def debug_pendentes():

    return jsonify({
        "total": len(
            pending
        ),
        "pendentes": pending
    })


@app.route("/debug/historico")
def debug_historico():

    resumo = {}

    for league_id, dados in historico_cache.items():

        if not isinstance(
            dados,
            dict
        ):
            continue

        resumo[league_id] = {
            "nome": dados.get(
                "nome"
            ),
            "timestamp": dados.get(
                "timestamp"
            ),
            "jogos": len(
                dados.get(
                    "jogos",
                    []
                )
            )
        }

    return jsonify({
        "total_ligas_cache": len(
            resumo
        ),
        "total_jogos": stats[
            "historico_jogos"
        ],
        "ultima_atualizacao": stats[
            "ultimo_historico"
        ],
        "ligas": resumo
    })


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    thread = threading.Thread(
        target=ciclo_sinais,
        daemon=True
    )

    thread.start()

    return jsonify({
        "ok": True,
        "mensagem": (
            "Ciclo de análise iniciado "
            "em segundo plano."
        )
    })


@app.route("/debug/resultados-agora")
def debug_resultados_agora():

    thread = threading.Thread(
        target=ciclo_resultados,
        daemon=True
    )

    thread.start()

    return jsonify({
        "ok": True,
        "mensagem": (
            "Verificação de resultados "
            "iniciada."
        )
    })


@app.route("/debug/api")
def debug_api():

    try:

        payload = api_get(
            "/status"
        )

        return jsonify({
            "ok": True,
            "api": payload,
            "rate_limit": rate_state
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "erro": str(e),
            "rate_limit": rate_state
        }), 500


# ============================================================
# START
# ============================================================

if MONITOR_ATIVO:

    thread_monitor = threading.Thread(
        target=monitor,
        daemon=True
    )

    thread_monitor.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
)
