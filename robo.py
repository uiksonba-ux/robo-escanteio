import os
import json
import time
import html
import math
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# ROBÔ ESCANTEIOS V18
#
# LINHAS DINÂMICAS
#
# HT:
#   média de cantos HT + margem
#   Ex.: média 4.0 + 2 = Under 6.0
#
# FT:
#   média de cantos FT - margem
#   Ex.: média 6.0 - 2 = Over 4.0
#
# FILTROS:
#   - mínimo de jogos históricos
#   - Under HT >= 70%
#   - Over FT >= 70%
#   - Bet365 precisa ter mercado HT
#   - Bet365 precisa ter mercado FT
#
# COMBINAÇÃO:
#   apenas informativa
#
# ASSERTIVIDADE:
#   calculada pelos sinais realmente resolvidos
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v18")


# ============================================================
# API
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
# ESTRATÉGIA DINÂMICA
# ============================================================

MARGEM_HT = float(
    os.getenv("MARGEM_HT", "2.0")
)

MARGEM_FT = float(
    os.getenv("MARGEM_FT", "2.0")
)

MIN_LINHA_UNDER_HT = float(
    os.getenv("MIN_LINHA_UNDER_HT", "3.0")
)

MAX_LINHA_UNDER_HT = float(
    os.getenv("MAX_LINHA_UNDER_HT", "8.0")
)

MIN_LINHA_OVER_FT = float(
    os.getenv("MIN_LINHA_OVER_FT", "2.0")
)

MAX_LINHA_OVER_FT = float(
    os.getenv("MAX_LINHA_OVER_FT", "12.0")
)


# ============================================================
# ASSERTIVIDADE HISTÓRICA MÍNIMA
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv("MIN_TAXA_UNDER_HT", "0.70")
)

MIN_TAXA_OVER_FT = float(
    os.getenv("MIN_TAXA_OVER_FT", "0.70")
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

ARQUIVO_STATS = "stats_v18.json"
ARQUIVO_SINAIS = "sinais_v18.json"
ARQUIVO_CACHE = "cache_v18.json"


# ============================================================
# REQUEST SESSION
# ============================================================

session = requests.Session()

if API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-cantos-v18/1.0"
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
# JSON
# ============================================================

def carregar_json(caminho, padrao):

    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)

    except Exception as erro:
        logger.error("Erro lendo %s: %s", caminho, erro)
        return padrao


def salvar_json(caminho, dados):

    temporario = caminho + ".tmp"

    try:

        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, caminho)

    except Exception as erro:
        logger.error("Erro salvando %s: %s", caminho, erro)


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

stats.setdefault("por_gale", {})


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

cache_persistente.setdefault("historicos", {})
cache_persistente.setdefault("mercados", {})


cache_fixtures = {
    "timestamp": 0,
    "dados": []
}


cursor_fixture = 0


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

        if len(historico_requisicoes) >= API_MAX_REQUESTS_PER_MINUTE:

            espera = (
                60
                - (agora - historico_requisicoes[0])
                + 1
            )

            logger.info(
                "Rate limit interno. Esperando %.1fs",
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
        logger.error("API KEY não configurada.")
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
                    "API 429. Esperando %ss",
                    API_BACKOFF_429
                )

                time.sleep(API_BACKOFF_429)
                continue

            if resposta.status_code in (400, 403):

                logger.error(
                    "API %s: %s",
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
            logger.error("JSON inválido em %s", endpoint)
            return None

    return None


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


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def taxa(parte, total):

    if total <= 0:
        return 0.0

    return parte / total


def pct(valor):

    return f"{valor * 100:.1f}%"


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
# ARREDONDAMENTO DAS LINHAS
#
# Utilizamos passos de 0.5.
#
# HT:
# média + margem -> arredonda PARA CIMA
#
# FT:
# média - margem -> arredonda PARA BAIXO
# ============================================================

def arredondar_cima_meio(valor):
    return math.ceil(valor * 2) / 2


def arredondar_baixo_meio(valor):
    return math.floor(valor * 2) / 2


def limitar(valor, minimo, maximo):
    return max(minimo, min(valor, maximo))


def criar_linha_under(media):

    linha = arredondar_cima_meio(
        media + MARGEM_HT
    )

    return limitar(
        linha,
        MIN_LINHA_UNDER_HT,
        MAX_LINHA_UNDER_HT
    )


def criar_linha_over(media):

    linha = arredondar_baixo_meio(
        media - MARGEM_FT
    )

    return limitar(
        linha,
        MIN_LINHA_OVER_FT,
        MAX_LINHA_OVER_FT
    )


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
# ASSERTIVIDADE REAL DO GRUPO
#
# PUSH não entra no denominador da assertividade.
# ============================================================

def estatisticas_grupo():

    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    pushes = int(stats.get("pushes", 0))

    decisoes = wins + losses

    assertividade = (
        wins / decisoes
        if decisoes > 0
        else 0.0
    )

    por_gale = {}

    for gale in range(MAX_GALES + 1):

        dados = stats.get(
            "por_gale",
            {}
        ).get(
            str(gale),
            {}
        )

        w = int(dados.get("wins", 0))
        l = int(dados.get("losses", 0))
        p = int(dados.get("pushes", 0))

        total_decisoes = w + l

        taxa_gale = (
            w / total_decisoes
            if total_decisoes
            else 0.0
        )

        por_gale[str(gale)] = {
            "wins": w,
            "losses": l,
            "pushes": p,
            "assertividade": taxa_gale
        }

    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "resolvidos": wins + losses + pushes,
        "assertividade": assertividade,
        "por_gale": por_gale
    }


def texto_assertividade_grupo():

    grupo = estatisticas_grupo()

    linhas = [
        "━━━━━━━━━━━━━━━━━━",
        "📊 <b>ASSERTIVIDADE DO GRUPO</b>",
        "",
        f"🎯 Geral: <b>{pct(grupo['assertividade'])}</b>",
        (
            f"✅ {grupo['wins']} WIN | "
            f"❌ {grupo['losses']} LOSS | "
            f"⚪ {grupo['pushes']} VOID"
        ),
        ""
    ]

    for gale in range(MAX_GALES + 1):

        g = grupo["por_gale"][str(gale)]

        if gale == 0:
            nome = "Sem Gale"
        else:
            nome = f"Gale {gale}"

        if g["wins"] + g["losses"] == 0:
            valor = "—"
        else:
            valor = pct(g["assertividade"])

        linhas.append(
            f"🔹 {nome}: {valor}"
        )

    linhas.extend([
        "",
        f"📌 Sinais resolvidos: {grupo['resolvidos']}",
        "━━━━━━━━━━━━━━━━━━"
    ])

    return "\n".join(linhas)


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
# FIXTURES FUTUROS
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

    # Prioriza os jogos mais próximos quando kickoff_ts existir.
    jogos.sort(
        key=lambda x: x.get(
            "kickoff_ts",
            9999999999
        )
        if isinstance(x, dict)
        else 9999999999
    )

    cache_fixtures["timestamp"] = agora
    cache_fixtures["dados"] = jogos

    logger.info(
        "%s jogos nas próximas %sh",
        len(jogos),
        JANELA_HORAS
    )

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
# BET365
# ============================================================

def extrair_bet365(resposta):

    if not isinstance(resposta, dict):
        return None

    data = resposta.get("data")

    if not isinstance(data, dict):
        return None

    bookmakers = data.get("bookmakers")

    if not isinstance(bookmakers, list):
        return None

    for bookmaker in bookmakers:

        if not isinstance(bookmaker, dict):
            continue

        if str(
            bookmaker.get("slug", "")
        ).lower() == "bet365":

            return bookmaker

    return None


# ============================================================
# PEGAR LINHA ATUAL
#
# Para jogo pré-live:
# prioriza closing (última pré-jogo)
# depois opening
# depois inplay.
# ============================================================

def extrair_linha_mercado(mercado):

    if not isinstance(mercado, dict):
        return None

    for fase in (
        "closing",
        "opening",
        "inplay"
    ):

        item = mercado.get(fase)

        if not isinstance(item, dict):
            continue

        linha = numero(item.get("line"))
        over = numero(item.get("over"))
        under = numero(item.get("under"))

        if (
            linha is not None
            and over is not None
            and under is not None
            and over > 1
            and under > 1
        ):

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

    odds = bookmaker.get("odds")

    if not isinstance(odds, dict):
        return None

    mercado = odds.get(chave)

    return extrair_linha_mercado(
        mercado
    )


def verificar_mercados_bet365(
    fixture_id
):

    chave = str(fixture_id)

    agora = time.time()

    cache = cache_persistente[
        "mercados"
    ].get(chave)

    if cache:

        idade = (
            agora
            - cache.get("timestamp", 0)
        )

        if idade < CACHE_MERCADO_TTL:

            return cache.get(
                "dados",
                {}
            )

    # FT
    ft = consultar_mercado_bet365(
        fixture_id,
        "corner",
        "corner_line"
    )

    if not ft:

        resultado = {
            "ft": False,
            "ht": False,
            "ft_detalhes": None,
            "ht_detalhes": None
        }

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

    # HT
    ht = consultar_mercado_bet365(
        fixture_id,
        "corner_half",
        "corner_line_half"
    )

    resultado = {
        "ft": ft is not None,
        "ht": ht is not None,
        "ft_detalhes": ft,
        "ht_detalhes": ht
    }

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

    logger.info(
        "BET365 %s | FT=%s | HT=%s",
        fixture_id,
        resultado["ft"],
        resultado["ht"]
    )

    return resultado


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

            return item.get(
                "dados",
                []
            )

    logger.info(
        "Buscando %s jogos do time %s",
        QTD_HISTORICO_TIME,
        team_id
    )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
            "per_page": QTD_HISTORICO_TIME
        }
    )

    jogos = extrair_lista(dados)

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


def combinar_historicos(home, away):

    resultado = []
    vistos = set()

    for jogo in list(home) + list(away):

        fixture_id = obter_id_jogo(jogo)

        if fixture_id:

            if fixture_id in vistos:
                continue

            vistos.add(fixture_id)

        resultado.append(jogo)

    return resultado


# ============================================================
# CANTOS
#
# FT = home + away
# HT = half_home + half_away
#
# Não somar HT novamente no FT.
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return None

    corners = jogo.get("corners")

    if not isinstance(corners, dict):
        return None

    ft_home = numero(corners.get("home"))
    ft_away = numero(corners.get("away"))

    ht_home = numero(corners.get("half_home"))
    ht_away = numero(corners.get("half_away"))

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
# RESULTADOS PARA QUALQUER LINHA
#
# Linhas inteiras:
#   igualdade = VOID
#
# Linhas .5:
#   não existe VOID.
# ============================================================

def resultado_under(valor, linha):

    if valor < linha:
        return "WIN"

    if valor == linha:
        return "VOID"

    return "LOSS"


def resultado_over(valor, linha):

    if valor > linha:
        return "WIN"

    if valor == linha:
        return "VOID"

    return "LOSS"


# ============================================================
# PREPARAR HISTÓRICO
# ============================================================

def preparar_historico(jogos):

    registros = []
    ignorados = 0

    for jogo in jogos:

        cantos = extrair_cantos(jogo)

        if not cantos:

            ignorados += 1
            continue

        registros.append(cantos)

    return registros, ignorados


# ============================================================
# ANÁLISE DINÂMICA
# ============================================================

def analisar_historico(jogos):

    registros, ignorados = preparar_historico(
        jogos
    )

    total = len(registros)

    if total == 0:
        return None

    # Médias reais
    media_ht = (
        sum(r["ht"] for r in registros)
        / total
    )

    media_ft = (
        sum(r["ft"] for r in registros)
        / total
    )

    # Linhas dinâmicas
    linha_under = criar_linha_under(
        media_ht
    )

    linha_over = criar_linha_over(
        media_ft
    )

    under_wins = 0
    under_voids = 0
    under_losses = 0

    over_wins = 0
    over_voids = 0
    over_losses = 0

    comb_wins = 0
    comb_voids = 0
    comb_losses = 0

    for r in registros:

        under = resultado_under(
            r["ht"],
            linha_under
        )

        over = resultado_over(
            r["ft"],
            linha_over
        )

        if under == "WIN":
            under_wins += 1
        elif under == "VOID":
            under_voids += 1
        else:
            under_losses += 1

        if over == "WIN":
            over_wins += 1
        elif over == "VOID":
            over_voids += 1
        else:
            over_losses += 1

        if (
            under == "LOSS"
            or over == "LOSS"
        ):
            comb_losses += 1

        elif (
            under == "VOID"
            or over == "VOID"
        ):
            comb_voids += 1

        else:
            comb_wins += 1

    # VOID continua no denominador.
    taxa_under = taxa(
        under_wins,
        total
    )

    taxa_over = taxa(
        over_wins,
        total
    )

    taxa_combinada = taxa(
        comb_wins,
        total
    )

    margem_real_ht = (
        linha_under - media_ht
    )

    margem_real_ft = (
        media_ft - linha_over
    )

    return {
        "total": total,
        "ignorados": ignorados,

        "media_ht": media_ht,
        "media_ft": media_ft,

        "linha_under": linha_under,
        "linha_over": linha_over,

        "margem_ht": margem_real_ht,
        "margem_ft": margem_real_ft,

        "under_wins": under_wins,
        "under_voids": under_voids,
        "under_losses": under_losses,
        "taxa_under": taxa_under,

        "over_wins": over_wins,
        "over_voids": over_voids,
        "over_losses": over_losses,
        "taxa_over": taxa_over,

        "combinada_wins": comb_wins,
        "combinada_voids": comb_voids,
        "combinada_losses": comb_losses,
        "taxa_combinada": taxa_combinada
    }


# ============================================================
# FILTROS V18
#
# COMBINAÇÃO NÃO FILTRA.
# ============================================================

def passou_filtros(analise):

    if not analise:
        return False

    if analise["total"] < MIN_JOGOS_HISTORICO:

        logger.info(
            "Amostra %s < %s",
            analise["total"],
            MIN_JOGOS_HISTORICO
        )

        return False

    if analise["taxa_under"] < MIN_TAXA_UNDER_HT:

        logger.info(
            "Under %.1f%% < %.1f%%",
            analise["taxa_under"] * 100,
            MIN_TAXA_UNDER_HT * 100
        )

        return False

    if analise["taxa_over"] < MIN_TAXA_OVER_FT:

        logger.info(
            "Over %.1f%% < %.1f%%",
            analise["taxa_over"] * 100,
            MIN_TAXA_OVER_FT * 100
        )

        return False

    return True


# ============================================================
# FIXTURE UTILIZADO
# ============================================================

def fixture_ja_utilizado(fixture_id):

    fixture_id = str(fixture_id)

    return any(
        str(s.get("fixture_id")) == fixture_id
        for s in sinais
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(
    fixture,
    analise,
    mercados,
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

    ht_api = (
        mercados.get("ht_detalhes")
        or {}
    )

    ft_api = (
        mercados.get("ft_detalhes")
        or {}
    )

    sinal = {
        "fixture_id": fixture_id,

        "home_id": home_id,
        "away_id": away_id,

        "home": home,
        "away": away,

        "versao": "V18",

        "linha_under_ht":
            analise["linha_under"],

        "linha_over_ft":
            analise["linha_over"],

        "media_ht":
            round(analise["media_ht"], 2),

        "media_ft":
            round(analise["media_ft"], 2),

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

        "bet365_linha_ht":
            ht_api.get("linha"),

        "bet365_linha_ft":
            ft_api.get("linha"),

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

        "🚨 <b>SINAL V18</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x {html.escape(away)}</b>\n\n"


        "1️⃣ <b>ESCANTEIOS HT</b>\n"

        f"📊 Média: "
        f"{analise['media_ht']:.1f}\n"

        f"🎯 Linha escolhida: "
        f"<b>Under "
        f"{analise['linha_under']:.1f}</b>\n"

        f"🛡️ Margem sobre média: "
        f"+{analise['margem_ht']:.1f}\n"

        f"📈 Histórico da linha: "
        f"<b>{pct(analise['taxa_under'])}</b>\n"

        f"✅ {analise['under_wins']} WIN | "
        f"⚪ {analise['under_voids']} VOID | "
        f"❌ {analise['under_losses']} LOSS\n\n"


        "2️⃣ <b>ESCANTEIOS FT</b>\n"

        f"📊 Média: "
        f"{analise['media_ft']:.1f}\n"

        f"🎯 Linha escolhida: "
        f"<b>Over "
        f"{analise['linha_over']:.1f}</b>\n"

        f"🛡️ Margem abaixo da média: "
        f"{analise['margem_ft']:.1f}\n"

        f"📈 Histórico da linha: "
        f"<b>{pct(analise['taxa_over'])}</b>\n"

        f"✅ {analise['over_wins']} WIN | "
        f"⚪ {analise['over_voids']} VOID | "
        f"❌ {analise['over_losses']} LOSS\n\n"


        "🎰 <b>BET365</b>\n"

        "✅ Mercados HT e FT encontrados\n"

        f"↳ Linha principal API HT: "
        f"{ht_api.get('linha', '-')}\n"

        f"↳ Linha principal API FT: "
        f"{ft_api.get('linha', '-')}\n"

        "ℹ️ A linha acima é a principal retornada "
        "pela API; confira a linha dinâmica no app.\n\n"


        "📊 <b>COMBINAÇÃO</b>\n"

        f"Ocorrência conjunta: "
        f"<b>{pct(analise['taxa_combinada'])}</b>\n"

        f"✅ {analise['combinada_wins']} WIN | "
        f"⚪ {analise['combinada_voids']} VOID | "
        f"❌ {analise['combinada_losses']} LOSS\n"

        "ℹ️ Apenas informativa\n\n"


        "💰 <b>GESTÃO</b>\n"

        f"🏦 Banca: {banca['id']}\n"

        f"🔄 Gale: "
        f"{banca['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}\n\n"


        + texto_assertividade_grupo()
    )

    telegram(mensagem)

    logger.info(
        "APROVADO V18 | %s x %s | "
        "Under %.1f = %.1f%% | "
        "Over %.1f = %.1f%% | "
        "Comb %.1f%%",
        home,
        away,
        analise["linha_under"],
        analise["taxa_under"] * 100,
        analise["linha_over"],
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100
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


def fixture_finalizado(fixture):

    status = str(
        fixture.get("status", "")
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

def liberar_banca(banca, resultado):

    if resultado == "WIN":

        banca["gale"] = 0

    elif resultado == "LOSS":

        if banca["gale"] < MAX_GALES:
            banca["gale"] += 1

        else:
            banca["gale"] = 0

    # PUSH mantém o Gale.

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
# RESOLVER SINAL DINÂMICO
# ============================================================

def resolver_sinal(sinal, fixture):

    cantos = extrair_cantos(fixture)

    if not cantos:

        logger.warning(
            "Fixture %s sem dados de cantos",
            sinal["fixture_id"]
        )

        return

    linha_under = float(
        sinal["linha_under_ht"]
    )

    linha_over = float(
        sinal["linha_over_ft"]
    )

    under = resultado_under(
        cantos["ht"],
        linha_under
    )

    over = resultado_over(
        cantos["ft"],
        linha_over
    )

    if (
        under == "LOSS"
        or over == "LOSS"
    ):
        resultado = "LOSS"

    elif (
        under == "VOID"
        or over == "VOID"
    ):
        resultado = "PUSH"

    else:
        resultado = "WIN"

    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado

    sinal["resultado_under_ht"] = under
    sinal["resultado_over_ft"] = over

    sinal["cantos_ht_final"] = cantos["ht"]
    sinal["cantos_ft_final"] = cantos["ft"]

    sinal["resolvido_em"] = agora_iso()

    if resultado == "WIN":
        stats["wins"] = (
            stats.get("wins", 0) + 1
        )

    elif resultado == "LOSS":
        stats["losses"] = (
            stats.get("losses", 0) + 1
        )

    else:
        stats["pushes"] = (
            stats.get("pushes", 0) + 1
        )

    stats["total_resolvidos"] = (
        stats.get(
            "total_resolvidos",
            0
        ) + 1
    )

    gale = str(
        sinal.get("gale", 0)
    )

    stats["por_gale"].setdefault(
        gale,
        {
            "wins": 0,
            "losses": 0,
            "pushes": 0
        }
    )

    if resultado == "WIN":
        stats["por_gale"][gale]["wins"] += 1

    elif resultado == "LOSS":
        stats["por_gale"][gale]["losses"] += 1

    else:
        stats["por_gale"][gale]["pushes"] += 1

    banca = next(
        (
            b
            for b in bancas
            if b["id"] == sinal["banca"]
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

    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "⚪"
    }.get(resultado, "ℹ️")

    mensagem = (

        f"{emoji} <b>{resultado} V18</b>\n\n"

        f"⚽ <b>{html.escape(sinal['home'])}"
        f" x {html.escape(sinal['away'])}</b>\n\n"

        f"1️⃣ Under {linha_under:.1f} HT\n"
        f"🚩 Cantos HT: {cantos['ht']:.0f}\n"
        f"Resultado: <b>{under}</b>\n\n"

        f"2️⃣ Over {linha_over:.1f} FT\n"
        f"🚩 Cantos FT: {cantos['ft']:.0f}\n"
        f"Resultado: <b>{over}</b>\n\n"

        f"🔥 Resultado do sinal: "
        f"<b>{resultado}</b>\n\n"

        f"🏦 Banca: {sinal['banca']}\n"
        f"🔄 Gale usado: {sinal['gale']}\n\n"

        + texto_assertividade_grupo()
    )

    telegram(mensagem)


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    pendentes = [
        sinal
        for sinal in sinais
        if sinal.get("status") == "PENDENTE"
    ]

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
        return

    logger.info(
        "V18 | %s x %s",
        home,
        away
    )

    # ========================================================
    # BET365
    # ========================================================

    mercados = verificar_mercados_bet365(
        fixture_id
    )

    if not mercados.get("ft"):
        logger.info(
            "%s x %s sem cantos FT Bet365",
            home,
            away
        )
        return

    if not mercados.get("ht"):
        logger.info(
            "%s x %s sem cantos HT Bet365",
            home,
            away
        )
        return

    # ========================================================
    # HISTÓRICO
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

    analise = analisar_historico(
        historico
    )

    if not analise:
        return

    logger.info(
        "%s x %s | Jogos=%s | "
        "Média HT=%.2f -> Under %.1f = %.1f%% | "
        "Média FT=%.2f -> Over %.1f = %.1f%% | "
        "Comb=%.1f%%",
        home,
        away,
        analise["total"],
        analise["media_ht"],
        analise["linha_under"],
        analise["taxa_under"] * 100,
        analise["media_ft"],
        analise["linha_over"],
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100
    )

    if not passou_filtros(analise):

        logger.info(
            "REPROVADO V18 | %s x %s",
            home,
            away
        )

        return

    banca = obter_banca_livre()

    if not banca:

        logger.info(
            "Todas as bancas ocupadas"
        )

        return

    enviar_sinal(
        fixture,
        analise,
        mercados,
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
        "CICLO V18"
    )

    try:

        verificar_resultados()

        if obter_banca_livre() is None:

            logger.info(
                "Todas as bancas ocupadas"
            )

            return

        fixtures = buscar_fixtures()

        if not fixtures:

            logger.info(
                "Sem fixtures"
            )

            return

        total = len(fixtures)

        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )

        logger.info(
            "Analisando %s de %s jogos",
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
                    "Erro analisando fixture"
                )

    except Exception:
        logger.exception(
            "Erro geral no ciclo"
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "ROBÔ V18 INICIADO"
    )

    logger.info(
        "HT: média + %.1f -> Under",
        MARGEM_HT
    )

    logger.info(
        "FT: média - %.1f -> Over",
        MARGEM_FT
    )

    logger.info(
        "Under mínimo: %.1f%%",
        MIN_TAXA_UNDER_HT * 100
    )

    logger.info(
        "Over mínimo: %.1f%%",
        MIN_TAXA_OVER_FT * 100
    )

    logger.info(
        "Combinação: apenas informativa"
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
        "status": "online",
        "versao": "V18",

        "estrategia":
            "Linhas dinâmicas de escanteios",

        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "min_taxa_under":
            MIN_TAXA_UNDER_HT,

        "min_taxa_over":
            MIN_TAXA_OVER_FT,

        "combinacao_filtra":
            False,

        "assertividade_grupo":
            round(
                grupo["assertividade"] * 100,
                2
            ),

        "wins":
            grupo["wins"],

        "losses":
            grupo["losses"],

        "pushes":
            grupo["pushes"],

        "bancas":
            bancas
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,
        "versao": "V18",

        "estrategia": {
            "ht":
                "media HT + margem -> Under",

            "ft":
                "media FT - margem -> Over"
        },

        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "min_taxa_under_ht":
            MIN_TAXA_UNDER_HT,

        "min_taxa_over_ft":
            MIN_TAXA_OVER_FT,

        "combinacao":
            "apenas informativa",

        "qtd_historico_time":
            QTD_HISTORICO_TIME,

        "min_jogos_historico":
            MIN_JOGOS_HISTORICO,

        "janela_horas":
            JANELA_HORAS,

        "max_jogos_ciclo":
            MAX_JOGOS_ANALISADOS_CICLO,

        "intervalo_segundos":
            INTERVALO_ANALISE_SEGUNDOS,

        "total_bancas":
            TOTAL_BANCAS,

        "entrada_inicial":
            ENTRADA_INICIAL,

        "multiplicador_gale":
            MULTIPLICADOR_GALE,

        "max_gales":
            MAX_GALES
    })


@app.route("/stats")
def rota_stats():

    grupo = estatisticas_grupo()

    resposta = dict(stats)

    resposta[
        "assertividade"
    ] = round(
        grupo["assertividade"] * 100,
        2
    )

    resposta[
        "detalhes_grupo"
    ] = grupo

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
        "version": "V18"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v18"
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
)
