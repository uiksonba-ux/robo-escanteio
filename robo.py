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
# ROBÔ V19.2
#
# 1 SINAL = 1 JOGO = 3 MERCADOS
#
# 1) UNDER ESCANTEIOS HT
#    linha = média HT + margem
#
# 2) OVER ESCANTEIOS FT
#    linha = média FT - margem
#
# 3) UNDER GOLS FT
#    linha = média gols FT + margem
#
# Cada mercado precisa atingir a taxa mínima individual.
#
# A combinação histórica dos 3 mercados é INFORMATIVA.
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v19-2")


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

# UNDER ESCANTEIOS HT = média + 2
MARGEM_HT = float(
    os.getenv("MARGEM_HT", "2.0")
)

# OVER ESCANTEIOS FT = média - 2
MARGEM_FT = float(
    os.getenv("MARGEM_FT", "2.0")
)

# UNDER GOLS FT = média + 2
MARGEM_GOLS = float(
    os.getenv("MARGEM_GOLS", "2.0")
)


# ============================================================
# LIMITES DAS LINHAS
# ============================================================

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

MIN_LINHA_UNDER_GOLS = float(
    os.getenv("MIN_LINHA_UNDER_GOLS", "1.5")
)

MAX_LINHA_UNDER_GOLS = float(
    os.getenv("MAX_LINHA_UNDER_GOLS", "6.5")
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
#
# Mantidos os nomes V19 para preservar estatísticas/bancas.
# ============================================================

ARQUIVO_STATS = "stats_v19.json"
ARQUIVO_SINAIS = "sinais_v19.json"
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
        "User-Agent": "robo-v19-2/1.0"
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
# CACHE FIXTURES
# ============================================================

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

        with open(
            caminho,
            "r",
            encoding="utf-8"
        ) as arquivo:

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
        min(
            valor,
            maximo
        )
    )


def arredondar_cima_meio(valor):

    return math.ceil(
        valor * 2
    ) / 2


def arredondar_baixo_meio(valor):

    return math.floor(
        valor * 2
    ) / 2


# ============================================================
# LINHAS DINÂMICAS
# ============================================================

def criar_linha_under_ht(media):

    linha = arredondar_cima_meio(
        media + MARGEM_HT
    )

    return limitar(
        linha,
        MIN_LINHA_UNDER_HT,
        MAX_LINHA_UNDER_HT
    )


def criar_linha_over_ft(media):

    linha = arredondar_baixo_meio(
        media - MARGEM_FT
    )

    return limitar(
        linha,
        MIN_LINHA_OVER_FT,
        MAX_LINHA_OVER_FT
    )


def criar_linha_under_gols(media):

    linha = arredondar_cima_meio(
        media + MARGEM_GOLS
    )

    return limitar(
        linha,
        MIN_LINHA_UNDER_GOLS,
        MAX_LINHA_UNDER_GOLS
    )


# ============================================================
# EXTRAIR LISTA API
# ============================================================

def extrair_lista(dados):

    if isinstance(
        dados,
        list
    ):
        return dados

    if not isinstance(
        dados,
        dict
    ):
        return []

    for chave in (
        "data",
        "fixtures",
        "results",
        "response"
    ):

        valor = dados.get(
            chave
        )

        if isinstance(
            valor,
            list
        ):
            return valor

        if isinstance(
            valor,
            dict
        ):

            for sub in (
                "data",
                "fixtures",
                "results",
                "response"
            ):

                lista = valor.get(
                    sub
                )

                if isinstance(
                    lista,
                    list
                ):
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
                max(
                    1,
                    espera
                )
            )

        agora = time.time()

        intervalo = (
            agora
            - ultima_requisicao
        )

        if (
            intervalo
            < API_MIN_INTERVAL_SECONDS
        ):

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
                "chat_id":
                    CHAT_ID,

                "text":
                    mensagem,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True
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
# ASSERTIVIDADE DO GRUPO
# ============================================================

def estatisticas_grupo():

    wins = int(
        stats.get(
            "wins",
            0
        )
    )

    losses = int(
        stats.get(
            "losses",
            0
        )
    )

    pushes = int(
        stats.get(
            "pushes",
            0
        )
    )

    decisoes = (
        wins
        + losses
    )

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
            .get(
                "por_gale",
                {}
            )
            .get(
                str(gale),
                {}
            )
        )

        w = int(
            dados.get(
                "wins",
                0
            )
        )

        l = int(
            dados.get(
                "losses",
                0
            )
        )

        p = int(
            dados.get(
                "pushes",
                0
            )
        )

        total = (
            w + l
        )

        por_gale[
            str(gale)
        ] = {

            "wins":
                w,

            "losses":
                l,

            "pushes":
                p,

            "taxa":
                (
                    w / total
                    if total
                    else 0
                )
        }

    return {

        "wins":
            wins,

        "losses":
            losses,

        "pushes":
            pushes,

        "assertividade":
            geral,

        "resolvidos":
            wins + losses + pushes,

        "por_gale":
            por_gale
    }


def texto_assertividade():

    dados = estatisticas_grupo()

    if (
        dados["wins"]
        + dados["losses"]
        == 0
    ):

        geral = (
            "Aguardando resultados"
        )

    else:

        geral = pct(
            dados[
                "assertividade"
            ]
        )

    linhas = [
        "━━━━━━━━━━━━━━━━━━",
        "📊 <b>ASSERTIVIDADE V19.2</b>",
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
            pct(
                g["taxa"]
            )
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
                int(
                    inicio.timestamp()
                ),

            "end_time":
                int(
                    fim.timestamp()
                ),

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
            jogo.get(
                "kickoff_ts"
            )
            or jogo.get(
                "start_time"
            )
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
                "fase":
                    fase,

                "linha":
                    linha,

                "over":
                    over,

                "under":
                    under
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


# ============================================================
# VERIFICAR MERCADOS BET365
#
# Mantém validação de cantos da V19
# e acrescenta mercado de gols FT.
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
        ].get(
            chave_cache
        )
    )

    if item:

        idade = (
            agora
            - item.get(
                "timestamp",
                0
            )
        )

        dados_cache = item.get(
            "dados",
            {}
        )

        # Cache antigo da V19 não tinha gols.
        # Só reutilizamos se já possuir gols_ft.
        if (
            idade < CACHE_MERCADO_TTL
            and
            "gols_ft" in dados_cache
        ):

            return dados_cache


    # ========================================================
    # ESCANTEIOS FT
    # ========================================================

    ft = consultar_mercado_bet365(
        fixture_id,
        "corner",
        "corner_line"
    )

    if not ft:

        resultado = {
            "ft": False,
            "ht": False,
            "gols_ft": False,
            "ft_detalhes": None,
            "ht_detalhes": None,
            "gols_ft_detalhes": None
        }

    else:

        # ====================================================
        # ESCANTEIOS HT
        # ====================================================

        ht = consultar_mercado_bet365(
            fixture_id,
            "corner_half",
            "corner_line_half"
        )

        if not ht:

            resultado = {
                "ft": True,
                "ht": False,
                "gols_ft": False,
                "ft_detalhes": ft,
                "ht_detalhes": None,
                "gols_ft_detalhes": None
            }

        else:

            # ================================================
            # GOLS FT
            # ================================================

            gols_ft = consultar_mercado_bet365(
                fixture_id,
                "goalline",
                "goal_line"
            )

            resultado = {
                "ft": True,
                "ht": True,
                "gols_ft":
                    gols_ft is not None,

                "ft_detalhes":
                    ft,

                "ht_detalhes":
                    ht,

                "gols_ft_detalhes":
                    gols_ft
            }

    cache_persistente[
        "mercados"
    ][chave_cache] = {
        "timestamp":
            agora,

        "dados":
            resultado
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
        ].get(
            chave
        )
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
        "timestamp":
            agora,

        "dados":
            jogos
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
# ESCANTEIOS
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
        corners.get(
            "home"
        )
    )

    ft_away = numero(
        corners.get(
            "away"
        )
    )

    ht_home = numero(
        corners.get(
            "half_home"
        )
    )

    ht_away = numero(
        corners.get(
            "half_away"
        )
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
            ht_home
            + ht_away,

        "ft":
            ft_home
            + ft_away
    }


# ============================================================
# GOLS FT
#
# Aceita diferentes estruturas possíveis de fixture.
# ============================================================

def extrair_gols(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return None


    # ========================================================
    # FORMATO:
    # goals: {home: X, away: Y}
    # ========================================================

    goals = jogo.get(
        "goals"
    )

    if isinstance(
        goals,
        dict
    ):

        home = numero(
            goals.get(
                "home"
            )
        )

        away = numero(
            goals.get(
                "away"
            )
        )

        if (
            home is not None
            and away is not None
        ):

            return (
                home + away
            )


    # ========================================================
    # FORMATO:
    # score: {fulltime: {home: X, away: Y}}
    # ========================================================

    score = jogo.get(
        "score"
    )

    if isinstance(
        score,
        dict
    ):

        fulltime = (
            score.get(
                "fulltime"
            )
            or score.get(
                "full_time"
            )
        )

        if isinstance(
            fulltime,
            dict
        ):

            home = numero(
                fulltime.get(
                    "home"
                )
            )

            away = numero(
                fulltime.get(
                    "away"
                )
            )

            if (
                home is not None
                and away is not None
            ):

                return (
                    home + away
                )


    # ========================================================
    # FORMATO:
    # scores: {fulltime: {home: X, away: Y}}
    # ========================================================

    scores = jogo.get(
        "scores"
    )

    if isinstance(
        scores,
        dict
    ):

        fulltime = (
            scores.get(
                "fulltime"
            )
            or scores.get(
                "full_time"
            )
        )

        if isinstance(
            fulltime,
            dict
        ):

            home = numero(
                fulltime.get(
                    "home"
                )
            )

            away = numero(
                fulltime.get(
                    "away"
                )
            )

            if (
                home is not None
                and away is not None
            ):

                return (
                    home + away
                )


    # ========================================================
    # FORMATO DIRETO:
    # home_score / away_score
    # ========================================================

    home = numero(
        jogo.get(
            "home_score"
        )
    )

    away = numero(
        jogo.get(
            "away_score"
        )
    )

    if (
        home is not None
        and away is not None
    ):

        return (
            home + away
        )


    return None


# ============================================================
# REGISTRO COMPLETO
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

    # Para entrar no histórico da V19.2,
    # precisa ter dados dos 3 mercados.
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

    media_gols = (
        sum(
            r["gols_ft"]
            for r in registros
        )
        / total
    )


    # ========================================================
    # LINHAS DINÂMICAS
    # ========================================================

    linha_under_ht = (
        criar_linha_under_ht(
            media_ht
        )
    )

    linha_over_ft = (
        criar_linha_over_ft(
            media_ft
        )
    )

    linha_under_gols = (
        criar_linha_under_gols(
            media_gols
        )
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

    comb_wins = 0
    comb_voids = 0
    comb_losses = 0


    # ========================================================
    # BACKTEST
    # ========================================================

    for registro in registros:

        under_ht = resultado_under(
            registro[
                "cantos_ht"
            ],
            linha_under_ht
        )

        over_ft = resultado_over(
            registro[
                "cantos_ft"
            ],
            linha_over_ft
        )

        under_gols = resultado_under(
            registro[
                "gols_ft"
            ],
            linha_under_gols
        )


        # ====================================================
        # UNDER CANTOS HT
        # ====================================================

        if under_ht == "WIN":

            under_ht_wins += 1

        elif under_ht == "VOID":

            under_ht_voids += 1

        else:

            under_ht_losses += 1


        # ====================================================
        # OVER CANTOS FT
        # ====================================================

        if over_ft == "WIN":

            over_ft_wins += 1

        elif over_ft == "VOID":

            over_ft_voids += 1

        else:

            over_ft_losses += 1


        # ====================================================
        # UNDER GOLS FT
        # ====================================================

        if under_gols == "WIN":

            under_gols_wins += 1

        elif under_gols == "VOID":

            under_gols_voids += 1

        else:

            under_gols_losses += 1


        # ====================================================
        # COMBINAÇÃO DOS 3
        # ====================================================

        pernas = [
            under_ht,
            over_ft,
            under_gols
        ]

        if "LOSS" in pernas:

            comb_losses += 1

        elif "VOID" in pernas:

            comb_voids += 1

        else:

            comb_wins += 1


    # ========================================================
    # TAXAS
    #
    # VOID continua no denominador, igual à V19 original.
    # ========================================================

    taxa_under_ht = taxa(
        under_ht_wins,
        total
    )

    taxa_over_ft = taxa(
        over_ft_wins,
        total
    )

    taxa_under_gols = taxa(
        under_gols_wins,
        total
    )

    taxa_combinada = taxa(
        comb_wins,
        total
    )


    return {

        "total":
            total,


        # MÉDIAS

        "media_ht":
            media_ht,

        "media_ft":
            media_ft,

        "media_gols":
            media_gols,


        # LINHAS

        "linha_under_ht":
            linha_under_ht,

        "linha_over_ft":
            linha_over_ft,

        "linha_under_gols":
            linha_under_gols,


        # MARGENS REAIS APÓS ARREDONDAMENTO

        "margem_ht":
            linha_under_ht
            - media_ht,

        "margem_ft":
            media_ft
            - linha_over_ft,

        "margem_gols":
            linha_under_gols
            - media_gols,


        # UNDER CANTOS HT

        "under_ht_wins":
            under_ht_wins,

        "under_ht_voids":
            under_ht_voids,

        "under_ht_losses":
            under_ht_losses,

        "taxa_under_ht":
            taxa_under_ht,


        # OVER CANTOS FT

        "over_ft_wins":
            over_ft_wins,

        "over_ft_voids":
            over_ft_voids,

        "over_ft_losses":
            over_ft_losses,

        "taxa_over_ft":
            taxa_over_ft,


        # UNDER GOLS FT

        "under_gols_wins":
            under_gols_wins,

        "under_gols_voids":
            under_gols_voids,

        "under_gols_losses":
            under_gols_losses,

        "taxa_under_gols":
            taxa_under_gols,


        # CONJUNTA

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

    # Mínimo de jogos válidos
    if (
        analise["total"]
        < MIN_JOGOS_HISTORICO
    ):

        return False


    # UNDER ESCANTEIOS HT >= 70%
    if (
        analise[
            "taxa_under_ht"
        ]
        < MIN_TAXA_UNDER_HT
    ):

        return False


    # OVER ESCANTEIOS FT >= 70%
    if (
        analise[
            "taxa_over_ft"
        ]
        < MIN_TAXA_OVER_FT
    ):

        return False


    # UNDER GOLS FT >= 70%
    if (
        analise[
            "taxa_under_gols"
        ]
        < MIN_TAXA_UNDER_GOLS
    ):

        return False


    # IMPORTANTE:
    # taxa_combinada NÃO reprova.
    # Continua apenas informativa.

    return True


# ============================================================
# JOGO JÁ UTILIZADO
#
# Compatível tanto com sinais antigos da V19 (2 jogos)
# quanto com novos sinais da V19.2 (1 jogo).
# ============================================================

def fixture_ja_usado(
    fixture_id
):

    fixture_id = str(
        fixture_id
    )

    for sinal in sinais:

        # ====================================================
        # V19 ANTIGA
        # ====================================================

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


        # ====================================================
        # V19.2
        # ====================================================

        jogo = sinal.get(
            "jogo"
        )

        if isinstance(
            jogo,
            dict
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


# ============================================================
# CRIAR SINAL
#
# AGORA NÃO EXISTE MAIS FILA.
# UM JOGO APROVADO = UM SINAL.
# ============================================================

def criar_sinal(
    fixture,
    analise,
    mercados
):

    with lock_dados:

        banca = obter_banca_livre()

        if not banca:

            logger.info(
                "Sem banca livre"
            )

            return False


        fixture_id = str(
            fixture.get(
                "id"
            )
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


        jogo = {

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


            # ================================================
            # LINHAS
            # ================================================

            "linha_under_ht":
                analise[
                    "linha_under_ht"
                ],

            "linha_over_ft":
                analise[
                    "linha_over_ft"
                ],

            "linha_under_gols":
                analise[
                    "linha_under_gols"
                ],


            # ================================================
            # MÉDIAS
            # ================================================

            "media_ht":
                analise[
                    "media_ht"
                ],

            "media_ft":
                analise[
                    "media_ft"
                ],

            "media_gols":
                analise[
                    "media_gols"
                ],


            # ================================================
            # TAXAS
            # ================================================

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

            "taxa_combinada":
                analise[
                    "taxa_combinada"
                ],


            # ================================================
            # CONTAGENS UNDER CANTOS
            # ================================================

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


            # ================================================
            # CONTAGENS OVER CANTOS
            # ================================================

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


            # ================================================
            # CONTAGENS UNDER GOLS
            # ================================================

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


            # ================================================
            # CONJUNTA
            # ================================================

            "comb_wins":
                analise[
                    "comb_wins"
                ],

            "comb_voids":
                analise[
                    "comb_voids"
                ],

            "comb_losses":
                analise[
                    "comb_losses"
                ],


            "amostra":
                analise[
                    "total"
                ],


            # ================================================
            # BET365
            # ================================================

            "bet365_ht":
                mercados.get(
                    "ht_detalhes"
                ),

            "bet365_ft":
                mercados.get(
                    "ft_detalhes"
                ),

            "bet365_gols_ft":
                mercados.get(
                    "gols_ft_detalhes"
                )
        }


        sinal = {

            "id":
                sinal_id,

            "versao":
                "V19.2",

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

            "jogo":
                jogo
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

        salvar_bancas()


    enviar_mensagem_sinal(
        sinal
    )

    return True


# ============================================================
# TELEGRAM - SINAL
# ============================================================

def enviar_mensagem_sinal(
    sinal
):

    jogo = sinal[
        "jogo"
    ]


    ht_api = (
        jogo.get(
            "bet365_ht"
        )
        or {}
    )

    ft_api = (
        jogo.get(
            "bet365_ft"
        )
        or {}
    )

    gols_api = (
        jogo.get(
            "bet365_gols_ft"
        )
        or {}
    )


    mensagem = (

        "🚨 <b>SINAL V19.2 — "
        "1 JOGO / 3 MERCADOS</b>\n\n"


        f"⚽ <b>"
        f"{html.escape(jogo['home'])}"
        f" x "
        f"{html.escape(jogo['away'])}"
        f"</b>\n\n"


        # ====================================================
        # 1 - UNDER CANTOS HT
        # ====================================================

        "1️⃣ <b>ESCANTEIOS HT</b>\n"

        f"🎯 Under "
        f"<b>{jogo['linha_under_ht']:.1f}</b>\n"

        f"📊 Média HT: "
        f"{jogo['media_ht']:.1f}\n"

        f"📐 Margem: +{MARGEM_HT:.1f}\n"

        f"📈 Histórico: "
        f"<b>{pct(jogo['taxa_under_ht'])}</b>\n"

        f"✅ {jogo['under_ht_wins']} WIN | "
        f"⚪ {jogo['under_ht_voids']} VOID | "
        f"❌ {jogo['under_ht_losses']} LOSS\n\n"


        # ====================================================
        # 2 - OVER CANTOS FT
        # ====================================================

        "2️⃣ <b>ESCANTEIOS FT</b>\n"

        f"🎯 Over "
        f"<b>{jogo['linha_over_ft']:.1f}</b>\n"

        f"📊 Média FT: "
        f"{jogo['media_ft']:.1f}\n"

        f"📐 Margem: -{MARGEM_FT:.1f}\n"

        f"📈 Histórico: "
        f"<b>{pct(jogo['taxa_over_ft'])}</b>\n"

        f"✅ {jogo['over_ft_wins']} WIN | "
        f"⚪ {jogo['over_ft_voids']} VOID | "
        f"❌ {jogo['over_ft_losses']} LOSS\n\n"


        # ====================================================
        # 3 - UNDER GOLS
        # ====================================================

        "3️⃣ <b>GOLS FT</b>\n"

        f"🎯 Under "
        f"<b>{jogo['linha_under_gols']:.1f}</b>\n"

        f"📊 Média de gols: "
        f"{jogo['media_gols']:.1f}\n"

        f"📐 Margem: +{MARGEM_GOLS:.1f}\n"

        f"📈 Histórico: "
        f"<b>{pct(jogo['taxa_under_gols'])}</b>\n"

        f"✅ {jogo['under_gols_wins']} WIN | "
        f"⚪ {jogo['under_gols_voids']} VOID | "
        f"❌ {jogo['under_gols_losses']} LOSS\n\n"


        # ====================================================
        # CONJUNTA
        # ====================================================

        "━━━━━━━━━━━━━━━━━━\n"

        "🔥 <b>COMBINAÇÃO — 3 MERCADOS</b>\n\n"

        f"1️⃣ Under "
        f"{jogo['linha_under_ht']:.1f} "
        f"escanteios HT\n"

        f"2️⃣ Over "
        f"{jogo['linha_over_ft']:.1f} "
        f"escanteios FT\n"

        f"3️⃣ Under "
        f"{jogo['linha_under_gols']:.1f} "
        f"gols FT\n\n"

        f"📊 Ocorrência conjunta histórica: "
        f"<b>{pct(jogo['taxa_combinada'])}</b>\n"

        f"📚 Amostra válida: "
        f"{jogo['amostra']} jogos\n"

        "ℹ️ Conjunta apenas informativa\n\n"


        # ====================================================
        # BET365
        # ====================================================

        "🎰 <b>BET365 API</b>\n"

        f"↳ Linha principal cantos HT: "
        f"{ht_api.get('linha', '-')}\n"

        f"↳ Linha principal cantos FT: "
        f"{ft_api.get('linha', '-')}\n"

        f"↳ Linha principal gols FT: "
        f"{gols_api.get('linha', '-')}\n\n"


        # ====================================================
        # GESTÃO
        # ====================================================

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
            "time sem ID",
            home,
            away
        )

        return


    logger.info(
        "V19.2 analisando %s x %s",
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
            "REPROVADO %s x %s | "
            "sem mercado cantos FT Bet365",
            home,
            away
        )

        return


    if not mercados.get(
        "ht"
    ):

        logger.info(
            "REPROVADO %s x %s | "
            "sem mercado cantos HT Bet365",
            home,
            away
        )

        return


    if not mercados.get(
        "gols_ft"
    ):

        logger.info(
            "REPROVADO %s x %s | "
            "sem mercado gols FT Bet365",
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

        logger.info(
            "REPROVADO %s x %s | "
            "sem histórico completo",
            home,
            away
        )

        return


    # ========================================================
    # LOG COMPLETO
    # ========================================================

    logger.info(
        "%s x %s | "
        "N=%s | "
        "Under cantos HT %.1f = %.1f%% | "
        "Over cantos FT %.1f = %.1f%% | "
        "Under gols %.1f = %.1f%% | "
        "Conjunta = %.1f%%",
        home,
        away,
        analise["total"],
        analise["linha_under_ht"],
        analise["taxa_under_ht"] * 100,
        analise["linha_over_ft"],
        analise["taxa_over_ft"] * 100,
        analise["linha_under_gols"],
        analise["taxa_under_gols"] * 100,
        analise["taxa_combinada"] * 100
    )


    # ========================================================
    # FILTROS
    # ========================================================

    if not passou_filtros(
        analise
    ):

        logger.info(
            "REPROVADO V19.2 | "
            "%s x %s | "
            "HT %.1f%% | "
            "FT %.1f%% | "
            "GOLS %.1f%%",
            home,
            away,
            analise["taxa_under_ht"] * 100,
            analise["taxa_over_ft"] * 100,
            analise["taxa_under_gols"] * 100
        )

        return


    # ========================================================
    # APROVADO
    # ========================================================

    logger.info(
        "APROVADO V19.2 | "
        "%s x %s",
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
        isinstance(
            response,
            list
        )
        and response
    ):

        return response[0]


    return dados


# ============================================================
# FIXTURE FINALIZADO
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
# RESOLVER JOGO
# ============================================================

def resolver_jogo(
    jogo,
    fixture
):

    cantos = extrair_cantos(
        fixture
    )

    gols = extrair_gols(
        fixture
    )


    if not cantos:
        return None

    if gols is None:
        return None


    # ========================================================
    # 1 - UNDER CANTOS HT
    # ========================================================

    under_ht = resultado_under(
        cantos["ht"],
        float(
            jogo[
                "linha_under_ht"
            ]
        )
    )


    # ========================================================
    # 2 - OVER CANTOS FT
    # ========================================================

    over_ft = resultado_over(
        cantos["ft"],
        float(
            jogo[
                "linha_over_ft"
            ]
        )
    )


    # ========================================================
    # 3 - UNDER GOLS FT
    # ========================================================

    under_gols = resultado_under(
        gols,
        float(
            jogo[
                "linha_under_gols"
            ]
        )
    )


    return {

        "under_ht":
            under_ht,

        "over_ft":
            over_ft,

        "under_gols":
            under_gols,

        "cantos_ht":
            cantos["ht"],

        "cantos_ft":
            cantos["ft"],

        "gols_ft":
            gols
    }


# ============================================================
# RESULTADO FINAL DAS 3 PERNAS
#
# Mantendo comportamento conservador da V19:
#
# qualquer LOSS = LOSS
# qualquer VOID = PUSH
# todas WIN = WIN
# ============================================================

def calcular_resultado_sinal(
    resultado
):

    pernas = [
        resultado[
            "under_ht"
        ],

        resultado[
            "over_ft"
        ],

        resultado[
            "under_gols"
        ]
    ]


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

        banca[
            "gale"
        ] = 0


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

            banca[
                "gale"
            ] = (
                gale_atual
                + 1
            )

        else:

            banca[
                "gale"
            ] = 0


    # PUSH mantém o Gale atual.

    banca[
        "ocupada"
    ] = False

    banca[
        "sinal_id"
    ] = None


    salvar_bancas()


# ============================================================
# ATUALIZAR ESTATÍSTICAS
# ============================================================

def registrar_resultado_stats(
    sinal,
    resultado
):

    if resultado == "WIN":

        stats[
            "wins"
        ] = (
            int(
                stats.get(
                    "wins",
                    0
                )
            )
            + 1
        )


    elif resultado == "LOSS":

        stats[
            "losses"
        ] = (
            int(
                stats.get(
                    "losses",
                    0
                )
            )
            + 1
        )


    else:

        stats[
            "pushes"
        ] = (
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
# TELEGRAM - RESULTADO
# ============================================================

def enviar_resultado(
    sinal,
    resultado,
    resultado_final
):

    jogo = sinal[
        "jogo"
    ]


    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "⚪"
    }.get(
        resultado_final,
        "ℹ️"
    )


    mensagem = (

        f"{emoji} <b>RESULTADO V19.2 — "
        f"{resultado_final}</b>\n\n"


        f"⚽ <b>"
        f"{html.escape(jogo['home'])}"
        f" x "
        f"{html.escape(jogo['away'])}"
        f"</b>\n\n"


        # ====================================================
        # UNDER HT
        # ====================================================

        "1️⃣ <b>ESCANTEIOS HT</b>\n"

        f"🎯 Under "
        f"{jogo['linha_under_ht']:.1f}\n"

        f"🚩 Resultado: "
        f"{resultado['cantos_ht']:.0f} cantos\n"

        f"📌 <b>"
        f"{resultado['under_ht']}"
        f"</b>\n\n"


        # ====================================================
        # OVER FT
        # ====================================================

        "2️⃣ <b>ESCANTEIOS FT</b>\n"

        f"🎯 Over "
        f"{jogo['linha_over_ft']:.1f}\n"

        f"🚩 Resultado: "
        f"{resultado['cantos_ft']:.0f} cantos\n"

        f"📌 <b>"
        f"{resultado['over_ft']}"
        f"</b>\n\n"


        # ====================================================
        # UNDER GOLS
        # ====================================================

        "3️⃣ <b>GOLS FT</b>\n"

        f"🎯 Under "
        f"{jogo['linha_under_gols']:.1f}\n"

        f"⚽ Resultado: "
        f"{resultado['gols_ft']:.0f} gols\n"

        f"📌 <b>"
        f"{resultado['under_gols']}"
        f"</b>\n\n"


        "━━━━━━━━━━━━━━━━━━\n"

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
#
# Também ignora sinais antigos da V19 de 2 jogos para não
# tentar processá-los com a estrutura nova.
# ============================================================

def verificar_resultados():

    pendentes = [
        sinal
        for sinal in sinais
        if (
            sinal.get(
                "status"
            )
            == "PENDENTE"
            and
            sinal.get(
                "versao"
            )
            == "V19.2"
        )
    ]


    for sinal in pendentes:

        jogo = sinal.get(
            "jogo"
        )


        if not isinstance(
            jogo,
            dict
        ):

            continue


        fixture = buscar_fixture(
            jogo[
                "fixture_id"
            ]
        )


        if not fixture:
            continue


        if not fixture_finalizado(
            fixture
        ):
            continue


        resultado = resolver_jogo(
            jogo,
            fixture
        )


        if not resultado:

            logger.info(
                "Fixture %s finalizado "
                "mas sem dados completos",
                jogo[
                    "fixture_id"
                ]
            )

            continue


        resultado_final = (
            calcular_resultado_sinal(
                resultado
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
                "resultado_jogo"
            ] = resultado

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
                    if b.get(
                        "id"
                    )
                    == sinal.get(
                        "banca"
                    )
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
            resultado,
            resultado_final
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
        "CICLO V19.2 | "
        "Bancas livres=%s",
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
        # PRIMEIRO RESOLVE
        # ====================================================

        verificar_resultados()


        # ====================================================
        # SEM BANCA LIVRE
        # ====================================================

        if (
            obter_banca_livre()
            is None
        ):

            logger.info(
                "Todas as bancas ocupadas"
            )

            return


        # ====================================================
        # FIXTURES
        # ====================================================

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


        # ====================================================
        # ANALISA PARTIDAS
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


            if (
                obter_banca_livre()
                is None
            ):

                break


    except Exception:

        logger.exception(
            "Erro geral ciclo V19.2"
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "================================"
    )

    logger.info(
        "ROBÔ V19.2 INICIADO"
    )

    logger.info(
        "Estratégia: "
        "1 jogo / 3 mercados"
    )

    logger.info(
        "Under cantos HT = "
        "média + %.1f",
        MARGEM_HT
    )

    logger.info(
        "Over cantos FT = "
        "média - %.1f",
        MARGEM_FT
    )

    logger.info(
        "Under gols FT = "
        "média + %.1f",
        MARGEM_GOLS
    )

    logger.info(
        "Filtros: "
        "Under HT >= %.1f%% | "
        "Over FT >= %.1f%% | "
        "Under gols >= %.1f%%",
        MIN_TAXA_UNDER_HT * 100,
        MIN_TAXA_OVER_FT * 100,
        MIN_TAXA_UNDER_GOLS * 100
    )

    logger.info(
        "================================"
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
            "V19.2",

        "estrategia":
            "1 jogo / 3 mercados",

        "jogos_por_sinal":
            1,

        "mercados_por_sinal":
            3,

        "mercados": [
            "Under escanteios HT",
            "Over escanteios FT",
            "Under gols FT"
        ],


        # MARGENS

        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "margem_gols":
            MARGEM_GOLS,


        # FILTROS

        "min_taxa_under_ht":
            MIN_TAXA_UNDER_HT,

        "min_taxa_over_ft":
            MIN_TAXA_OVER_FT,

        "min_taxa_under_gols":
            MIN_TAXA_UNDER_GOLS,


        # HISTÓRICO

        "historico_por_time":
            QTD_HISTORICO_TIME,

        "min_jogos":
            MIN_JOGOS_HISTORICO,


        # STATS

        "assertividade":
            round(
                grupo[
                    "assertividade"
                ] * 100,
                2
            ),

        "wins":
            grupo[
                "wins"
            ],

        "losses":
            grupo[
                "losses"
            ],

        "pushes":
            grupo[
                "pushes"
            ],


        # BANCAS

        "bancas":
            bancas
    })


@app.route("/status")
def status():

    return jsonify({

        "online":
            True,

        "versao":
            "V19.2",

        "estrategia":
            "1 jogo / 3 mercados",

        "jogos_por_sinal":
            1,

        "mercados_por_sinal":
            3,


        "under_cantos_ht":
            "media HT + margem",

        "over_cantos_ft":
            "media FT - margem",

        "under_gols_ft":
            "media gols FT + margem",


        "margem_ht":
            MARGEM_HT,

        "margem_ft":
            MARGEM_FT,

        "margem_gols":
            MARGEM_GOLS,


        "taxa_min_under_ht":
            MIN_TAXA_UNDER_HT,

        "taxa_min_over_ft":
            MIN_TAXA_OVER_FT,

        "taxa_min_under_gols":
            MIN_TAXA_UNDER_GOLS,


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


@app.route("/bancas")
def rota_bancas():

    return jsonify(
        bancas
    )


@app.route("/health")
def health():

    return jsonify({
        "status":
            "ok",

        "version":
            "V19.2"
    })


# ============================================================
# THREAD
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v19-2"
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
