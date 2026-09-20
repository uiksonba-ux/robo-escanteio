import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# ROBÔ V20.1
#
# 1 BILHETE = 2 JOGOS
#
# JOGO 1:
#   Favorito forte
#   Vitória simples
#   Odd 1X2 Bet365 entre 1.20 e 1.70
#
# JOGO 2:
#   Favorito de jogo mais equilibrado
#   Odd 1X2 Bet365 entre 1.71 e 2.20
#   Entrada: 1X ou X2
#
# Apenas 1 bilhete ativo por vez.
# O próximo só é procurado após a resolução do vigente.
# ============================================================


app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-v20")


# ============================================================
# LEITURA SEGURA DAS VARIÁVEIS
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
    valor_original = os.getenv(nome)

    if valor_original is None:
        return float(padrao)

    try:
        valor = limpar_env(valor_original)
        valor = valor.replace(",", ".")
        return float(valor)

    except (ValueError, TypeError):
        logger.warning(
            "Variável %s inválida: %r. Usando padrão %s.",
            nome,
            valor_original,
            padrao
        )
        return float(padrao)


def env_int(nome, padrao):
    valor_original = os.getenv(nome)

    if valor_original is None:
        return int(padrao)

    try:
        valor = limpar_env(valor_original)
        return int(float(valor))

    except (ValueError, TypeError):
        logger.warning(
            "Variável %s inválida: %r. Usando padrão %s.",
            nome,
            valor_original,
            padrao
        )
        return int(padrao)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = env_str(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = env_str("FIVE_DOLLAR_KEY")
TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN")
CHAT_ID = env_str("CHAT_ID")

PORT = env_int("PORT", 10000)


# ============================================================
# JOGO 1 - VITÓRIA SIMPLES
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
    0.65
)

MIN_LOSS_ADVERSARIO = env_float(
    "MIN_LOSS_ADVERSARIO",
    0.40
)


# ============================================================
# JOGO 2 - DUPLA CHANCE
# ============================================================

ODD_MIN_DUPLA = env_float(
    "ODD_MIN_DUPLA",
    1.71
)

ODD_MAX_DUPLA = env_float(
    "ODD_MAX_DUPLA",
    2.20
)

MIN_NAO_PERDE_FAVORITO = env_float(
    "MIN_NAO_PERDE_FAVORITO",
    0.70
)

MIN_NAO_VENCE_ADVERSARIO = env_float(
    "MIN_NAO_VENCE_ADVERSARIO",
    0.60
)


# ============================================================
# HISTÓRICO
# ============================================================

QTD_HISTORICO = env_int(
    "QTD_HISTORICO",
    20
)

MIN_JOGOS_HISTORICO = env_int(
    "MIN_JOGOS_HISTORICO",
    20
)


# ============================================================
# EXECUÇÃO
# ============================================================

JANELA_HORAS = env_float(
    "JANELA_HORAS",
    24
)

# API permite no máximo janela de 24h em /fixtures
JANELA_HORAS = min(
    max(JANELA_HORAS, 1),
    24
)

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
# ARQUIVOS NOVOS DA V20
# ============================================================

ARQUIVO_SINAL = "sinal_v20.json"
ARQUIVO_STATS = "stats_v20.json"
ARQUIVO_CACHE = "cache_v20.json"


# ============================================================
# LOCKS
# ============================================================

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

    "simples_win": 0,
    "simples_loss": 0,

    "dupla_win": 0,
    "dupla_loss": 0
}


stats = carregar_json(
    ARQUIVO_STATS,
    STATS_PADRAO.copy()
)

for chave, valor in STATS_PADRAO.items():
    stats.setdefault(chave, valor)


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


def taxa_simples():

    return calcular_taxa(
        stats["simples_win"],
        stats["simples_loss"]
    )


def taxa_dupla():

    return calcular_taxa(
        stats["dupla_win"],
        stats["dupla_loss"]
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


def salvar_sinal():

    with lock_sinal:

        salvar_json(
            ARQUIVO_SINAL,
            sinal_atual
        )


# ============================================================
# CACHE
# ============================================================

cache = carregar_json(
    ARQUIVO_CACHE,
    {"historicos": {}}
)

if not isinstance(cache, dict):
    cache = {"historicos": {}}

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
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-favoritos-v20.1"
    }


def api_get(endpoint, params=None, tentativas=2):

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
                agora - ultimo_request
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
                "API %s retornou HTTP %s: %s",
                endpoint,
                resposta.status_code,
                resposta.text[:300]
            )

            return None

        try:

            payload = resposta.json()

        except ValueError:

            logger.warning(
                "Resposta não JSON em %s.",
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

        return payload.get("data")

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
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
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
# EXTRAÇÃO DE DADOS
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

    home = teams.get(
        "home"
    ) or {}

    away = teams.get(
        "away"
    ) or {}

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
#
# Formato oficial:
#
# odds:
#   1x2:
#     opening:
#       home
#       draw
#       away
#     closing:
#       home
#       draw
#       away
# ============================================================

def extrair_1x2_de_mercado(mercado):

    if not isinstance(
        mercado,
        dict
    ):
        return None

    # Para jogo futuro, "closing" pode ainda não
    # existir. Então usamos closing quando disponível
    # e opening como fallback.

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

    # Primeiro tenta as odds que já vieram
    # no próprio fixture.

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

    # Fallback: endpoint específico de odds.

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

        mercado = mercados.get(
            "1x2"
        )

        odds = extrair_1x2_de_mercado(
            mercado
        )

        if odds:
            return odds

    return None


# ============================================================
# FAVORITO
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
            "lado": "home",
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
        "lado": "away",
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

def buscar_historico(
    team_id
):

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
                return jogos

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params={
            "status": "finished",
            "order": "desc",
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
# ANALISAR HISTÓRICO DO TIME
# ============================================================

def analisar_time(
    team_id
):

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
        "jogos": validos,

        "wins": wins,
        "draws": draws,
        "losses": losses,

        "taxa_win":
            wins / validos,

        "taxa_draw":
            draws / validos,

        "taxa_loss":
            losses / validos,

        "taxa_nao_perde":
            (
                wins
                + draws
            ) / validos,

        "taxa_nao_vence":
            (
                draws
                + losses
            ) / validos,

        "media_gols_pro":
            gols_pro / validos,

        "media_gols_contra":
            gols_contra / validos
    }


# ============================================================
# CANDIDATO VITÓRIA SIMPLES
# ============================================================

def analisar_simples(
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
        favorito["team_id"]
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
        return None

    if (
        hist_fav["taxa_win"]
        < MIN_WIN_FAVORITO
    ):

        logger.info(
            "Reprovado simples %s: "
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
            "Reprovado simples %s: "
            "derrotas adversário %.1f%% < %.1f%%",
            favorito["nome"],
            hist_adv["taxa_loss"] * 100,
            MIN_LOSS_ADVERSARIO * 100
        )

        return None

    times = obter_times(
        fixture
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

        "favorito_lado":
            favorito["lado"],

        "favorito_id":
            favorito["team_id"],

        "odd":
            odd,

        "mercado":
            "VITORIA_SIMPLES",

        "selecao":
            favorito["nome"],

        "historico_favorito":
            hist_fav,

        "historico_adversario":
            hist_adv
    }


# ============================================================
# CANDIDATO DUPLA CHANCE
# ============================================================

def analisar_dupla(
    fixture,
    favorito
):

    odd = favorito[
        "odd"
    ]

    if not (
        ODD_MIN_DUPLA
        <= odd
        <= ODD_MAX_DUPLA
    ):
        return None

    hist_fav = analisar_time(
        favorito["team_id"]
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
        return None

    if (
        hist_fav[
            "taxa_nao_perde"
        ]
        < MIN_NAO_PERDE_FAVORITO
    ):

        logger.info(
            "Reprovado dupla %s: "
            "não perde %.1f%% < %.1f%%",
            favorito["nome"],
            hist_fav[
                "taxa_nao_perde"
            ] * 100,
            MIN_NAO_PERDE_FAVORITO * 100
        )

        return None

    if (
        hist_adv[
            "taxa_nao_vence"
        ]
        < MIN_NAO_VENCE_ADVERSARIO
    ):

        logger.info(
            "Reprovado dupla %s: "
            "adversário não vence %.1f%% < %.1f%%",
            favorito["nome"],
            hist_adv[
                "taxa_nao_vence"
            ] * 100,
            MIN_NAO_VENCE_ADVERSARIO * 100
        )

        return None

    times = obter_times(
        fixture
    )

    selecao = (
        "1X"
        if favorito["lado"] == "home"
        else "X2"
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

        "favorito_lado":
            favorito["lado"],

        "favorito_id":
            favorito["team_id"],

        "odd_favorito_1x2":
            odd,

        "mercado":
            "DUPLA_CHANCE",

        "selecao":
            selecao,

        "historico_favorito":
            hist_fav,

        "historico_adversario":
            hist_adv
    }


# ============================================================
# PRÓXIMOS JOGOS
# ============================================================

def buscar_proximos_jogos():

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

            "include":
                "odds",

            "per_page":
                100
        }
    )

    if not isinstance(
        dados,
        list
    ):

        return []

    return dados


# ============================================================
# ESCOLHER MELHORES CANDIDATOS
# ============================================================

def score_simples(jogo):

    hf = jogo[
        "historico_favorito"
    ]

    ha = jogo[
        "historico_adversario"
    ]

    # Apenas usado para ordenar candidatos aprovados.
    return (
        hf["taxa_win"]
        +
        ha["taxa_loss"]
    )


def score_dupla(jogo):

    hf = jogo[
        "historico_favorito"
    ]

    ha = jogo[
        "historico_adversario"
    ]

    return (
        hf["taxa_nao_perde"]
        +
        ha["taxa_nao_vence"]
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
            "Nenhum jogo agendado encontrado."
        )

        return

    logger.info(
        "%s jogos encontrados na janela.",
        len(fixtures)
    )

    candidatos_simples = []
    candidatos_dupla = []

    for fixture in fixtures:

        if existe_sinal_pendente():
            return

        fixture_id = fixture.get(
            "id"
        )

        times = obter_times(
            fixture
        )

        if (
            not fixture_id
            or
            not times
        ):
            continue

        odds = obter_odds_1x2(
            fixture
        )

        if not odds:

            logger.info(
                "%s x %s | sem odds 1X2 Bet365.",
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
            "%s x %s | favorito: %s | odd %.2f",
            times["home"],
            times["away"],
            favorito["nome"],
            favorito["odd"]
        )

        # A faixa da odd determina qual
        # análise será executada.
        if (
            ODD_MIN_SIMPLES
            <= favorito["odd"]
            <= ODD_MAX_SIMPLES
        ):

            candidato = analisar_simples(
                fixture,
                favorito
            )

            if candidato:

                candidatos_simples.append(
                    candidato
                )

                logger.info(
                    "APROVADO SIMPLES: %s",
                    favorito["nome"]
                )

        elif (
            ODD_MIN_DUPLA
            <= favorito["odd"]
            <= ODD_MAX_DUPLA
        ):

            candidato = analisar_dupla(
                fixture,
                favorito
            )

            if candidato:

                candidatos_dupla.append(
                    candidato
                )

                logger.info(
                    "APROVADO DUPLA: %s %s",
                    favorito["nome"],
                    candidato["selecao"]
                )

    if (
        not candidatos_simples
        or
        not candidatos_dupla
    ):

        logger.info(
            "Sem combinação completa. "
            "Simples aprovados=%s | "
            "Dupla aprovados=%s",
            len(candidatos_simples),
            len(candidatos_dupla)
        )

        return

    candidatos_simples.sort(
        key=score_simples,
        reverse=True
    )

    candidatos_dupla.sort(
        key=score_dupla,
        reverse=True
    )

    for simples in candidatos_simples:

        for dupla in candidatos_dupla:

            # Nunca colocar o mesmo fixture
            # duas vezes no bilhete.
            if (
                simples["fixture_id"]
                ==
                dupla["fixture_id"]
            ):
                continue

            criar_bilhete(
                simples,
                dupla
            )

            return

    logger.info(
        "Não foi possível formar dois jogos distintos."
    )


# ============================================================
# CRIAR BILHETE
# ============================================================

def criar_bilhete(
    simples,
    dupla
):

    global sinal_atual

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
                "V20.1",

            "status":
                "PENDENTE",

            "resultado":
                None,

            "criado_em":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "jogo_simples":
                simples,

            "jogo_dupla":
                dupla
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

    hf1 = simples[
        "historico_favorito"
    ]

    ha1 = simples[
        "historico_adversario"
    ]

    hf2 = dupla[
        "historico_favorito"
    ]

    ha2 = dupla[
        "historico_adversario"
    ]

    mensagem = (
        "🎯 <b>SINAL V20.1 — 2 JOGOS</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⭐ <b>JOGO 1 — VITÓRIA SIMPLES</b>\n\n"

        f"⚽ {simples['home']} x {simples['away']}\n"
        f"🏆 <b>{simples['favorito']} VENCE</b>\n"
        f"💰 Odd Bet365 1X2: <b>{simples['odd']:.2f}</b>\n\n"

        f"📊 Favorito:\n"
        f"• {hf1['wins']}V / {hf1['draws']}E / {hf1['losses']}D\n"
        f"• Vitórias: <b>{hf1['taxa_win'] * 100:.1f}%</b>\n"
        f"• Média gols: {hf1['media_gols_pro']:.2f}\n\n"

        f"📉 Adversário:\n"
        f"• Derrotas: <b>{ha1['taxa_loss'] * 100:.1f}%</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🛡️ <b>JOGO 2 — DUPLA CHANCE</b>\n\n"

        f"⚽ {dupla['home']} x {dupla['away']}\n"
        f"⭐ Favorito: <b>{dupla['favorito']}</b>\n"
        f"🎟️ Entrada: <b>{dupla['selecao']}</b>\n"
        f"📌 Odd 1X2 do favorito: "
        f"{dupla['odd_favorito_1x2']:.2f}\n\n"

        f"📊 Favorito:\n"
        f"• {hf2['wins']}V / {hf2['draws']}E / {hf2['losses']}D\n"
        f"• Não perdeu: "
        f"<b>{hf2['taxa_nao_perde'] * 100:.1f}%</b>\n\n"

        f"📉 Adversário:\n"
        f"• Não venceu: "
        f"<b>{ha2['taxa_nao_vence'] * 100:.1f}%</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ <b>BILHETE PENDENTE</b>\n\n"

        "🔒 Nenhum outro sinal será enviado "
        "até este bilhete ser resolvido."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "BILHETE %s CRIADO.",
        sinal_id
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


def resolver_simples(
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

        return (
            "WIN"
            if gh > ga
            else "LOSS"
        )

    return (
        "WIN"
        if ga > gh
        else "LOSS"
    )


def resolver_dupla(
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
        jogo["selecao"]
        == "1X"
    ):

        return (
            "WIN"
            if gh >= ga
            else "LOSS"
        )

    if (
        jogo["selecao"]
        == "X2"
    ):

        return (
            "WIN"
            if ga >= gh
            else "LOSS"
        )

    return None


def verificar_resultado():

    global sinal_atual

    if not existe_sinal_pendente():
        return

    simples = sinal_atual.get(
        "jogo_simples"
    )

    dupla = sinal_atual.get(
        "jogo_dupla"
    )

    if (
        not simples
        or
        not dupla
    ):
        return

    fixture1 = buscar_fixture(
        simples["fixture_id"]
    )

    if not fixture1:
        return

    # Evita gastar segunda requisição
    # enquanto o primeiro ainda não terminou.
    if (
        fixture1.get("status")
        != "finished"
    ):
        return

    fixture2 = buscar_fixture(
        dupla["fixture_id"]
    )

    if not fixture2:
        return

    if (
        fixture2.get("status")
        != "finished"
    ):
        return

    r1 = resolver_simples(
        simples,
        fixture1
    )

    r2 = resolver_dupla(
        dupla,
        fixture2
    )

    if (
        r1 is None
        or
        r2 is None
    ):
        return

    resultado = (
        "WIN"
        if (
            r1 == "WIN"
            and
            r2 == "WIN"
        )
        else "LOSS"
    )

    placar1 = extrair_gols(
        fixture1
    )

    placar2 = extrair_gols(
        fixture2
    )

    with lock_sinal:

        sinal_atual[
            "resultado_simples"
        ] = r1

        sinal_atual[
            "resultado_dupla"
        ] = r2

        sinal_atual[
            "resultado"
        ] = resultado

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

        if r1 == "WIN":
            stats["simples_win"] += 1
        else:
            stats["simples_loss"] += 1

        if r2 == "WIN":
            stats["dupla_win"] += 1
        else:
            stats["dupla_loss"] += 1

        if resultado == "WIN":
            stats["bilhetes_win"] += 1
        else:
            stats["bilhetes_loss"] += 1

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

    mensagem = (
        "📊 <b>RESULTADO V20.1</b>\n\n"

        "⭐ <b>VITÓRIA SIMPLES</b>\n"
        f"⚽ {simples['home']} x {simples['away']}\n"
        f"🔢 {placar1[0]} x {placar1[1]}\n"
        f"🎯 {simples['favorito']} vence\n"
        f"Resultado: <b>{r1}</b>\n\n"

        "🛡️ <b>DUPLA CHANCE</b>\n"
        f"⚽ {dupla['home']} x {dupla['away']}\n"
        f"🔢 {placar2[0]} x {placar2[1]}\n"
        f"🎯 {dupla['selecao']}\n"
        f"Resultado: <b>{r2}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        f"🎟️ BILHETE: <b>{resultado}</b>\n\n"

        f"📈 Bilhetes: <b>{taxa_bilhetes():.2f}%</b>\n"
        f"✅ {stats['bilhetes_win']} WIN\n"
        f"❌ {stats['bilhetes_loss']} LOSS\n\n"

        f"⭐ Vitória simples: {taxa_simples():.2f}%\n"
        f"🛡️ Dupla chance: {taxa_dupla():.2f}%\n\n"

        "🔓 Próximo sinal liberado."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "BILHETE %s RESOLVIDO: %s",
        sinal_atual.get("id"),
        resultado
    )


# ============================================================
# LOOPS
# ============================================================

def loop_analise():

    logger.info(
        "Thread de análise iniciada."
    )

    time.sleep(10)

    while True:

        try:

            if not existe_sinal_pendente():

                procurar_bilhete()

            else:

                logger.info(
                    "Bilhete pendente. "
                    "Análise bloqueada."
                )

        except Exception:

            logger.exception(
                "Erro no loop de análise."
            )

        time.sleep(
            INTERVALO_ANALISE
        )


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
        "online": True,
        "versao": "V20.1",

        "estrategia": {
            "bilhete":
                "2 jogos",

            "jogo_1":
                "Vitória simples do favorito",

            "odd_simples":
                [
                    ODD_MIN_SIMPLES,
                    ODD_MAX_SIMPLES
                ],

            "jogo_2":
                "Dupla chance no favorito",

            "odd_favorito_dupla":
                [
                    ODD_MIN_DUPLA,
                    ODD_MAX_DUPLA
                ]
        },

        "sinal_pendente":
            existe_sinal_pendente(),

        "stats": {
            "total_sinais":
                stats["total_sinais"],

            "wins":
                stats["bilhetes_win"],

            "losses":
                stats["bilhetes_loss"],

            "assertividade":
                taxa_bilhetes()
        }
    })


@app.route("/status")
def status():

    return jsonify({
        "online": True,

        "versao":
            "V20.1",

        "sinal_pendente":
            existe_sinal_pendente(),

        "sinal_atual":
            sinal_atual,

        "config": {
            "simples": {
                "odd_min":
                    ODD_MIN_SIMPLES,

                "odd_max":
                    ODD_MAX_SIMPLES,

                "win_favorito":
                    MIN_WIN_FAVORITO,

                "loss_adversario":
                    MIN_LOSS_ADVERSARIO
            },

            "dupla": {
                "odd_min":
                    ODD_MIN_DUPLA,

                "odd_max":
                    ODD_MAX_DUPLA,

                "nao_perde_favorito":
                    MIN_NAO_PERDE_FAVORITO,

                "nao_vence_adversario":
                    MIN_NAO_VENCE_ADVERSARIO
            },

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

            "taxa_simples":
                taxa_simples(),

            "taxa_dupla":
                taxa_dupla()
        }
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "version": "V20.1"
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
            "ROBÔ V20.1 INICIADO"
        )

        logger.info(
            "Vitória simples: odd %.2f a %.2f",
            ODD_MIN_SIMPLES,
            ODD_MAX_SIMPLES
        )

        logger.info(
            "Dupla chance: favorito 1X2 %.2f a %.2f",
            ODD_MIN_DUPLA,
            ODD_MAX_DUPLA
        )

        logger.info(
            "Histórico: %s jogos | mínimo %s",
            QTD_HISTORICO,
            MIN_JOGOS_HISTORICO
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
