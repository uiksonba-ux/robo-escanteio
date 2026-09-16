import os
import json
import time
import logging
import threading
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

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = (
    os.getenv("FIVE_DOLLAR_API_KEY")
    or os.getenv("FIVE_DOLLAR_KEY")
    or ""
).strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))

ENTRADA_INICIAL = float(
    os.getenv("ENTRADA_INICIAL", "10")
)

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(
    os.getenv("MAX_GALES", "2")
)

ODD_MINIMA = float(
    os.getenv("ODD_MINIMA", "1.80")
)

ODD_MAXIMA = float(
    os.getenv("ODD_MAXIMA", "2.50")
)

ODD_ALVO = float(
    os.getenv("ODD_ALVO", "2.00")
)

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "900")
)

MAX_CONSULTAS_ODDS = int(
    os.getenv("MAX_CONSULTAS_ODDS", "25")
)

ARQUIVO_ESTADO = "estado.json"

lock = threading.Lock()


# ============================================================
# ESTADO INICIAL
# ============================================================

def criar_estado_inicial():

    bancas = {}

    for numero in range(1, TOTAL_BANCAS + 1):

        bancas[str(numero)] = {
            "status": "livre",
            "gale": 0,
            "sinal_id": None
        }

    return {
        "stats": {
            "enviados": 0,
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "assertividade": 0.0,
            "saldo_teorico": 0.0
        },
        "bancas": bancas,
        "sinais": {}
    }


def carregar_estado():

    inicial = criar_estado_inicial()

    if not os.path.exists(ARQUIVO_ESTADO):
        return inicial

    try:

        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as arquivo:

            dados = json.load(arquivo)

        if not isinstance(dados, dict):
            return inicial

        dados.setdefault(
            "stats",
            inicial["stats"]
        )

        dados.setdefault(
            "bancas",
            {}
        )

        dados.setdefault(
            "sinais",
            {}
        )

        for chave, valor in inicial["stats"].items():

            dados["stats"].setdefault(
                chave,
                valor
            )

        for numero in range(
            1,
            TOTAL_BANCAS + 1
        ):

            chave = str(numero)

            if chave not in dados["bancas"]:

                dados["bancas"][chave] = {
                    "status": "livre",
                    "gale": 0,
                    "sinal_id": None
                }

        return dados

    except Exception as erro:

        logging.error(
            "Erro carregando estado: %s",
            erro
        )

        return inicial


estado = carregar_estado()


def salvar_estado():

    try:

        temporario = (
            ARQUIVO_ESTADO + ".tmp"
        )

        with open(
            temporario,
            "w",
            encoding="utf-8"
        ) as arquivo:

            json.dump(
                estado,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporario,
            ARQUIVO_ESTADO
        )

    except Exception as erro:

        logging.error(
            "Erro salvando estado: %s",
            erro
        )


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        logging.error(
            "API KEY não configurada."
        )

        return None

    url = (
        BASE_API
        + "/"
        + endpoint.lstrip("/")
    )

    headers = {
        "Authorization":
            "Bearer " + API_KEY,
        "Accept":
            "application/json",
        "User-Agent":
            "robo-combinado/4.0"
    }

    try:

        logging.info(
            "GET %s | params=%s",
            url,
            params
        )

        resposta = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        logging.info(
            "URL FINAL API: %s",
            resposta.url
        )

        if resposta.status_code != 200:

            logging.error(
                "API %s | %s",
                resposta.status_code,
                resposta.text[:3000]
            )

            return None

        try:

            dados = resposta.json()

        except ValueError:

            logging.error(
                "API retornou resposta inválida."
            )

            return None

        if (
            isinstance(dados, dict)
            and dados.get("success") == 0
        ):

            logging.error(
                "Erro API: %s",
                json.dumps(
                    dados,
                    ensure_ascii=False
                )
            )

            return None

        return dados

    except requests.RequestException as erro:

        logging.error(
            "Erro conexão API: %s",
            erro
        )

        return None

    except Exception as erro:

        logging.exception(
            "Erro inesperado API: %s",
            erro
        )

        return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN:

        logging.warning(
            "TELEGRAM_TOKEN não configurado."
        )

        return False

    if not CHAT_ID:

        logging.warning(
            "CHAT_ID não configurado."
        )

        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        resposta = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if resposta.status_code != 200:

            logging.error(
                "Telegram %s | %s",
                resposta.status_code,
                resposta.text[:2000]
            )

            return False

        return True

    except Exception as erro:

        logging.error(
            "Erro Telegram: %s",
            erro
        )

        return False


# ============================================================
# FIXTURES
# ============================================================

def extrair_lista_fixtures(resposta):

    if not resposta:
        return []

    if isinstance(resposta, list):
        return resposta

    if not isinstance(resposta, dict):
        return []

    data = resposta.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        fixtures = data.get(
            "fixtures"
        )

        if isinstance(fixtures, list):
            return fixtures

        results = data.get(
            "results"
        )

        if isinstance(results, list):
            return results

        dados = data.get(
            "data"
        )

        if isinstance(dados, list):
            return dados

    return []


def buscar_fixtures():

    # ========================================================
    # CORREÇÃO IMPORTANTE
    #
    # API exige:
    #
    # start_time = UNIX TIMESTAMP EM SEGUNDOS UTC
    # end_time   = UNIX TIMESTAMP EM SEGUNDOS UTC
    #
    # NÃO enviar ISO 8601.
    # NÃO enviar milissegundos.
    # ========================================================

    agora_utc = datetime.now(
        timezone.utc
    )

    fim_utc = (
        agora_utc
        + timedelta(
            hours=JANELA_HORAS
        )
    )

    start_timestamp = int(
        agora_utc.timestamp()
    )

    end_timestamp = int(
        fim_utc.timestamp()
    )

    logging.info(
        "UTC agora: %s",
        agora_utc.isoformat()
    )

    logging.info(
        "UTC fim: %s",
        fim_utc.isoformat()
    )

    logging.info(
        "start_time UNIX: %d",
        start_timestamp
    )

    logging.info(
        "end_time UNIX: %d",
        end_timestamp
    )

    # Proteção contra timestamp em milissegundos

    if start_timestamp > 9999999999:

        start_timestamp = int(
            start_timestamp / 1000
        )

    if end_timestamp > 9999999999:

        end_timestamp = int(
            end_timestamp / 1000
        )

    params = {
        "start_time": start_timestamp,
        "end_time": end_timestamp,
        "status": "scheduled",
        "per_page": 100
    }

    resposta = api_get(
        "fixtures",
        params=params
    )

    fixtures = extrair_lista_fixtures(
        resposta
    )

    logging.info(
        "Fixtures encontrados: %d",
        len(fixtures)
    )

    return fixtures


# ============================================================
# DADOS DO FIXTURE
# ============================================================

def obter_fixture_id(fixture):

    if not isinstance(
        fixture,
        dict
    ):
        return None

    fixture_id = fixture.get(
        "id"
    )

    if fixture_id is not None:
        return fixture_id

    return fixture.get(
        "fixture_id"
    )


def obter_nome_time(
    valor,
    padrao
):

    if isinstance(valor, dict):

        nome = valor.get(
            "name"
        )

        if nome:
            return nome

        nome = valor.get(
            "team_name"
        )

        if nome:
            return nome

        nome = valor.get(
            "short_name"
        )

        if nome:
            return nome

    if isinstance(valor, str):

        if valor.strip():
            return valor.strip()

    return padrao


def obter_times(fixture):

    home = "Casa"
    away = "Fora"

    if not isinstance(
        fixture,
        dict
    ):
        return home, away

    home = obter_nome_time(
        fixture.get("home"),
        home
    )

    away = obter_nome_time(
        fixture.get("away"),
        away
    )

    home = obter_nome_time(
        fixture.get("home_team"),
        home
    )

    away = obter_nome_time(
        fixture.get("away_team"),
        away
    )

    teams = fixture.get(
        "teams"
    )

    if isinstance(teams, dict):

        home = obter_nome_time(
            teams.get("home"),
            home
        )

        away = obter_nome_time(
            teams.get("away"),
            away
        )

    return home, away


# ============================================================
# ODDS
# ============================================================

def buscar_odds(fixture_id):

    endpoint = (
        "fixtures/"
        + str(fixture_id)
        + "/odds"
    )

    resposta = api_get(
        endpoint
    )

    if resposta:

        logging.info(
            "Estrutura odds fixture %s: %s",
            fixture_id,
            json.dumps(
                resposta,
                ensure_ascii=False
            )[:5000]
        )

    return resposta


def selecionar_pre_jogo(
    mercado
):

    if not isinstance(
        mercado,
        dict
    ):
        return None, None

    closing = mercado.get(
        "closing"
    )

    if isinstance(
        closing,
        dict
    ):
        return closing, "closing"

    opening = mercado.get(
        "opening"
    )

    if isinstance(
        opening,
        dict
    ):
        return opening, "opening"

    return None, None


# ============================================================
# EXTRAÇÃO DE OVER ESCANTEIOS + UNDER GOLS
# ============================================================

def extrair_combinacoes(
    resposta
):

    combinacoes = []

    if not isinstance(
        resposta,
        dict
    ):
        return combinacoes

    data = resposta.get(
        "data"
    )

    if not isinstance(
        data,
        dict
    ):
        return combinacoes

    bookmakers = data.get(
        "bookmakers"
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return combinacoes

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome_bookmaker = (
            bookmaker.get("name")
            or "Bookmaker"
        )

        odds = bookmaker.get(
            "odds"
        )

        if not isinstance(
            odds,
            dict
        ):
            continue

        # Estrutura confirmada pelo seu log:
        #
        # goal_line:
        # closing:
        #   line: 2.75
        #   over: 1.85
        #   under: 1.95
        #
        # corner_line:
        # closing:
        #   line: 10.5
        #   over: 1.975
        #   under: 1.825

        goal_line = odds.get(
            "goal_line"
        )

        corner_line = odds.get(
            "corner_line"
        )

        gols, origem_gols = (
            selecionar_pre_jogo(
                goal_line
            )
        )

        cantos, origem_cantos = (
            selecionar_pre_jogo(
                corner_line
            )
        )

        if not gols:
            continue

        if not cantos:
            continue

        linha_gols = gols.get(
            "line"
        )

        odd_under = gols.get(
            "under"
        )

        linha_cantos = cantos.get(
            "line"
        )

        odd_over = cantos.get(
            "over"
        )

        if linha_gols is None:
            continue

        if odd_under is None:
            continue

        if linha_cantos is None:
            continue

        if odd_over is None:
            continue

        try:

            linha_gols = float(
                linha_gols
            )

            odd_under = float(
                odd_under
            )

            linha_cantos = float(
                linha_cantos
            )

            odd_over = float(
                odd_over
            )

        except (
            TypeError,
            ValueError
        ):

            continue

        if odd_under <= 1:
            continue

        if odd_over <= 1:
            continue

        odd_combinada = (
            odd_under
            * odd_over
        )

        odd_combinada = round(
            odd_combinada,
            3
        )

        combinacao = {
            "bookmaker":
                nome_bookmaker,

            "linha_gols":
                linha_gols,

            "odd_gols":
                odd_under,

            "linha_cantos":
                linha_cantos,

            "odd_cantos":
                odd_over,

            "odd_combinada":
                odd_combinada,

            "origem_gols":
                origem_gols,

            "origem_cantos":
                origem_cantos
        }

        combinacoes.append(
            combinacao
        )

    return combinacoes


def escolher_combinacao(
    resposta
):

    combinacoes = (
        extrair_combinacoes(
            resposta
        )
    )

    validas = []

    for item in combinacoes:

        logging.info(
            "%s | "
            "Over %.2f cantos @%.3f | "
            "Under %.2f gols @%.3f | "
            "Combinada %.3f",
            item["bookmaker"],
            item["linha_cantos"],
            item["odd_cantos"],
            item["linha_gols"],
            item["odd_gols"],
            item["odd_combinada"]
        )

        odd = item[
            "odd_combinada"
        ]

        if (
            ODD_MINIMA
            <= odd
            <= ODD_MAXIMA
        ):

            validas.append(
                item
            )

    if not validas:
        return None

    validas.sort(
        key=lambda item:
        abs(
            item["odd_combinada"]
            - ODD_ALVO
        )
    )

    return validas[0]


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():

    livres = []

    for numero, banca in (
        estado["bancas"].items()
    ):

        if (
            banca.get("status")
            == "livre"
        ):

            livres.append(
                int(numero)
            )

    return sorted(
        livres
    )


def fixture_ja_utilizado(
    fixture_id
):

    for sinal in (
        estado["sinais"].values()
    ):

        antigo_id = sinal.get(
            "fixture_id"
        )

        if (
            str(antigo_id)
            == str(fixture_id)
        ):

            return True

    return False


# ============================================================
# CRIAÇÃO DO SINAL
# ============================================================

def criar_sinal(
    fixture,
    combinacao,
    numero_banca
):

    fixture_id = obter_fixture_id(
        fixture
    )

    home, away = obter_times(
        fixture
    )

    banca = estado["bancas"][
        str(numero_banca)
    ]

    gale = int(
        banca.get(
            "gale",
            0
        )
    )

    entrada = (
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** gale
        )
    )

    entrada = round(
        entrada,
        2
    )

    sinal_id = (
        str(fixture_id)
        + "-B"
        + str(numero_banca)
        + "-"
        + str(int(time.time()))
    )

    sinal = {
        "id":
            sinal_id,

        "fixture_id":
            fixture_id,

        "home":
            home,

        "away":
            away,

        "banca":
            numero_banca,

        "gale":
            gale,

        "entrada":
            entrada,

        "bookmaker":
            combinacao[
                "bookmaker"
            ],

        "linha_cantos":
            combinacao[
                "linha_cantos"
            ],

        "odd_cantos":
            combinacao[
                "odd_cantos"
            ],

        "linha_gols":
            combinacao[
                "linha_gols"
            ],

        "odd_gols":
            combinacao[
                "odd_gols"
            ],

        "odd_combinada":
            combinacao[
                "odd_combinada"
            ],

        "status":
            "PENDENTE",

        "criado_em":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    return sinal


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def montar_mensagem_sinal(
    sinal
):

    texto = (
        "🎯 <b>NOVO SINAL PRÉ-JOGO</b>\n\n"

        "⚽ <b>"
        + str(sinal["home"])
        + " x "
        + str(sinal["away"])
        + "</b>\n\n"

        "🏦 Banca: <b>"
        + str(sinal["banca"])
        + "</b>\n"

        "🔁 Gale: <b>"
        + str(sinal["gale"])
        + "</b>\n"

        "💰 Entrada: <b>R$ "
        + format(
            sinal["entrada"],
            ".2f"
        )
        + "</b>\n\n"

        "🚩 Over <b>"
        + str(
            sinal[
                "linha_cantos"
            ]
        )
        + "</b> escanteios\n"

        "Odd individual: <b>"
        + str(
            sinal[
                "odd_cantos"
            ]
        )
        + "</b>\n\n"

        "⚽ Under <b>"
        + str(
            sinal[
                "linha_gols"
            ]
        )
        + "</b> gols\n"

        "Odd individual: <b>"
        + str(
            sinal[
                "odd_gols"
            ]
        )
        + "</b>\n\n"

        "🔥 Odd combinada teórica: <b>"
        + str(
            sinal[
                "odd_combinada"
            ]
        )
        + "</b>\n\n"

        "🏢 Bookmaker: <b>"
        + str(
            sinal[
                "bookmaker"
            ]
        )
        + "</b>"
    )

    return texto


def registrar_sinal(
    sinal
):

    mensagem = (
        montar_mensagem_sinal(
            sinal
        )
    )

    enviado = enviar_telegram(
        mensagem
    )

    if not enviado:

        logging.error(
            "Sinal %s não registrado "
            "porque Telegram falhou.",
            sinal["id"]
        )

        return False

    estado["sinais"][
        sinal["id"]
    ] = sinal

    banca = estado["bancas"][
        str(
            sinal["banca"]
        )
    ]

    banca["status"] = (
        "ocupada"
    )

    banca["sinal_id"] = (
        sinal["id"]
    )

    estado["stats"][
        "enviados"
    ] += 1

    salvar_estado()

    logging.info(
        "SINAL ENVIADO | "
        "%s x %s | "
        "Banca %s | "
        "Gale %s | "
        "Entrada R$ %.2f | "
        "Odd %.3f",

        sinal["home"],
        sinal["away"],
        sinal["banca"],
        sinal["gale"],
        sinal["entrada"],
        sinal["odd_combinada"]
    )

    return True


# ============================================================
# BUSCAR RESULTADO
# ============================================================

def buscar_fixture_final(
    fixture_id
):

    endpoint = (
        "fixtures/"
        + str(fixture_id)
    )

    return api_get(
        endpoint
    )


def extrair_fixture_detalhado(
    resposta
):

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

    fixture = data.get(
        "fixture"
    )

    if isinstance(
        fixture,
        dict
    ):

        return fixture

    return data


def extrair_totais(
    fixture
):

    if not isinstance(
        fixture,
        dict
    ):

        return None

    goals = fixture.get(
        "goals"
    )

    corners = fixture.get(
        "corners"
    )

    if not isinstance(
        goals,
        dict
    ):

        return None

    if not isinstance(
        corners,
        dict
    ):

        return None

    gols_home = goals.get(
        "home"
    )

    gols_away = goals.get(
        "away"
    )

    cantos_home = corners.get(
        "home"
    )

    cantos_away = corners.get(
        "away"
    )

    valores = [
        gols_home,
        gols_away,
        cantos_home,
        cantos_away
    ]

    for valor in valores:

        if valor is None:
            return None

    try:

        total_gols = (
            float(gols_home)
            + float(gols_away)
        )

        total_cantos = (
            float(cantos_home)
            + float(cantos_away)
        )

    except (
        TypeError,
        ValueError
    ):

        return None

    return {
        "gols":
            total_gols,

        "cantos":
            total_cantos
    }


# ============================================================
# AVALIAR LINHA
# ============================================================

def avaliar_linha_simples(
    total,
    linha,
    lado
):

    total = float(total)
    linha = float(linha)

    if lado == "over":

        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    if total < linha:
        return "WIN"

    if total == linha:
        return "PUSH"

    return "LOSS"


def avaliar_linha(
    total,
    linha,
    lado
):

    total = float(total)
    linha = float(linha)

    inteiro = int(
        linha
    )

    fracao = round(
        linha - inteiro,
        2
    )

    # Linha inteira
    if fracao == 0.0:

        return avaliar_linha_simples(
            total,
            linha,
            lado
        )

    # Linha .5
    if fracao == 0.5:

        return avaliar_linha_simples(
            total,
            linha,
            lado
        )

    # Asian .25
    if fracao == 0.25:

        linha_a = float(
            inteiro
        )

        linha_b = (
            float(inteiro)
            + 0.5
        )

    # Asian .75
    elif fracao == 0.75:

        linha_a = (
            float(inteiro)
            + 0.5
        )

        linha_b = (
            float(inteiro)
            + 1.0
        )

    else:

        return avaliar_linha_simples(
            total,
            linha,
            lado
        )

    resultado_a = (
        avaliar_linha_simples(
            total,
            linha_a,
            lado
        )
    )

    resultado_b = (
        avaliar_linha_simples(
            total,
            linha_b,
            lado
        )
    )

    resultados = {
        resultado_a,
        resultado_b
    }

    if resultados == {"WIN"}:
        return "WIN"

    if resultados == {"LOSS"}:
        return "LOSS"

    if resultados == {"PUSH"}:
        return "PUSH"

    if resultados == {
        "WIN",
        "PUSH"
    }:
        return "HALF_WIN"

    if resultados == {
        "LOSS",
        "PUSH"
    }:
        return "HALF_LOSS"

    if resultados == {
        "WIN",
        "LOSS"
    }:
        return "PUSH"

    return "PUSH"


# ============================================================
# RETORNO DE CADA PERNA
# ============================================================

def retorno_perna(
    valor,
    odd,
    resultado
):

    valor = float(
        valor
    )

    odd = float(
        odd
    )

    if resultado == "WIN":

        return (
            valor
            * odd
        )

    if resultado == "PUSH":

        return valor

    if resultado == "HALF_WIN":

        metade = (
            valor / 2
        )

        return (
            metade * odd
            + metade
        )

    if resultado == "HALF_LOSS":

        return (
            valor / 2
        )

    return 0.0


# ============================================================
# RETORNO COMBINADO
# ============================================================

def calcular_retorno_combinada(
    sinal,
    resultado_cantos,
    resultado_gols
):

    entrada = float(
        sinal["entrada"]
    )

    retorno_cantos = (
        retorno_perna(
            entrada,
            sinal[
                "odd_cantos"
            ],
            resultado_cantos
        )
    )

    if retorno_cantos <= 0:

        return 0.0

    retorno_final = (
        retorno_perna(
            retorno_cantos,
            sinal[
                "odd_gols"
            ],
            resultado_gols
        )
    )

    return round(
        retorno_final,
        2
    )


def classificar_retorno(
    entrada,
    retorno
):

    entrada = float(
        entrada
    )

    retorno = float(
        retorno
    )

    lucro = round(
        retorno - entrada,
        2
    )

    if lucro > 0:
        return "WIN"

    if lucro < 0:
        return "LOSS"

    return "PUSH"


# ============================================================
# ASSERTIVIDADE
# ============================================================

def atualizar_assertividade():

    stats = estado[
        "stats"
    ]

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

    decisoes = (
        wins
        + losses
    )

    if decisoes <= 0:

        stats[
            "assertividade"
        ] = 0.0

        return

    taxa = (
        wins
        / decisoes
        * 100
    )

    stats[
        "assertividade"
    ] = round(
        taxa,
        2
    )


# ============================================================
# MENSAGEM RESULTADO
# ============================================================

def montar_mensagem_resultado(
    sinal,
    totais,
    resultado_cantos,
    resultado_gols,
    resultado_final,
    retorno,
    lucro
):

    assertividade = estado[
        "stats"
    ][
        "assertividade"
    ]

    saldo = estado[
        "stats"
    ][
        "saldo_teorico"
    ]

    texto = (
        "📊 <b>SINAL RESOLVIDO</b>\n\n"

        "⚽ "
        + str(sinal["home"])
        + " x "
        + str(sinal["away"])
        + "\n\n"

        "🏦 Banca: "
        + str(sinal["banca"])
        + "\n"

        "🔁 Gale: "
        + str(sinal["gale"])
        + "\n\n"

        "🚩 Escanteios: "
        + str(
            int(
                totais["cantos"]
            )
        )
        + "\n"

        "Over "
        + str(
            sinal[
                "linha_cantos"
            ]
        )
        + " = <b>"
        + resultado_cantos
        + "</b>\n\n"

        "⚽ Gols: "
        + str(
            int(
                totais["gols"]
            )
        )
        + "\n"

        "Under "
        + str(
            sinal[
                "linha_gols"
            ]
        )
        + " = <b>"
        + resultado_gols
        + "</b>\n\n"

        "🎯 Resultado final: <b>"
        + resultado_final
        + "</b>\n\n"

        "💵 Entrada: R$ "
        + format(
            sinal["entrada"],
            ".2f"
        )
        + "\n"

        "💰 Retorno teórico: R$ "
        + format(
            retorno,
            ".2f"
        )
        + "\n"

        "📈 Lucro/Prejuízo: R$ "
        + format(
            lucro,
            "+.2f"
        )
        + "\n\n"

        "📊 Assertividade: <b>"
        + format(
            assertividade,
            ".2f"
        )
        + "%</b>\n"

        "💼 Saldo teórico: <b>R$ "
        + format(
            saldo,
            ".2f"
        )
        + "</b>"
    )

    return texto


# ============================================================
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal
):

    fixture_id = sinal[
        "fixture_id"
    ]

    resposta = (
        buscar_fixture_final(
            fixture_id
        )
    )

    fixture = (
        extrair_fixture_detalhado(
            resposta
        )
    )

    if not fixture:

        logging.info(
            "Fixture %s ainda sem "
            "dados detalhados.",
            fixture_id
        )

        return False

    totais = extrair_totais(
        fixture
    )

    if not totais:

        logging.info(
            "Fixture %s ainda sem "
            "gols/cantos completos.",
            fixture_id
        )

        return False

    resultado_cantos = (
        avaliar_linha(
            totais["cantos"],
            sinal[
                "linha_cantos"
            ],
            "over"
        )
    )

    resultado_gols = (
        avaliar_linha(
            totais["gols"],
            sinal[
                "linha_gols"
            ],
            "under"
        )
    )

    retorno = (
        calcular_retorno_combinada(
            sinal,
            resultado_cantos,
            resultado_gols
        )
    )

    resultado_final = (
        classificar_retorno(
            sinal["entrada"],
            retorno
        )
    )

    lucro = round(
        retorno
        - float(
            sinal["entrada"]
        ),
        2
    )

    sinal["status"] = (
        "RESOLVIDO"
    )

    sinal["resultado"] = (
        resultado_final
    )

    sinal[
        "resultado_cantos"
    ] = resultado_cantos

    sinal[
        "resultado_gols"
    ] = resultado_gols

    sinal[
        "gols_totais"
    ] = totais["gols"]

    sinal[
        "cantos_totais"
    ] = totais["cantos"]

    sinal[
        "retorno_teorico"
    ] = retorno

    sinal[
        "lucro_teorico"
    ] = lucro

    sinal[
        "resolvido_em"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    stats = estado[
        "stats"
    ]

    stats[
        "resolvidos"
    ] += 1

    saldo_atual = float(
        stats.get(
            "saldo_teorico",
            0
        )
    )

    stats[
        "saldo_teorico"
    ] = round(
        saldo_atual + lucro,
        2
    )

    if resultado_final == "WIN":

        stats[
            "wins"
        ] += 1

    elif resultado_final == "LOSS":

        stats[
            "losses"
        ] += 1

    else:

        stats[
            "pushes"
        ] += 1

    # ========================================================
    # CONTROLE DO GALE
    # ========================================================

    banca = estado[
        "bancas"
    ][
        str(
            sinal["banca"]
        )
    ]

    gale_atual = int(
        banca.get(
            "gale",
            0
        )
    )

    if resultado_final == "LOSS":

        if gale_atual < MAX_GALES:

            banca["gale"] = (
                gale_atual + 1
            )

            logging.info(
                "Banca %s avançou "
                "para Gale %s",
                sinal["banca"],
                banca["gale"]
            )

        else:

            banca["gale"] = 0

            logging.info(
                "Banca %s perdeu "
                "o último Gale. "
                "Novo ciclo iniciado.",
                sinal["banca"]
            )

    elif resultado_final == "WIN":

        banca["gale"] = 0

    else:

        # PUSH não aumenta Gale
        banca["gale"] = (
            gale_atual
        )

    banca["status"] = (
        "livre"
    )

    banca["sinal_id"] = (
        None
    )

    atualizar_assertividade()

    salvar_estado()

    mensagem = (
        montar_mensagem_resultado(
            sinal,
            totais,
            resultado_cantos,
            resultado_gols,
            resultado_final,
            retorno,
            lucro
        )
    )

    enviar_telegram(
        mensagem
    )

    logging.info(
        "RESOLVIDO | "
        "fixture=%s | "
        "resultado=%s | "
        "lucro=%+.2f",
        fixture_id,
        resultado_final,
        lucro
    )

    return True


# ============================================================
# RESOLVER PENDENTES
# ============================================================

def resolver_pendentes():

    pendentes = []

    for sinal in (
        estado[
            "sinais"
        ].values()
    ):

        if (
            sinal.get("status")
            == "PENDENTE"
        ):

            pendentes.append(
                sinal
            )

    logging.info(
        "Sinais pendentes: %d",
        len(pendentes)
    )

    for sinal in pendentes:

        try:

            resolver_sinal(
                sinal
            )

        except Exception as erro:

            logging.exception(
                "Erro resolvendo "
                "sinal %s: %s",
                sinal.get("id"),
                erro
            )


# ============================================================
# ANÁLISE PRINCIPAL
# ============================================================

def analisar():

    logging.info(
        "=" * 60
    )

    logging.info(
        "NOVO CICLO DE ANÁLISE"
    )

    logging.info(
        "=" * 60
    )

    # Primeiro resolve jogos anteriores

    resolver_pendentes()

    livres = bancas_livres()

    logging.info(
        "Bancas livres: %s",
        livres
    )

    if not livres:

        logging.info(
            "Todas as bancas "
            "estão ocupadas."
        )

        return

    fixtures = buscar_fixtures()

    if not fixtures:

        logging.info(
            "Nenhum fixture "
            "encontrado."
        )

        return

    candidatos = []

    consultas_odds = 0

    for fixture in fixtures:

        if (
            consultas_odds
            >= MAX_CONSULTAS_ODDS
        ):

            logging.info(
                "Limite de %d "
                "consultas de odds "
                "atingido.",
                MAX_CONSULTAS_ODDS
            )

            break

        fixture_id = (
            obter_fixture_id(
                fixture
            )
        )

        if not fixture_id:

            logging.warning(
                "Fixture sem ID."
            )

            continue

        if fixture_ja_utilizado(
            fixture_id
        ):

            logging.info(
                "Fixture %s já "
                "foi utilizado.",
                fixture_id
            )

            continue

        home, away = (
            obter_times(
                fixture
            )
        )

        logging.info(
            "Analisando fixture "
            "%s | %s x %s",
            fixture_id,
            home,
            away
        )

        resposta_odds = (
            buscar_odds(
                fixture_id
            )
        )

        consultas_odds += 1

        if not resposta_odds:

            logging.warning(
                "Fixture %s "
                "sem odds.",
                fixture_id
            )

            continue

        combinacao = (
            escolher_combinacao(
                resposta_odds
            )
        )

        if not combinacao:

            todas = (
                extrair_combinacoes(
                    resposta_odds
                )
            )

            if todas:

                melhor = min(
                    todas,
                    key=lambda item:
                    abs(
                        item[
                            "odd_combinada"
                        ]
                        - ODD_ALVO
                    )
                )

                logging.info(
                    "REJEITADO %s | "
                    "Over %.2f cantos @%.3f + "
                    "Under %.2f gols @%.3f "
                    "= %.3f | "
                    "faixa aceita %.2f-%.2f",

                    fixture_id,

                    melhor[
                        "linha_cantos"
                    ],

                    melhor[
                        "odd_cantos"
                    ],

                    melhor[
                        "linha_gols"
                    ],

                    melhor[
                        "odd_gols"
                    ],

                    melhor[
                        "odd_combinada"
                    ],

                    ODD_MINIMA,

                    ODD_MAXIMA
                )

            else:

                logging.info(
                    "REJEITADO %s | "
                    "goal_line ou "
                    "corner_line "
                    "indisponível.",
                    fixture_id
                )

            continue

        distancia = abs(
            combinacao[
                "odd_combinada"
            ]
            - ODD_ALVO
        )

        candidatos.append({
            "fixture":
                fixture,

            "combinacao":
                combinacao,

            "distancia":
                distancia
        })

        logging.info(
            "CANDIDATO %s | "
            "%s x %s | "
            "Over %.2f cantos @%.3f + "
            "Under %.2f gols @%.3f "
            "= %.3f",

            fixture_id,

            home,

            away,

            combinacao[
                "linha_cantos"
            ],

            combinacao[
                "odd_cantos"
            ],

            combinacao[
                "linha_gols"
            ],

            combinacao[
                "odd_gols"
            ],

            combinacao[
                "odd_combinada"
            ]
        )

    candidatos.sort(
        key=lambda item:
        item["distancia"]
    )

    logging.info(
        "RESUMO | "
        "fixtures=%d | "
        "consultas_odds=%d | "
        "candidatos=%d | "
        "bancas_livres=%d",

        len(fixtures),

        consultas_odds,

        len(candidatos),

        len(livres)
    )

    quantidade = min(
        len(candidatos),
        len(livres)
    )

    if quantidade <= 0:

        logging.info(
            "Nenhum sinal "
            "dentro dos filtros."
        )

        return

    for indice in range(
        quantidade
    ):

        candidato = (
            candidatos[indice]
        )

        numero_banca = (
            livres[indice]
        )

        sinal = criar_sinal(
            candidato[
                "fixture"
            ],

            candidato[
                "combinacao"
            ],

            numero_banca
        )

        registrar_sinal(
            sinal
        )


# ============================================================
# LOOP DO ROBÔ
# ============================================================

def loop_robo():

    logging.info(
        "=" * 60
    )

    logging.info(
        "ROBÔ COMBINADO V4.0 INICIADO"
    )

    logging.info(
        "=" * 60
    )

    if API_KEY:

        logging.info(
            "API configurada: SIM"
        )

    else:

        logging.error(
            "API configurada: NÃO"
        )

    if (
        TELEGRAM_TOKEN
        and CHAT_ID
    ):

        logging.info(
            "Telegram configurado: SIM"
        )

    else:

        logging.error(
            "Telegram configurado: NÃO"
        )

    logging.info(
        "Bancas: %d",
        TOTAL_BANCAS
    )

    logging.info(
        "Entrada inicial: "
        "R$ %.2f",
        ENTRADA_INICIAL
    )

    logging.info(
        "Gale: x%.2f | "
        "Máximo: %d",
        MULTIPLICADOR_GALE,
        MAX_GALES
    )

    logging.info(
        "Odd: %.2f até %.2f | "
        "Alvo %.2f",
        ODD_MINIMA,
        ODD_MAXIMA,
        ODD_ALVO
    )

    logging.info(
        "Janela: %dh",
        JANELA_HORAS
    )

    logging.info(
        "Intervalo: %ds",
        INTERVALO_ANALISE
    )

    while True:

        try:

            with lock:

                analisar()

        except Exception as erro:

            logging.exception(
                "Erro geral no "
                "ciclo: %s",
                erro
            )

        logging.info(
            "Próxima análise "
            "em %d segundos.",
            INTERVALO_ANALISE
        )

        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status":
            "online",

        "versao":
            "4.0",

        "modo":
            "pre-jogo",

        "estrategia":
            "Over escanteios + Under gols",

        "janela_horas":
            JANELA_HORAS,

        "odd_minima":
            ODD_MINIMA,

        "odd_alvo":
            ODD_ALVO,

        "odd_maxima":
            ODD_MAXIMA,

        "total_bancas":
            TOTAL_BANCAS,

        "bancas":
            estado["bancas"],

        "stats":
            estado["stats"]
    })


@app.route("/health")
def health():

    timestamp = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    return jsonify({
        "ok":
            True,

        "versao":
            "4.0",

        "timestamp":
            timestamp
    })


@app.route("/status")
def status():

    return jsonify(
        estado
    )


@app.route("/stats")
def stats():

    return jsonify(
        estado["stats"]
    )


@app.route("/bancas")
def bancas():

    return jsonify(
        estado["bancas"]
    )


@app.route("/sinais")
def sinais():

    return jsonify(
        estado["sinais"]
    )


# ============================================================
# INICIAR THREAD
# ============================================================

thread_iniciada = False


def iniciar_thread():

    global thread_iniciada

    if thread_iniciada:
        return

    thread_iniciada = True

    thread = threading.Thread(
        target=loop_robo,
        daemon=True,
        name="robo-thread"
    )

    thread.start()


iniciar_thread()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
