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
# ROBÔ V13
#
# ESTRATÉGIA:
#
# 1) UNDER 6.0 ESCANTEIOS NO PRIMEIRO TEMPO
# 2) CHANCE DUPLA 1X OU X2
#
# FILTROS HISTÓRICOS:
#
# Under 6 HT >= 70%
# Chance dupla >= 70%
# As duas juntas >= 70%
#
# Histórico:
# últimos 10 jogos finalizados de cada time.
#
# NÃO UTILIZA ODDS.
# ============================================================


app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v13")


# ============================================================
# API
# ============================================================

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN"
)

CHAT_ID = os.getenv(
    "CHAT_ID"
)

PORT = int(
    os.getenv("PORT", "10000")
)


# ============================================================
# ESTRATÉGIA
# ============================================================

LINHA_UNDER_CANTOS_HT = float(
    os.getenv(
        "LINHA_UNDER_CANTOS_HT",
        "6.0"
    )
)

MIN_TAXA_UNDER_HT = float(
    os.getenv(
        "MIN_TAXA_UNDER_HT",
        "0.70"
    )
)

MIN_TAXA_CHANCE_DUPLA = float(
    os.getenv(
        "MIN_TAXA_CHANCE_DUPLA",
        "0.70"
    )
)

MIN_TAXA_COMBINADA = float(
    os.getenv(
        "MIN_TAXA_COMBINADA",
        "0.70"
    )
)


# ============================================================
# HISTÓRICO
# ============================================================

QTD_HISTORICO_TIME = int(
    os.getenv(
        "QTD_HISTORICO_TIME",
        "10"
    )
)

MIN_JOGOS_HISTORICO = int(
    os.getenv(
        "MIN_JOGOS_HISTORICO",
        "10"
    )
)


# ============================================================
# JANELA DE JOGOS
# ============================================================

JANELA_HORAS = int(
    os.getenv(
        "JANELA_HORAS",
        "24"
    )
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv(
        "INTERVALO_ANALISE_SEGUNDOS",
        "900"
    )
)

MAX_JOGOS_ANALISADOS_CICLO = int(
    os.getenv(
        "MAX_JOGOS_ANALISADOS_CICLO",
        "4"
    )
)


# ============================================================
# BANCAS
# ============================================================

TOTAL_BANCAS = int(
    os.getenv(
        "TOTAL_BANCAS",
        "3"
    )
)

ENTRADA_INICIAL = float(
    os.getenv(
        "ENTRADA_INICIAL",
        "10"
    )
)

MULTIPLICADOR_GALE = float(
    os.getenv(
        "MULTIPLICADOR_GALE",
        "2"
    )
)

MAX_GALES = int(
    os.getenv(
        "MAX_GALES",
        "2"
    )
)


# ============================================================
# RATE LIMIT
# ============================================================

API_MAX_REQUESTS_PER_MINUTE = int(
    os.getenv(
        "API_MAX_REQUESTS_PER_MINUTE",
        "9"
    )
)

API_MIN_INTERVAL_SECONDS = float(
    os.getenv(
        "API_MIN_INTERVAL_SECONDS",
        "6.8"
    )
)

API_BACKOFF_429 = int(
    os.getenv(
        "API_BACKOFF_429",
        "65"
    )
)


# ============================================================
# CACHE
# ============================================================

CACHE_FIXTURES_TTL = int(
    os.getenv(
        "CACHE_FIXTURES_TTL",
        "600"
    )
)

CACHE_HISTORICO_TTL = int(
    os.getenv(
        "CACHE_HISTORICO_TTL",
        "21600"
    )
)


# ============================================================
# ARQUIVOS
#
# V13 usa arquivos novos para não misturar estatísticas
# com V11/V12.
# ============================================================

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS_V13",
    "stats_v13.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS_V13",
    "sinais_v13.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE_V13",
    "cache_v13.json"
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-v13"
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
                "⏳ Rate limit: aguardando %.1fs",
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

    url = f"{BASE_API}{endpoint}"

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
                    "⚠️ Rate limit 429. "
                    "Aguardando %ss...",
                    API_BACKOFF_429
                )

                time.sleep(
                    API_BACKOFF_429
                )

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

    return None


# ============================================================
# JSON
# ============================================================

def carregar_json(
    caminho,
    padrao
):

    if not os.path.exists(caminho):

        return padrao

    try:

        with open(
            caminho,
            "r",
            encoding="utf-8"
        ) as arquivo:

            return json.load(
                arquivo
            )

    except Exception as erro:

        logger.error(
            "❌ Erro lendo %s: %s",
            caminho,
            erro
        )

        return padrao


def salvar_json(
    caminho,
    dados
):

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
# ESTATÍSTICAS
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


# ============================================================
# SINAIS
# ============================================================

sinais = carregar_json(
    ARQUIVO_SINAIS,
    []
)


# ============================================================
# CACHE
# ============================================================

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

cursor_fixture = 0


# ============================================================
# UTILITÁRIOS
# ============================================================

def numero(valor):

    try:

        if valor is None:
            return None

        return float(valor)

    except (
        TypeError,
        ValueError
    ):

        return None


def agora_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def calcular_taxa(
    wins,
    losses
):

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

            for subchave in (
                "data",
                "fixtures",
                "results"
            ):

                subvalor = valor.get(
                    subchave
                )

                if isinstance(
                    subvalor,
                    list
                ):

                    return subvalor

    return []


# ============================================================
# BANCAS
# ============================================================

def criar_bancas():

    return [

        {
            "id": numero_banca,
            "ocupada": False,
            "fixture_id": None,
            "gale": 0,
            "entrada": ENTRADA_INICIAL
        }

        for numero_banca in range(
            1,
            TOTAL_BANCAS + 1
        )
    ]


bancas = criar_bancas()


# ============================================================
# RECONSTRUIR BANCAS
# ============================================================

def reconstruir_bancas():

    for sinal in sinais:

        if (
            sinal.get("status")
            != "PENDENTE"
        ):
            continue

        banca_id = sinal.get(
            "banca"
        )

        for banca in bancas:

            if banca["id"] == banca_id:

                banca["ocupada"] = True

                banca["fixture_id"] = str(
                    sinal["fixture_id"]
                )

                banca["gale"] = int(
                    sinal.get(
                        "gale",
                        0
                    )
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

def telegram(
    mensagem
):

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

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
# BUSCAR PRÓXIMOS JOGOS
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

    fim = (
        inicio
        + timedelta(
            hours=JANELA_HORAS
        )
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

    logger.info(
        "📅 %s jogos nas próximas %sh.",
        len(jogos),
        JANELA_HORAS
    )

    cache_fixtures[
        "timestamp"
    ] = agora

    cache_fixtures[
        "dados"
    ] = jogos

    return jogos


# ============================================================
# EXTRAIR TIMES
# ============================================================

def extrair_times(
    fixture
):

    teams = fixture.get(
        "teams",
        {}
    )

    if not isinstance(
        teams,
        dict
    ):

        return (
            None,
            None,
            "Casa",
            "Fora"
        )

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
            or
            home.get("team_id")
        )

        home_nome = (
            home.get("name")
            or
            home.get("team_name")
            or
            "Casa"
        )

    if isinstance(
        away,
        dict
    ):

        away_id = (
            away.get("id")
            or
            away.get("team_id")
        )

        away_nome = (
            away.get("name")
            or
            away.get("team_name")
            or
            "Fora"
        )

    return (
        home_id,
        away_id,
        str(home_nome),
        str(away_nome)
    )


# ============================================================
# HISTÓRICO DO TIME
# ============================================================

def buscar_historico_time(
    team_id
):

    chave = str(
        team_id
    )

    agora = time.time()

    cache = cache_persistente[
        "historicos"
    ].get(
        chave
    )

    if cache:

        idade = (
            agora
            - cache.get(
                "timestamp",
                0
            )
        )

        if idade < CACHE_HISTORICO_TTL:

            jogos = cache.get(
                "dados",
                []
            )

            logger.info(
                "📦 Time %s: "
                "%s jogos históricos no cache.",
                team_id,
                len(jogos)
            )

            return jogos

    logger.info(
        "📊 Buscando últimos %s jogos "
        "do time %s...",
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

    jogos = extrair_lista(
        dados
    )

    jogos = jogos[
        :QTD_HISTORICO_TIME
    ]

    logger.info(
        "📊 Time %s: "
        "%s jogos obtidos.",
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
# DADOS HISTÓRICOS
#
# corners:
#
# half_home = cantos da casa no 1º tempo
# half_away = cantos visitante no 1º tempo
#
# goals:
#
# home = gols FT casa
# away = gols FT visitante
# ============================================================

def extrair_dados_partida(
    jogo
):

    if not isinstance(
        jogo,
        dict
    ):

        return None

    corners = jogo.get(
        "corners"
    )

    goals = jogo.get(
        "goals"
    )

    if not isinstance(
        corners,
        dict
    ):

        return None

    if not isinstance(
        goals,
        dict
    ):

        return None

    cantos_home_ht = numero(
        corners.get(
            "half_home"
        )
    )

    cantos_away_ht = numero(
        corners.get(
            "half_away"
        )
    )

    gols_home = numero(
        goals.get(
            "home"
        )
    )

    gols_away = numero(
        goals.get(
            "away"
        )
    )

    if (
        cantos_home_ht is None
        or
        cantos_away_ht is None
        or
        gols_home is None
        or
        gols_away is None
    ):

        return None

    return {
        "cantos_ht":
            cantos_home_ht
            + cantos_away_ht,

        "gols_home":
            gols_home,

        "gols_away":
            gols_away
    }


# ============================================================
# UNDER 6 HT
#
# 0-5 = WIN
# 6   = VOID
# 7+  = LOSS
# ============================================================

def resultado_under_ht(
    cantos_ht
):

    if (
        cantos_ht
        < LINHA_UNDER_CANTOS_HT
    ):

        return "WIN"

    if (
        cantos_ht
        == LINHA_UNDER_CANTOS_HT
    ):

        return "VOID"

    return "LOSS"


# ============================================================
# TIME NÃO PERDEU
# ============================================================

def time_nao_perdeu(
    jogo,
    team_id
):

    teams = jogo.get(
        "teams",
        {}
    )

    if not isinstance(
        teams,
        dict
    ):

        return None

    home = teams.get(
        "home",
        {}
    )

    away = teams.get(
        "away",
        {}
    )

    if (
        not isinstance(
            home,
            dict
        )
        or
        not isinstance(
            away,
            dict
        )
    ):

        return None

    home_id = (
        home.get("id")
        or
        home.get("team_id")
    )

    away_id = (
        away.get("id")
        or
        away.get("team_id")
    )

    dados = extrair_dados_partida(
        jogo
    )

    if not dados:

        return None

    team_id = str(
        team_id
    )

    if (
        home_id is not None
        and
        str(home_id) == team_id
    ):

        return (
            dados["gols_home"]
            >=
            dados["gols_away"]
        )

    if (
        away_id is not None
        and
        str(away_id) == team_id
    ):

        return (
            dados["gols_away"]
            >=
            dados["gols_home"]
        )

    return None


# ============================================================
# ANALISAR ÚLTIMOS 10 JOGOS
# ============================================================

def analisar_historico_time(
    historico,
    team_id
):

    registros = []

    for jogo in historico:

        dados = extrair_dados_partida(
            jogo
        )

        if not dados:
            continue

        nao_perdeu = time_nao_perdeu(
            jogo,
            team_id
        )

        if nao_perdeu is None:
            continue

        under = resultado_under_ht(
            dados["cantos_ht"]
        )

        registros.append({
            "under": under,

            "nao_perdeu":
                nao_perdeu,

            "cantos_ht":
                dados["cantos_ht"]
        })

    total = len(
        registros
    )

    if total == 0:

        return None


    # ========================================================
    # UNDER
    # ========================================================

    under_wins = sum(
        1
        for registro in registros
        if registro["under"] == "WIN"
    )

    under_voids = sum(
        1
        for registro in registros
        if registro["under"] == "VOID"
    )

    under_losses = sum(
        1
        for registro in registros
        if registro["under"] == "LOSS"
    )

    # VOID não entra no cálculo da taxa de acerto
    under_decididos = (
        under_wins
        + under_losses
    )

    if under_decididos:

        taxa_under = (
            under_wins
            / under_decididos
        )

    else:

        taxa_under = 0


    # ========================================================
    # CHANCE DUPLA
    # ========================================================

    dc_wins = sum(
        1
        for registro in registros
        if registro["nao_perdeu"]
    )

    dc_losses = (
        total
        - dc_wins
    )

    taxa_dc = (
        dc_wins
        / total
    )


    # ========================================================
    # COMBINAÇÃO
    #
    # Under WIN + DC WIN = acerto
    # Under VOID + DC WIN = não perdeu combinação
    #
    # Under LOSS = falha
    # DC LOSS = falha
    # ========================================================

    combinada_wins = 0

    combinada_losses = 0

    combinada_voids = 0

    for registro in registros:

        under = registro[
            "under"
        ]

        dc = registro[
            "nao_perdeu"
        ]

        if (
            under == "WIN"
            and dc
        ):

            combinada_wins += 1

        elif (
            under == "VOID"
            and dc
        ):

            combinada_voids += 1

        else:

            combinada_losses += 1


    # Para medir a taxa conjunta:
    #
    # VOID não entra nem como vitória nem derrota.
    # ========================================================

    combinada_decididos = (
        combinada_wins
        + combinada_losses
    )

    if combinada_decididos:

        taxa_combinada = (
            combinada_wins
            / combinada_decididos
        )

    else:

        taxa_combinada = 0


    # ========================================================
    # MÉDIA CANTOS HT
    # ========================================================

    media_cantos_ht = (
        sum(
            registro["cantos_ht"]
            for registro in registros
        )
        / total
    )


    return {

        "total":
            total,

        # UNDER

        "under_wins":
            under_wins,

        "under_voids":
            under_voids,

        "under_losses":
            under_losses,

        "taxa_under":
            taxa_under,


        # CHANCE DUPLA

        "dc_wins":
            dc_wins,

        "dc_losses":
            dc_losses,

        "taxa_dc":
            taxa_dc,


        # COMBINAÇÃO

        "combinada_wins":
            combinada_wins,

        "combinada_voids":
            combinada_voids,

        "combinada_losses":
            combinada_losses,

        "taxa_combinada":
            taxa_combinada,


        # MÉDIA

        "media_cantos_ht":
            media_cantos_ht
    }


# ============================================================
# ESCOLHER 1X OU X2
# ============================================================

def escolher_chance_dupla(
    home_id,
    away_id,
    historico_home,
    historico_away
):

    analise_1x = analisar_historico_time(
        historico_home,
        home_id
    )

    analise_x2 = analisar_historico_time(
        historico_away,
        away_id
    )


    candidatos = []


    if analise_1x:

        candidatos.append({
            "mercado": "1X",
            "analise": analise_1x
        })


    if analise_x2:

        candidatos.append({
            "mercado": "X2",
            "analise": analise_x2
        })


    # ========================================================
    # Primeiro escolhe maior taxa conjunta.
    #
    # Em empate:
    # maior taxa da chance dupla.
    # ========================================================

    candidatos.sort(
        key=lambda candidato: (
            candidato[
                "analise"
            ][
                "taxa_combinada"
            ],

            candidato[
                "analise"
            ][
                "taxa_dc"
            ],

            candidato[
                "analise"
            ][
                "taxa_under"
            ]
        ),
        reverse=True
    )


    for candidato in candidatos:

        analise = candidato[
            "analise"
        ]


        logger.info(
            "📊 %s | "
            "Under %.1f%% | "
            "DC %.1f%% | "
            "Juntas %.1f%% | "
            "%s jogos",
            candidato["mercado"],
            analise["taxa_under"] * 100,
            analise["taxa_dc"] * 100,
            analise["taxa_combinada"] * 100,
            analise["total"]
        )


        # ====================================================
        # Exige histórico mínimo
        # ====================================================

        if (
            analise["total"]
            < MIN_JOGOS_HISTORICO
        ):

            continue


        # ====================================================
        # UNDER >= 70%
        # ====================================================

        if (
            analise["taxa_under"]
            < MIN_TAXA_UNDER_HT
        ):

            continue


        # ====================================================
        # CHANCE DUPLA >= 70%
        # ====================================================

        if (
            analise["taxa_dc"]
            < MIN_TAXA_CHANCE_DUPLA
        ):

            continue


        # ====================================================
        # AS DUAS JUNTAS >= 70%
        # ====================================================

        if (
            analise["taxa_combinada"]
            < MIN_TAXA_COMBINADA
        ):

            continue


        return candidato


    return None


# ============================================================
# FIXTURE JÁ UTILIZADO
# ============================================================

def fixture_ja_utilizado(
    fixture_id
):

    fixture_id = str(
        fixture_id
    )

    return any(

        str(
            sinal.get(
                "fixture_id"
            )
        )
        == fixture_id

        for sinal in sinais
    )


# ============================================================
# ENVIAR SINAL
# ============================================================

def enviar_sinal(
    fixture,
    candidato,
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
    ) = extrair_times(
        fixture
    )


    analise = candidato[
        "analise"
    ]

    mercado_dc = candidato[
        "mercado"
    ]


    entrada = (
        ENTRADA_INICIAL
        *
        (
            MULTIPLICADOR_GALE
            ** banca["gale"]
        )
    )


    sinal = {

        "fixture_id":
            fixture_id,

        "home":
            home,

        "away":
            away,

        "home_id":
            home_id,

        "away_id":
            away_id,

        "mercado":
            (
                "Under 6 cantos HT + "
                f"{mercado_dc}"
            ),

        "linha_under_cantos_ht":
            LINHA_UNDER_CANTOS_HT,

        "chance_dupla":
            mercado_dc,

        "taxa_under_ht":
            round(
                analise[
                    "taxa_under"
                ] * 100,
                2
            ),

        "taxa_chance_dupla":
            round(
                analise[
                    "taxa_dc"
                ] * 100,
                2
            ),

        "taxa_combinada":
            round(
                analise[
                    "taxa_combinada"
                ] * 100,
                2
            ),

        "amostra":
            analise[
                "total"
            ],

        "media_cantos_ht":
            round(
                analise[
                    "media_cantos_ht"
                ],
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

        sinais.append(
            sinal
        )

        banca[
            "ocupada"
        ] = True

        banca[
            "fixture_id"
        ] = fixture_id

        banca[
            "entrada"
        ] = entrada

        salvar_json(
            ARQUIVO_SINAIS,
            sinais
        )


    mensagem = (

        "🚨 <b>SINAL V13</b>\n\n"

        f"⚽ <b>{html.escape(home)}"
        f" x "
        f"{html.escape(away)}</b>\n\n"

        "1️⃣ <b>UNDER ESCANTEIOS HT</b>\n"

        f"📉 Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f}\n"

        f"📊 Histórico: "
        f"{analise['taxa_under'] * 100:.1f}%\n"

        f"📈 Média HT: "
        f"{analise['media_cantos_ht']:.2f}\n"

        f"✅ WIN: "
        f"{analise['under_wins']}\n"

        f"⚪ VOID: "
        f"{analise['under_voids']}\n"

        f"❌ LOSS: "
        f"{analise['under_losses']}\n\n"


        "2️⃣ <b>CHANCE DUPLA</b>\n"

        f"🛡 {mercado_dc}\n"

        f"📊 Histórico: "
        f"{analise['taxa_dc'] * 100:.1f}%\n"

        f"✅ Não perdeu: "
        f"{analise['dc_wins']}\n"

        f"❌ Perdeu: "
        f"{analise['dc_losses']}\n\n"


        "🔥 <b>AS DUAS JUNTAS</b>\n"

        f"📊 Histórico conjunto: "
        f"{analise['taxa_combinada'] * 100:.1f}%\n"

        f"✅ WIN: "
        f"{analise['combinada_wins']}\n"

        f"⚪ VOID: "
        f"{analise['combinada_voids']}\n"

        f"❌ LOSS: "
        f"{analise['combinada_losses']}\n"

        f"📚 Amostra: "
        f"{analise['total']} jogos\n\n"


        "💰 <b>GESTÃO</b>\n"

        f"🏦 Banca: "
        f"{banca['id']}\n"

        f"🔄 Gale: "
        f"{banca['gale']}/{MAX_GALES}\n"

        f"💵 Entrada: "
        f"R$ {entrada:.2f}"
    )


    telegram(
        mensagem
    )


    logger.info(
        "🚨 SINAL V13 | "
        "%s x %s | "
        "Under 6 HT + %s | "
        "Under %.1f%% | "
        "DC %.1f%% | "
        "Juntas %.1f%%",
        home,
        away,
        mercado_dc,
        analise["taxa_under"] * 100,
        analise["taxa_dc"] * 100,
        analise["taxa_combinada"] * 100
    )


# ============================================================
# BUSCAR FIXTURE ESPECÍFICO
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
# FINALIZADO
# ============================================================

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


# ============================================================
# CHANCE DUPLA REAL
# ============================================================

def resolver_chance_dupla(
    fixture,
    mercado
):

    dados = extrair_dados_partida(
        fixture
    )

    if not dados:

        return None


    home = dados[
        "gols_home"
    ]

    away = dados[
        "gols_away"
    ]


    if mercado == "1X":

        if home >= away:
            return "WIN"

        return "LOSS"


    if mercado == "X2":

        if away >= home:
            return "WIN"

        return "LOSS"


    return None


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

        if (
            banca["gale"]
            < MAX_GALES
        ):

            banca[
                "gale"
            ] += 1

        else:

            banca[
                "gale"
            ] = 0


    # PUSH mantém Gale

    banca[
        "ocupada"
    ] = False

    banca[
        "fixture_id"
    ] = None

    banca[
        "entrada"
    ] = (
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

    dados = extrair_dados_partida(
        fixture
    )

    if not dados:

        logger.warning(
            "⚠️ Fixture %s sem dados "
            "suficientes para resolver.",
            sinal["fixture_id"]
        )

        return


    resultado_under = (
        resultado_under_ht(
            dados["cantos_ht"]
        )
    )


    resultado_dc = (
        resolver_chance_dupla(
            fixture,
            sinal["chance_dupla"]
        )
    )


    if resultado_dc is None:

        return


    # ========================================================
    # RESULTADO DA COMBINAÇÃO
    #
    # UNDER LOSS = LOSS
    # DC LOSS = LOSS
    #
    # UNDER WIN + DC WIN = WIN
    #
    # UNDER VOID + DC WIN = PUSH
    #
    # Aqui usamos PUSH porque não temos odd da combinação.
    # ========================================================

    if (
        resultado_under == "LOSS"
        or
        resultado_dc == "LOSS"
    ):

        resultado = "LOSS"

    elif (
        resultado_under == "VOID"
        and
        resultado_dc == "WIN"
    ):

        resultado = "PUSH"

    else:

        resultado = "WIN"


    sinal[
        "status"
    ] = "RESOLVIDO"

    sinal[
        "resultado"
    ] = resultado

    sinal[
        "resultado_under_ht"
    ] = resultado_under

    sinal[
        "resultado_chance_dupla"
    ] = resultado_dc

    sinal[
        "cantos_ht_final"
    ] = dados[
        "cantos_ht"
    ]

    sinal[
        "gols_home"
    ] = dados[
        "gols_home"
    ]

    sinal[
        "gols_away"
    ] = dados[
        "gols_away"
    ]

    sinal[
        "resolvido_em"
    ] = agora_iso()


    # ========================================================
    # STATS
    # ========================================================

    if resultado == "WIN":

        stats["wins"] = (
            stats.get(
                "wins",
                0
            )
            + 1
        )


    elif resultado == "LOSS":

        stats["losses"] = (
            stats.get(
                "losses",
                0
            )
            + 1
        )


    else:

        stats["pushes"] = (
            stats.get(
                "pushes",
                0
            )
            + 1
        )


    stats[
        "total_resolvidos"
    ] = (
        stats.get(
            "total_resolvidos",
            0
        )
        + 1
    )


    gale = str(
        sinal.get(
            "gale",
            0
        )
    )


    if (
        gale
        not in stats[
            "por_gale"
        ]
    ):

        stats[
            "por_gale"
        ][gale] = {

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
            if (
                item["id"]
                == sinal["banca"]
            )
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

        f"{emoji} <b>{resultado} V13</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f" x "
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"


        "1️⃣ Under "
        f"{LINHA_UNDER_CANTOS_HT:.1f} "
        "cantos HT\n"

        f"🚩 Cantos HT: "
        f"{dados['cantos_ht']:.0f}\n"

        f"Resultado: "
        f"{resultado_under}\n\n"


        "2️⃣ Chance dupla "
        f"{sinal['chance_dupla']}\n"

        f"⚽ Placar FT: "
        f"{dados['gols_home']:.0f}"
        f" x "
        f"{dados['gols_away']:.0f}\n"

        f"Resultado: "
        f"{resultado_dc}\n\n"


        f"🏦 Banca: "
        f"{sinal['banca']}\n"

        f"🔄 Gale usado: "
        f"{sinal['gale']}"
    )


    telegram(
        mensagem
    )


    logger.info(
        "%s | %s x %s | "
        "Under=%s | %s=%s",
        resultado,
        sinal["home"],
        sinal["away"],
        resultado_under,
        sinal["chance_dupla"],
        resultado_dc
    )


# ============================================================
# VERIFICAR RESULTADOS
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
        )
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


        if not fixture_finalizado(
            fixture
        ):

            continue


        resolver_sinal(
            sinal,
            fixture
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


    if fixture_ja_utilizado(
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
            "⛔ %s x %s sem IDs.",
            home,
            away
        )

        return


    logger.info(
        "🔎 V13 | %s x %s",
        home,
        away
    )


    # ========================================================
    # ÚLTIMOS 10 DO MANDANTE
    # ========================================================

    historico_home = (
        buscar_historico_time(
            home_id
        )
    )


    # ========================================================
    # ÚLTIMOS 10 DO VISITANTE
    # ========================================================

    historico_away = (
        buscar_historico_time(
            away_id
        )
    )


    candidato = (
        escolher_chance_dupla(
            home_id,
            away_id,
            historico_home,
            historico_away
        )
    )


    if not candidato:

        logger.info(
            "⛔ %s x %s não atingiu "
            "os filtros.",
            home,
            away
        )

        return


    analise = candidato[
        "analise"
    ]


    logger.info(
        "🔥 APROVADO | "
        "%s x %s | "
        "%s | "
        "Under %.1f%% | "
        "DC %.1f%% | "
        "Juntas %.1f%%",
        home,
        away,
        candidato["mercado"],
        analise["taxa_under"] * 100,
        analise["taxa_dc"] * 100,
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
        candidato,
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
        "🔄 Iniciando ciclo V13"
    )


    try:

        # ====================================================
        # PRIMEIRO RESOLVE SINAIS
        # ====================================================

        verificar_resultados()


        # ====================================================
        # SEM BANCA LIVRE
        # ====================================================

        if obter_banca_livre() is None:

            logger.info(
                "🏦 Todas as %s bancas "
                "estão ocupadas.",
                TOTAL_BANCAS
            )

            return


        fixtures = buscar_fixtures()


        if not fixtures:

            logger.info(
                "📭 Nenhum jogo disponível."
            )

            return


        total = len(
            fixtures
        )


        quantidade = min(
            MAX_JOGOS_ANALISADOS_CICLO,
            total
        )


        logger.info(
            "🔎 Analisando até "
            "%s de %s jogos.",
            quantidade,
            total
        )


        for _ in range(
            quantidade
        ):


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
            "❌ Erro geral no ciclo V13."
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logger.info(
        "🤖 ROBÔ V13 INICIADO"
    )

    logger.info(
        "🚩 Under %.1f cantos HT "
        "+ Chance Dupla",
        LINHA_UNDER_CANTOS_HT
    )

    logger.info(
        "📊 Under mínimo: %.0f%%",
        MIN_TAXA_UNDER_HT * 100
    )

    logger.info(
        "🛡 Chance dupla mínima: %.0f%%",
        MIN_TAXA_CHANCE_DUPLA * 100
    )

    logger.info(
        "🔥 Combinação mínima: %.0f%%",
        MIN_TAXA_COMBINADA * 100
    )

    logger.info(
        "📚 Histórico: últimos %s jogos",
        QTD_HISTORICO_TIME
    )

    logger.info(
        "💰 FILTRO DE ODD: DESATIVADO"
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

        "status":
            "online",

        "versao":
            "V13",

        "estrategia":
            (
                "Under 6 cantos HT "
                "+ Chance Dupla"
            ),

        "odds":
            "desativadas",

        "historico":
            QTD_HISTORICO_TIME,

        "linha_under_ht":
            LINHA_UNDER_CANTOS_HT,

        "min_under":
            MIN_TAXA_UNDER_HT,

        "min_chance_dupla":
            MIN_TAXA_CHANCE_DUPLA,

        "min_combinada":
            MIN_TAXA_COMBINADA,

        "wins":
            wins,

        "losses":
            losses,

        "pushes":
            pushes,

        "assertividade":
            calcular_taxa(
                wins,
                losses
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
            "V13",

        "mercado_1":
            "Under 6.0 cantos HT",

        "mercado_2":
            "Chance dupla 1X/X2",

        "historico":
            "Últimos 10 jogos",

        "filtro_under":
            f"{MIN_TAXA_UNDER_HT * 100:.0f}%",

        "filtro_chance_dupla":
            f"{MIN_TAXA_CHANCE_DUPLA * 100:.0f}%",

        "filtro_combinado":
            f"{MIN_TAXA_COMBINADA * 100:.0f}%",

        "filtro_odd":
            False
    })


@app.route("/stats")
def rota_stats():

    resposta = dict(
        stats
    )

    resposta[
        "assertividade"
    ] = calcular_taxa(
        stats.get(
            "wins",
            0
        ),
        stats.get(
            "losses",
            0
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
        "version": "V13"
    })


# ============================================================
# START
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True,
    name="robo-v13"
)

thread_robo.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
