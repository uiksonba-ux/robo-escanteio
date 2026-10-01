import os
import time
import math
import threading
import logging
from collections import defaultdict

import requests
from flask import Flask, jsonify

try:
    from iqoptionapi.stable_api import IQ_Option
except Exception:
    IQ_Option = None


# ============================================================
# ROBÔ IQ OPTION V3
#
# NORMAL + OTC
# M1 / M5 / M15
#
# SINAL:
# Entrada -> G1 -> G2
#
# RESULTADO:
# WIN
# WIN G1
# WIN G2
# LOSS
#
# BACKTEST:
# 7 dias
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-iq-v3")


# ============================================================
# ENV
# ============================================================

def env_str(nome, padrao=""):
    return os.getenv(nome, padrao).strip()


def env_int(nome, padrao):
    try:
        return int(os.getenv(nome, str(padrao)))
    except Exception:
        return padrao


def env_float(nome, padrao):
    try:
        return float(os.getenv(nome, str(padrao)))
    except Exception:
        return padrao


def env_bool(nome, padrao=False):

    valor = os.getenv(nome)

    if valor is None:
        return padrao

    return valor.strip().lower() in (
        "1",
        "true",
        "yes",
        "sim",
        "on"
    )


# ============================================================
# CONFIGURAÇÃO
# ============================================================

PORT = env_int(
    "PORT",
    10000
)

TELEGRAM_TOKEN = env_str(
    "TELEGRAM_TOKEN"
)

CHAT_ID = env_str(
    "CHAT_ID"
)

IQ_EMAIL = env_str(
    "IQ_EMAIL"
)

IQ_PASSWORD = env_str(
    "IQ_PASSWORD"
)


# ============================================================
# SEGURANÇA
# ============================================================

# Esta versão trabalha em PRACTICE.
IQ_BALANCE = "PRACTICE"

# false = sinais + acompanhamento virtual
# true  = reservado para execução PRACTICE posterior
EXECUTAR_ORDENS = env_bool(
    "EXECUTAR_ORDENS",
    False
)


# ============================================================
# FILTROS
# ============================================================

SCORE_MIN = env_int(
    "SCORE_MIN",
    65
)

INTERVALO_ANALISE = env_int(
    "INTERVALO_ANALISE",
    20
)

ENTRADA_BASE = env_float(
    "ENTRADA_BASE",
    2.00
)

MULTIPLICADOR_GALE = 2.0

GALES_INICIAIS = 2

MAX_GALES = 5

RECUPERACAO_PERCENTUAL = 0.10


TIMEFRAMES = {
    1: 60,
    5: 300,
    15: 900
}


ATIVOS_BASE = [

    ativo.strip().upper()

    for ativo in env_str(
        "ATIVOS",
        (
            "EURUSD,"
            "EURGBP,"
            "GBPUSD,"
            "USDJPY,"
            "AUDUSD,"
            "EURJPY,"
            "GBPJPY,"
            "USDCHF,"
            "AUDCAD,"
            "AUDJPY,"
            "EURCAD,"
            "USDCAD,"
            "NZDUSD"
        )
    ).split(",")

    if ativo.strip()
]


# ============================================================
# ESTADO
# ============================================================

api = None

api_lock = threading.RLock()

estado_lock = threading.RLock()

ultimo_sinal = {}

sinais_pendentes = {}


stats = {

    "sinais": 0,

    "wins": 0,

    "win_direto": 0,

    "win_g1": 0,

    "win_g2": 0,

    "losses": 0,

    "draws": 0,

    "candles_recebidos": 0,

    "ciclos_scanner": 0,

    "erros": 0
}


stats_timeframe = defaultdict(
    lambda: {
        "wins": 0,
        "losses": 0
    }
)


stats_mercado = defaultdict(
    lambda: {
        "wins": 0,
        "losses": 0
    }
)


stats_ativo = defaultdict(
    lambda: {
        "wins": 0,
        "losses": 0
    }
)


# ============================================================
# GERENCIAMENTO
# ============================================================

gerenciamento = {

    "entrada_base_original":
        ENTRADA_BASE,

    "entrada_base_atual":
        ENTRADA_BASE,

    "gales_permitidos":
        GALES_INICIAIS,

    "divida":
        0.0,

    "em_recuperacao":
        False
}


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_TOKEN
            + "/sendMessage"
        )

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

        return resposta.ok

    except Exception:

        logger.exception(
            "Erro Telegram"
        )

        return False


# ============================================================
# IQ OPTION
# ============================================================

def conectar_iq():

    global api

    if IQ_Option is None:

        logger.error(
            "iqoptionapi não carregada"
        )

        return False


    if not IQ_EMAIL or not IQ_PASSWORD:

        logger.warning(
            "Credenciais IQ não configuradas"
        )

        return False


    try:

        nova_api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        ok, motivo = nova_api.connect()

        if not ok:

            logger.error(
                "Falha IQ: %s",
                motivo
            )

            return False


        nova_api.change_balance(
            "PRACTICE"
        )


        with api_lock:
            api = nova_api


        logger.info(
            "IQ Option conectada em PRACTICE"
        )


        enviar_telegram(
            "🟢 <b>ROBÔ ONLINE</b>\n\n"
            "Normal + OTC\n"
            "M1 / M5 / M15\n"
            f"Score mínimo: {SCORE_MIN}\n"
            "Modo: acompanhamento virtual"
        )


        return True


    except Exception:

        logger.exception(
            "Erro conexão IQ"
        )

        return False


def garantir_conexao():

    try:

        if (
            api is not None
            and api.check_connect()
        ):
            return True

    except Exception:
        pass

    return conectar_iq()


# ============================================================
# INDICADORES
# ============================================================

def media(valores):

    if not valores:
        return 0.0

    return sum(valores) / len(valores)


def ema(valores, periodo):

    if len(valores) < periodo:
        return None

    multiplicador = (
        2.0
        / (periodo + 1)
    )

    valor = media(
        valores[:periodo]
    )

    for preco in valores[periodo:]:

        valor = (
            preco
            * multiplicador
            +
            valor
            * (
                1.0
                - multiplicador
            )
        )

    return valor


def rsi(valores, periodo=14):

    if len(valores) <= periodo:
        return 50.0

    ganhos = []

    perdas = []

    dados = valores[
        -(periodo + 1):
    ]

    for i in range(
        len(dados) - 1
    ):

        diferenca = (
            dados[i + 1]
            - dados[i]
        )

        ganhos.append(
            max(
                diferenca,
                0
            )
        )

        perdas.append(
            max(
                -diferenca,
                0
            )
        )


    ganho = media(
        ganhos
    )

    perda = media(
        perdas
    )


    if perda == 0:

        if ganho > 0:
            return 100.0

        return 50.0


    rs = ganho / perda

    return (
        100
        -
        (
            100
            /
            (
                1
                + rs
            )
        )
    )


def desvio_padrao(valores):

    if not valores:
        return 0

    m = media(
        valores
    )

    variancia = media([
        (
            x
            - m
        ) ** 2

        for x in valores
    ])

    return math.sqrt(
        variancia
    )


def bollinger(
    valores,
    periodo=20
):

    if len(valores) < periodo:

        return (
            None,
            None,
            None
        )

    dados = valores[
        -periodo:
    ]

    centro = media(
        dados
    )

    desvio = desvio_padrao(
        dados
    )

    return (
        centro
        - 2 * desvio,

        centro,

        centro
        + 2 * desvio
    )


def macd(valores):

    e12 = ema(
        valores,
        12
    )

    e26 = ema(
        valores,
        26
    )

    if (
        e12 is None
        or e26 is None
    ):
        return 0

    return e12 - e26


# ============================================================
# CANDLES
# ============================================================

def normalizar_candles(candles):

    resultado = []

    if not candles:
        return resultado

    for candle in candles:

        try:

            resultado.append({

                "open":
                    float(
                        candle["open"]
                    ),

                "close":
                    float(
                        candle["close"]
                    ),

                "max":
                    float(
                        candle["max"]
                    ),

                "min":
                    float(
                        candle["min"]
                    ),

                "from":
                    int(
                        candle.get(
                            "from",
                            0
                        )
                    )
            })

        except Exception:
            continue


    resultado.sort(
        key=lambda x:
            x["from"]
    )

    return resultado


# ============================================================
# ESTRATÉGIA
# ============================================================

def analisar_candles(candles):

    candles = normalizar_candles(
        candles
    )

    if len(candles) < 60:
        return None


    opens = [
        x["open"]
        for x in candles
    ]

    closes = [
        x["close"]
        for x in candles
    ]

    highs = [
        x["max"]
        for x in candles
    ]

    lows = [
        x["min"]
        for x in candles
    ]


    call = 0

    put = 0


    # ========================================================
    # EMA
    # ========================================================

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    if (
        ema20 is not None
        and ema50 is not None
    ):

        if ema20 > ema50:
            call += 20

        elif ema20 < ema50:
            put += 20


    # ========================================================
    # RSI
    # ========================================================

    valor_rsi = rsi(
        closes,
        14
    )

    if valor_rsi <= 35:
        call += 15

    elif valor_rsi >= 65:
        put += 15


    # ========================================================
    # BOLLINGER
    # ========================================================

    inferior, _, superior = (
        bollinger(
            closes
        )
    )

    ultimo = closes[-1]

    if inferior is not None:

        if ultimo <= inferior:
            call += 10

        elif ultimo >= superior:
            put += 10


    # ========================================================
    # PRICE ACTION
    # ========================================================

    abertura = opens[-1]

    fechamento = closes[-1]

    amplitude = max(
        highs[-1]
        - lows[-1],
        0.00000001
    )

    corpo = abs(
        fechamento
        - abertura
    )

    forca = corpo / amplitude

    if (
        fechamento > abertura
        and forca >= 0.55
    ):

        call += 15

    elif (
        fechamento < abertura
        and forca >= 0.55
    ):

        put += 15


    # ========================================================
    # SUPORTE / RESISTÊNCIA
    # ========================================================

    resistencia = max(
        highs[-11:-1]
    )

    suporte = min(
        lows[-11:-1]
    )

    faixa = max(
        resistencia
        - suporte,
        0.00000001
    )

    tolerancia = faixa * 0.10


    if (
        ultimo
        <= suporte
        + tolerancia
    ):

        call += 10


    if (
        ultimo
        >= resistencia
        - tolerancia
    ):

        put += 10


    # ========================================================
    # BREAKOUT
    # ========================================================

    if ultimo > resistencia:
        call += 15

    elif ultimo < suporte:
        put += 15


    # ========================================================
    # MACD
    # ========================================================

    valor_macd = macd(
        closes
    )

    if valor_macd > 0:
        call += 10

    elif valor_macd < 0:
        put += 10


    # ========================================================
    # MOMENTUM
    # ========================================================

    if (
        closes[-1]
        > closes[-2]
        > closes[-3]
    ):

        call += 5

    elif (
        closes[-1]
        < closes[-2]
        < closes[-3]
    ):

        put += 5


    call = min(
        call,
        100
    )

    put = min(
        put,
        100
    )


    if abs(
        call - put
    ) < 15:

        return None


    if (
        call >= SCORE_MIN
        and call > put
    ):

        return {
            "direcao": "call",
            "score": call
        }


    if (
        put >= SCORE_MIN
        and put > call
    ):

        return {
            "direcao": "put",
            "score": put
        }


    return None


# ============================================================
# ASSERTIVIDADE
# ============================================================

def assertividade():

    total = (
        stats["wins"]
        + stats["losses"]
    )

    if total == 0:
        return 0.0

    return round(
        stats["wins"]
        / total
        * 100,
        2
    )


# ============================================================
# SEQUÊNCIA FINANCEIRA
# ============================================================

def sequencia_entradas():

    base = gerenciamento[
        "entrada_base_atual"
    ]

    qtd_gales = gerenciamento[
        "gales_permitidos"
    ]

    valores = []

    for nivel in range(
        qtd_gales + 1
    ):

        valores.append(
            round(
                base
                * (
                    MULTIPLICADOR_GALE
                    ** nivel
                ),
                2
            )
        )

    return valores


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def mensagem_sinal(
    ativo,
    timeframe,
    sinal
):

    mercado = (
        "OTC"
        if "-OTC" in ativo
        else "NORMAL"
    )

    entradas = (
        sequencia_entradas()
    )

    partes = [
        f"R$ {entradas[0]:.2f}"
    ]

    for i, valor in enumerate(
        entradas[1:],
        start=1
    ):

        partes.append(
            f"G{i} R$ {valor:.2f}"
        )

    linha_gales = (
        " → ".join(
            partes
        )
    )

    return (
        "📊 <b>NOVO SINAL</b>\n\n"

        f"💱 <b>{ativo}</b>\n"
        f"🌐 {mercado}\n"
        f"⏱ M{timeframe}\n"
        f"🎯 <b>{sinal['direcao'].upper()}</b>\n"
        f"🔥 Score: <b>{sinal['score']}/100</b>\n\n"

        f"💰 {linha_gales}\n\n"

        f"📈 Acerto: <b>{assertividade():.2f}%</b>\n"
        f"📊 {stats['wins']} WIN / "
        f"{stats['losses']} LOSS"
    )


# ============================================================
# RESULTADO DE UMA VELA
# ============================================================

def resultado_direcao(
    abertura,
    fechamento,
    direcao
):

    if fechamento == abertura:
        return "draw"

    if direcao == "call":

        return (
            "win"
            if fechamento > abertura
            else "loss"
        )

    return (
        "win"
        if fechamento < abertura
        else "loss"
    )


# ============================================================
# REGISTRAR RESULTADO FINAL
# ============================================================

def registrar_resultado_final(
    sinal,
    resultado,
    nivel
):

    ativo = sinal["ativo"]

    timeframe = sinal[
        "timeframe"
    ]

    mercado = sinal[
        "mercado"
    ]


    with estado_lock:

        if resultado == "win":

            stats[
                "wins"
            ] += 1

            if nivel == 0:

                stats[
                    "win_direto"
                ] += 1

            elif nivel == 1:

                stats[
                    "win_g1"
                ] += 1

            elif nivel == 2:

                stats[
                    "win_g2"
                ] += 1


            stats_timeframe[
                f"M{timeframe}"
            ][
                "wins"
            ] += 1


            stats_mercado[
                mercado
            ][
                "wins"
            ] += 1


            stats_ativo[
                ativo
            ][
                "wins"
            ] += 1


        else:

            stats[
                "losses"
            ] += 1


            stats_timeframe[
                f"M{timeframe}"
            ][
                "losses"
            ] += 1


            stats_mercado[
                mercado
            ][
                "losses"
            ] += 1


            stats_ativo[
                ativo
            ][
                "losses"
            ] += 1


    taxa = assertividade()


    if resultado == "win":

        if nivel == 0:

            titulo = (
                "✅ <b>WIN</b>"
            )

        else:

            titulo = (
                f"✅ <b>WIN G{nivel}</b>"
            )

    else:

        titulo = (
            "❌ <b>LOSS</b>"
        )


    enviar_telegram(

        f"{titulo}\n\n"

        f"💱 {ativo} | "
        f"M{timeframe} | "
        f"{sinal['direcao'].upper()}\n\n"

        f"📊 {stats['wins']} WIN / "
        f"{stats['losses']} LOSS\n"

        f"🎯 Acerto: "
        f"<b>{taxa:.2f}%</b>"
    )


# ============================================================
# ACOMPANHAR SINAL VIRTUAL
# ============================================================

def acompanhar_sinal(
    chave
):

    try:

        sinal = sinais_pendentes.get(
            chave
        )

        if not sinal:
            return


        timeframe = sinal[
            "timeframe"
        ]

        segundos = (
            timeframe
            * 60
        )

        direcao = sinal[
            "direcao"
        ]

        ativo = sinal[
            "ativo"
        ]


        # Espera o fechamento da vela
        # seguinte ao sinal.
        proximo_fechamento = (
            (
                int(
                    time.time()
                )
                // segundos
            )
            + 1
        ) * segundos


        espera = (
            proximo_fechamento
            - time.time()
            + 2
        )

        if espera > 0:
            time.sleep(
                espera
            )


        # Entrada + Gales
        qtd_tentativas = (
            sinal[
                "gales"
            ]
            + 1
        )


        for nivel in range(
            qtd_tentativas
        ):

            if not garantir_conexao():
                return


            fim = int(
                time.time()
            )


            with api_lock:

                candles = api.get_candles(
                    ativo,
                    segundos,
                    3,
                    fim
                )


            candles = normalizar_candles(
                candles
            )


            if not candles:

                time.sleep(
                    segundos
                )

                continue


            # Procuramos a vela
            # que acabou de fechar.
            candle = candles[-2] if (
                len(candles) >= 2
            ) else candles[-1]


            resultado = resultado_direcao(
                candle["open"],
                candle["close"],
                direcao
            )


            if resultado == "win":

                registrar_resultado_final(
                    sinal,
                    "win",
                    nivel
                )

                return


            if resultado == "draw":

                # Empate não conta como
                # WIN nem LOSS.
                stats[
                    "draws"
                ] += 1


            # Se ainda existe Gale,
            # espera a próxima vela.
            if (
                nivel
                <
                qtd_tentativas - 1
            ):

                proximo = (
                    (
                        int(
                            time.time()
                        )
                        // segundos
                    )
                    + 1
                ) * segundos


                espera = (
                    proximo
                    - time.time()
                    + 2
                )


                if espera > 0:

                    time.sleep(
                        espera
                    )


        # Perdeu entrada + todos os Gales.
        registrar_resultado_final(
            sinal,
            "loss",
            sinal[
                "gales"
            ]
        )


    except Exception:

        logger.exception(
            "Erro acompanhando sinal"
        )


    finally:

        sinais_pendentes.pop(
            chave,
            None
        )


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    ativo,
    timeframe,
    sinal
):

    mercado = (
        "OTC"
        if "-OTC" in ativo
        else "NORMAL"
    )


    chave = (
        f"{ativo}:"
        f"{timeframe}"
    )


    if chave in sinais_pendentes:
        return


    agora = time.time()


    ultimo = ultimo_sinal.get(
        chave,
        0
    )


    if (
        agora - ultimo
        <
        timeframe * 60
    ):
        return


    ultimo_sinal[
        chave
    ] = agora


    dados = {

        "ativo":
            ativo,

        "mercado":
            mercado,

        "timeframe":
            timeframe,

        "direcao":
            sinal[
                "direcao"
            ],

        "score":
            sinal[
                "score"
            ],

        "gales":
            GALES_INICIAIS,

        "criado":
            agora
    }


    sinais_pendentes[
        chave
    ] = dados


    stats[
        "sinais"
    ] += 1


    enviar_telegram(
        mensagem_sinal(
            ativo,
            timeframe,
            sinal
        )
    )


    logger.info(
        "SINAL | %s | M%s | %s | %s",
        ativo,
        timeframe,
        sinal[
            "direcao"
        ],
        sinal[
            "score"
        ]
    )


    threading.Thread(
        target=acompanhar_sinal,
        args=(
            chave,
        ),
        daemon=True
    ).start()


# ============================================================
# ATIVOS
# ============================================================

def ativos_candidatos():

    resultado = []

    for ativo in ATIVOS_BASE:

        resultado.append(
            ativo
        )

        resultado.append(
            f"{ativo}-OTC"
        )

    return list(
        dict.fromkeys(
            resultado
        )
    )


# ============================================================
# SCANNER
# ============================================================

def analisar_ativo(
    ativo,
    timeframe,
    segundos
):

    try:

        if not garantir_conexao():
            return


        with api_lock:

            candles = api.get_candles(
                ativo,
                segundos,
                100,
                time.time()
            )


        candles = normalizar_candles(
            candles
        )


        if len(candles) < 60:
            return


        stats[
            "candles_recebidos"
        ] += 1


        sinal = analisar_candles(
            candles
        )


        if sinal:

            criar_sinal(
                ativo,
                timeframe,
                sinal
            )


    except Exception:

        # Ativo fechado/inexistente
        # não derruba scanner.
        return


def loop_scanner():

    time.sleep(
        5
    )


    while True:

        try:

            if not garantir_conexao():

                time.sleep(
                    30
                )

                continue


            stats[
                "ciclos_scanner"
            ] += 1


            for ativo in ativos_candidatos():

                for timeframe, segundos in (
                    TIMEFRAMES.items()
                ):

                    analisar_ativo(
                        ativo,
                        timeframe,
                        segundos
                    )

                    time.sleep(
                        0.8
                    )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception:

            stats[
                "erros"
            ] += 1

            logger.exception(
                "Erro scanner"
            )

            time.sleep(
                20
            )


# ============================================================
# BACKTEST
# ============================================================

backtest_estado = {

    "executando":
        False,

    "concluido":
        False,

    "resultado":
        None
}


def baixar_historico(
    ativo,
    segundos,
    dias=7
):

    quantidade_desejada = int(
        (
            dias
            * 24
            * 60
            * 60
        )
        / segundos
    )


    # Precisamos de candles extras
    # para os indicadores.
    quantidade_desejada += 100


    todos = []

    fim = time.time()


    while (
        len(todos)
        <
        quantidade_desejada
    ):

        quantidade = min(
            1000,
            quantidade_desejada
            - len(todos)
        )


        try:

            with api_lock:

                lote = api.get_candles(
                    ativo,
                    segundos,
                    quantidade,
                    fim
                )


        except Exception:

            break


        lote = normalizar_candles(
            lote
        )


        if not lote:
            break


        todos = (
            lote
            + todos
        )


        primeiro = lote[0][
            "from"
        ]


        fim = (
            primeiro
            - 1
        )


        time.sleep(
            0.5
        )


    # Remove duplicados
    unicos = {}

    for candle in todos:

        unicos[
            candle["from"]
        ] = candle


    resultado = list(
        unicos.values()
    )


    resultado.sort(
        key=lambda x:
            x["from"]
    )


    return resultado


def backtest_timeframe(
    candles
):

    resultado = {

        "sinais": 0,

        "wins": 0,

        "win_direto": 0,

        "win_g1": 0,

        "win_g2": 0,

        "losses": 0
    }


    # 60 candles para análise
    # + 3 candles futuros
    # para Entrada/G1/G2.
    limite = (
        len(candles)
        - 3
    )


    i = 60


    while i < limite:

        janela = candles[
            i - 60:
            i
        ]


        sinal = analisar_candles(
            janela
        )


        if not sinal:

            i += 1
            continue


        resultado[
            "sinais"
        ] += 1


        direcao = sinal[
            "direcao"
        ]


        ganhou = False


        # Entrada
        # G1
        # G2
        for gale in range(
            3
        ):

            candle = candles[
                i + gale
            ]


            r = resultado_direcao(
                candle["open"],
                candle["close"],
                direcao
            )


            if r == "win":

                resultado[
                    "wins"
                ] += 1


                if gale == 0:

                    resultado[
                        "win_direto"
                    ] += 1

                elif gale == 1:

                    resultado[
                        "win_g1"
                    ] += 1

                else:

                    resultado[
                        "win_g2"
                    ] += 1


                ganhou = True

                break


        if not ganhou:

            resultado[
                "losses"
            ] += 1


        # Evita gerar outro sinal
        # dentro da sequência de Gales.
        i += 3


    total = (
        resultado[
            "wins"
        ]
        +
        resultado[
            "losses"
        ]
    )


    resultado[
        "assertividade"
    ] = round(
        (
            resultado[
                "wins"
            ]
            / total
            * 100
        )
        if total
        else 0,
        2
    )


    return resultado


def executar_backtest():

    if backtest_estado[
        "executando"
    ]:

        return


    backtest_estado[
        "executando"
    ] = True


    backtest_estado[
        "concluido"
    ] = False


    enviar_telegram(
        "🧪 <b>BACKTEST INICIADO</b>\n\n"
        "Período: 7 dias\n"
        "Normal + OTC\n"
        "M1 / M5 / M15\n"
        f"Score mínimo: {SCORE_MIN}\n"
        "Entrada + G1 + G2"
    )


    geral = {

        "sinais": 0,

        "wins": 0,

        "losses": 0,

        "win_direto": 0,

        "win_g1": 0,

        "win_g2": 0,

        "por_timeframe": {},

        "por_mercado": {
            "NORMAL": {
                "wins": 0,
                "losses": 0
            },

            "OTC": {
                "wins": 0,
                "losses": 0
            }
        }
    }


    try:

        if not garantir_conexao():

            raise RuntimeError(
                "IQ Option desconectada"
            )


        for ativo in ativos_candidatos():

            mercado = (
                "OTC"
                if "-OTC" in ativo
                else "NORMAL"
            )


            for timeframe, segundos in (
                TIMEFRAMES.items()
            ):

                logger.info(
                    "BACKTEST | %s | M%s",
                    ativo,
                    timeframe
                )


                candles = baixar_historico(
                    ativo,
                    segundos,
                    dias=7
                )


                if len(candles) < 100:
                    continue


                resultado = backtest_timeframe(
                    candles
                )


                chave = (
                    f"{ativo}_M{timeframe}"
                )


                geral[
                    "por_timeframe"
                ][
                    chave
                ] = resultado


                geral[
                    "sinais"
                ] += resultado[
                    "sinais"
                ]


                geral[
                    "wins"
                ] += resultado[
                    "wins"
                ]


                geral[
                    "losses"
                ] += resultado[
                    "losses"
                ]


                geral[
                    "win_direto"
                ] += resultado[
                    "win_direto"
                ]


                geral[
                    "win_g1"
                ] += resultado[
                    "win_g1"
                ]


                geral[
                    "win_g2"
                ] += resultado[
                    "win_g2"
                ]


                geral[
                    "por_mercado"
                ][
                    mercado
                ][
                    "wins"
                ] += resultado[
                    "wins"
                ]


                geral[
                    "por_mercado"
                ][
                    mercado
                ][
                    "losses"
                ] += resultado[
                    "losses"
                ]


        total = (
            geral[
                "wins"
            ]
            +
            geral[
                "losses"
            ]
        )


        geral[
            "assertividade"
        ] = round(
            (
                geral[
                    "wins"
                ]
                / total
                * 100
            )
            if total
            else 0,
            2
        )


        backtest_estado[
            "resultado"
        ] = geral


        backtest_estado[
            "concluido"
        ] = True


        normal = geral[
            "por_mercado"
        ][
            "NORMAL"
        ]


        otc = geral[
            "por_mercado"
        ][
            "OTC"
        ]


        total_normal = (
            normal[
                "wins"
            ]
            +
            normal[
                "losses"
            ]
        )


        total_otc = (
            otc[
                "wins"
            ]
            +
            otc[
                "losses"
            ]
        )


        taxa_normal = (
            normal[
                "wins"
            ]
            / total_normal
            * 100

            if total_normal

            else 0
        )


        taxa_otc = (
            otc[
                "wins"
            ]
            / total_otc
            * 100

            if total_otc

            else 0
        )


        enviar_telegram(

            "🧪 <b>BACKTEST CONCLUÍDO</b>\n\n"

            "📅 Últimos 7 dias\n"

            f"📊 Sinais: "
            f"{geral['sinais']}\n\n"

            f"✅ WIN: "
            f"{geral['wins']}\n"

            f"❌ LOSS: "
            f"{geral['losses']}\n"

            f"🎯 Acerto: "
            f"<b>{geral['assertividade']:.2f}%</b>\n\n"

            f"✅ Direto: "
            f"{geral['win_direto']}\n"

            f"🔄 G1: "
            f"{geral['win_g1']}\n"

            f"🔄 G2: "
            f"{geral['win_g2']}\n\n"

            f"🌐 NORMAL: "
            f"{taxa_normal:.2f}%\n"

            f"🌙 OTC: "
            f"{taxa_otc:.2f}%"
        )


    except Exception:

        logger.exception(
            "Erro no backtest"
        )


    finally:

        backtest_estado[
            "executando"
        ] = False


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "robo":
            "IQ V3",

        "status":
            "online",

        "conta":
            "PRACTICE",

        "execucao_automatica":
            EXECUTAR_ORDENS,

        "score_minimo":
            SCORE_MIN,

        "sinais_pendentes":
            len(
                sinais_pendentes
            ),

        "estatisticas":
            stats,

        "assertividade":
            assertividade(),

        "gerenciamento":
            gerenciamento,

        "backtest":
            backtest_estado
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "versao": "IQ-V3"
    })


@app.route("/backtest")
def iniciar_backtest():

    if backtest_estado[
        "executando"
    ]:

        return jsonify({
            "status":
                "backtest já está executando"
        })


    threading.Thread(
        target=executar_backtest,
        daemon=True
    ).start()


    return jsonify({
        "status":
            "backtest iniciado",

        "periodo":
            "7 dias"
    })


# ============================================================
# THREADS
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
            target=loop_scanner,
            daemon=True
        ).start()


iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
