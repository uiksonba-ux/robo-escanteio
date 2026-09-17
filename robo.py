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
# ROBÔ V15
#
# ESTRATÉGIA:
#   UNDER 6.0 ESCANTEIOS HT
#   +
#   OVER 7.0 ESCANTEIOS FT
#
# FILTROS:
#   Bet365 precisa possuir mercado de cantos HT
#   Bet365 precisa possuir mercado de cantos FT
#
#   Under 6 HT >= 70%
#   Over 7 FT >= 70%
#   Combinação >= 70%
#
# HISTÓRICO:
#   últimos 20 jogos do mandante
#   +
#   últimos 20 jogos do visitante
#   removendo partidas duplicadas
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

logger = logging.getLogger("robo-v15")


# ============================================================
# API / TELEGRAM
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
# MERCADOS
# ============================================================

BOOKMAKER = "bet365"

LINHA_UNDER_CANTOS_HT = float(
    os.getenv("LINHA_UNDER_CANTOS_HT", "6.0")
)

LINHA_OVER_CANTOS_FT = float(
    os.getenv("LINHA_OVER_CANTOS_FT", "7.0")
)


# ============================================================
# TAXAS
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv("MIN_TAXA_UNDER_HT", "0.70")
)

MIN_TAXA_OVER_FT = float(
    os.getenv("MIN_TAXA_OVER_FT", "0.70")
)

MIN_TAXA_COMBINADA = float(
    os.getenv("MIN_TAXA_COMBINADA", "0.70")
)


# ============================================================
# HISTÓRICO
# ============================================================

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "20")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "20")
)


# ============================================================
# FIXTURES
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
    os.getenv("CACHE_MERCADO_TTL", "1800")
)


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS_V15",
    "stats_v15.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS_V15",
    "sinais_v15.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE_V15",
    "cache_v15.json"
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

if API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-cantos-v15/1.0"
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
                "⏳ Rate limit interno. Aguardando %.1fs.",
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


# ============================================================
# API GET
# ============================================================

def api_get(endpoint, params=None, tentativas=3):

    if not API_KEY:

        logger.error(
            "❌ FIVE_DOLLAR_API_KEY/FIVE_DOLLAR_KEY não configurada."
        )

        return None

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
                    "⚠️ API retornou 429. Aguardando %ss.",
                    API_BACKOFF_429
                )

                time.sleep(API_BACKOFF_429)

                continue

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

        except ValueError as erro:

            logger.error(
                "❌ JSON inválido em %s: %s",
                endpoint,
                erro
            )

            return None

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

    except Exception as erro:

        logger.error(
            "❌ Erro lendo %s: %s",
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


def calcular_taxa(wins, losses):

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
                "results",
                "response"
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

        banca_id = sinal.get("banca")

        for banca in bancas:

            if banca["id"] == banca_id:

                banca["ocupada"] = True

                banca["fixture_id"] = str(
                    sinal["fixture_id"]
                )

                banca["gale"] = int(
                    sinal.get("gale", 0)
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

        if not banca["ocupada"]:
            return banca

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
        agora - cache_fixtures["timestamp"]
        < CACHE_FIXTURES_TTL
    ):

        logger.info(
            "📦 Fixtures em cache: %s",
            len(cache_fixtures["dados"])
        )

        return cache_fixtures["dados"]

    inicio = datetime.now(
        timezone.utc
    )

    fim = inicio + timedelta(
        hours=JANELA_HORAS
    )

    dados = api_get(
        "/fixtures",
        params={
            "start_time": int(
                inicio.timestamp()
            ),
            "end_time": int(
                fim.timestamp()
            ),
            "status": "scheduled",
            "per_page": 100
        }
    )

    jogos = extrair_lista(dados)

    logger.info(
        "📅 %s jogos nas próximas %sh.",
        len(jogos),
        JANELA_HORAS
    )

    cache_fixtures["timestamp"] = agora
    cache_fixtures["dados"] = jogos

    return jogos


# ============================================================
# EXTRAIR TIMES
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
# BET365
# ============================================================

def encontrar_bet365(objeto):
    """
    Procura recursivamente um objeto da Bet365.

    Isso deixa a leitura mais tolerante a pequenas diferenças
    no envelope JSON retornado pela API.
    """

    if isinstance(objeto, dict):

        identificadores = [
            objeto.get("slug"),
            objeto.get("name"),
            objeto.get("bookmaker"),
            objeto.get("bookmaker_name")
        ]

        for identificador in identificadores:

            if identificador is None:
                continue

            normalizado = (
                str(identificador)
                .lower()
                .replace(" ", "")
                .replace("-", "")
            )

            if normalizado == "bet365":
                return objeto

        for valor in objeto.values():

            encontrado = encontrar_bet365(
                valor
            )

            if encontrado is not None:
                return encontrado

    elif isinstance(objeto, list):

        for item in objeto:

            encontrado = encontrar_bet365(
                item
            )

            if encontrado is not None:
                return encontrado

    return None


def mercado_tem_dados(mercado):
    """
    Confirma que o mercado possui algum conteúdo real.

    Não verifica valor da odd.
    """

    if mercado is None:
        return False

    if isinstance(mercado, list):

        return any(
            mercado_tem_dados(item)
            for item in mercado
        )

    if not isinstance(mercado, dict):
        return bool(mercado)

    # Formato com opening / closing / inplay

    for fase in (
        "opening",
        "closing",
        "inplay"
    ):

        valor = mercado.get(fase)

        if valor:

            if isinstance(valor, dict):

                if (
                    valor.get("line") is not None
                    or valor.get("over") is not None
                    or valor.get("under") is not None
                ):
                    return True

                if len(valor) > 0:
                    return True

            elif isinstance(valor, list):

                if len(valor) > 0:
                    return True

            else:
                return True

    # Formato direto

    if (
        mercado.get("line") is not None
        or mercado.get("over") is not None
        or mercado.get("under") is not None
    ):
        return True

    # Linhas armazenadas em array

    linhas = mercado.get("lines")

    if isinstance(linhas, list) and linhas:
        return True

    return False


def localizar_mercado(objeto, nomes):
    """
    Procura recursivamente pelas chaves do mercado.
    """

    if isinstance(objeto, dict):

        for nome in nomes:

            if nome in objeto:

                mercado = objeto.get(nome)

                if mercado_tem_dados(mercado):
                    return mercado

        for valor in objeto.values():

            encontrado = localizar_mercado(
                valor,
                nomes
            )

            if encontrado is not None:
                return encontrado

    elif isinstance(objeto, list):

        for item in objeto:

            encontrado = localizar_mercado(
                item,
                nomes
            )

            if encontrado is not None:
                return encontrado

    return None


def verificar_mercados_bet365(fixture_id):

    chave = str(fixture_id)

    agora = time.time()

    item_cache = cache_persistente[
        "mercados"
    ].get(chave)

    if item_cache:

        idade = (
            agora
            - item_cache.get(
                "timestamp",
                0
            )
        )

        if idade < CACHE_MERCADO_TTL:

            resultado = item_cache.get(
                "dados",
                {}
            )

            logger.info(
                "📦 Bet365 cache | fixture %s | HT=%s | FT=%s",
                fixture_id,
                resultado.get("ht"),
                resultado.get("ft")
            )

            return resultado

    logger.info(
        "🎰 Verificando Bet365 | fixture %s...",
        fixture_id
    )

    # Uma consulta, sem filtro de valor de odd.
    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "bookmakers": BOOKMAKER
        }
    )

    bet365 = encontrar_bet365(dados)

    if not bet365:

        resultado = {
            "ht": False,
            "ft": False
        }

        logger.info(
            "⛔ Fixture %s sem Bet365.",
            fixture_id
        )

    else:

        # Mercado FT
        mercado_ft = localizar_mercado(
            bet365,
            (
                "corner_line",
                "corner",
                "corners"
            )
        )

        # Mercado HT
        mercado_ht = localizar_mercado(
            bet365,
            (
                "corner_line_half",
                "corner_half",
                "corners_half",
                "first_half_corner_line"
            )
        )

        resultado = {
            "ht": mercado_ht is not None,
            "ft": mercado_ft is not None
        }

        logger.info(
            "🎰 Bet365 | fixture %s | "
            "Cantos HT=%s | Cantos FT=%s",
            fixture_id,
            "SIM" if resultado["ht"] else "NÃO",
            "SIM" if resultado["ft"] else "NÃO"
        )

    cache_persistente[
        "mercados"
    ][chave] = {
        "timestamp": agora,
        "dados": resultado
    }

    salvar_json(
        ARQUIVO_CACHE,
        cache_persistente
    )

    return resultado


# ============================================================
# HISTÓRICO DO TIME
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
                "📦 Time %s: %s jogos em cache.",
                team_id,
                len(jogos)
            )

            return jogos

    logger.info(
        "📊 Buscando últimos %s jogos do time %s...",
        QTD_HISTORICO_TIME,
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
        "📊 Time %s: %s jogos encontrados.",
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
# ID DO JOGO
# ============================================================

def obter_id_jogo(jogo):

    if not isinstance(jogo, dict):
        return None

    fixture_id = (
        jogo.get("id")
        or jogo.get("fixture_id")
    )

    if fixture_id is not None:
        return str(fixture_id)

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        fixture_id = fixture.get("id")

        if fixture_id is not None:
            return str(fixture_id)

    return None


# ============================================================
# COMBINAR 20 + 20
# ============================================================

def combinar_historicos(
    historico_home,
    historico_away
):

    combinados = []

    ids_vistos = set()

    for jogo in (
        list(historico_home)
        + list(historico_away)
    ):

        fixture_id = obter_id_jogo(
            jogo
        )

        if fixture_id is not None:

            if fixture_id in ids_vistos:
                continue

            ids_vistos.add(
                fixture_id
            )

        combinados.append(
            jogo
        )

    return combinados


# ============================================================
# EXTRAIR CANTOS
#
# corners.home + corners.away = TOTAL FT
#
# corners.half_home + corners.half_away = TOTAL HT
#
# NÃO SOMAMOS HT NOVAMENTE AO FT.
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return None

    corners = jogo.get("corners")

    if not isinstance(corners, dict):
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
        "ht": ht_home + ht_away,
        "ft": ft_home + ft_away
    }


# ============================================================
# UNDER 6 HT
#
# 0-5 = WIN
# 6   = VOID
# 7+  = LOSS
# ============================================================

def resultado_under_ht(cantos):

    if cantos < LINHA_UNDER_CANTOS_HT:
        return "WIN"

    if cantos == LINHA_UNDER_CANTOS_HT:
        return "VOID"

    return "LOSS"


# ============================================================
# OVER 7 FT
#
# 8+  = WIN
# 7   = VOID
# 0-6 = LOSS
# ============================================================

def resultado_over_ft(cantos):

    if cantos > LINHA_OVER_CANTOS_FT:
        return "WIN"

    if cantos == LINHA_OVER_CANTOS_FT:
        return "VOID"

    return "LOSS"


# ============================================================
# ANALISAR HISTÓRICO
# ============================================================

def analisar_historico(jogos):

    registros = []

    ignorados = 0

    for jogo in jogos:

        dados = extrair_cantos(jogo)

        if not dados:

            ignorados += 1
            continue

        under = resultado_under_ht(
            dados["ht"]
        )

        over = resultado_over_ft(
            dados["ft"]
        )

        registros.append({
            "under": under,
            "over": over,
            "cantos_ht": dados["ht"],
            "cantos_ft": dados["ft"]
        })

    total = len(registros)

    if total == 0:
        return None


    # ========================================================
    # UNDER HT
    # ========================================================

    under_wins = sum(
        1
        for r in registros
        if r["under"] == "WIN"
    )

    under_voids = sum(
        1
        for r in registros
        if r["under"] == "VOID"
    )

    under_losses = sum(
        1
        for r in registros
        if r["under"] == "LOSS"
    )

    under_decididos = (
        under_wins
        + under_losses
    )

    taxa_under = (
        under_wins / under_decididos
        if under_decididos
        else 0
    )


    # ========================================================
    # OVER FT
    # ========================================================

    over_wins = sum(
        1
        for r in registros
        if r["over"] == "WIN"
    )

    over_voids = sum(
        1
        for r in registros
        if r["over"] == "VOID"
    )

    over_losses = sum(
        1
        for r in registros
        if r["over"] == "LOSS"
    )

    over_decididos = (
        over_wins
        + over_losses
    )

    taxa_over = (
        over_wins / over_decididos
        if over_decididos
        else 0
    )


    # ========================================================
    # COMBINAÇÃO
    #
    # WIN:
    #   Under WIN + Over WIN
    #
    # VOID:
    #   nenhuma perna perde,
    #   mas pelo menos uma dá VOID
    #
    # LOSS:
    #   qualquer perna LOSS
    # ========================================================

    combinada_wins = 0
    combinada_voids = 0
    combinada_losses = 0

    for r in registros:

        under = r["under"]
        over = r["over"]

        if (
            under == "LOSS"
            or over == "LOSS"
        ):

            combinada_losses += 1

        elif (
            under == "VOID"
            or over == "VOID"
        ):

            combinada_voids += 1

        else:

            combinada_wins += 1


    combinada_decididos = (
        combinada_wins
        + combinada_losses
    )

    taxa_combinada = (
        combinada_wins
        / combinada_decididos
        if combinada_decididos
        else 0
    )


    # ========================================================
    # MÉDIAS
    # ========================================================

    media_ht = (
        sum(
            r["cantos_ht"]
            for r in registros
        )
        / total
    )

    media_ft = (
        sum(
            r["cantos_ft"]
            for r in registros
        )
        / total
    )


    return {
        "total": total,
        "ignorados": ignorados,

        "under_wins": under_wins,
        "under_voids": under_voids,
        "under_losses": under_losses,
        "taxa_under": taxa_under,

        "over_wins": over_wins,
        "over_voids": over_voids,
        "over_losses": over_losses,
        "taxa_over": taxa_over,

        "combinada_wins": combinada_wins,
        "combinada_voids": combinada_voids,
        "combinada_losses": combinada_losses,
        "taxa_combinada": taxa_combinada,

        "media_ht": media_ht,
        "media_ft": media_ft
    }


# ============================================================
# FILTROS 70%
# ============================================================

def passou_filtros(analise):

    if not analise:
        return False

    if (
        analise["total"]
        < MIN_JOGOS_HISTORICO
    ):
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
# JOGO JÁ UTILIZADO
# ============================================================

def fixture_ja_utilizado(fixture_id):

    fixture_id = str(fixture_id)

    return any(
        str(
            sinal.get("fixture_id")
        ) == fixture_id

        for sinal in sinais
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(
    fixture,
    analise,
    banca
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
        *
        (
            MULTIPLICADOR_GALE
            ** banca["gale"]
        )
    )


    sinal = {
        "fixture_id": fixture_id,

        "home_id": home_id,
        "away_id": away_id,

        "home": home,
        "away": away,

        "bookmaker": "bet365",

        "mercado":
            "Under 6 cantos HT + Over 7 cantos FT",

        "linha_under_ht":
            LINHA_UNDER_CANTOS_HT,

        "linha_over_ft":
            LINHA_OVER_CANTOS_FT,

        "taxa_under":
            round(
                analise["taxa_under"] * 100,
                2
            ),

        "taxa_over":
            round(
                analise["taxa_over"] * 100,
                2
            ),

        "taxa_combinada":
            round(
                analise["taxa_combinada"] * 100,
                2
            ),

        "amostra":
            analise["total"],

        "media_ht":
            round(
                analise["media_ht"],
                2
            ),

        "media_ft":
            round(
                analise["media_ft"],
                2
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

        sinais.append(sinal)

        banca["ocupada"] = True
        banca["fixture_id"] = fixture_id
        banca["entrada"] = entrada

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )


    mensagem = (

        "🚨 <b>SINAL V15</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x "
        f"{html.escape(away)}</b>\n\n"


        "1️⃣ <b>ESCANTEIOS 1º TEMPO</b>\n"

        f"📉 Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f}\n"

        f"📊 Histórico: "
        f"<b>{analise['taxa_under'] * 100:.1f}%</b>\n"

        f"📈 Média HT: "
        f"{analise['media_ht']:.2f}\n"

        f"✅ {analise['under_wins']} WIN | "
        f"⚪ {analise['under_voids']} VOID | "
        f"❌ {analise['under_losses']} LOSS\n\n"


        "2️⃣ <b>ESCANTEIOS JOGO INTEIRO</b>\n"

        f"📈 Over "
        f"{LINHA_OVER_CANTOS_FT:.1f}\n"

        f"📊 Histórico: "
        f"<b>{analise['taxa_over'] * 100:.1f}%</b>\n"

        f"📈 Média FT: "
        f"{analise['media_ft']:.2f}\n"

        f"✅ {analise['over_wins']} WIN | "
        f"⚪ {analise['over_voids']} VOID | "
        f"❌ {analise['over_losses']} LOSS\n\n"


        "🔥 <b>COMBINAÇÃO</b>\n"

        f"Under {LINHA_UNDER_CANTOS_HT:.1f} HT "
        f"+ Over {LINHA_OVER_CANTOS_FT:.1f} FT\n"

        f"📊 Ocorrência conjunta: "
        f"<b>{analise['taxa_combinada'] * 100:.1f}%</b>\n"

        f"✅ {analise['combinada_wins']} WIN | "
        f"⚪ {analise['combinada_voids']} VOID | "
        f"❌ {analise['combinada_losses']} LOSS\n\n"


        "🎰 <b>BET365</b>\n"

        "✅ Mercado de escanteios HT disponível\n"
        "✅ Mercado de escanteios FT disponível\n\n"


        "📚 <b>HISTÓRICO</b>\n"

        f"Jogos válidos: "
        f"{analise['total']}\n"

        f"Sem dados ignorados: "
        f"{analise['ignorados']}\n"

        "20 jogos de cada time "
        "(duplicados removidos)\n\n"


        "💰 <b>GESTÃO</b>\n"

        f"🏦 Banca: "
        f"{banca['id']}\n"

        f"🔄 Gale: "
        f"{banca['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )


    telegram(mensagem)


    logger.info(
        "🚨 SINAL V15 | %s x %s | "
        "Under %.1f%% | "
        "Over %.1f%% | "
        "Combinada %.1f%% | "
        "%s jogos",
        home,
        away,
        analise["taxa_under"] * 100,
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100,
        analise["total"]
    )


# ============================================================
# BUSCAR FIXTURE
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

    response = dados.get("response")

    if (
        isinstance(response, list)
        and response
    ):
        return response[0]

    return dados


# ============================================================
# FIXTURE FINALIZADO
# ============================================================

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
    banca,
    resultado
):

    if resultado == "WIN":

        banca["gale"] = 0

    elif resultado == "LOSS":

        if banca["gale"] < MAX_GALES:

            banca["gale"] += 1

        else:

            banca["gale"] = 0

    # PUSH mantém o Gale atual

    banca["ocupada"] = False
    banca["fixture_id"] = None

    banca["entrada"] = (
        ENTRADA_INICIAL
        *
        (
            MULTIPLICADOR_GALE
            ** banca["gale"]
        )
    )


# ============================================================
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal,
    fixture
):

    dados = extrair_cantos(fixture)

    if not dados:

        logger.warning(
            "⚠️ Fixture %s sem dados de cantos.",
            sinal["fixture_id"]
        )

        return


    resultado_under = resultado_under_ht(
        dados["ht"]
    )

    resultado_over = resultado_over_ft(
        dados["ft"]
    )


    # ========================================================
    # RESULTADO FINAL
    # ========================================================

    if (
        resultado_under == "LOSS"
        or resultado_over == "LOSS"
    ):

        resultado = "LOSS"

    elif (
        resultado_under == "VOID"
        or resultado_over == "VOID"
    ):

        resultado = "PUSH"

    else:

        resultado = "WIN"


    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado

    sinal[
        "resultado_under_ht"
    ] = resultado_under

    sinal[
        "resultado_over_ft"
    ] = resultado_over

    sinal[
        "cantos_ht_final"
    ] = dados["ht"]

    sinal[
        "cantos_ft_final"
    ] = dados["ft"]

    sinal[
        "resolvido_em"
    ] = agora_iso()


    # ========================================================
    # STATS
    # ========================================================

    if resultado == "WIN":

        stats["wins"] = (
            stats.get("wins", 0)
            + 1
        )

    elif resultado == "LOSS":

        stats["losses"] = (
            stats.get("losses", 0)
            + 1
        )

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
        ][gale]["pushes"] += 1


    # ========================================================
    # BANCA
    # ========================================================

    banca = next(
        (
            item
            for item in bancas
            if item["id"] == sinal["banca"]
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


    # ========================================================
    # TELEGRAM RESULTADO
    # ========================================================

    if resultado == "WIN":
        emoji = "✅"

    elif resultado == "LOSS":
        emoji = "❌"

    else:
        emoji = "⚪"


    mensagem = (

        f"{emoji} <b>{resultado} V15</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"


        f"1️⃣ Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f} "
        f"cantos HT\n"

        f"🚩 Cantos HT: "
        f"{dados['ht']:.0f}\n"

        f"Resultado: "
        f"<b>{resultado_under}</b>\n\n"


        f"2️⃣ Over "
        f"{LINHA_OVER_CANTOS_FT:.1f} "
        f"cantos FT\n"

        f"🚩 Cantos FT: "
        f"{dados['ft']:.0f}\n"

        f"Resultado: "
        f"<b>{resultado_over}</b>\n\n"


        f"🔥 Resultado combinado: "
        f"<b>{resultado}</b>\n\n"


        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale usado: "
        f"{sinal['gale']}"
    )


    telegram(mensagem)


    logger.info(
        "%s | %s x %s | "
        "HT %.0f = %s | "
        "FT %.0f = %s",
        resultado,
        sinal["home"],
        sinal["away"],
        dados["ht"],
        resultado_under,
        dados["ft"],
        resultado_over
    )


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    pendentes = [
        sinal

        for sinal in sinais

        if sinal.get("status") == "PENDENTE"
    ]


    if pendentes:

        logger.info(
            "⏳ %s sinal(is) pendente(s).",
            len(pendentes)
        )


    for sinal in pendentes:

        fixture = buscar_fixture(
            sinal["fixture_id"]
        )

        if not fixture:
            continue

        if not fixture_finalizado(fixture):
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
    ) = extrair_times(fixture)


    if not home_id or not away_id:

        logger.info(
            "⛔ %s x %s sem IDs.",
            home,
            away
        )

        return


    logger.info(
        "🔎 V15 | %s x %s",
        home,
        away
    )


    # ========================================================
    # 1. BET365 PRIMEIRO
    # ========================================================

    mercados = verificar_mercados_bet365(
        fixture_id
    )


    if not mercados.get("ht"):

        logger.info(
            "⛔ %s x %s | "
            "Bet365 sem mercado de escanteios HT.",
            home,
            away
        )

        return


    if not mercados.get("ft"):

        logger.info(
            "⛔ %s x %s | "
            "Bet365 sem mercado de escanteios FT.",
            home,
            away
        )

        return


    logger.info(
        "✅ BET365 | %s x %s | "
        "Cantos HT + FT disponíveis.",
        home,
        away
    )


    # ========================================================
    # 2. HISTÓRICO DO MANDANTE
    # ========================================================

    historico_home = buscar_historico_time(
        home_id
    )


    # ========================================================
    # 3. HISTÓRICO DO VISITANTE
    # ========================================================

    historico_away = buscar_historico_time(
        away_id
    )


    # ========================================================
    # 4. COMBINAR 20 + 20
    # ========================================================

    historico = combinar_historicos(
        historico_home,
        historico_away
    )


    logger.info(
        "📚 %s x %s | "
        "%s jogos após remover duplicados.",
        home,
        away,
        len(historico)
    )


    # ========================================================
    # 5. ESTATÍSTICAS
    # ========================================================

    analise = analisar_historico(
        historico
    )


    if not analise:

        logger.info(
            "⛔ %s x %s sem histórico válido.",
            home,
            away
        )

        return


    logger.info(
        "📊 %s x %s | "
        "Under HT %.1f%% | "
        "Over FT %.1f%% | "
        "Combinada %.1f%% | "
        "Amostra %s",
        home,
        away,
        analise["taxa_under"] * 100,
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100,
        analise["total"]
    )


    # ========================================================
    # 6. FILTROS DE 70%
    # ========================================================

    if not passou_filtros(
        analise
    ):

        logger.info(
            "⛔ %s x %s não atingiu "
            "todos os filtros de 70%%.",
            home,
            away
        )

        return


    # ========================================================
    # 7. APROVADO
    # ========================================================

    logger.info(
        "🔥 APROVADO V15 | "
        "%s x %s | "
        "Bet365 HT+FT SIM | "
        "Under %.1f%% | "
        "Over %.1f%% | "
        "Combinada %.1f%%",
        home,
        away,
        analise["taxa_under"] * 100,
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100
    )


    banca = obter_banca_livre()


    if not banca:

        logger.info(
            "🏦 Todas as bancas ocupadas."
        )

        return


    enviar_sinal(
        fixture,
        analise,
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
        "🔄 Iniciando ciclo V15"
    )


    try:

        # Primeiro resolve sinais existentes

        verificar_resultados()


        if obter_banca_livre() is None:

            logger.info(
                "🏦 Todas as %s bancas estão ocupadas.",
                TOTAL_BANCAS
            )

            return


        fixtures = buscar_fixtures()


        if not fixtures:

            logger.info(
                "📭 Nenhum jogo disponível."
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
                    "❌ Erro analisando fixture."
                )


    except Exception:

        logger.exception(
            "❌ Erro geral no ciclo V15."
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "🤖 ROBÔ V15 INICIADO"
    )

    logger.info(
        "🎰 Bookmaker: BET365"
    )

    logger.info(
        "🚩 Under %.1f cantos HT "
        "+ Over %.1f cantos FT",
        LINHA_UNDER_CANTOS_HT,
        LINHA_OVER_CANTOS_FT
    )

    logger.info(
        "📊 Under mínimo: %.0f%%",
        MIN_TAXA_UNDER_HT * 100
    )

    logger.info(
        "📊 Over mínimo: %.0f%%",
        MIN_TAXA_OVER_FT * 100
    )

    logger.info(
        "🔥 Combinação mínima: %.0f%%",
        MIN_TAXA_COMBINADA * 100
    )

    logger.info(
        "📚 Histórico: últimos %s jogos por time",
        QTD_HISTORICO_TIME
    )

    logger.info(
        "💰 Filtro pelo valor da odd: DESATIVADO"
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

    pushes = stats.get(
        "pushes",
        0
    )


    return jsonify({

        "status": "online",

        "versao": "V15",

        "bookmaker": "Bet365",

        "estrategia":
            "Under 6 cantos HT + Over 7 cantos FT",

        "requer_mercado_bet365_ht": True,
        "requer_mercado_bet365_ft": True,

        "filtro_valor_odd": False,

        "historico_por_time":
            QTD_HISTORICO_TIME,

        "linha_under_ht":
            LINHA_UNDER_CANTOS_HT,

        "linha_over_ft":
            LINHA_OVER_CANTOS_FT,

        "min_under":
            MIN_TAXA_UNDER_HT,

        "min_over":
            MIN_TAXA_OVER_FT,

        "min_combinada":
            MIN_TAXA_COMBINADA,

        "wins": wins,
        "losses": losses,
        "pushes": pushes,

        "assertividade":
            calcular_taxa(
                wins,
                losses
            ),

        "bancas": bancas
    })


@app.route("/status")
def status():

    return jsonify({

        "online": True,

        "versao": "V15",

        "bookmaker":
            "Bet365",

        "mercado_1":
            "Under 6.0 cantos HT",

        "mercado_2":
            "Over 7.0 cantos FT",

        "historico":
            "20 jogos por time + remoção de duplicados",

        "filtro_under":
            f"{MIN_TAXA_UNDER_HT * 100:.0f}%",

        "filtro_over":
            f"{MIN_TAXA_OVER_FT * 100:.0f}%",

        "filtro_combinado":
            f"{MIN_TAXA_COMBINADA * 100:.0f}%",

        "exige_bet365_ht": True,
        "exige_bet365_ft": True,

        "filtro_valor_odd": False
    })


@app.route("/stats")
def rota_stats():

    resposta = dict(stats)

    resposta[
        "assertividade"
    ] = calcular_taxa(
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
        "version": "V15"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v15"
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
