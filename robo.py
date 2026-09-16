import os
import json
import time
import math
import html
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-over-v9")

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))

# Estratégia
MARGEM_CANTOS = float(os.getenv("MARGEM_CANTOS", "0.5"))
MARGEM_GOLS = float(os.getenv("MARGEM_GOLS", "0.5"))

MIN_TAXA_CANTOS = float(os.getenv("MIN_TAXA_CANTOS", "0.65"))
MIN_TAXA_GOLS = float(os.getenv("MIN_TAXA_GOLS", "0.70"))
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
JANELA_HORAS = int(os.getenv("JANELA_HORAS", "24"))

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv("MAX_JOGOS_ANALISADOS_CICLO", "4")
)

# Gestão
TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))

ENTRADA_INICIAL = float(
    os.getenv("ENTRADA_INICIAL", "10")
)

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(os.getenv("MAX_GALES", "2"))

# API / rate limit
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

BOOKMAKER = os.getenv("BOOKMAKER", "bet365").lower()

DEBUG_ODDS = (
    os.getenv("DEBUG_ODDS", "true").lower() == "true"
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
    "User-Agent": "robo-over-v9/9.0"
})


# ============================================================
# LOCKS
# ============================================================

lock_dados = threading.RLock()
lock_api = threading.Lock()
stop_event = threading.Event()


# ============================================================
# RATE LIMIT
# ============================================================

historico_requisicoes = deque()
ultima_requisicao = 0.0


def aguardar_rate_limit():

    global ultima_requisicao

    with lock_api:

        agora = time.time()

        while historico_requisicoes and (
            agora - historico_requisicoes[0] >= 60
        ):
            historico_requisicoes.popleft()

        if len(historico_requisicoes) >= API_MAX_REQUESTS_PER_MINUTE:

            espera = (
                60
                - (agora - historico_requisicoes[0])
                + 1
            )

            logger.info(
                "Rate limit preventivo: aguardando %.1fs",
                espera
            )

            time.sleep(max(espera, 1))

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

            r = session.get(
                url,
                params=params,
                timeout=30
            )

            if r.status_code == 429:

                logger.warning(
                    "429 recebido. Backoff de %ss.",
                    API_BACKOFF_429
                )

                time.sleep(API_BACKOFF_429)
                continue

            r.raise_for_status()

            return r.json()

        except requests.RequestException as e:

            logger.error(
                "Erro API %s tentativa %s/%s: %s",
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
        ) as f:

            return json.load(f)

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
# BANCAS
# ============================================================

def criar_bancas():

    bancas = []

    for numero_banca in range(1, TOTAL_BANCAS + 1):

        bancas.append({
            "id": numero_banca,
            "ocupada": False,
            "fixture_id": None,
            "gale": 0,
            "entrada": ENTRADA_INICIAL
        })

    return bancas


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
# UTILITÁRIOS
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (TypeError, ValueError):
        return None


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

    return []


def taxa(wins, losses):

    total = wins + losses

    if total == 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


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
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        r = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if not r.ok:

            logger.error(
                "Telegram: %s",
                r.text[:500]
            )

        return r.ok

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

        return cache_fixtures["dados"]

    inicio = datetime.now(timezone.utc)

    fim = inicio + timedelta(
        hours=JANELA_HORAS
    )

    params = {
        "start_time": int(inicio.timestamp()),
        "end_time": int(fim.timestamp()),
        "status": "scheduled",
        "per_page": 100
    }

    dados = api_get(
        "/fixtures",
        params=params
    )

    jogos = extrair_lista(dados)

    cache_fixtures["timestamp"] = agora
    cache_fixtures["dados"] = jogos

    logger.info(
        "%s jogos encontrados nas próximas %sh.",
        len(jogos),
        JANELA_HORAS
    )

    return jogos


# ============================================================
# TIMES
# ============================================================

def extrair_times(fixture):

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

    home_id = (
        fixture.get("home_team_id")
        or (
            home.get("id")
            if isinstance(home, dict)
            else None
        )
    )

    away_id = (
        fixture.get("away_team_id")
        or (
            away.get("id")
            if isinstance(away, dict)
            else None
        )
    )

    home_nome = (
        home.get("name", "Casa")
        if isinstance(home, dict)
        else str(home)
    )

    away_nome = (
        away.get("name", "Fora")
        if isinstance(away, dict)
        else str(away)
    )

    return (
        home_id,
        away_id,
        home_nome,
        away_nome
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

        idade = agora - item.get(
            "timestamp",
            0
        )

        if idade < CACHE_HISTORICO_TTL:

            return item.get(
                "dados",
                []
            )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "per_page": QTD_HISTORICO_TIME
        }
    )

    jogos = extrair_lista(dados)

    jogos = jogos[:QTD_HISTORICO_TIME]

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
# EXTRAÇÃO DE GOLS / CANTOS
# ============================================================

def extrair_totais(jogo):

    gols = None
    cantos = None

    # ------------------------
    # GOLS
    # ------------------------

    for chave in (
        "total_goals",
        "goals_total"
    ):

        valor = numero(
            jogo.get(chave)
        )

        if valor is not None:
            gols = valor
            break

    if gols is None:

        hg = numero(
            jogo.get("home_score")
        )

        ag = numero(
            jogo.get("away_score")
        )

        if hg is not None and ag is not None:
            gols = hg + ag

    if gols is None:

        score = jogo.get("score")

        if isinstance(score, dict):

            hg = numero(
                score.get("home")
            )

            ag = numero(
                score.get("away")
            )

            if hg is not None and ag is not None:
                gols = hg + ag

    # ------------------------
    # CANTOS
    # ------------------------

    for chave in (
        "total_corners",
        "corners_total"
    ):

        valor = numero(
            jogo.get(chave)
        )

        if valor is not None:
            cantos = valor
            break

    if cantos is None:

        hc = numero(
            jogo.get("home_corners")
        )

        ac = numero(
            jogo.get("away_corners")
        )

        if hc is not None and ac is not None:
            cantos = hc + ac

    stats_jogo = jogo.get(
        "statistics"
    )

    if (
        cantos is None
        and isinstance(stats_jogo, dict)
    ):

        hc = numero(
            stats_jogo.get("home_corners")
        )

        ac = numero(
            stats_jogo.get("away_corners")
        )

        if hc is not None and ac is not None:
            cantos = hc + ac

    return gols, cantos


def combinar_historicos(
    home_history,
    away_history
):

    unicos = {}

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
            continue

        unicos[str(fid)] = {
            "id": str(fid),
            "gols": gols,
            "cantos": cantos
        }

    return list(
        unicos.values()
    )


def medias_historicas(partidas):

    if not partidas:
        return None, None

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
# NOVA V9:
# OVER CANTOS + OVER GOLS
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

        # OVER CANTOS
        ok_cantos = (
            partida["cantos"]
            > linha_cantos
        )

        # OVER GOLS - V9
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
        "total": total,

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

    if item:

        if (
            agora - item["timestamp"]
            < CACHE_ODDS_TTL
        ):

            return item["dados"]

    dados = api_get(
        f"/fixtures/{fixture_id}/odds"
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
                bookmaker.get("name", "")
            ).lower()

            if BOOKMAKER in nome:
                return bookmaker

        if bookmakers:
            return bookmakers[0]

    # Algumas respostas já vêm
    # diretamente com "odds"
    if isinstance(
        dados.get("odds"),
        dict
    ):
        return dados

    # data wrapper
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

    if not isinstance(
        mercado,
        dict
    ):
        return None

    # Preferimos closing.
    # Nunca usamos inplay.
    for estagio in (
        "closing",
        "opening"
    ):

        dados = mercado.get(
            estagio
        )

        # Formato completo:
        # closing: {
        #   line: 2.5,
        #   over: 1.90,
        #   under: 1.90
        # }
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

        # Formato simples:
        # closing: 2.5
        linha = numero(dados)

        if linha is not None:

            return {
                "estagio": estagio,
                "line": linha,
                "odd": None
            }

    # Formato direto
    linha = numero(
        mercado.get("line")
    )

    if linha is not None:

        return {
            "estagio": "direto",
            "line": linha,
            "odd": numero(
                mercado.get(lado)
            )
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

    # V9:
    # os dois mercados são OVER
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
            "ODDS RAW | corner=%s | goal=%s",
            mercado_cantos,
            mercado_gols
        )

    if not cantos or not gols:
        return None

    return {
        "cantos": cantos,
        "gols": gols
    }


# ============================================================
# SELEÇÃO DO MERCADO
# ============================================================

def selecionar_mercado(
    fixture,
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

    linha_cantos = linhas[
        "cantos"
    ]["line"]

    linha_gols = linhas[
        "gols"
    ]["line"]

    odd_cantos = linhas[
        "cantos"
    ].get("odd")

    odd_gols = linhas[
        "gols"
    ].get("odd")

    # ========================================================
    # MARGEM OBRIGATÓRIA DE CANTOS
    # ========================================================

    folga_cantos = (
        media_cantos
        - linha_cantos
    )

    if folga_cantos < MARGEM_CANTOS:

        logger.info(
            "⛔ Over %.2f cantos | "
            "média %.2f | "
            "folga %.2f | mínimo %.2f",
            linha_cantos,
            media_cantos,
            folga_cantos,
            MARGEM_CANTOS
        )

        return None

    logger.info(
        "✅ CANTOS | Over %.2f | "
        "média %.2f | folga %.2f",
        linha_cantos,
        media_cantos,
        folga_cantos
    )

    # ========================================================
    # NOVA REGRA V9 - OVER GOLS
    # ========================================================

    folga_gols = (
        media_gols
        - linha_gols
    )

    if folga_gols < MARGEM_GOLS:

        logger.info(
            "⛔ Over %.2f gols | "
            "média %.2f | "
            "folga %.2f | mínimo %.2f",
            linha_gols,
            media_gols,
            folga_gols,
            MARGEM_GOLS
        )

        return None

    logger.info(
        "✅ GOLS | Over %.2f | "
        "média %.2f | folga %.2f",
        linha_gols,
        media_gols,
        folga_gols
    )

    # ========================================================
    # HISTÓRICO
    # ========================================================

    historico = avaliar_linhas(
        partidas,
        linha_cantos,
        linha_gols
    )

    logger.info(
        "Histórico %s jogos | "
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
            "⛔ Frequência de cantos abaixo de %.0f%%",
            MIN_TAXA_CANTOS * 100
        )

        return None

    if (
        historico["taxa_gols"]
        < MIN_TAXA_GOLS
    ):

        logger.info(
            "⛔ Frequência de gols abaixo de %.0f%%",
            MIN_TAXA_GOLS * 100
        )

        return None

    if (
        historico["taxa_combinada"]
        < MIN_TAXA_COMBINADA
    ):

        logger.info(
            "⛔ Frequência combinada abaixo de %.0f%%",
            MIN_TAXA_COMBINADA * 100
        )

        return None

    # Odd somente informativa
    odd_combinada = None

    if (
        odd_cantos is not None
        and odd_gols is not None
    ):

        odd_combinada = (
            odd_cantos
            * odd_gols
        )

    # Score não utiliza odd
    score = (
        historico["taxa_combinada"] * 100
        + historico["taxa_cantos"] * 10
        + historico["taxa_gols"] * 10
        + folga_cantos
        + folga_gols
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

        "historico": historico,
        "score": score,

        "mercado_cantos": "OVER",
        "mercado_gols": "OVER"
    }


# ============================================================
# SINAIS
# ============================================================

def fixture_ja_utilizado(
    fixture_id
):

    fid = str(fixture_id)

    return any(
        str(s.get("fixture_id")) == fid
        for s in sinais
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

        "mercado": (
            "Over cantos + Over gols"
        ),

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
            selecao[
                "historico"
            ]["total"],

        "odd_cantos":
            selecao.get(
                "odd_cantos"
            ),

        "odd_gols":
            selecao.get(
                "odd_gols"
            ),

        "odd_combinada":
            odd_combinada,

        "banca": banco["id"],
        "gale": banco["gale"],
        "entrada": entrada,

        "status": "PENDENTE",
        "resultado": None,

        "criado_em": agora_iso()
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

    odd_txt = (
        f"{odd_combinada:.2f}"
        if odd_combinada is not None
        else "N/D"
    )

    mensagem = (
        "🚨 <b>SINAL V9</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x "
        f"{html.escape(away)}</b>\n\n"

        "🚩 <b>ESCANTEIOS</b>\n"
        f"Over {selecao['linha_cantos']:.2f}\n"
        f"Média: {selecao['media_cantos']:.2f}\n"
        f"Folga: {selecao['folga_cantos']:.2f}\n"
        f"Histórico: "
        f"{selecao['historico']['taxa_cantos'] * 100:.1f}%\n\n"

        "⚽ <b>GOLS</b>\n"
        f"Over {selecao['linha_gols']:.2f}\n"
        f"Média: {selecao['media_gols']:.2f}\n"
        f"Folga: {selecao['folga_gols']:.2f}\n"
        f"Histórico: "
        f"{selecao['historico']['taxa_gols'] * 100:.1f}%\n\n"

        "🔥 <b>COMBINADA</b>\n"
        f"Histórico conjunto: "
        f"{selecao['historico']['taxa_combinada'] * 100:.1f}%\n"
        f"Amostra: "
        f"{selecao['historico']['total']} jogos\n\n"

        f"💰 Odd informativa: {odd_txt}\n\n"

        f"🏦 Banca: {banco['id']}\n"
        f"🔄 Gale: {banco['gale']}/{MAX_GALES}\n"
        f"💵 Entrada: R$ {entrada:.2f}"
    )

    telegram(mensagem)

    logger.info(
        "🚨 SINAL ENVIADO | %s x %s | "
        "Over %.2f cantos + Over %.2f gols",
        home,
        away,
        selecao["linha_cantos"],
        selecao["linha_gols"]
    )


# ============================================================
# RESULTADOS
# ============================================================

def buscar_fixture(
    fixture_id
):

    dados = api_get(
        f"/fixtures/{fixture_id}"
    )

    if isinstance(dados, dict):

        data = dados.get("data")

        if isinstance(data, dict):
            return data

        response = dados.get(
            "response"
        )

        if isinstance(response, list):

            if response:
                return response[0]

        return dados

    return None


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

    # PUSH não avança Gale
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

    if gols is None or cantos is None:

        logger.warning(
            "Resultado sem gols/cantos suficientes: %s",
            sinal["fixture_id"]
        )

        return

    linha_cantos = float(
        sinal["linha_cantos"]
    )

    linha_gols = float(
        sinal["linha_gols"]
    )

    # V9: OS DOIS SÃO OVER
    ganhou_cantos = (
        cantos > linha_cantos
    )

    ganhou_gols = (
        gols > linha_gols
    )

    # Para linhas inteiras, igualdade é tratada
    # separadamente em vez de marcar LOSS.
    push_cantos = (
        cantos == linha_cantos
    )

    push_gols = (
        gols == linha_gols
    )

    if ganhou_cantos and ganhou_gols:

        resultado = "WIN"

    elif (
        push_cantos
        or push_gols
    ):

        resultado = "PUSH"

    else:

        resultado = "LOSS"

    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado

    sinal["gols_final"] = gols
    sinal["cantos_final"] = cantos

    sinal["resolvido_em"] = agora_iso()

    entrada = float(
        sinal["entrada"]
    )

    odd = numero(
        sinal.get(
            "odd_combinada"
        )
    )

    lucro = 0.0

    if resultado == "WIN":

        stats["wins"] += 1

        if odd is not None:
            lucro = entrada * (
                odd - 1
            )

    elif resultado == "LOSS":

        stats["losses"] += 1
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
            b for b in bancas
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

        f"⚽ {html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}\n\n"

        f"🚩 Over {linha_cantos:.2f} cantos\n"
        f"Resultado: {cantos:.0f}\n\n"

        f"⚽ Over {linha_gols:.2f} gols\n"
        f"Resultado: {gols:.0f}\n\n"

        f"🏦 Banca {sinal['banca']}\n"
        f"🔄 Gale {sinal['gale']}"
    )

    telegram(mensagem)

    logger.info(
        "%s | %s x %s | "
        "cantos %.0f | gols %.0f",
        resultado,
        sinal["home"],
        sinal["away"],
        cantos,
        gols
    )


def verificar_resultados():

    pendentes = [
        s for s in sinais
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
# ANÁLISE DE JOGO
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
    ) = extrair_times(fixture)

    if not home_id or not away_id:

        logger.info(
            "⛔ %s x %s sem IDs dos times.",
            home,
            away
        )

        return

    logger.info(
        "🔎 Analisando %s x %s",
        home,
        away
    )

    home_history = buscar_historico_time(
        home_id
    )

    away_history = buscar_historico_time(
        away_id
    )

    partidas = combinar_historicos(
        home_history,
        away_history
    )

    if len(partidas) < MIN_JOGOS_HISTORICO:

        logger.info(
            "⛔ Histórico insuficiente: %s jogos.",
            len(partidas)
        )

        return

    media_cantos, media_gols = (
        medias_historicas(
            partidas
        )
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

        # tenta odds embutidas no fixture
        odds_embutidas = fixture.get(
            "odds"
        )

        if odds_embutidas:

            odds_data = {
                "odds":
                    odds_embutidas
            }

    if not odds_data:

        logger.info(
            "⛔ Sem odds/linhas para %s x %s",
            home,
            away
        )

        return

    selecao = selecionar_mercado(
        fixture,
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
            "⏳ Sinal aprovado, mas "
            "as 3 bancas estão ocupadas."
        )

        return

    enviar_sinal(
        fixture,
        selecao,
        banco
    )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def ciclo():

    global cursor_fixture

    logger.info(
        "======================================"
    )

    logger.info(
        "🔄 Iniciando ciclo V9"
    )

    try:

        verificar_resultados()

        # Não precisamos analisar novos jogos
        # se as 3 bancas estiverem ocupadas.
        if banco_livre() is None:

            logger.info(
                "🏦 Todas as bancas ocupadas."
            )

            return

        fixtures = buscar_fixtures()

        if not fixtures:

            logger.info(
                "Nenhum fixture disponível."
            )

            return

        total = len(fixtures)

        qtd = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )

        for _ in range(qtd):

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
                    "Erro analisando fixture."
                )

    except Exception:

        logger.exception(
            "Erro geral no ciclo."
        )


def loop_robo():

    logger.info(
        "🤖 Robô V9 iniciado."
    )

    logger.info(
        "Estratégia: OVER CANTOS + OVER GOLS"
    )

    logger.info(
        "Margens: cantos %.2f | gols %.2f",
        MARGEM_CANTOS,
        MARGEM_GOLS
    )

    while not stop_event.is_set():

        ciclo()

        stop_event.wait(
            INTERVALO_ANALISE_SEGUNDOS
        )


# ============================================================
# ROTAS FLASK
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
        "status": "online",
        "versao": "V9",
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

        "odd_como_filtro": False,

        "wins": wins,
        "losses": losses,

        "assertividade":
            taxa(wins, losses),

        "bancas": bancas
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,
        "versao": "V9",
        "mercado_cantos": "OVER",
        "mercado_gols": "OVER",
        "margem_cantos": MARGEM_CANTOS,
        "margem_gols": MARGEM_GOLS,
        "odd_como_filtro": False
    })


@app.route("/stats")
def rota_stats():

    wins = stats.get(
        "wins",
        0
    )

    losses = stats.get(
        "losses",
        0
    )

    resposta = dict(
        stats
    )

    resposta["assertividade"] = (
        taxa(
            wins,
            losses
        )
    )

    return jsonify(
        resposta
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
        "status": "ok",
        "version": "v9"
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
