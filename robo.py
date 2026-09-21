import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# ROBÔ V21.1 — 4 FAVORITOS / 72 HORAS
#
# 1 SINAL = 4 PARTIDAS
# Mercado: Resultado Final / Vitória Simples 1X2
#
# Padrão:
# - Odd favorito: 1.20 até 1.70
# - Histórico: últimos 10 jogos
# - Favorito: mínimo 55% de vitórias
# - Adversário: mínimo 40% de derrotas
# - Janela: próximas 72 horas
# - Busca em blocos de no máximo 24 horas
# - Escolhe os 4 melhores candidatos aprovados
# - Apenas 1 bilhete ativo por vez
# ============================================================


app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-v21.1")


# ============================================================
# VARIÁVEIS SEGURAS
# ============================================================

def limpar_env(valor):
    if valor is None:
        return None

    return (
        str(valor)
        .strip()
        .replace("\u00a0", "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace('"', "")
        .replace("'", "")
    )


def env_str(nome, padrao=""):
    valor = os.getenv(nome)

    if valor is None:
        return padrao

    valor = limpar_env(valor)

    return valor if valor else padrao


def env_float(nome, padrao):
    original = os.getenv(nome)

    if original is None:
        return float(padrao)

    try:
        valor = limpar_env(original)
        valor = valor.replace(",", ".")
        return float(valor)

    except (ValueError, TypeError):
        logger.warning(
            "Variável %s inválida: %r. Usando padrão %s.",
            nome,
            original,
            padrao
        )

        return float(padrao)


def env_int(nome, padrao):
    original = os.getenv(nome)

    if original is None:
        return int(padrao)

    try:
        valor = limpar_env(original)
        return int(float(valor))

    except (ValueError, TypeError):
        logger.warning(
            "Variável %s inválida: %r. Usando padrão %s.",
            nome,
            original,
            padrao
        )

        return int(padrao)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE_API = env_str(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = env_str(
    "FIVE_DOLLAR_KEY"
)

TELEGRAM_TOKEN = env_str(
    "TELEGRAM_TOKEN"
)

CHAT_ID = env_str(
    "CHAT_ID"
)

PORT = env_int(
    "PORT",
    10000
)


# ============================================================
# ESTRATÉGIA
# ============================================================

ODD_MIN_SIMPLES = env_float(
    "ODD_MIN_SIMPLES",
    1.20
)

ODD_MAX_SIMPLES = env_float(
    "ODD_MAX_SIMPLES",
    1.70
)

MIN_WIN_FAVORITO = env_float(
    "MIN_WIN_FAVORITO",
    0.55
)

MIN_LOSS_ADVERSARIO = env_float(
    "MIN_LOSS_ADVERSARIO",
    0.40
)

QTD_JOGOS_BILHETE = env_int(
    "QTD_JOGOS_BILHETE",
    4
)


# ============================================================
# HISTÓRICO
# ============================================================

QTD_HISTORICO = env_int(
    "QTD_HISTORICO",
    10
)

MIN_JOGOS_HISTORICO = env_int(
    "MIN_JOGOS_HISTORICO",
    10
)


# ============================================================
# JANELA
# ============================================================

JANELA_HORAS = env_float(
    "JANELA_HORAS",
    72
)

# Permite no máximo 72 horas nesta versão.
JANELA_HORAS = min(
    max(JANELA_HORAS, 1),
    72
)


# ============================================================
# INTERVALOS
# ============================================================

INTERVALO_ANALISE = env_int(
    "INTERVALO_ANALISE",
    120
)

INTERVALO_RESULTADOS = env_int(
    "INTERVALO_RESULTADOS",
    120
)


# ============================================================
# RATE LIMIT
# ============================================================

API_MIN_INTERVAL_SECONDS = env_float(
    "API_MIN_INTERVAL_SECONDS",
    6.8
)

API_BACKOFF_429 = env_int(
    "API_BACKOFF_429",
    65
)

ultimo_request = 0.0
request_lock = threading.RLock()


# ============================================================
# CACHE
# ============================================================

CACHE_HISTORICO_TTL = env_int(
    "CACHE_HISTORICO_TTL",
    21600
)


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_SINAL = "sinal_v21.json"
ARQUIVO_STATS = "stats_v21.json"
ARQUIVO_CACHE = "cache_v21.json"

lock_sinal = threading.RLock()
lock_stats = threading.RLock()
lock_cache = threading.RLock()


# ============================================================
# JSON
# ============================================================

def carregar_json(arquivo, padrao):
    try:
        if not os.path.exists(arquivo):
            return padrao

        with open(
            arquivo,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        logger.error(
            "Erro lendo %s: %s",
            arquivo,
            e
        )

        return padrao


def salvar_json(arquivo, dados):
    temporario = arquivo + ".tmp"

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
            arquivo
        )

    except Exception:
        logger.exception(
            "Erro salvando %s",
            arquivo
        )


# ============================================================
# ESTATÍSTICAS
# ============================================================

STATS_PADRAO = {
    "total_sinais": 0,
    "bilhetes_win": 0,
    "bilhetes_loss": 0,
    "selecoes_win": 0,
    "selecoes_loss": 0
}

stats = carregar_json(
    ARQUIVO_STATS,
    STATS_PADRAO.copy()
)

for chave, valor in STATS_PADRAO.items():
    stats.setdefault(
        chave,
        valor
    )


def calcular_taxa(wins, losses):
    total = wins + losses

    if total <= 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def taxa_bilhetes():
    return calcular_taxa(
        stats["bilhetes_win"],
        stats["bilhetes_loss"]
    )


def taxa_selecoes():
    return calcular_taxa(
        stats["selecoes_win"],
        stats["selecoes_loss"]
    )


# ============================================================
# SINAL
# ============================================================

sinal_atual = carregar_json(
    ARQUIVO_SINAL,
    None
)


def existe_sinal_pendente():
    return (
        isinstance(sinal_atual, dict)
        and
        sinal_atual.get("status") == "PENDENTE"
    )


# ============================================================
# CACHE
# ============================================================

cache = carregar_json(
    ARQUIVO_CACHE,
    {
        "historicos": {}
    }
)

if not isinstance(cache, dict):
    cache = {
        "historicos": {}
    }

cache.setdefault(
    "historicos",
    {}
)


def salvar_cache():
    with lock_cache:
        salvar_json(
            ARQUIVO_CACHE,
            cache
        )


# ============================================================
# API
# ============================================================

def headers_api():
    return {
        "Authorization":
            f"Bearer {API_KEY}",

        "Accept":
            "application/json",

        "User-Agent":
            "robo-favoritos-v21.1"
    }


def api_get(
    endpoint,
    params=None,
    tentativas=2
):
    global ultimo_request

    if not API_KEY:
        logger.error(
            "FIVE_DOLLAR_KEY não configurada."
        )
        return None

    for tentativa in range(
        1,
        tentativas + 1
    ):

        with request_lock:

            agora = time.monotonic()

            decorrido = (
                agora
                - ultimo_request
            )

            espera = (
                API_MIN_INTERVAL_SECONDS
                - decorrido
            )

            if espera > 0:
                time.sleep(espera)

            url = (
                BASE_API
                + endpoint
            )

            try:
                resposta = requests.get(
                    url,
                    headers=headers_api(),
                    params=params,
                    timeout=30
                )

                ultimo_request = (
                    time.monotonic()
                )

            except requests.RequestException as e:

                logger.warning(
                    "Erro de conexão API %s: %s",
                    endpoint,
                    e
                )

                if tentativa < tentativas:
                    time.sleep(5)
                    continue

                return None

        if resposta.status_code == 429:

            logger.warning(
                "API 429 - aguardando %ss.",
                API_BACKOFF_429
            )

            time.sleep(
                API_BACKOFF_429
            )

            continue

        if resposta.status_code != 200:

            logger.warning(
                "API %s HTTP %s: %s",
                endpoint,
                resposta.status_code,
                resposta.text[:300]
            )

            return None

        try:
            payload = resposta.json()

        except ValueError:
            logger.warning(
                "Resposta inválida da API em %s.",
                endpoint
            )
            return None

        if not isinstance(
            payload,
            dict
        ):
            return None

        if payload.get("success") != 1:

            logger.warning(
                "API success != 1 em %s: %s",
                endpoint,
                payload
            )

            return None

        return payload.get(
            "data"
        )

    return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):
    if (
        not TELEGRAM_TOKEN
        or
        not CHAT_ID
    ):
        logger.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        resposta = requests.post(
            url,
            data={
                "chat_id":
                    CHAT_ID,

                "text":
                    texto,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True
            },
            timeout=20
        )

        if not resposta.ok:

            logger.warning(
                "Telegram HTTP %s: %s",
                resposta.status_code,
                resposta.text[:300]
            )

        return resposta.ok

    except Exception:

        logger.exception(
            "Erro enviando Telegram."
        )

        return False


# ============================================================
# TIMES
# ============================================================

def obter_times(fixture):
    if not isinstance(
        fixture,
        dict
    ):
        return None

    teams = fixture.get(
        "teams"
    )

    if not isinstance(
        teams,
        dict
    ):
        return None

    home = (
        teams.get("home")
        or {}
    )

    away = (
        teams.get("away")
        or {}
    )

    return {
        "home_id":
            home.get("id"),

        "home":
            home.get(
                "name",
                "Mandante"
            ),

        "away_id":
            away.get("id"),

        "away":
            away.get(
                "name",
                "Visitante"
            )
    }


# ============================================================
# GOLS
# ============================================================

def extrair_gols(fixture):
    if not isinstance(
        fixture,
        dict
    ):
        return None

    goals = fixture.get(
        "goals"
    )

    if not isinstance(
        goals,
        dict
    ):
        return None

    home = goals.get(
        "home"
    )

    away = goals.get(
        "away"
    )

    if (
        home is None
        or
        away is None
    ):
        return None

    try:
        return (
            int(home),
            int(away)
        )

    except (
        ValueError,
        TypeError
    ):
        return None


# ============================================================
# ODDS 1X2
# ============================================================

def extrair_1x2_de_mercado(
    mercado
):
    if not isinstance(
        mercado,
        dict
    ):
        return None

    fase = (
        mercado.get("closing")
        or
        mercado.get("opening")
    )

    if not isinstance(
        fase,
        dict
    ):
        return None

    try:
        home = float(
            fase["home"]
        )

        draw = float(
            fase["draw"]
        )

        away = float(
            fase["away"]
        )

    except (
        KeyError,
        ValueError,
        TypeError
    ):
        return None

    if (
        home <= 1
        or
        draw <= 1
        or
        away <= 1
    ):
        return None

    return {
        "home": home,
        "draw": draw,
        "away": away
    }


def obter_odds_1x2(fixture):

    # Primeiro tenta usar odds já incluídas
    # na consulta de fixtures.

    odds_inline = fixture.get(
        "odds"
    )

    if isinstance(
        odds_inline,
        dict
    ):
        mercado = odds_inline.get(
            "1x2"
        )

        odds = extrair_1x2_de_mercado(
            mercado
        )

        if odds:
            return odds

    # Fallback: consulta endpoint específico.

    fixture_id = fixture.get(
        "id"
    )

    if not fixture_id:
        return None

    dados = api_get(
        f"/fixtures/{fixture_id}/odds"
    )

    if not isinstance(
        dados,
        dict
    ):
        return None

    bookmakers = dados.get(
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

        if slug != "bet365":
            continue

        mercados = bookmaker.get(
            "odds"
        )

        if not isinstance(
            mercados,
            dict
        ):
            continue

        odds = extrair_1x2_de_mercado(
            mercados.get(
                "1x2"
            )
        )

        if odds:
            return odds

    return None


# ============================================================
# IDENTIFICAR FAVORITO
# ============================================================

def identificar_favorito(
    fixture,
    odds
):
    times = obter_times(
        fixture
    )

    if not times:
        return None

    if (
        not times["home_id"]
        or
        not times["away_id"]
    ):
        return None

    odd_home = odds["home"]
    odd_away = odds["away"]

    if odd_home == odd_away:
        return None

    if odd_home < odd_away:

        return {
            "lado":
                "home",

            "team_id":
                times["home_id"],

            "nome":
                times["home"],

            "adversario_id":
                times["away_id"],

            "adversario":
                times["away"],

            "odd":
                odd_home
        }

    return {
        "lado":
            "away",

        "team_id":
            times["away_id"],

        "nome":
            times["away"],

        "adversario_id":
            times["home_id"],

        "adversario":
            times["home"],

        "odd":
            odd_away
    }


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico(team_id):

    chave = str(
        team_id
    )

    entrada_cache = (
        cache[
            "historicos"
        ].get(chave)
    )

    if isinstance(
        entrada_cache,
        dict
    ):

        timestamp = (
            entrada_cache.get(
                "timestamp",
                0
            )
        )

        if (
            time.time()
            - timestamp
            < CACHE_HISTORICO_TTL
        ):

            jogos = (
                entrada_cache.get(
                    "jogos"
                )
            )

            if isinstance(
                jogos,
                list
            ):
                return jogos[
                    :QTD_HISTORICO
                ]

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status":
                "finished",

            "order":
                "desc",

            "per_page":
                min(
                    QTD_HISTORICO,
                    100
                )
        }
    )

    if not isinstance(
        dados,
        list
    ):
        return []

    jogos = dados[
        :QTD_HISTORICO
    ]

    cache[
        "historicos"
    ][chave] = {
        "timestamp":
            time.time(),

        "jogos":
            jogos
    }

    salvar_cache()

    return jogos


# ============================================================
# ANALISAR TIME
# ============================================================

def analisar_time(team_id):

    jogos = buscar_historico(
        team_id
    )

    wins = 0
    draws = 0
    losses = 0
    validos = 0

    gols_pro = 0
    gols_contra = 0

    for jogo in jogos:

        times = obter_times(
            jogo
        )

        placar = extrair_gols(
            jogo
        )

        if (
            not times
            or
            placar is None
        ):
            continue

        gh, ga = placar

        if (
            str(team_id)
            ==
            str(times["home_id"])
        ):

            gp = gh
            gc = ga

        elif (
            str(team_id)
            ==
            str(times["away_id"])
        ):

            gp = ga
            gc = gh

        else:
            continue

        validos += 1

        gols_pro += gp
        gols_contra += gc

        if gp > gc:
            wins += 1

        elif gp == gc:
            draws += 1

        else:
            losses += 1

    if validos == 0:
        return None

    return {
        "jogos":
            validos,

        "wins":
            wins,

        "draws":
            draws,

        "losses":
            losses,

        "taxa_win":
            wins / validos,

        "taxa_loss":
            losses / validos,

        "media_gols_pro":
            gols_pro / validos,

        "media_gols_contra":
            gols_contra / validos
    }


# ============================================================
# ANALISAR CANDIDATO
# ============================================================

def analisar_candidato(
    fixture,
    favorito
):

    odd = favorito[
        "odd"
    ]

    if not (
        ODD_MIN_SIMPLES
        <= odd
        <= ODD_MAX_SIMPLES
    ):
        return None

    hist_fav = analisar_time(
        favorito[
            "team_id"
        ]
    )

    hist_adv = analisar_time(
        favorito[
            "adversario_id"
        ]
    )

    if (
        not hist_fav
        or
        not hist_adv
    ):
        return None

    if (
        hist_fav["jogos"]
        < MIN_JOGOS_HISTORICO
        or
        hist_adv["jogos"]
        < MIN_JOGOS_HISTORICO
    ):

        logger.info(
            "Reprovado %s: "
            "histórico insuficiente.",
            favorito["nome"]
        )

        return None

    if (
        hist_fav["taxa_win"]
        < MIN_WIN_FAVORITO
    ):

        logger.info(
            "Reprovado %s: "
            "vitórias %.1f%% < %.1f%%",
            favorito["nome"],
            hist_fav["taxa_win"] * 100,
            MIN_WIN_FAVORITO * 100
        )

        return None

    if (
        hist_adv["taxa_loss"]
        < MIN_LOSS_ADVERSARIO
    ):

        logger.info(
            "Reprovado %s: "
            "derrotas adversário "
            "%.1f%% < %.1f%%",
            favorito["nome"],
            hist_adv["taxa_loss"] * 100,
            MIN_LOSS_ADVERSARIO * 100
        )

        return None

    times = obter_times(
        fixture
    )

    score = (
        hist_fav["taxa_win"]
        +
        hist_adv["taxa_loss"]
    )

    return {
        "fixture_id":
            fixture["id"],

        "kickoff":
            fixture.get(
                "kickoff_utc"
            ),

        "home":
            times["home"],

        "away":
            times["away"],

        "favorito":
            favorito["nome"],

        "favorito_id":
            favorito["team_id"],

        "favorito_lado":
            favorito["lado"],

        "odd":
            odd,

        "score":
            score,

        "historico_favorito":
            hist_fav,

        "historico_adversario":
            hist_adv
    }


# ============================================================
# BUSCAR PRÓXIMOS JOGOS
#
# 72h são divididas em:
# 0-24h
# 24-48h
# 48-72h
# ============================================================

def buscar_proximos_jogos():

    agora = datetime.now(
        timezone.utc
    )

    todos_jogos = []
    ids_adicionados = set()

    horas_restantes = (
        JANELA_HORAS
    )

    inicio_janela = agora

    numero_janela = 1

    while horas_restantes > 0:

        tamanho_janela = min(
            24,
            horas_restantes
        )

        fim_janela = (
            inicio_janela
            + timedelta(
                hours=tamanho_janela
            )
        )

        logger.info(
            "Janela %s | buscando "
            "%s até %s",
            numero_janela,
            inicio_janela.strftime(
                "%d/%m %H:%M"
            ),
            fim_janela.strftime(
                "%d/%m %H:%M"
            )
        )

        dados = api_get(
            "/fixtures",
            params={
                "start_time":
                    int(
                        inicio_janela.timestamp()
                    ),

                "end_time":
                    int(
                        fim_janela.timestamp()
                    ),

                "status":
                    "scheduled",

                "include":
                    "odds",

                "per_page":
                    100
            }
        )

        encontrados = 0

        if isinstance(
            dados,
            list
        ):

            for jogo in dados:

                fixture_id = jogo.get(
                    "id"
                )

                if not fixture_id:
                    continue

                if (
                    fixture_id
                    in ids_adicionados
                ):
                    continue

                ids_adicionados.add(
                    fixture_id
                )

                todos_jogos.append(
                    jogo
                )

                encontrados += 1

        logger.info(
            "Janela %s: %s jogos adicionados.",
            numero_janela,
            encontrados
        )

        horas_restantes -= (
            tamanho_janela
        )

        inicio_janela = (
            fim_janela
        )

        numero_janela += 1

    logger.info(
        "Total encontrado nas próximas "
        "%.0f horas: %s jogos.",
        JANELA_HORAS,
        len(todos_jogos)
    )

    return todos_jogos


# ============================================================
# ODD COMBINADA
# ============================================================

def calcular_odd_combinada(
    jogos
):

    odd = 1.0

    for jogo in jogos:

        odd *= jogo[
            "odd"
        ]

    return round(
        odd,
        2
    )


# ============================================================
# PROCURAR BILHETE
# ============================================================

def procurar_bilhete():

    if existe_sinal_pendente():

        logger.info(
            "Há um bilhete pendente. "
            "Nenhum novo sinal será procurado."
        )

        return

    fixtures = buscar_proximos_jogos()

    if not fixtures:

        logger.info(
            "Nenhum jogo encontrado "
            "na janela de %.0fh.",
            JANELA_HORAS
        )

        return

    logger.info(
        "%s jogos encontrados "
        "para análise.",
        len(fixtures)
    )

    candidatos = []

    for fixture in fixtures:

        if existe_sinal_pendente():
            return

        times = obter_times(
            fixture
        )

        if not times:
            continue

        odds = obter_odds_1x2(
            fixture
        )

        if not odds:

            logger.info(
                "%s x %s | "
                "sem odds 1X2 Bet365.",
                times["home"],
                times["away"]
            )

            continue

        favorito = identificar_favorito(
            fixture,
            odds
        )

        if not favorito:
            continue

        logger.info(
            "%s x %s | "
            "favorito: %s | "
            "odd %.2f",
            times["home"],
            times["away"],
            favorito["nome"],
            favorito["odd"]
        )

        # Evita consultar histórico
        # se a odd já estiver fora da faixa.

        if not (
            ODD_MIN_SIMPLES
            <= favorito["odd"]
            <= ODD_MAX_SIMPLES
        ):
            continue

        candidato = analisar_candidato(
            fixture,
            favorito
        )

        if candidato:

            candidatos.append(
                candidato
            )

            logger.info(
                "APROVADO: %s | "
                "odd %.2f | "
                "vitórias %.1f%% | "
                "adversário derrotas %.1f%%",
                candidato["favorito"],
                candidato["odd"],
                candidato[
                    "historico_favorito"
                ]["taxa_win"] * 100,
                candidato[
                    "historico_adversario"
                ]["taxa_loss"] * 100
            )

    logger.info(
        "Varredura concluída: "
        "%s candidatos aprovados.",
        len(candidatos)
    )

    if (
        len(candidatos)
        < QTD_JOGOS_BILHETE
    ):

        logger.info(
            "São necessários %s aprovados. "
            "Encontrados: %s.",
            QTD_JOGOS_BILHETE,
            len(candidatos)
        )

        return

    # Ordena todos os aprovados.
    # Maior score primeiro.

    candidatos.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    escolhidos = candidatos[
        :QTD_JOGOS_BILHETE
    ]

    logger.info(
        "Selecionando os %s "
        "melhores candidatos.",
        QTD_JOGOS_BILHETE
    )

    for i, jogo in enumerate(
        escolhidos,
        start=1
    ):

        logger.info(
            "TOP %s: %s | "
            "odd %.2f | "
            "score %.3f",
            i,
            jogo["favorito"],
            jogo["odd"],
            jogo["score"]
        )

    criar_bilhete(
        escolhidos
    )


# ============================================================
# CRIAR BILHETE
# ============================================================

def criar_bilhete(
    jogos
):

    global sinal_atual

    if (
        len(jogos)
        != QTD_JOGOS_BILHETE
    ):
        return

    odd_combinada = (
        calcular_odd_combinada(
            jogos
        )
    )

    with lock_sinal:

        if existe_sinal_pendente():
            return

        sinal_id = (
            datetime.now(
                timezone.utc
            )
            .strftime(
                "%Y%m%d%H%M%S"
            )
        )

        sinal_atual = {
            "id":
                sinal_id,

            "versao":
                "V21.1",

            "status":
                "PENDENTE",

            "resultado":
                None,

            "criado_em":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "odd_combinada":
                odd_combinada,

            "jogos":
                jogos
        }

        salvar_json(
            ARQUIVO_SINAL,
            sinal_atual
        )

    with lock_stats:

        stats[
            "total_sinais"
        ] += 1

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

    mensagem = (
        "🎯 <b>SINAL V21.1 — "
        "4 FAVORITOS</b>\n\n"

        "🏆 <b>RESULTADO FINAL — "
        "VITÓRIA SIMPLES</b>\n\n"
    )

    for indice, jogo in enumerate(
        jogos,
        start=1
    ):

        hf = jogo[
            "historico_favorito"
        ]

        ha = jogo[
            "historico_adversario"
        ]

        mensagem += (
            "━━━━━━━━━━━━━━━━━━\n"

            f"<b>{indice}️⃣ "
            f"{jogo['home']} x "
            f"{jogo['away']}</b>\n"

            f"⭐ <b>{jogo['favorito']} "
            f"VENCE</b>\n"

            f"💰 Odd Bet365: "
            f"<b>{jogo['odd']:.2f}</b>\n"

            f"📈 Favorito: "
            f"<b>{hf['taxa_win'] * 100:.1f}% "
            f"de vitórias</b> "
            f"({hf['wins']}V/"
            f"{hf['jogos']}J)\n"

            f"📉 Adversário: "
            f"<b>{ha['taxa_loss'] * 100:.1f}% "
            f"de derrotas</b> "
            f"({ha['losses']}D/"
            f"{ha['jogos']}J)\n\n"
        )

    mensagem += (
        "━━━━━━━━━━━━━━━━━━\n"

        f"🔥 <b>ODD COMBINADA: "
        f"{odd_combinada:.2f}</b>\n\n"

        f"📊 Histórico utilizado: "
        f"últimos {QTD_HISTORICO} jogos\n"

        f"🕒 Janela analisada: "
        f"{JANELA_HORAS:.0f} horas\n\n"

        "⏳ <b>STATUS: PENDENTE</b>\n\n"

        "🔒 O próximo sinal somente será "
        "procurado após a resolução "
        "deste bilhete."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "BILHETE V21.1 %s CRIADO | "
        "%s jogos | odd %.2f",
        sinal_id,
        len(jogos),
        odd_combinada
    )


# ============================================================
# RESULTADOS
# ============================================================

def buscar_fixture(
    fixture_id
):

    return api_get(
        f"/fixtures/{fixture_id}"
    )


def resolver_selecao(
    jogo,
    fixture
):

    placar = extrair_gols(
        fixture
    )

    if placar is None:
        return None

    gh, ga = placar

    if (
        jogo["favorito_lado"]
        == "home"
    ):

        if gh > ga:
            return "WIN"

        return "LOSS"

    if ga > gh:
        return "WIN"

    return "LOSS"


def verificar_resultado():

    global sinal_atual

    if not existe_sinal_pendente():
        return

    jogos = sinal_atual.get(
        "jogos",
        []
    )

    if (
        len(jogos)
        != QTD_JOGOS_BILHETE
    ):

        logger.warning(
            "Bilhete pendente possui "
            "quantidade inválida de jogos."
        )

        return

    resultados = []

    # O bilhete somente será resolvido
    # quando TODAS as partidas terminarem.

    for jogo in jogos:

        fixture = buscar_fixture(
            jogo["fixture_id"]
        )

        if not fixture:
            return

        status_fixture = (
            fixture.get(
                "status"
            )
        )

        if (
            status_fixture
            != "finished"
        ):

            logger.info(
                "Aguardando: %s x %s | "
                "status=%s",
                jogo["home"],
                jogo["away"],
                status_fixture
            )

            return

        resultado = resolver_selecao(
            jogo,
            fixture
        )

        placar = extrair_gols(
            fixture
        )

        if (
            resultado is None
            or
            placar is None
        ):
            return

        resultados.append({
            "fixture_id":
                jogo["fixture_id"],

            "home":
                jogo["home"],

            "away":
                jogo["away"],

            "favorito":
                jogo["favorito"],

            "odd":
                jogo["odd"],

            "resultado":
                resultado,

            "gols_home":
                placar[0],

            "gols_away":
                placar[1]
        })

    # 4 acertos = WIN.
    # Qualquer erro = LOSS.

    bilhete_win = all(
        item["resultado"] == "WIN"
        for item in resultados
    )

    resultado_bilhete = (
        "WIN"
        if bilhete_win
        else "LOSS"
    )

    with lock_sinal:

        sinal_atual[
            "resultados"
        ] = resultados

        sinal_atual[
            "resultado"
        ] = resultado_bilhete

        sinal_atual[
            "status"
        ] = "RESOLVIDO"

        sinal_atual[
            "resolvido_em"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

        salvar_json(
            ARQUIVO_SINAL,
            sinal_atual
        )

    with lock_stats:

        for item in resultados:

            if (
                item["resultado"]
                == "WIN"
            ):

                stats[
                    "selecoes_win"
                ] += 1

            else:

                stats[
                    "selecoes_loss"
                ] += 1

        if (
            resultado_bilhete
            == "WIN"
        ):

            stats[
                "bilhetes_win"
            ] += 1

        else:

            stats[
                "bilhetes_loss"
            ] += 1

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

    mensagem = (
        "📊 <b>RESULTADO V21.1 — "
        "4 FAVORITOS</b>\n\n"
    )

    for indice, item in enumerate(
        resultados,
        start=1
    ):

        simbolo = (
            "✅"
            if item["resultado"] == "WIN"
            else "❌"
        )

        mensagem += (
            f"{simbolo} <b>JOGO "
            f"{indice}</b>\n"

            f"⚽ {item['home']} "
            f"{item['gols_home']} x "
            f"{item['gols_away']} "
            f"{item['away']}\n"

            f"⭐ {item['favorito']} "
            f"@ {item['odd']:.2f}\n"

            f"Resultado: "
            f"<b>{item['resultado']}</b>\n\n"
        )

    mensagem += (
        "━━━━━━━━━━━━━━━━━━\n"

        f"🎟️ <b>BILHETE: "
        f"{resultado_bilhete}</b>\n\n"

        f"📈 Assertividade dos bilhetes: "
        f"<b>{taxa_bilhetes():.2f}%</b>\n"

        f"🎯 Assertividade individual: "
        f"<b>{taxa_selecoes():.2f}%</b>\n\n"

        f"✅ Bilhetes WIN: "
        f"{stats['bilhetes_win']}\n"

        f"❌ Bilhetes LOSS: "
        f"{stats['bilhetes_loss']}\n\n"

        f"✅ Seleções WIN: "
        f"{stats['selecoes_win']}\n"

        f"❌ Seleções LOSS: "
        f"{stats['selecoes_loss']}\n\n"

        "🔓 Próximo sinal liberado."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "BILHETE V21.1 RESOLVIDO: %s",
        resultado_bilhete
    )


# ============================================================
# LOOP DE ANÁLISE
# ============================================================

def loop_analise():

    logger.info(
        "Thread de análise iniciada."
    )

    time.sleep(10)

    while True:

        try:

            if existe_sinal_pendente():

                logger.info(
                    "Bilhete pendente. "
                    "Busca bloqueada."
                )

            else:

                procurar_bilhete()

        except Exception:

            logger.exception(
                "Erro no loop de análise."
            )

        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# LOOP DE RESULTADOS
# ============================================================

def loop_resultados():

    logger.info(
        "Thread de resultados iniciada."
    )

    time.sleep(30)

    while True:

        try:

            if existe_sinal_pendente():

                verificar_resultado()

        except Exception:

            logger.exception(
                "Erro no loop de resultados."
            )

        time.sleep(
            INTERVALO_RESULTADOS
        )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "online":
            True,

        "versao":
            "V21.1",

        "estrategia":
            "4 favoritos - vitória simples",

        "quantidade_jogos":
            QTD_JOGOS_BILHETE,

        "janela_horas":
            JANELA_HORAS,

        "historico":
            QTD_HISTORICO,

        "odd_min":
            ODD_MIN_SIMPLES,

        "odd_max":
            ODD_MAX_SIMPLES,

        "min_win_favorito":
            MIN_WIN_FAVORITO,

        "min_loss_adversario":
            MIN_LOSS_ADVERSARIO,

        "sinal_pendente":
            existe_sinal_pendente(),

        "total_sinais":
            stats["total_sinais"],

        "wins":
            stats["bilhetes_win"],

        "losses":
            stats["bilhetes_loss"],

        "assertividade_bilhetes":
            taxa_bilhetes(),

        "assertividade_selecoes":
            taxa_selecoes()
    })


@app.route("/status")
def status():

    return jsonify({
        "online":
            True,

        "versao":
            "V21.1",

        "sinal_pendente":
            existe_sinal_pendente(),

        "sinal_atual":
            sinal_atual,

        "config": {

            "qtd_jogos_bilhete":
                QTD_JOGOS_BILHETE,

            "odd_min":
                ODD_MIN_SIMPLES,

            "odd_max":
                ODD_MAX_SIMPLES,

            "min_win_favorito":
                MIN_WIN_FAVORITO,

            "min_loss_adversario":
                MIN_LOSS_ADVERSARIO,

            "historico":
                QTD_HISTORICO,

            "min_historico":
                MIN_JOGOS_HISTORICO,

            "janela_horas":
                JANELA_HORAS
        },

        "stats": {
            **stats,

            "taxa_bilhetes":
                taxa_bilhetes(),

            "taxa_selecoes":
                taxa_selecoes()
        }
    })


@app.route("/health")
def health():

    return jsonify({
        "status":
            "ok",

        "version":
            "V21.1"
    })


# ============================================================
# START
# ============================================================

_threads_iniciadas = False
_threads_lock = threading.Lock()


def iniciar_threads():

    global _threads_iniciadas

    with _threads_lock:

        if _threads_iniciadas:
            return

        _threads_iniciadas = True

        threading.Thread(
            target=loop_analise,
            daemon=True,
            name="analise"
        ).start()

        threading.Thread(
            target=loop_resultados,
            daemon=True,
            name="resultados"
        ).start()

        logger.info(
            "ROBÔ V21.1 INICIADO"
        )

        logger.info(
            "Estratégia: %s favoritos "
            "em vitória simples.",
            QTD_JOGOS_BILHETE
        )

        logger.info(
            "Odds: %.2f até %.2f",
            ODD_MIN_SIMPLES,
            ODD_MAX_SIMPLES
        )

        logger.info(
            "Histórico: últimos %s jogos | "
            "mínimo %s válidos",
            QTD_HISTORICO,
            MIN_JOGOS_HISTORICO
        )

        logger.info(
            "Filtro favorito: >= %.1f%% vitórias",
            MIN_WIN_FAVORITO * 100
        )

        logger.info(
            "Filtro adversário: >= %.1f%% derrotas",
            MIN_LOSS_ADVERSARIO * 100
        )

        logger.info(
            "Janela: próximas %.0f horas "
            "em blocos de até 24h",
            JANELA_HORAS
        )

        logger.info(
            "Apenas 1 bilhete ativo por vez."
        )


iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
