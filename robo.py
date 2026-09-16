import os
import json
import time
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
logger = logging.getLogger("robo")


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

# Estratégia
ENTRADA_INICIAL = float(os.getenv("ENTRADA_INICIAL", "10"))
MULTIPLICADOR_GALE = float(os.getenv("MULTIPLICADOR_GALE", "2"))
MAX_GALES = int(os.getenv("MAX_GALES", "2"))
TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))

# Janela
JANELA_HORAS = int(os.getenv("JANELA_HORAS", "24"))
INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

# Histórico
QTD_HISTORICO_TIME = int(os.getenv("QTD_HISTORICO_TIME", "10"))
MIN_JOGOS_HISTORICO = int(os.getenv("MIN_JOGOS_HISTORICO", "8"))

MIN_MEDIA_ESCANTEIOS = float(
    os.getenv("MIN_MEDIA_ESCANTEIOS", "8.5")
)

MAX_MEDIA_GOLS = float(
    os.getenv("MAX_MEDIA_GOLS", "4.0")
)

MIN_PROBABILIDADE_HISTORICA = float(
    os.getenv("MIN_PROBABILIDADE_HISTORICA", "0.62")
)

# Odds
ODD_MINIMA = float(os.getenv("ODD_MINIMA", "1.80"))
ODD_MAXIMA = float(os.getenv("ODD_MAXIMA", "2.50"))
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))

LINHA_ESCANTEIOS = float(os.getenv("LINHA_ESCANTEIOS", "8.5"))
LINHA_GOLS = float(os.getenv("LINHA_GOLS", "4.5"))

BOOKMAKER = os.getenv("BOOKMAKER", "bet365")

# Quantos jogos novos investigar por ciclo
MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv("MAX_JOGOS_ANALISADOS_CICLO", "4")
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

# Arquivos
ARQUIVO_STATS = os.getenv("ARQUIVO_STATS", "stats.json")
ARQUIVO_SINAIS = os.getenv("ARQUIVO_SINAIS", "sinais.json")
ARQUIVO_CACHE = os.getenv("ARQUIVO_CACHE", "cache.json")


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

        with open(arquivo, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        logger.error("Erro lendo %s: %s", arquivo, e)
        return padrao


def salvar_json(arquivo, dados):
    try:
        with arquivo_lock:
            temp = arquivo + ".tmp"

            with open(temp, "w", encoding="utf-8") as f:
                json.dump(
                    dados,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(temp, arquivo)

    except Exception as e:
        logger.error("Erro salvando %s: %s", arquivo, e)


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

sinais = carregar_json(ARQUIVO_SINAIS, [])

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

cache_persistente.setdefault("historicos", {})
cache_persistente.setdefault("cursor_partidas", 0)

for gale in range(MAX_GALES + 1):
    stats["por_gale"].setdefault(
        str(gale),
        {"wins": 0, "losses": 0}
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
    return datetime.now(timezone.utc)


def iso_agora():
    return utc_agora().isoformat()


def timestamp_agora():
    return int(utc_agora().timestamp())


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram não configurado.")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        if r.status_code != 200:
            logger.error("Telegram %s | %s", r.status_code, r.text)
            return False

        return True

    except Exception as e:
        logger.error("Erro Telegram: %s", e)
        return False


# ============================================================
# RATE LIMIT GLOBAL
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
                diferenca = agora - ultimo_request_api

                if diferenca < API_MIN_INTERVAL_SECONDS:
                    espera = max(
                        espera,
                        API_MIN_INTERVAL_SECONDS - diferenca
                    )

            if len(api_request_times) >= API_MAX_REQUESTS_PER_MINUTE:
                espera_janela = (
                    60
                    - (agora - api_request_times[0])
                    + 0.5
                )

                espera = max(espera, espera_janela)

            if espera <= 0:
                agora = time.monotonic()

                api_request_times.append(agora)
                ultimo_request_api = agora
                return

        logger.info(
            "⏳ Rate limit: aguardando %.1fs",
            espera
        )

        time.sleep(max(0.1, espera))


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None, permitir_403=False):
    global api_backoff_until

    if not API_KEY:
        logger.error("FIVE_DOLLAR_API_KEY não configurada.")
        return None

    esperar_limite_api()

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-combinado/4.0"
    }

    try:
        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

    except requests.RequestException as e:
        logger.error("Erro de conexão API: %s", e)
        return None

    if r.status_code == 429:
        retry = r.headers.get("Retry-After")

        try:
            espera = int(retry)
        except (TypeError, ValueError):
            espera = API_BACKOFF_429

        with api_lock:
            api_backoff_until = max(
                api_backoff_until,
                time.monotonic() + espera
            )

        logger.error(
            "API 429 | aguardando %ss | %s",
            espera,
            r.text
        )

        return None

    if r.status_code == 403 and permitir_403:
        logger.warning("API 403 | %s", r.text)
        return {"_status": 403}

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
        logger.error("JSON inválido da API.")
        return None


def extrair_data(resposta):
    if resposta is None:
        return None

    if isinstance(resposta, dict) and "data" in resposta:
        return resposta["data"]

    return resposta


# ============================================================
# FIXTURES FUTUROS
# ============================================================

def buscar_partidas():
    agora_cache = time.time()

    with dados_lock:
        if (
            cache_fixtures["data"]
            and agora_cache - cache_fixtures["timestamp"]
            < CACHE_FIXTURES_TTL
        ):
            logger.info(
                "Usando cache: %d partidas.",
                len(cache_fixtures["data"])
            )
            return cache_fixtures["data"]

    agora = utc_agora()
    fim = agora + timedelta(hours=JANELA_HORAS)

    params = {
        "start_time": int(agora.timestamp()),
        "end_time": int(fim.timestamp()),
        "status": "scheduled",
        "per_page": 50,
        "include": "odds",
        "lang": "pt"
    }

    logger.info(
        "Buscando partidas: %s -> %s",
        params["start_time"],
        params["end_time"]
    )

    resposta = api_get(
        "/fixtures",
        params=params,
        permitir_403=True
    )

    # Free pode não aceitar include=odds.
    if isinstance(resposta, dict) and resposta.get("_status") == 403:
        logger.info(
            "Plano não aceitou include=odds. "
            "Tentando /fixtures sem expansão de odds."
        )

        params.pop("include", None)

        resposta = api_get(
            "/fixtures",
            params=params
        )

    dados = extrair_data(resposta)

    if not isinstance(dados, list):
        logger.warning("Nenhuma lista de partidas retornada.")
        return []

    with dados_lock:
        cache_fixtures["timestamp"] = agora_cache
        cache_fixtures["data"] = dados

    logger.info("%d partidas encontradas.", len(dados))

    return dados


# ============================================================
# INFORMAÇÕES DO FIXTURE
# ============================================================

def fixture_id(jogo):
    if not isinstance(jogo, dict):
        return None

    valor = jogo.get("id")

    if valor is None:
        valor = jogo.get("fixture_id")

    return str(valor) if valor is not None else None


def dados_times(jogo):
    teams = jogo.get("teams", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    if not isinstance(home, dict):
        home = {}

    if not isinstance(away, dict):
        away = {}

    home_id = home.get("id")
    away_id = away.get("id")

    return {
        "home_id": str(home_id) if home_id is not None else None,
        "away_id": str(away_id) if away_id is not None else None,
        "home": home.get("name", "Casa"),
        "away": away.get("name", "Fora")
    }


def kickoff_ts(jogo):
    valor = jogo.get("kickoff_ts")

    try:
        return int(valor)
    except (TypeError, ValueError):
        pass

    valor = jogo.get("kickoff_utc")

    if valor:
        try:
            dt = datetime.fromisoformat(
                str(valor).replace("Z", "+00:00")
            )

            return int(dt.timestamp())

        except Exception:
            pass

    return None


# ============================================================
# HISTÓRICO REAL DO TIME
# ============================================================

def buscar_historico_time(team_id):
    if not team_id:
        return []

    chave = str(team_id)
    agora = time.time()

    with dados_lock:
        item = cache_persistente["historicos"].get(chave)

        if item:
            idade = agora - float(item.get("timestamp", 0))

            if idade < CACHE_HISTORICO_TTL:
                partidas = item.get("data", [])

                logger.info(
                    "📦 Histórico time %s via cache: %d jogos",
                    team_id,
                    len(partidas)
                )

                return partidas

    logger.info(
        "📊 Buscando histórico real do time %s...",
        team_id
    )

    resposta = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
            "per_page": QTD_HISTORICO_TIME,
            "lang": "pt"
        }
    )

    dados = extrair_data(resposta)

    if not isinstance(dados, list):
        logger.warning(
            "Histórico do time %s indisponível.",
            team_id
        )
        return []

    dados = dados[:QTD_HISTORICO_TIME]

    with dados_lock:
        cache_persistente["historicos"][chave] = {
            "timestamp": agora,
            "data": dados
        }

        salvar_json(
            ARQUIVO_CACHE,
            cache_persistente
        )

    logger.info(
        "📊 Time %s: %d jogos históricos obtidos.",
        team_id,
        len(dados)
    )

    return dados


# ============================================================
# GOLS / ESCANTEIOS DE UM JOGO
# ============================================================

def extrair_totais(jogo):
    if not isinstance(jogo, dict):
        return None, None

    goals = jogo.get("goals", {})
    corners = jogo.get("corners", {})

    if not isinstance(goals, dict):
        return None, None

    if not isinstance(corners, dict):
        return None, None

    try:
        gh = float(goals.get("home"))
        ga = float(goals.get("away"))
        ch = float(corners.get("home"))
        ca = float(corners.get("away"))

    except (TypeError, ValueError):
        return None, None

    return gh + ga, ch + ca


# ============================================================
# COMBINAR HISTÓRICOS DOS DOIS TIMES
# ============================================================

def combinar_historicos(home_history, away_history):
    """
    Une os últimos jogos dos dois times e remove duplicatas
    pelo fixture id.

    Isso evita contar duas vezes um confronto entre eles.
    """

    unicos = {}

    for jogo in home_history + away_history:
        if not isinstance(jogo, dict):
            continue

        fid = jogo.get("id")

        if fid is None:
            continue

        gols, cantos = extrair_totais(jogo)

        if gols is None or cantos is None:
            continue

        unicos[str(fid)] = {
            "id": str(fid),
            "gols": gols,
            "cantos": cantos
        }

    return list(unicos.values())


# ============================================================
# CALCULAR HISTÓRICO
# ============================================================

def calcular_estatisticas_historicas(
    home_history,
    away_history
):
    partidas = combinar_historicos(
        home_history,
        away_history
    )

    total = len(partidas)

    if total < MIN_JOGOS_HISTORICO:
        return None

    media_gols = (
        sum(x["gols"] for x in partidas)
        / total
    )

    media_cantos = (
        sum(x["cantos"] for x in partidas)
        / total
    )

    over_cantos = sum(
        1
        for x in partidas
        if x["cantos"] > LINHA_ESCANTEIOS
    )

    under_gols = sum(
        1
        for x in partidas
        if x["gols"] < LINHA_GOLS
    )

    combinado = sum(
        1
        for x in partidas
        if (
            x["cantos"] > LINHA_ESCANTEIOS
            and x["gols"] < LINHA_GOLS
        )
    )

    return {
        "jogos": total,

        "media_gols": round(media_gols, 2),
        "media_escanteios": round(media_cantos, 2),

        "over_8_5_cantos": over_cantos,
        "under_4_5_gols": under_gols,
        "acertos_combinados": combinado,

        "taxa_over_cantos": round(
            over_cantos / total * 100,
            2
        ),

        "taxa_under_gols": round(
            under_gols / total * 100,
            2
        ),

        "taxa_combinada": round(
            combinado / total * 100,
            2
        )
    }


def historico_aprovado(hist):
    if not hist:
        return False

    if hist["media_escanteios"] < MIN_MEDIA_ESCANTEIOS:
        return False

    if hist["media_gols"] > MAX_MEDIA_GOLS:
        return False

    if (
        hist["taxa_combinada"] / 100
        < MIN_PROBABILIDADE_HISTORICA
    ):
        return False

    return True


# ============================================================
# ODDS
# ============================================================

def buscar_odds(jogo):
    fid = fixture_id(jogo)

    if not fid:
        return None

    # Se include=odds trouxe o objeto completo, tenta primeiro.
    odds_embutidas = jogo.get("odds")

    if odds_embutidas:
        return {
            "fixture_id": fid,
            "bookmakers": [
                {
                    "name": "Bet 365",
                    "slug": "bet365",
                    "odds": odds_embutidas
                }
            ]
        }

    agora = time.time()

    with dados_lock:
        item = cache_odds.get(fid)

        if item:
            if agora - item["timestamp"] < CACHE_ODDS_TTL:
                return item["data"]

    logger.info("💰 Buscando odds fixture %s...", fid)

    resposta = api_get(
        f"/fixtures/{fid}/odds",
        params={
            "bookmakers": BOOKMAKER
        }
    )

    dados = extrair_data(resposta)

    if dados:
        with dados_lock:
            cache_odds[fid] = {
                "timestamp": agora,
                "data": dados
            }

    return dados


def pegar_bookmaker_odds(dados):
    if not isinstance(dados, dict):
        return None

    # Formato oficial atual:
    # data.bookmakers[].odds

    bookmakers = dados.get("bookmakers")

    if isinstance(bookmakers, list):
        for book in bookmakers:
            if not isinstance(book, dict):
                continue

            slug = str(book.get("slug", "")).lower()

            if slug == BOOKMAKER.lower():
                odds = book.get("odds")

                if isinstance(odds, dict):
                    return odds

        # fallback primeiro bookmaker
        for book in bookmakers:
            if isinstance(book, dict):
                odds = book.get("odds")

                if isinstance(odds, dict):
                    return odds

    # Compatibilidade com include=odds
    if "goal_line" in dados or "corner_line" in dados:
        return dados

    odds = dados.get("odds")

    if isinstance(odds, dict):
        return odds

    return None


def melhor_estagio(mercado):
    """
    Para pré-jogo:
    closing = preço pré-match mais recente.
    Se não existir, usa opening.
    """

    if not isinstance(mercado, dict):
        return None

    closing = mercado.get("closing")

    if isinstance(closing, dict):
        return closing

    opening = mercado.get("opening")

    if isinstance(opening, dict):
        return opening

    return None


def extrair_odds_estrategia(dados):
    odds = pegar_bookmaker_odds(dados)

    if not odds:
        return None

    corner_market = odds.get("corner_line")
    goal_market = odds.get("goal_line")

    corner = melhor_estagio(corner_market)
    goal = melhor_estagio(goal_market)

    if not corner or not goal:
        return None

    try:
        linha_cantos_api = float(corner.get("line"))
        odd_over_cantos = float(corner.get("over"))

        linha_gols_api = float(goal.get("line"))
        odd_under_gols = float(goal.get("under"))

    except (TypeError, ValueError):
        return None

    # A estratégia é especificamente:
    # Over 8.5 corners + Under 4.5 goals.
    #
    # Não vamos fingir que uma linha 9.5 é 8.5
    # nem que uma linha 2.5 é 4.5.

    if abs(linha_cantos_api - LINHA_ESCANTEIOS) > 0.001:
        return {
            "valido": False,
            "motivo": (
                f"linha de cantos disponível "
                f"{linha_cantos_api}, procuramos "
                f"{LINHA_ESCANTEIOS}"
            )
        }

    if abs(linha_gols_api - LINHA_GOLS) > 0.001:
        return {
            "valido": False,
            "motivo": (
                f"linha de gols disponível "
                f"{linha_gols_api}, procuramos "
                f"{LINHA_GOLS}"
            )
        }

    odd_combinada = odd_over_cantos * odd_under_gols

    return {
        "valido": True,
        "linha_cantos": linha_cantos_api,
        "odd_over_cantos": round(odd_over_cantos, 3),
        "linha_gols": linha_gols_api,
        "odd_under_gols": round(odd_under_gols, 3),

        # Produto matemático das duas cotações.
        # Não é garantia de preço de Bet Builder.
        "odd_combinada_teorica": round(
            odd_combinada,
            3
        )
    }


# ============================================================
# SINAIS ATIVOS
# ============================================================

def jogo_ja_usado(fid):
    fid = str(fid)

    with dados_lock:
        return any(
            str(s.get("fixture_id")) == fid
            and s.get("status") == "ATIVO"
            for s in sinais
        )


def bancas_livres():
    with dados_lock:
        return [
            banca
            for banca, dados in stats["bancas"].items()
            if not dados.get("ativa", False)
        ]


def valor_entrada(banca):
    gale = int(stats["bancas"][banca].get("gale", 0))

    return round(
        ENTRADA_INICIAL
        * (MULTIPLICADOR_GALE ** gale),
        2
    )


# ============================================================
# PRÉ-SELEÇÃO ROTATIVA
# ============================================================

def selecionar_lote(partidas):
    elegiveis = []

    agora = timestamp_agora()

    for jogo in partidas:
        fid = fixture_id(jogo)

        if not fid or jogo_ja_usado(fid):
            continue

        times = dados_times(jogo)

        if not times["home_id"] or not times["away_id"]:
            continue

        inicio = kickoff_ts(jogo)

        if not inicio or inicio <= agora:
            continue

        elegiveis.append(jogo)

    if not elegiveis:
        return []

    elegiveis.sort(
        key=lambda x: kickoff_ts(x) or 9999999999
    )

    cursor = int(
        cache_persistente.get("cursor_partidas", 0)
    )

    if cursor >= len(elegiveis):
        cursor = 0

    lote = []

    indice = cursor

    while (
        len(lote) < MAX_JOGOS_ANALISADOS_CICLO
        and len(lote) < len(elegiveis)
    ):
        lote.append(
            elegiveis[indice % len(elegiveis)]
        )

        indice += 1

    cache_persistente["cursor_partidas"] = (
        indice % len(elegiveis)
    )

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    logger.info(
        "🔎 Analisando lote de %d/%d partidas "
        "(cursor=%d).",
        len(lote),
        len(elegiveis),
        cursor
    )

    return lote


# ============================================================
# ANALISAR UM JOGO
# ============================================================

def analisar_jogo(jogo):
    fid = fixture_id(jogo)
    times = dados_times(jogo)

    logger.info(
        "🔍 %s x %s | fixture %s",
        times["home"],
        times["away"],
        fid
    )

    # --------------------------------------------------------
    # HISTÓRICO DOS DOIS TIMES
    # --------------------------------------------------------

    hist_home = buscar_historico_time(
        times["home_id"]
    )

    hist_away = buscar_historico_time(
        times["away_id"]
    )

    hist = calcular_estatisticas_historicas(
        hist_home,
        hist_away
    )

    if not hist:
        logger.info(
            "❌ %s x %s | histórico insuficiente.",
            times["home"],
            times["away"]
        )
        return None

    logger.info(
        "📊 %s x %s | jogos=%d | "
        "cantos=%.2f | gols=%.2f | combinado=%.2f%%",
        times["home"],
        times["away"],
        hist["jogos"],
        hist["media_escanteios"],
        hist["media_gols"],
        hist["taxa_combinada"]
    )

    if not historico_aprovado(hist):
        logger.info(
            "❌ Reprovado histórico | %s x %s",
            times["home"],
            times["away"]
        )
        return None

    # --------------------------------------------------------
    # ODDS
    # --------------------------------------------------------

    dados_odds = buscar_odds(jogo)

    if not dados_odds:
        logger.info(
            "❌ Sem odds | %s x %s",
            times["home"],
            times["away"]
        )
        return None

    mercado = extrair_odds_estrategia(
        dados_odds
    )

    if not mercado:
        logger.info(
            "❌ Mercado de gols/cantos indisponível | %s x %s",
            times["home"],
            times["away"]
        )
        return None

    if not mercado.get("valido"):
        logger.info(
            "❌ %s x %s | %s",
            times["home"],
            times["away"],
            mercado.get("motivo")
        )
        return None

    odd = mercado["odd_combinada_teorica"]

    if odd < ODD_MINIMA or odd > ODD_MAXIMA:
        logger.info(
            "❌ Odd %.2f fora de %.2f-%.2f | %s x %s",
            odd,
            ODD_MINIMA,
            ODD_MAXIMA,
            times["home"],
            times["away"]
        )
        return None

    distancia = abs(odd - ODD_ALVO)

    score = (
        hist["taxa_combinada"]
        + hist["taxa_over_cantos"] * 0.15
        + hist["taxa_under_gols"] * 0.15
        - distancia * 10
    )

    logger.info(
        "✅ CANDIDATO | %s x %s | "
        "%.2f%% | odd teórica %.2f",
        times["home"],
        times["away"],
        hist["taxa_combinada"],
        odd
    )

    return {
        "fixture_id": fid,
        "home": times["home"],
        "away": times["away"],
        "home_id": times["home_id"],
        "away_id": times["away_id"],
        "inicio": kickoff_ts(jogo),
        "historico": hist,
        "mercado": mercado,
        "odd_combinada": odd,
        "score": round(score, 3)
    }


# ============================================================
# HORÁRIO BRASIL
# ============================================================

def horario_br(ts):
    if not ts:
        return "Não informado"

    try:
        dt = datetime.fromtimestamp(
            int(ts),
            timezone.utc
        ) - timedelta(hours=3)

        return dt.strftime("%d/%m/%Y %H:%M")

    except Exception:
        return str(ts)


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(banca, candidato):
    with dados_lock:
        gale = int(
            stats["bancas"][banca].get("gale", 0)
        )

        entrada = valor_entrada(banca)

        sinal = {
            "id": (
                f"{candidato['fixture_id']}-"
                f"{banca}-{int(time.time())}"
            ),
            "fixture_id": candidato["fixture_id"],
            "home": candidato["home"],
            "away": candidato["away"],
            "inicio": candidato["inicio"],
            "banca": banca,
            "gale": gale,
            "entrada": entrada,

            "mercado": (
                "Over 8.5 escanteios + "
                "Under 4.5 gols"
            ),

            "odd_over_cantos": candidato[
                "mercado"
            ]["odd_over_cantos"],

            "odd_under_gols": candidato[
                "mercado"
            ]["odd_under_gols"],

            "odd_combinada": candidato[
                "odd_combinada"
            ],

            "historico": candidato["historico"],
            "status": "ATIVO",
            "resultado": None,
            "criado_em": iso_agora(),
            "resolvido_em": None
        }

        sinais.append(sinal)

        stats["bancas"][banca]["ativa"] = True

        salvar_json(ARQUIVO_SINAIS, sinais)
        salvar_json(ARQUIVO_STATS, stats)

    h = candidato["historico"]

    texto = (
        "🚨 <b>NOVO SINAL PRÉ-JOGO</b>\n\n"

        f"⚽ <b>{candidato['home']} x "
        f"{candidato['away']}</b>\n"

        f"🕒 {horario_br(candidato['inicio'])}\n\n"

        "🎯 <b>ENTRADA COMBINADA</b>\n"
        "🚩 Over 8.5 escanteios\n"
        "🥅 Under 4.5 gols\n\n"

        f"🚩 Odd Over 8.5: "
        f"{candidato['mercado']['odd_over_cantos']:.2f}\n"

        f"🥅 Odd Under 4.5: "
        f"{candidato['mercado']['odd_under_gols']:.2f}\n"

        f"🔥 Odd combinada teórica: "
        f"{candidato['odd_combinada']:.2f}\n\n"

        f"🏦 Banca: {banca}\n"
        f"🔄 Gale: {gale}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}\n\n"

        "📊 <b>HISTÓRICO REAL</b>\n"
        f"Jogos analisados: {h['jogos']}\n"
        f"Média escanteios: {h['media_escanteios']:.2f}\n"
        f"Média gols: {h['media_gols']:.2f}\n"
        f"Over 8.5 cantos: {h['taxa_over_cantos']:.2f}%\n"
        f"Under 4.5 gols: {h['taxa_under_gols']:.2f}%\n"
        f"🎯 Combinação: {h['taxa_combinada']:.2f}%\n\n"

        "ℹ️ Odd combinada exibida é o produto "
        "matemático das duas odds individuais."
    )

    enviar_telegram(texto)

    logger.info(
        "🚨 SINAL | Banca %s | Gale %d | "
        "%s x %s | R$ %.2f",
        banca,
        gale,
        candidato["home"],
        candidato["away"],
        entrada
    )


# ============================================================
# RESULTADO FINAL
# ============================================================

def buscar_fixture(fid):
    resposta = api_get(
        f"/fixtures/{fid}"
    )

    dados = extrair_data(resposta)

    return dados if isinstance(dados, dict) else None


def registrar_resultado(sinal, resultado, gols, cantos):
    with dados_lock:
        banca = str(sinal["banca"])
        gale = int(sinal["gale"])
        entrada = float(sinal["entrada"])
        odd = float(sinal["odd_combinada"])

        sinal["status"] = "FINALIZADO"
        sinal["resultado"] = resultado
        sinal["gols_final"] = gols
        sinal["cantos_final"] = cantos
        sinal["resolvido_em"] = iso_agora()

        stats["total"] += 1

        stats["por_gale"].setdefault(
            str(gale),
            {"wins": 0, "losses": 0}
        )

        if resultado == "WIN":
            lucro = entrada * (odd - 1)

            stats["wins"] += 1
            stats["por_gale"][str(gale)]["wins"] += 1

            stats["bancas"][banca]["wins"] += 1
            stats["bancas"][banca]["lucro"] += lucro

            # WIN reseta
            stats["bancas"][banca]["gale"] = 0

        else:
            lucro = -entrada

            stats["losses"] += 1
            stats["por_gale"][str(gale)]["losses"] += 1

            stats["bancas"][banca]["losses"] += 1
            stats["bancas"][banca]["lucro"] += lucro

            if gale < MAX_GALES:
                stats["bancas"][banca]["gale"] = gale + 1
            else:
                stats["bancas"][banca]["gale"] = 0

        stats["lucro_teorico"] += lucro

        stats["lucro_teorico"] = round(
            stats["lucro_teorico"],
            2
        )

        stats["bancas"][banca]["lucro"] = round(
            stats["bancas"][banca]["lucro"],
            2
        )

        stats["bancas"][banca]["ativa"] = False

        salvar_json(ARQUIVO_SINAIS, sinais)
        salvar_json(ARQUIVO_STATS, stats)

        total = stats["wins"] + stats["losses"]

        assertividade = (
            stats["wins"] / total * 100
            if total
            else 0
        )

    emoji = "✅" if resultado == "WIN" else "❌"

    enviar_telegram(
        f"{emoji} <b>{resultado}</b>\n\n"

        f"⚽ {sinal['home']} x {sinal['away']}\n"

        f"🥅 Gols: {int(gols)}\n"
        f"🚩 Escanteios: {int(cantos)}\n\n"

        f"🏦 Banca: {banca}\n"
        f"🔄 Gale: {gale}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}\n\n"

        f"✅ Wins: {stats['wins']}\n"
        f"❌ Losses: {stats['losses']}\n"
        f"🎯 Assertividade: {assertividade:.2f}%\n"

        f"💰 Resultado teórico acumulado: "
        f"R$ {stats['lucro_teorico']:.2f}"
    )

    logger.info(
        "%s | %s x %s | gols=%s | cantos=%s",
        resultado,
        sinal["home"],
        sinal["away"],
        gols,
        cantos
    )


# ============================================================
# RESOLVER SINAIS
# ============================================================

def resolver_sinais():
    with dados_lock:
        ativos = [
            s
            for s in sinais
            if s.get("status") == "ATIVO"
        ]

    if not ativos:
        return

    agora = timestamp_agora()

    for sinal in ativos:
        inicio = sinal.get("inicio")

        # Não desperdiça chamada antes do jogo.
        if inicio and agora < int(inicio) + 90 * 60:
            continue

        jogo = buscar_fixture(
            sinal["fixture_id"]
        )

        if not jogo:
            continue

        status = str(
            jogo.get("status", "")
        ).lower()

        # A documentação recomenda settlement
        # somente em "finished".
        if status != "finished":
            logger.info(
                "⏳ Fixture %s ainda não finalizado: %s",
                sinal["fixture_id"],
                status
            )
            continue

        gols, cantos = extrair_totais(jogo)

        if gols is None or cantos is None:
            logger.warning(
                "Fixture %s finished mas sem gols/cantos.",
                sinal["fixture_id"]
            )
            continue

        ganhou_cantos = cantos > LINHA_ESCANTEIOS
        ganhou_gols = gols < LINHA_GOLS

        resultado = (
            "WIN"
            if ganhou_cantos and ganhou_gols
            else "LOSS"
        )

        registrar_resultado(
            sinal,
            resultado,
            gols,
            cantos
        )


# ============================================================
# ANÁLISE
# ============================================================

def analisar_partidas():
    livres = bancas_livres()

    if not livres:
        logger.info(
            "🏦 As %d bancas estão ocupadas.",
            TOTAL_BANCAS
        )
        return

    partidas = buscar_partidas()

    if not partidas:
        logger.info("Nenhuma partida futura encontrada.")
        return

    lote = selecionar_lote(partidas)

    if not lote:
        logger.info("Nenhum jogo elegível no lote.")
        return

    candidatos = []

    for jogo in lote:
        try:
            candidato = analisar_jogo(jogo)

            if candidato:
                candidatos.append(candidato)

        except Exception:
            logger.exception(
                "Erro analisando fixture %s",
                fixture_id(jogo)
            )

    if not candidatos:
        logger.info(
            "Nenhum candidato aprovado neste ciclo."
        )
        return

    candidatos.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    logger.info(
        "🏆 %d candidato(s) aprovado(s).",
        len(candidatos)
    )

    usados = set()

    for banca in livres:
        escolhido = None

        for candidato in candidatos:
            fid = candidato["fixture_id"]

            if fid in usados:
                continue

            if jogo_ja_usado(fid):
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
            escolhido["fixture_id"]
        )


# ============================================================
# CICLO
# ============================================================

def executar_ciclo():
    logger.info("=" * 70)
    logger.info("INICIANDO CICLO")

    try:
        resolver_sinais()

    except Exception:
        logger.exception(
            "Erro ao resolver sinais."
        )

    try:
        analisar_partidas()

    except Exception:
        logger.exception(
            "Erro na análise."
        )

    logger.info("CICLO FINALIZADO")


def loop_robo():
    logger.info("🤖 Robô iniciado.")
    logger.info(
        "API limitada internamente a %d req/min.",
        API_MAX_REQUESTS_PER_MINUTE
    )

    time.sleep(5)

    while True:
        inicio = time.time()

        executar_ciclo()

        duracao = time.time() - inicio

        espera = max(
            30,
            INTERVALO_ANALISE_SEGUNDOS - duracao
        )

        logger.info(
            "Próximo ciclo em %.0fs.",
            espera
        )

        time.sleep(espera)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():
    total = stats["wins"] + stats["losses"]

    taxa = (
        stats["wins"] / total * 100
        if total
        else 0
    )

    ativos = [
        s
        for s in sinais
        if s.get("status") == "ATIVO"
    ]

    return jsonify({
        "online": True,
        "estrategia": (
            "Over 8.5 escanteios + Under 4.5 gols"
        ),
        "janela_horas": JANELA_HORAS,
        "bancas": TOTAL_BANCAS,
        "bancas_livres": bancas_livres(),
        "sinais_ativos": len(ativos),
        "wins": stats["wins"],
        "losses": stats["losses"],
        "assertividade": round(taxa, 2),
        "lucro_teorico": stats["lucro_teorico"],
        "api_limite_interno": API_MAX_REQUESTS_PER_MINUTE
    })


@app.route("/status")
def status():
    return jsonify({
        "online": True,
        "hora_utc": iso_agora(),
        "bancas_livres": bancas_livres(),
        "cursor_partidas": cache_persistente.get(
            "cursor_partidas",
            0
        ),
        "times_em_cache": len(
            cache_persistente.get(
                "historicos",
                {}
            )
        )
    })


@app.route("/stats")
def rota_stats():
    total = stats["wins"] + stats["losses"]

    resposta = dict(stats)

    resposta["assertividade"] = round(
        stats["wins"] / total * 100,
        2
    ) if total else 0

    return jsonify(resposta)


@app.route("/bancas")
def rota_bancas():
    return jsonify(stats["bancas"])


@app.route("/sinais")
def rota_sinais():
    return jsonify(sinais)


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "timestamp": timestamp_agora()
    })


@app.route("/cache")
def rota_cache():
    return jsonify({
        "times_historico": len(
            cache_persistente.get(
                "historicos",
                {}
            )
        ),
        "odds_memoria": len(cache_odds),
        "cursor": cache_persistente.get(
            "cursor_partidas",
            0
        )
    })


# ============================================================
# THREAD
# ============================================================

thread_robo = None
thread_start_lock = threading.Lock()


def iniciar_thread():
    global thread_robo

    with thread_start_lock:
        if thread_robo is None or not thread_robo.is_alive():
            thread_robo = threading.Thread(
                target=loop_robo,
                daemon=True,
                name="robo-thread"
            )

            thread_robo.start()

            logger.info(
                "Thread principal iniciada."
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
