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
# ROBÔ DE ESCANTEIOS V19
#
# 1 SINAL = 2 JOGOS = 4 MERCADOS
#
# JOGO 1:
#   Under cantos HT
#   Over cantos FT
#
# JOGO 2:
#   Under cantos HT
#   Over cantos FT
#
# LINHAS DINÂMICAS:
#   Under HT = média HT + margem
#   Over FT  = média FT - margem
#
# Cada mercado precisa atingir a taxa mínima individual.
#
# Combinação histórica de cada jogo é INFORMATIVA.
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v19")


# ============================================================
# CONFIGURAÇÃO API
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
# ESTRATÉGIA
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
# FILTROS
# ============================================================

MIN_TAXA_UNDER_HT = float(
    os.getenv("MIN_TAXA_UNDER_HT", "0.70")
)

MIN_TAXA_OVER_FT = float(
    os.getenv("MIN_TAXA_OVER_FT", "0.70")
)

QTD_HISTORICO_TIME = int(
    os.getenv("QTD_HISTORICO_TIME", "20")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "20")
)


# ============================================================
# JANELA DE ANÁLISE
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

ARQUIVO_STATS = "stats_v19.json"
ARQUIVO_SINAIS = "sinais_v19.json"
ARQUIVO_FILA = "fila_v19.json"
ARQUIVO_BANCAS = "bancas_v19.json"
ARQUIVO_CACHE = "cache_v19.json"


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

if API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-cantos-v19/1.0"
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


fila_aprovados = carregar_json(
    ARQUIVO_FILA,
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


def limitar(
    valor,
    minimo,
    maximo
):

    return max(
        minimo,
        min(valor, maximo)
    )


def arredondar_cima_meio(valor):

    return math.ceil(
        valor * 2
    ) / 2


def arredondar_baixo_meio(valor):

    return math.floor(
        valor * 2
    ) / 2


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
# EXTRAIR LISTA API
# ============================================================

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
            and
            agora - historico_requisicoes[0] >= 60
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
# API GET
# ============================================================

def api_get(
    endpoint,
    params=None,
    tentativas=3
):

    if not API_KEY:

        logger.error(
            "FIVE_DOLLAR_API_KEY ausente"
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
                    "429. Aguardando %ss",
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
                "JSON inválido: %s",
                endpoint
            )

            return None

    return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

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


# Garante novas bancas caso TOTAL_BANCAS aumente.
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
# ASSERTIVIDADE DO GRUPO
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

    decisoes = wins + losses

    geral = (
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

        total = w + l

        por_gale[str(gale)] = {
            "wins": w,
            "losses": l,
            "pushes": p,
            "taxa": (
                w / total
                if total
                else 0
            )
        }

    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "assertividade": geral,
        "resolvidos":
            wins + losses + pushes,
        "por_gale": por_gale
    }


def texto_assertividade():

    dados = estatisticas_grupo()

    if (
        dados["wins"]
        + dados["losses"]
        == 0
    ):
        geral = "Aguardando resultados"
    else:
        geral = pct(
            dados["assertividade"]
        )

    linhas = [
        "━━━━━━━━━━━━━━━━━━",
        "📊 <b>ASSERTIVIDADE V19</b>",
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

        decisoes = (
            g["wins"]
            + g["losses"]
        )

        valor = (
            pct(g["taxa"])
            if decisoes
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
# FIXTURES FUTUROS
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

    def ordenar(jogo):

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
        key=ordenar
    )

    cache_fixtures[
        "timestamp"
    ] = agora

    cache_fixtures[
        "dados"
    ] = jogos

    logger.info(
        "%s jogos encontrados",
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
            "bookmakers":
                "bet365",
            "market":
                market
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

        if (
            idade
            < CACHE_MERCADO_TTL
        ):

            return item.get(
                "dados",
                {}
            )

    # Primeiro FT
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

    else:

        # Só gasta chamada HT
        # se FT existir.
        ht = consultar_mercado_bet365(
            fixture_id,
            "corner_half",
            "corner_line_half"
        )

        resultado = {
            "ft": True,
            "ht": ht is not None,
            "ft_detalhes": ft,
            "ht_detalhes": ht
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

        if (
            idade
            < CACHE_HISTORICO_TTL
        ):

            return item.get(
                "dados",
                []
            )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
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
# CANTOS
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
# RESULTADO DE MERCADO
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
# ANÁLISE HISTÓRICA
# ============================================================

def analisar_historico(
    jogos
):

    registros = []

    for jogo in jogos:

        cantos = extrair_cantos(
            jogo
        )

        if cantos:
            registros.append(
                cantos
            )

    total = len(
        registros
    )

    if total == 0:
        return None

    media_ht = (
        sum(
            r["ht"]
            for r in registros
        )
        / total
    )

    media_ft = (
        sum(
            r["ft"]
            for r in registros
        )
        / total
    )

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

    for registro in registros:

        under = resultado_under(
            registro["ht"],
            linha_under
        )

        over = resultado_over(
            registro["ft"],
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


    return {

        "total": total,

        "media_ht":
            media_ht,

        "media_ft":
            media_ft,

        "linha_under":
            linha_under,

        "linha_over":
            linha_over,

        "margem_ht":
            linha_under
            - media_ht,

        "margem_ft":
            media_ft
            - linha_over,

        "under_wins":
            under_wins,

        "under_voids":
            under_voids,

        "under_losses":
            under_losses,

        "taxa_under":
            taxa_under,

        "over_wins":
            over_wins,

        "over_voids":
            over_voids,

        "over_losses":
            over_losses,

        "taxa_over":
            taxa_over,

        "comb_wins":
            comb_wins,

        "comb_voids":
            comb_voids,

        "comb_losses":
            comb_losses,

        "taxa_combinada":
            taxa_combinada
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
        analise["taxa_under"]
        < MIN_TAXA_UNDER_HT
    ):
        return False

    if (
        analise["taxa_over"]
        < MIN_TAXA_OVER_FT
    ):
        return False

    return True


# ============================================================
# JOGO JÁ USADO
# ============================================================

def fixture_ja_usado(
    fixture_id
):

    fixture_id = str(
        fixture_id
    )

    for sinal in sinais:

        for jogo in sinal.get(
            "jogos",
            []
        ):

            if (
                str(
                    jogo.get(
                        "fixture_id"
                    )
                )
                == fixture_id
            ):
                return True

    return False


def fixture_na_fila(
    fixture_id
):

    fixture_id = str(
        fixture_id
    )

    return any(
        str(
            item.get(
                "fixture_id"
            )
        )
        == fixture_id
        for item
        in fila_aprovados
    )


# ============================================================
# COLOCAR JOGO APROVADO NA FILA
# ============================================================

def adicionar_fila(
    fixture,
    analise,
    mercados
):

    fixture_id = str(
        fixture.get("id")
    )

    if fixture_na_fila(
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

    item = {

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

        "linha_under_ht":
            analise["linha_under"],

        "linha_over_ft":
            analise["linha_over"],

        "media_ht":
            analise["media_ht"],

        "media_ft":
            analise["media_ft"],

        "taxa_under":
            analise["taxa_under"],

        "taxa_over":
            analise["taxa_over"],

        "taxa_combinada":
            analise["taxa_combinada"],

        "under_wins":
            analise["under_wins"],

        "under_voids":
            analise["under_voids"],

        "under_losses":
            analise["under_losses"],

        "over_wins":
            analise["over_wins"],

        "over_voids":
            analise["over_voids"],

        "over_losses":
            analise["over_losses"],

        "comb_wins":
            analise["comb_wins"],

        "comb_voids":
            analise["comb_voids"],

        "comb_losses":
            analise["comb_losses"],

        "amostra":
            analise["total"],

        "bet365_ht":
            mercados.get(
                "ht_detalhes"
            ),

        "bet365_ft":
            mercados.get(
                "ft_detalhes"
            ),

        "aprovado_em":
            agora_iso()
    }

    with lock_dados:

        fila_aprovados.append(
            item
        )

        salvar_json(
            ARQUIVO_FILA,
            fila_aprovados
        )

    logger.info(
        "APROVADO PARA FILA V19 | "
        "%s x %s",
        home,
        away
    )


# ============================================================
# CRIAR SINAL COM DOIS JOGOS
# ============================================================

def tentar_criar_sinal():

    with lock_dados:

        banca = obter_banca_livre()

        if not banca:
            return False

        if len(
            fila_aprovados
        ) < 2:
            return False

        jogo1 = fila_aprovados.pop(0)
        jogo2 = fila_aprovados.pop(0)

        sinal_id = (
            f"{int(time.time())}-"
            f"{jogo1['fixture_id']}-"
            f"{jogo2['fixture_id']}"
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

        sinal = {

            "id":
                sinal_id,

            "versao":
                "V19",

            "status":
                "PENDENTE",

            "resultado":
                None,

            "banca":
                banca["id"],

            "gale":
                gale,

            "entrada":
                entrada,

            "criado_em":
                agora_iso(),

            "jogos": [
                jogo1,
                jogo2
            ]
        }

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

        salvar_json(
            ARQUIVO_FILA,
            fila_aprovados
        )

        salvar_bancas()


    enviar_mensagem_sinal(
        sinal
    )

    return True


# ============================================================
# TEXTO DE UM JOGO
# ============================================================

def texto_jogo(
    numero_jogo,
    jogo
):

    ht_api = (
        jogo.get("bet365_ht")
        or {}
    )

    ft_api = (
        jogo.get("bet365_ft")
        or {}
    )

    return (

        f"⚽ <b>JOGO {numero_jogo}</b>\n"

        f"<b>{html.escape(jogo['home'])}"
        f" x "
        f"{html.escape(jogo['away'])}</b>\n\n"


        f"{1 if numero_jogo == 1 else 3}️⃣ "
        "<b>UNDER HT</b>\n"

        f"🎯 Under "
        f"<b>{jogo['linha_under_ht']:.1f}</b>\n"

        f"📊 Média HT: "
        f"{jogo['media_ht']:.1f}\n"

        f"📈 Histórico: "
        f"<b>{pct(jogo['taxa_under'])}</b>\n"

        f"✅ {jogo['under_wins']} WIN | "
        f"⚪ {jogo['under_voids']} VOID | "
        f"❌ {jogo['under_losses']} LOSS\n\n"


        f"{2 if numero_jogo == 1 else 4}️⃣ "
        "<b>OVER FT</b>\n"

        f"🎯 Over "
        f"<b>{jogo['linha_over_ft']:.1f}</b>\n"

        f"📊 Média FT: "
        f"{jogo['media_ft']:.1f}\n"

        f"📈 Histórico: "
        f"<b>{pct(jogo['taxa_over'])}</b>\n"

        f"✅ {jogo['over_wins']} WIN | "
        f"⚪ {jogo['over_voids']} VOID | "
        f"❌ {jogo['over_losses']} LOSS\n\n"


        f"📊 Ocorrência conjunta do jogo: "
        f"<b>{pct(jogo['taxa_combinada'])}</b>\n"

        "ℹ️ Apenas informativa\n\n"


        "🎰 Bet365 API\n"

        f"↳ Linha principal HT: "
        f"{ht_api.get('linha', '-')}\n"

        f"↳ Linha principal FT: "
        f"{ft_api.get('linha', '-')}\n"
    )


# ============================================================
# TELEGRAM SINAL
# ============================================================

def enviar_mensagem_sinal(
    sinal
):

    jogo1 = sinal[
        "jogos"
    ][0]

    jogo2 = sinal[
        "jogos"
    ][1]

    mensagem = (

        "🚨 <b>SINAL V19 — 2 JOGOS / 4 MERCADOS</b>\n\n"

        + texto_jogo(
            1,
            jogo1
        )

        + "\n━━━━━━━━━━━━━━━━━━\n\n"

        + texto_jogo(
            2,
            jogo2
        )

        + "\n━━━━━━━━━━━━━━━━━━\n"

        "\n🔥 <b>COMBINAÇÃO — 4 MERCADOS</b>\n\n"

        f"1️⃣ {html.escape(jogo1['home'])}"
        f" x {html.escape(jogo1['away'])}"
        f" — Under {jogo1['linha_under_ht']:.1f} HT\n"

        f"2️⃣ {html.escape(jogo1['home'])}"
        f" x {html.escape(jogo1['away'])}"
        f" — Over {jogo1['linha_over_ft']:.1f} FT\n"

        f"3️⃣ {html.escape(jogo2['home'])}"
        f" x {html.escape(jogo2['away'])}"
        f" — Under {jogo2['linha_under_ht']:.1f} HT\n"

        f"4️⃣ {html.escape(jogo2['home'])}"
        f" x {html.escape(jogo2['away'])}"
        f" — Over {jogo2['linha_over_ft']:.1f} FT\n\n"

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

    if fixture_na_fila(
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
        return

    logger.info(
        "V19 analisando %s x %s",
        home,
        away
    )

    # ========================================================
    # MERCADOS BET365
    # ========================================================

    mercados = verificar_mercados_bet365(
        fixture_id
    )

    if not mercados.get(
        "ft"
    ):

        logger.info(
            "Reprovado: sem FT Bet365"
        )

        return

    if not mercados.get(
        "ht"
    ):

        logger.info(
            "Reprovado: sem HT Bet365"
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
        "%s x %s | "
        "N=%s | "
        "Under %.1f %.1f%% | "
        "Over %.1f %.1f%% | "
        "Conj %.1f%%",
        home,
        away,
        analise["total"],
        analise["linha_under"],
        analise["taxa_under"] * 100,
        analise["linha_over"],
        analise["taxa_over"] * 100,
        analise["taxa_combinada"] * 100
    )


    if not passou_filtros(
        analise
    ):

        logger.info(
            "REPROVADO V19 | %s x %s",
            home,
            away
        )

        return


    adicionar_fila(
        fixture,
        analise,
        mercados
    )

    tentar_criar_sinal()


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
        isinstance(
            response,
            list
        )
        and response
    ):
        return response[0]

    return dados


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
# RESOLVER JOGO
# ============================================================

def resolver_jogo(
    jogo,
    fixture
):

    cantos = extrair_cantos(
        fixture
    )

    if not cantos:
        return None

    under = resultado_under(
        cantos["ht"],
        float(
            jogo[
                "linha_under_ht"
            ]
        )
    )

    over = resultado_over(
        cantos["ft"],
        float(
            jogo[
                "linha_over_ft"
            ]
        )
    )

    return {
        "under":
            under,

        "over":
            over,

        "cantos_ht":
            cantos["ht"],

        "cantos_ft":
            cantos["ft"]
    }


# ============================================================
# RESULTADO FINAL DAS 4 PERNAS
# ============================================================

def calcular_resultado_sinal(
    resultados
):

    pernas = []

    for resultado in resultados:

        pernas.append(
            resultado["under"]
        )

        pernas.append(
            resultado["over"]
        )

    if "LOSS" in pernas:
        return "LOSS"

    if "VOID" in pernas:
        return "PUSH"

    return "WIN"


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

        if (
            gale_atual
            < MAX_GALES
        ):

            banca["gale"] = (
                gale_atual + 1
            )

        else:

            banca["gale"] = 0

    # PUSH:
    # mantém o nível atual.

    banca["ocupada"] = False
    banca["sinal_id"] = None

    salvar_bancas()


# ============================================================
# ATUALIZAR ESTATÍSTICAS
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

    chave = (
        "wins"
        if resultado == "WIN"
        else
        "losses"
        if resultado == "LOSS"
        else
        "pushes"
    )

    stats[
        "por_gale"
    ][gale][chave] += 1

    salvar_json(
        ARQUIVO_STATS,
        stats
    )


# ============================================================
# MENSAGEM DE RESULTADO
# ============================================================

def enviar_resultado(
    sinal,
    resultados,
    resultado_final
):

    jogo1 = sinal[
        "jogos"
    ][0]

    jogo2 = sinal[
        "jogos"
    ][1]

    r1 = resultados[0]
    r2 = resultados[1]

    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "⚪"
    }.get(
        resultado_final,
        "ℹ️"
    )

    mensagem = (

        f"{emoji} <b>RESULTADO V19 — "
        f"{resultado_final}</b>\n\n"


        "⚽ <b>JOGO 1</b>\n"

        f"{html.escape(jogo1['home'])}"
        f" x "
        f"{html.escape(jogo1['away'])}\n"

        f"1️⃣ Under "
        f"{jogo1['linha_under_ht']:.1f} HT: "
        f"<b>{r1['under']}</b> "
        f"({r1['cantos_ht']:.0f} cantos)\n"

        f"2️⃣ Over "
        f"{jogo1['linha_over_ft']:.1f} FT: "
        f"<b>{r1['over']}</b> "
        f"({r1['cantos_ft']:.0f} cantos)\n\n"


        "⚽ <b>JOGO 2</b>\n"

        f"{html.escape(jogo2['home'])}"
        f" x "
        f"{html.escape(jogo2['away'])}\n"

        f"3️⃣ Under "
        f"{jogo2['linha_under_ht']:.1f} HT: "
        f"<b>{r2['under']}</b> "
        f"({r2['cantos_ht']:.0f} cantos)\n"

        f"4️⃣ Over "
        f"{jogo2['linha_over_ft']:.1f} FT: "
        f"<b>{r2['over']}</b> "
        f"({r2['cantos_ft']:.0f} cantos)\n\n"


        f"🔥 Resultado da combinação: "
        f"<b>{resultado_final}</b>\n\n"

        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale utilizado: "
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

        jogos = sinal.get(
            "jogos",
            []
        )

        if len(jogos) != 2:
            continue

        resultados = []

        todos_finalizados = True

        for jogo in jogos:

            fixture = buscar_fixture(
                jogo[
                    "fixture_id"
                ]
            )

            if not fixture:

                todos_finalizados = False
                break

            if not fixture_finalizado(
                fixture
            ):

                todos_finalizados = False
                break

            resultado = resolver_jogo(
                jogo,
                fixture
            )

            if not resultado:

                todos_finalizados = False
                break

            resultados.append(
                resultado
            )


        if not todos_finalizados:
            continue


        resultado_final = (
            calcular_resultado_sinal(
                resultados
            )
        )


        with lock_dados:

            sinal[
                "status"
            ] = "RESOLVIDO"

            sinal[
                "resultado"
            ] = resultado_final

            sinal[
                "resultados_jogos"
            ] = resultados

            sinal[
                "resolvido_em"
            ] = agora_iso()


            registrar_resultado_stats(
                sinal,
                resultado_final
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
                    resultado_final
                )


            salvar_json(
                ARQUIVO_SINAIS,
                sinais
            )


        enviar_resultado(
            sinal,
            resultados,
            resultado_final
        )


# ============================================================
# LIMPAR FILA
#
# Remove jogos que já começaram ou desapareceram da janela
# depois de algumas horas.
# ============================================================

def limpar_fila():

    agora = datetime.now(
        timezone.utc
    )

    nova_fila = []

    for item in fila_aprovados:

        aprovado_em = item.get(
            "aprovado_em"
        )

        manter = True

        if aprovado_em:

            try:

                data = datetime.fromisoformat(
                    aprovado_em
                )

                idade = (
                    agora - data
                ).total_seconds()

                # Evita deixar candidato velho
                # eternamente na fila.
                if idade > (
                    JANELA_HORAS
                    * 3600
                ):
                    manter = False

            except Exception:
                pass

        if manter:
            nova_fila.append(
                item
            )


    if (
        len(nova_fila)
        != len(fila_aprovados)
    ):

        fila_aprovados[:] = (
            nova_fila
        )

        salvar_json(
            ARQUIVO_FILA,
            fila_aprovados
        )


# ============================================================
# CICLO
# ============================================================

def ciclo():

    global cursor_fixture

    logger.info(
        "================================"
    )

    logger.info(
        "CICLO V19 | "
        "Fila=%s | "
        "Bancas livres=%s",
        len(fila_aprovados),
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

        # Primeiro resolve sinais.
        verificar_resultados()

        limpar_fila()

        # Se já houver pares na fila,
        # cria sinais antes de gastar API.
        while (
            len(fila_aprovados) >= 2
            and
            obter_banca_livre()
            is not None
        ):

            if not tentar_criar_sinal():
                break


        # Sem banca livre:
        # não precisamos procurar
        # novos sinais.
        if (
            obter_banca_livre()
            is None
        ):

            logger.info(
                "Todas as bancas ocupadas"
            )

            return


        fixtures = buscar_fixtures()

        if not fixtures:

            logger.info(
                "Nenhum fixture"
            )

            return


        total = len(
            fixtures
        )

        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )


        for _ in range(
            quantidade
        ):

            # Permite continuar procurando
            # candidatos enquanto houver
            # possibilidade de formar sinais.
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


            # Caso tenha formado sinal e
            # todas as bancas tenham ocupado.
            if (
                obter_banca_livre()
                is None
            ):
                break


    except Exception:

        logger.exception(
            "Erro geral ciclo V19"
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "ROBÔ V19 INICIADO"
    )

    logger.info(
        "Estratégia: "
        "2 jogos / 4 mercados"
    )

    logger.info(
        "Under HT = média + %.1f",
        MARGEM_HT
    )

    logger.info(
        "Over FT = média - %.1f",
        MARGEM_FT
    )

    logger.info(
        "Filtros: Under >= %.1f%% | "
        "Over >= %.1f%%",
        MIN_TAXA_UNDER_HT * 100,
        MIN_TAXA_OVER_FT * 100
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
            "V19",

        "estrategia":
            "2 jogos / 4 mercados",

        "mercados_por_sinal":
            4,

        "jogos_por_sinal":
            2,

        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "min_taxa_under_ht":
            MIN_TAXA_UNDER_HT,

        "min_taxa_over_ft":
            MIN_TAXA_OVER_FT,

        "fila_aprovados":
            len(fila_aprovados),

        "assertividade":
            round(
                grupo[
                    "assertividade"
                ] * 100,
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

        "online":
            True,

        "versao":
            "V19",

        "estrategia":
            "2 jogos / 4 mercados",

        "under_ht":
            "media HT + margem",

        "over_ft":
            "media FT - margem",

        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "taxa_min_under":
            MIN_TAXA_UNDER_HT,

        "taxa_min_over":
            MIN_TAXA_OVER_FT,

        "historico_por_time":
            QTD_HISTORICO_TIME,

        "min_jogos":
            MIN_JOGOS_HISTORICO,

        "bancas":
            TOTAL_BANCAS,

        "max_gales":
            MAX_GALES
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


@app.route("/fila")
def rota_fila():

    return jsonify(
        fila_aprovados
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
        "version": "V19"
    })


# ============================================================
# THREAD
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v19"
)

thread_robo.start()


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
