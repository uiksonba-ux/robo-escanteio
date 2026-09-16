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

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

# ============================================================
# ESTRATÉGIA
# ============================================================

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

# ============================================================
# ODDS
# ============================================================

ODD_MINIMA = float(
    os.getenv("ODD_MINIMA", "1.80")
)

ODD_MAXIMA = float(
    os.getenv("ODD_MAXIMA", "2.50")
)

ODD_ALVO = float(
    os.getenv("ODD_ALVO", "2.00")
)

# ============================================================
# JANELA / INTERVALOS
# ============================================================

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

# 900 segundos = 15 minutos
INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "900")
)

# Limita chamadas /odds por ciclo
MAX_CONSULTAS_ODDS = int(
    os.getenv("MAX_CONSULTAS_ODDS", "25")
)

ARQUIVO_ESTADO = "estado.json"

lock = threading.Lock()


# ============================================================
# ESTADO
# ============================================================

def estado_inicial():
    return {
        "stats": {
            "enviados": 0,
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "half_wins": 0,
            "half_losses": 0,
            "assertividade": 0.0,
            "saldo_teorico": 0.0
        },

        "bancas": {
            str(i): {
                "status": "livre",
                "gale": 0,
                "sinal_id": None
            }
            for i in range(
                1,
                TOTAL_BANCAS + 1
            )
        },

        "sinais": {}
    }


def carregar_estado():

    if not os.path.exists(
        ARQUIVO_ESTADO
    ):
        return estado_inicial()

    try:

        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as arquivo:

            dados = json.load(
                arquivo
            )

        dados.setdefault(
            "stats",
            estado_inicial()["stats"]
        )

        dados.setdefault(
            "bancas",
            {}
        )

        dados.setdefault(
            "sinais",
            {}
        )

        for i in range(
            1,
            TOTAL_BANCAS + 1
        ):

            chave = str(i)

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

        return estado_inicial()


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

def api_get(
    endpoint,
    params=None
):

    if not API_KEY:

        logging.error(
            "FIVE_DOLLAR_API_KEY não configurada."
        )

        return None

    url = (
        f"{BASE_API}/"
        f"{endpoint.lstrip('/')}"
    )

    headers = {
        "Authorization":
            f"Bearer {API_KEY}",

        "Accept":
            "application/json",

        "User-Agent":
            "robo-combinado/2.1"
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

        if resposta.status_code != 200:

            logging.error(
                "API %s | %s",
                resposta.status_code,
                resposta.text[:1500]
            )

            return None

        try:

            dados = resposta.json()

        except ValueError:

            logging.error(
                "API retornou resposta "
                "que não é JSON."
            )

            return None

        if (
            isinstance(dados, dict)
            and dados.get("success") == 0
        ):

            logging.error(
                "Erro retornado pela API: %s",
                json.dumps(
                    dados,
                    ensure_ascii=False
                )
            )

            return None

        return dados

    except requests.RequestException as erro:

        logging.error(
            "Erro de conexão com API: %s",
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

def telegram(
    texto
):

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        logging.warning(
            "TELEGRAM_TOKEN ou "
            "CHAT_ID não configurado."
        )

        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
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
                resposta.text[:1000]
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

def extrair_lista_fixtures(
    resposta
):

    if not resposta:
        return []

    if isinstance(
        resposta,
        list
    ):
        return resposta

    data = resposta.get(
        "data",
        []
    )

    if isinstance(
        data,
        list
    ):
        return data

    if isinstance(
        data,
        dict
    ):

        for chave in (
            "fixtures",
            "data",
            "results"
        ):

            valor = data.get(
                chave
            )

            if isinstance(
                valor,
                list
            ):
                return valor

    return []


def buscar_fixtures():

    agora = datetime.now(
        timezone.utc
    )

    fim = agora + timedelta(
        hours=JANELA_HORAS
    )

    # ========================================================
    # CORREÇÃO IMPORTANTE
    #
    # A API exige UNIX TIMESTAMP EM SEGUNDOS UTC.
    # NÃO usar agora.isoformat()
    # ========================================================

    start_timestamp = int(
        agora.timestamp()
    )

    end_timestamp = int(
        fim.timestamp()
    )

    params = {
        "start_time":
            start_timestamp,

        "end_time":
            end_timestamp,

        "status":
            "scheduled",

        "per_page":
            100
    }

    logging.info(
        "Buscando fixtures | "
        "inicio=%s | fim=%s | "
        "janela=%sh",
        start_timestamp,
        end_timestamp,
        JANELA_HORAS
    )

    resposta = api_get(
        "fixtures",
        params=params
    )

    fixtures = extrair_lista_fixtures(
        resposta
    )

    logging.info(
        "%d fixtures encontrados "
        "nas próximas %sh.",
        len(fixtures),
        JANELA_HORAS
    )

    return fixtures


def fixture_id(
    fixture
):

    return (
        fixture.get("id")
        or fixture.get(
            "fixture_id"
        )
    )


def nome_times(
    fixture
):

    home = "Casa"
    away = "Fora"

    # Formato home / away

    if isinstance(
        fixture.get("home"),
        dict
    ):

        home = (
            fixture["home"].get(
                "name"
            )
            or home
        )

    if isinstance(
        fixture.get("away"),
        dict
    ):

        away = (
            fixture["away"].get(
                "name"
            )
            or away
        )

    # Formato home_team / away_team

    if isinstance(
        fixture.get("home_team"),
        dict
    ):

        home = (
            fixture[
                "home_team"
            ].get("name")
            or home
        )

    if isinstance(
        fixture.get("away_team"),
        dict
    ):

        away = (
            fixture[
                "away_team"
            ].get("name")
            or away
        )

    # Formato teams.home / teams.away

    teams = fixture.get(
        "teams"
    )

    if isinstance(
        teams,
        dict
    ):

        if isinstance(
            teams.get("home"),
            dict
        ):

            home = (
                teams[
                    "home"
                ].get("name")
                or home
            )

        if isinstance(
            teams.get("away"),
            dict
        ):

            away = (
                teams[
                    "away"
                ].get("name")
                or away
            )

    return home, away


# ============================================================
# ODDS
# ============================================================

def buscar_odds(
    fid
):

    resposta = api_get(
        f"fixtures/{fid}/odds"
    )

    if resposta:

        logging.info(
            "Estrutura odds fixture %s: %s",
            fid,
            json.dumps(
                resposta,
                ensure_ascii=False
            )[:5000]
        )

    return resposta


def selecionar_periodo(
    mercado
):

    if not isinstance(
        mercado,
        dict
    ):
        return None, None

    # Pré-jogo:
    # preferência para closing

    closing = mercado.get(
        "closing"
    )

    if isinstance(
        closing,
        dict
    ):

        return (
            closing,
            "closing"
        )

    # fallback opening

    opening = mercado.get(
        "opening"
    )

    if isinstance(
        opening,
        dict
    ):

        return (
            opening,
            "opening"
        )

    return None, None


def extrair_combinacoes(
    resposta
):

    resultados = []

    if not resposta:
        return resultados

    data = resposta.get(
        "data",
        {}
    )

    if not isinstance(
        data,
        dict
    ):
        return resultados

    bookmakers = data.get(
        "bookmakers",
        []
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return resultados

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome_bookmaker = (
            bookmaker.get(
                "name"
            )
            or "Desconhecida"
        )

        odds = bookmaker.get(
            "odds",
            {}
        )

        if not isinstance(
            odds,
            dict
        ):
            continue

        # ====================================================
        # FORMATO REAL CONFIRMADO PELO LOG
        # ====================================================

        goal_line = odds.get(
            "goal_line"
        )

        corner_line = odds.get(
            "corner_line"
        )

        gols, periodo_gols = (
            selecionar_periodo(
                goal_line
            )
        )

        cantos, periodo_cantos = (
            selecionar_periodo(
                corner_line
            )
        )

        if (
            not gols
            or not cantos
        ):

            logging.info(
                "%s sem goal_line/"
                "corner_line utilizável.",
                nome_bookmaker
            )

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

        if (
            linha_gols is None
            or odd_under is None
            or linha_cantos is None
            or odd_over is None
        ):
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
            ValueError,
            TypeError
        ):

            continue

        if (
            odd_under <= 1
            or odd_over <= 1
        ):
            continue

        odd_combinada = round(
            odd_under
            * odd_over,
            3
        )

        resultados.append({

            "bookmaker":
                nome_bookmaker,

            "linha_gols":
                linha_gols,

            "odd_gols":
                odd_under,

            "periodo_gols":
                periodo_gols,

            "linha_cantos":
                linha_cantos,

            "odd_cantos":
                odd_over,

            "periodo_cantos":
                periodo_cantos,

            "odd_combinada":
                odd_combinada
        })

    return resultados


def escolher_combinacao(
    resposta
):

    combinacoes = (
        extrair_combinacoes(
            resposta
        )
    )

    validas = []

    for combinacao in combinacoes:

        logging.info(
            "%s | "
            "Over %.2f cantos @%.3f | "
            "Under %.2f gols @%.3f | "
            "Combinada %.3f",
            combinacao["bookmaker"],
            combinacao["linha_cantos"],
            combinacao["odd_cantos"],
            combinacao["linha_gols"],
            combinacao["odd_gols"],
            combinacao["odd_combinada"]
        )

        if (
            ODD_MINIMA
            <= combinacao[
                "odd_combinada"
            ]
            <= ODD_MAXIMA
        ):

            validas.append(
                combinacao
            )

    if not validas:
        return None

    # Mais próxima de 2.00

    validas.sort(
        key=lambda x:
        abs(
            x["odd_combinada"]
            - ODD_ALVO
        )
    )

    return validas[0]


# ============================================================
# LINHAS ASIÁTICAS
# ============================================================

def combinar_metades(
    resultado1,
    resultado2
):

    resultados = {
        resultado1,
        resultado2
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


def avaliar_linha(
    total,
    linha,
    lado
):

    total = float(
        total
    )

    linha = float(
        linha
    )

    inteiro = int(
        linha
    )

    fracao = round(
        linha - inteiro,
        2
    )

    # ========================================================
    # LINHA INTEIRA
    # ========================================================

    if fracao == 0.0:

        if lado == "over":

            if total > linha:
                return "WIN"

            if total == linha:
                return "PUSH"

            return "LOSS"

        else:

            if total < linha:
                return "WIN"

            if total == linha:
                return "PUSH"

            return "LOSS"

    # ========================================================
    # LINHA .5
    # ========================================================

    if fracao == 0.5:

        if lado == "over":

            if total > linha:
                return "WIN"

            return "LOSS"

        else:

            if total < linha:
                return "WIN"

            return "LOSS"

    # ========================================================
    # LINHA .25
    # Ex: 2.25 = metade 2.0 + metade 2.5
    # ========================================================

    if fracao == 0.25:

        resultado1 = avaliar_linha(
            total,
            float(inteiro),
            lado
        )

        resultado2 = avaliar_linha(
            total,
            float(inteiro) + 0.5,
            lado
        )

        return combinar_metades(
            resultado1,
            resultado2
        )

    # ========================================================
    # LINHA .75
    # Ex: 2.75 = metade 2.5 + metade 3.0
    # ========================================================

    if fracao == 0.75:

        resultado1 = avaliar_linha(
            total,
            float(inteiro) + 0.5,
            lado
        )

        resultado2 = avaliar_linha(
            total,
            float(inteiro) + 1.0,
            lado
        )

        return combinar_metades(
            resultado1,
            resultado2
        )

    # fallback

    if lado == "over":

        return (
            "WIN"
            if total > linha
            else "LOSS"
        )

    return (
        "WIN"
        if total < linha
        else "LOSS"
    )


# ============================================================
# BUSCAR RESULTADO DO FIXTURE
# ============================================================

def buscar_fixture(
    fid
):

    return api_get(
        f"fixtures/{fid}"
    )


def extrair_fixture(
    resposta
):

    if not resposta:
        return None

    data = resposta.get(
        "data",
        resposta
    )

    if isinstance(
        data,
        dict
    ):
        return data

    return None


def extrair_totais(
    fixture
):

    goals = fixture.get(
        "goals",
        {}
    )

    corners = fixture.get(
        "corners",
        {}
    )

    if not isinstance(
        goals,
        dict
    ):
        goals = {}

    if not isinstance(
        corners,
        dict
    ):
        corners = {}

    gols_casa = goals.get(
        "home"
    )

    gols_fora = goals.get(
        "away"
    )

    cantos_casa = corners.get(
        "home"
    )

    cantos_fora = corners.get(
        "away"
    )

    if None in (
        gols_casa,
        gols_fora,
        cantos_casa,
        cantos_fora
    ):

        return None

    try:

        total_gols = (
            float(gols_casa)
            + float(gols_fora)
        )

        total_cantos = (
            float(cantos_casa)
            + float(cantos_fora)
        )

    except (
        ValueError,
        TypeError
    ):

        return None

    return {
        "gols": total_gols,
        "cantos": total_cantos
    }


# ============================================================
# LIQUIDAÇÃO TEÓRICA
# ============================================================

def retorno_perna(
    stake,
    odd,
    resultado
):

    if resultado == "WIN":

        return (
            stake * odd
        )

    if resultado == "HALF_WIN":

        metade = (
            stake / 2
        )

        return (
            metade * odd
            + metade
        )

    if resultado == "PUSH":

        return stake

    if resultado == "HALF_LOSS":

        return (
            stake / 2
        )

    return 0.0


def resolver_combinada(
    stake,
    odd_cantos,
    resultado_cantos,
    odd_gols,
    resultado_gols
):

    retorno_cantos = (
        retorno_perna(
            stake,
            odd_cantos,
            resultado_cantos
        )
    )

    if retorno_cantos <= 0:

        return 0.0

    retorno_final = (
        retorno_perna(
            retorno_cantos,
            odd_gols,
            resultado_gols
        )
    )

    return round(
        retorno_final,
        2
    )


def classificar_retorno(
    stake,
    retorno
):

    diferenca = round(
        retorno - stake,
        2
    )

    if retorno <= 0:

        return "LOSS"

    if diferenca < 0:

        return "HALF_LOSS"

    if abs(
        diferenca
    ) < 0.01:

        return "PUSH"

    return "WIN"


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():

    livres = []

    for (
        numero,
        banca
    ) in estado[
        "bancas"
    ].items():

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


def fixture_ja_usado(
    fid
):

    for sinal in estado[
        "sinais"
    ].values():

        if str(
            sinal.get(
                "fixture_id"
            )
        ) == str(fid):

            return True

    return False


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    fixture,
    combinacao,
    banca_numero
):

    fid = fixture_id(
        fixture
    )

    home, away = (
        nome_times(
            fixture
        )
    )

    banca = estado[
        "bancas"
    ][
        str(
            banca_numero
        )
    ]

    gale = int(
        banca.get(
            "gale",
            0
        )
    )

    entrada = round(
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** gale
        ),
        2
    )

    sinal_id = (
        f"{fid}-"
        f"B{banca_numero}-"
        f"{int(time.time())}"
    )

    return {

        "id":
            sinal_id,

        "fixture_id":
            fid,

        "home":
            home,

        "away":
            away,

        "banca":
            banca_numero,

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


def mensagem_sinal(
    sinal
):

    return (
        "🎯 <b>NOVO SINAL PRÉ-JOGO</b>\n\n"

        f"⚽ <b>{sinal['home']} "
        f"x {sinal['away']}</b>\n\n"

        f"🏦 Banca: "
        f"<b>{sinal['banca']}</b>\n"

        f"🔁 Gale: "
        f"<b>{sinal['gale']}</b>\n"

        f"💰 Entrada: "
        f"<b>R$ "
        f"{sinal['entrada']:.2f}</b>\n\n"

        f"🚩 Over "
        f"<b>{sinal['linha_cantos']}</b> "
        f"escanteios "
        f"@{sinal['odd_cantos']}\n"

        f"⚽ Under "
        f"<b>{sinal['linha_gols']}</b> "
        f"gols "
        f"@{sinal['odd_gols']}\n\n"

        f"🔥 Odd combinada teórica: "
        f"<b>{sinal['odd_combinada']}</b>\n"

        f"🏢 Bookmaker: "
        f"<b>{sinal['bookmaker']}</b>"
    )


def registrar_sinal(
    sinal
):

    enviado = telegram(
        mensagem_sinal(
            sinal
        )
    )

    if not enviado:

        logging.error(
            "Sinal não registrado "
            "porque Telegram falhou."
        )

        return False

    estado[
        "sinais"
    ][
        sinal["id"]
    ] = sinal

    banca = estado[
        "bancas"
    ][
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

    estado[
        "stats"
    ][
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
# ASSERTIVIDADE
# ============================================================

def atualizar_assertividade():

    stats = estado[
        "stats"
    ]

    decisoes = (
        stats.get(
            "wins",
            0
        )
        +
        stats.get(
            "losses",
            0
        )
    )

    if decisoes > 0:

        stats[
            "assertividade"
        ] = round(
            (
                stats["wins"]
                / decisoes
            )
            * 100,
            2
        )

    else:

        stats[
            "assertividade"
        ] = 0.0


# ============================================================
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal
):

    resposta = buscar_fixture(
        sinal[
            "fixture_id"
        ]
    )

    fixture = extrair_fixture(
        resposta
    )

    if not fixture:

        return False

    totais = extrair_totais(
        fixture
    )

    # Se ainda não há gols/cantos finais,
    # jogo permanece pendente.

    if not totais:

        logging.info(
            "Fixture %s ainda "
            "sem resultado completo.",
            sinal["fixture_id"]
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
        resolver_combinada(
            sinal["entrada"],
            sinal["odd_cantos"],
            resultado_cantos,
            sinal["odd_gols"],
            resultado_gols
        )
    )

    classificacao = (
        classificar_retorno(
            sinal[
                "entrada"
            ],
            retorno
        )
    )

    lucro = round(
        retorno
        - sinal["entrada"],
        2
    )

    sinal[
        "status"
    ] = "RESOLVIDO"

    sinal[
        "resultado"
    ] = classificacao

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

    stats[
        "saldo_teorico"
    ] = round(
        stats.get(
            "saldo_teorico",
            0
        )
        + lucro,
        2
    )

    if classificacao == "WIN":

        stats[
            "wins"
        ] += 1

    elif classificacao == "LOSS":

        stats[
            "losses"
        ] += 1

    elif classificacao == "PUSH":

        stats[
            "pushes"
        ] += 1

    elif classificacao == "HALF_WIN":

        stats[
            "half_wins"
        ] += 1

    elif classificacao == "HALF_LOSS":

        stats[
            "half_losses"
        ] += 1

    banca = estado[
        "bancas"
    ][
        str(
            sinal["banca"]
        )
    ]

    # ========================================================
    # GALE
    #
    # Prejuízo -> avança
    # Sem prejuízo -> volta para entrada inicial
    # ========================================================

    if lucro < 0:

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
                gale_atual + 1
            )

        else:

            banca[
                "gale"
            ] = 0

    else:

        banca[
            "gale"
        ] = 0

    banca[
        "status"
    ] = "livre"

    banca[
        "sinal_id"
    ] = None

    atualizar_assertividade()

    salvar_estado()

    telegram(
        "📊 <b>SINAL RESOLVIDO</b>\n\n"

        f"⚽ {sinal['home']} "
        f"x {sinal['away']}\n\n"

        f"🏦 Banca: "
        f"{
