import os
import time
import math
import threading
import logging

import requests
from flask import Flask, jsonify

try:
    from iqoptionapi.stable_api import IQ_Option
except Exception:
    IQ_Option = None


# ============================================================
# ROBÔ IQ OPTION - NORMAL + OTC
# M1 / M5 / M15
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo-iq")


# ============================================================
# VARIÁVEIS DE AMBIENTE
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


# ============================================================
# IQ OPTION
# ============================================================

IQ_EMAIL = env_str(
    "IQ_EMAIL"
)

IQ_PASSWORD = env_str(
    "IQ_PASSWORD"
)

# Esta versão trabalha em PRACTICE.
IQ_BALANCE = "PRACTICE"

# false = somente sinais
# true = permite ordens PRACTICE M1/M5
EXECUTAR_ORDENS = env_bool(
    "EXECUTAR_ORDENS",
    False
)


# ============================================================
# SCANNER
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
# GERENCIAMENTO
# ============================================================

MULTIPLICADOR_GALE = 2.0

GALES_INICIAIS = 2

MAX_GALES = 5

RECUPERACAO_PERCENTUAL = 0.10


gerenciamento = {

    "entrada_original":
        ENTRADA_BASE,

    "entrada_base_atual":
        ENTRADA_BASE,

    "gales_permitidos":
        GALES_INICIAIS,

    "gale_atual":
        0,

    "divida":
        0.0,

    "em_recuperacao":
        False
}


# ============================================================
# ESTATÍSTICAS
# ============================================================

stats = {

    "sinais": 0,

    "call": 0,

    "put": 0,

    "operacoes": 0,

    "wins": 0,

    "losses": 0,

    "draws": 0,

    "lucro": 0.0,

    "prejuizo": 0.0,

    "sequencia_loss_atual": 0,

    "maior_sequencia_loss": 0,

    "erros": 0,

    "ciclos_scanner": 0,

    "candles_recebidos": 0
}


stats_timeframe = {

    "M1": {
        "win": 0,
        "loss": 0
    },

    "M5": {
        "win": 0,
        "loss": 0
    },

    "M15": {
        "win": 0,
        "loss": 0
    }
}


stats_mercado = {

    "NORMAL": {
        "win": 0,
        "loss": 0
    },

    "OTC": {
        "win": 0,
        "loss": 0
    }
}


# ============================================================
# CONTROLE GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()

estado_lock = threading.RLock()

ultimo_sinal = {}

operacao_em_andamento = False

ultimo_telegram_conexao = 0


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:

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
                resposta.text[:200]
            )

        return resposta.ok

    except Exception:

        logger.exception(
            "Erro enviando Telegram."
        )

        return False


# ============================================================
# CONEXÃO IQ OPTION
# ============================================================

def conectar_iq():

    global api
    global ultimo_telegram_conexao

    if IQ_Option is None:

        logger.error(
            "iqoptionapi não carregada."
        )

        return False


    if not IQ_EMAIL or not IQ_PASSWORD:

        logger.warning(
            "IQ_EMAIL/IQ_PASSWORD não configurados."
        )

        return False


    try:

        logger.info(
            "Conectando à IQ Option..."
        )

        nova_api = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        ok, motivo = nova_api.connect()

        if not ok:

            logger.error(
                "Falha na conexão IQ: %s",
                motivo
            )

            return False


        nova_api.change_balance(
            "PRACTICE"
        )


        with api_lock:

            api = nova_api


        logger.info(
            "IQ Option conectada em PRACTICE."
        )


        agora = time.time()

        # Evita Telegram repetido em reconexões rápidas.
        if (
            agora
            - ultimo_telegram_conexao
            > 300
        ):

            enviar_telegram(
                "🟢 <b>ROBÔ CONECTADO</b>\n\n"
                "Conta: <b>PRACTICE</b>\n"
                "Mercados: <b>Normal + OTC</b>\n"
                "Tempos: <b>M1 / M5 / M15</b>\n"
                f"Score mínimo: <b>{SCORE_MIN}</b>\n"
                f"Entrada base: <b>R$ {ENTRADA_BASE:.2f}</b>\n\n"
                f"Execução automática: "
                f"<b>{'ATIVA' if EXECUTAR_ORDENS else 'DESATIVADA'}</b>"
            )

            ultimo_telegram_conexao = agora


        return True


    except Exception:

        logger.exception(
            "Erro conectando IQ Option."
        )

        return False


def garantir_conexao():

    global api

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
# MATEMÁTICA / INDICADORES
# ============================================================

def media(valores):

    if not valores:
        return 0.0

    return (
        sum(valores)
        / len(valores)
    )


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


    inicio = (
        len(valores)
        - periodo
        - 1
    )


    for i in range(
        inicio,
        len(valores) - 1
    ):

        diferenca = (
            valores[i + 1]
            - valores[i]
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


    ganho_medio = media(
        ganhos
    )

    perda_media = media(
        perdas
    )


    if perda_media == 0:

        if ganho_medio > 0:
            return 100.0

        return 50.0


    rs = (
        ganho_medio
        / perda_media
    )


    return (
        100.0
        -
        (
            100.0
            / (
                1.0
                + rs
            )
        )
    )


def desvio_padrao(valores):

    if not valores:
        return 0.0


    m = media(
        valores
    )


    variancia = media([
        (
            valor
            - m
        ) ** 2

        for valor in valores
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


    inferior = (
        centro
        - 2.0
        * desvio
    )


    superior = (
        centro
        + 2.0
        * desvio
    )


    return (
        inferior,
        centro,
        superior
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

        return 0.0


    return (
        e12
        - e26
    )


# ============================================================
# VALIDAÇÃO DOS CANDLES
# ============================================================

def normalizar_candles(candles):

    if not candles:
        return []


    resultado = []


    for candle in candles:

        try:

            abertura = float(
                candle["open"]
            )

            fechamento = float(
                candle["close"]
            )

            maxima = float(
                candle["max"]
            )

            minima = float(
                candle["min"]
            )

            timestamp = int(
                candle.get(
                    "from",
                    0
                )
            )


            if (
                abertura <= 0
                or fechamento <= 0
                or maxima <= 0
                or minima <= 0
            ):

                continue


            resultado.append({
                "open":
                    abertura,

                "close":
                    fechamento,

                "max":
                    maxima,

                "min":
                    minima,

                "from":
                    timestamp
            })


        except Exception:

            continue


    resultado.sort(
        key=lambda x:
            x["from"]
    )


    return resultado


# ============================================================
# MOTOR DE ESTRATÉGIAS
# ============================================================

def analisar_candles(candles):

    candles = normalizar_candles(
        candles
    )


    if len(candles) < 60:
        return None


    opens = [
        candle["open"]
        for candle in candles
    ]


    closes = [
        candle["close"]
        for candle in candles
    ]


    highs = [
        candle["max"]
        for candle in candles
    ]


    lows = [
        candle["min"]
        for candle in candles
    ]


    score_call = 0

    score_put = 0


    motivos_call = []

    motivos_put = []


    # ========================================================
    # 1. TENDÊNCIA EMA
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

            score_call += 20

            motivos_call.append(
                "EMA tendência de alta"
            )

        elif ema20 < ema50:

            score_put += 20

            motivos_put.append(
                "EMA tendência de baixa"
            )


    # ========================================================
    # 2. RSI
    # ========================================================

    valor_rsi = rsi(
        closes,
        14
    )


    if valor_rsi <= 35:

        score_call += 15

        motivos_call.append(
            f"RSI sobrevendido {valor_rsi:.1f}"
        )


    elif valor_rsi >= 65:

        score_put += 15

        motivos_put.append(
            f"RSI sobrecomprado {valor_rsi:.1f}"
        )


    # ========================================================
    # 3. BOLLINGER
    # ========================================================

    inferior, centro, superior = (
        bollinger(
            closes,
            20
        )
    )


    ultimo = closes[-1]


    if inferior is not None:

        if ultimo <= inferior:

            score_call += 10

            motivos_call.append(
                "Bollinger inferior"
            )


        elif ultimo >= superior:

            score_put += 10

            motivos_put.append(
                "Bollinger superior"
            )


    # ========================================================
    # 4. PRICE ACTION
    # ========================================================

    abertura = opens[-1]

    fechamento = closes[-1]

    maxima = highs[-1]

    minima = lows[-1]


    corpo = abs(
        fechamento
        - abertura
    )


    amplitude = max(
        maxima
        - minima,
        0.00000001
    )


    forca_corpo = (
        corpo
        / amplitude
    )


    if (
        fechamento > abertura
        and forca_corpo >= 0.55
    ):

        score_call += 15

        motivos_call.append(
            "Price Action comprador"
        )


    elif (
        fechamento < abertura
        and forca_corpo >= 0.55
    ):

        score_put += 15

        motivos_put.append(
            "Price Action vendedor"
        )


    # ========================================================
    # 5. SUPORTE / RESISTÊNCIA
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


    tolerancia = (
        faixa
        * 0.10
    )


    if (
        ultimo
        <= suporte
        + tolerancia
    ):

        score_call += 10

        motivos_call.append(
            "Região de suporte"
        )


    if (
        ultimo
        >= resistencia
        - tolerancia
    ):

        score_put += 10

        motivos_put.append(
            "Região de resistência"
        )


    # ========================================================
    # 6. ROMPIMENTO
    # ========================================================

    if ultimo > resistencia:

        score_call += 15

        motivos_call.append(
            "Rompimento de resistência"
        )


    elif ultimo < suporte:

        score_put += 15

        motivos_put.append(
            "Rompimento de suporte"
        )


    # ========================================================
    # 7. MACD
    # ========================================================

    valor_macd = macd(
        closes
    )


    if valor_macd > 0:

        score_call += 10

        motivos_call.append(
            "MACD positivo"
        )


    elif valor_macd < 0:

        score_put += 10

        motivos_put.append(
            "MACD negativo"
        )


    # ========================================================
    # 8. MOMENTUM
    # ========================================================

    if (
        closes[-1]
        > closes[-2]
        > closes[-3]
    ):

        score_call += 5

        motivos_call.append(
            "Momentum comprador"
        )


    elif (
        closes[-1]
        < closes[-2]
        < closes[-3]
    ):

        score_put += 5

        motivos_put.append(
            "Momentum vendedor"
        )


    score_call = min(
        score_call,
        100
    )


    score_put = min(
        score_put,
        100
    )


    # Evita sinal quando os dois lados
    # estão muito próximos.
    diferenca = abs(
        score_call
        - score_put
    )


    if diferenca < 15:
        return None


    if (
        score_call >= SCORE_MIN
        and score_call > score_put
    ):

        return {

            "direcao":
                "call",

            "score":
                score_call,

            "score_contra":
                score_put,

            "rsi":
                round(
                    valor_rsi,
                    2
                ),

            "motivos":
                motivos_call
        }


    if (
        score_put >= SCORE_MIN
        and score_put > score_call
    ):

        return {

            "direcao":
                "put",

            "score":
                score_put,

            "score_contra":
                score_call,

            "rsi":
                round(
                    valor_rsi,
                    2
                ),

            "motivos":
                motivos_put
        }


    return None


# ============================================================
# GERENCIAMENTO
# ============================================================

def valor_entrada():

    base = gerenciamento[
        "entrada_base_atual"
    ]


    gale = gerenciamento[
        "gale_atual"
    ]


    return round(
        base
        * (
            MULTIPLICADOR_GALE
            ** gale
        ),
        2
    )


def registrar_win(
    lucro
):

    gerenciamento[
        "gale_atual"
    ] = 0


    if not gerenciamento[
        "em_recuperacao"
    ]:

        return


    gerenciamento[
        "divida"
    ] = round(
        max(
            0.0,
            gerenciamento[
                "divida"
            ]
            - max(
                lucro,
                0
            )
        ),
        2
    )


    if (
        gerenciamento[
            "divida"
        ]
        <= 0.01
    ):

        gerenciamento[
            "divida"
        ] = 0.0


        gerenciamento[
            "em_recuperacao"
        ] = False


        gerenciamento[
            "entrada_base_atual"
        ] = ENTRADA_BASE


        gerenciamento[
            "gales_permitidos"
        ] = GALES_INICIAIS


        return


    recuperacao = (
        gerenciamento[
            "divida"
        ]
        * RECUPERACAO_PERCENTUAL
    )


    gerenciamento[
        "entrada_base_atual"
    ] = round(
        ENTRADA_BASE
        + recuperacao,
        2
    )


def registrar_loss(
    valor_perdido
):

    gerenciamento[
        "divida"
    ] = round(
        gerenciamento[
            "divida"
        ]
        + valor_perdido,
        2
    )


    # Ainda existem Gales
    # dentro do ciclo atual.
    if (
        gerenciamento[
            "gale_atual"
        ]
        <
        gerenciamento[
            "gales_permitidos"
        ]
    ):

        gerenciamento[
            "gale_atual"
        ] += 1

        return


    # ========================================================
    # CICLO COMPLETO PERDIDO
    # ========================================================

    gerenciamento[
        "em_recuperacao"
    ] = True


    gerenciamento[
        "gale_atual"
    ] = 0


    # 2 -> 3 -> 4 -> 5 Gales
    if (
        gerenciamento[
            "gales_permitidos"
        ]
        < MAX_GALES
    ):

        gerenciamento[
            "gales_permitidos"
        ] += 1


    # Exemplo:
    #
    # R$2 + R$4 + R$8 = R$14
    #
    # 10% de R$14 = R$1,40
    #
    # nova entrada =
    # R$2 + R$1,40 = R$3,40

    recuperacao = (
        gerenciamento[
            "divida"
        ]
        * RECUPERACAO_PERCENTUAL
    )


    gerenciamento[
        "entrada_base_atual"
    ] = round(
        ENTRADA_BASE
        + recuperacao,
        2
    )


# ============================================================
# ATIVOS NORMAL + OTC
# ============================================================

def buscar_ativos_candidatos():

    candidatos = []


    for ativo in ATIVOS_BASE:

        candidatos.append(
            ativo
        )


        candidatos.append(
            f"{ativo}-OTC"
        )


    # Remove duplicados
    # preservando a ordem.
    return list(
        dict.fromkeys(
            candidatos
        )
    )


# ============================================================
# CONTROLE DE SINAIS DUPLICADOS
# ============================================================

def pode_enviar(
    ativo,
    timeframe,
    direcao
):

    chave = (
        f"{ativo}:"
        f"M{timeframe}:"
        f"{direcao}"
    )


    agora = int(
        time.time()
    )


    # No máximo um sinal igual
    # por período do timeframe.
    limite = (
        timeframe
        * 60
    )


    anterior = ultimo_sinal.get(
        chave,
        0
    )


    if (
        agora
        - anterior
        < limite
    ):

        return False


    ultimo_sinal[
        chave
    ] = agora


    return True


# ============================================================
# TELEGRAM - SINAL
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


    motivos = "\n".join([
        f"• {motivo}"

        for motivo in sinal[
            "motivos"
        ][:6]
    ])


    return (
        "📊 <b>NOVO SINAL</b>\n\n"

        f"💱 Ativo: <b>{ativo}</b>\n"

        f"🌐 Mercado: <b>{mercado}</b>\n"

        f"⏱ Tempo: <b>M{timeframe}</b>\n"

        f"🎯 Direção: "
        f"<b>{sinal['direcao'].upper()}</b>\n"

        f"🔥 Score: "
        f"<b>{sinal['score']}/100</b>\n"

        f"↔ Score contrário: "
        f"{sinal['score_contra']}/100\n"

        f"📈 RSI: "
        f"{sinal['rsi']}\n\n"

        f"💰 Entrada calculada: "
        f"<b>R$ {valor_entrada():.2f}</b>\n"

        f"🔄 Gale: "
        f"{gerenciamento['gale_atual']}/"
        f"{gerenciamento['gales_permitidos']}\n"

        f"♻️ Recuperação: "
        f"{'SIM' if gerenciamento['em_recuperacao'] else 'NÃO'}\n\n"

        f"🔎 <b>Confirmações</b>\n"
        f"{motivos}"
    )


# ============================================================
# RESULTADO DAS OPERAÇÕES PRACTICE
# ============================================================

def aguardar_resultado_digital(
    order_id,
    ativo,
    timeframe,
    valor
):

    global operacao_em_andamento


    try:

        limite = (
            time.time()
            +
            (
                timeframe
                * 60
            )
            +
            120
        )


        resultado = None


        while (
            time.time()
            < limite
        ):

            try:

                with api_lock:

                    fechado, lucro = (
                        api.check_win_digital_v2(
                            order_id
                        )
                    )


                if fechado:

                    resultado = float(
                        lucro
                    )

                    break


            except Exception:

                pass


            time.sleep(
                2
            )


        if resultado is None:

            logger.warning(
                "Resultado não confirmado: %s",
                order_id
            )

            enviar_telegram(
                "⚠️ <b>RESULTADO NÃO CONFIRMADO</b>\n\n"
                f"Ativo: {ativo}\n"
                f"Tempo: M{timeframe}"
            )

            return


        mercado = (
            "OTC"
            if "-OTC" in ativo
            else "NORMAL"
        )


        chave_tf = (
            f"M{timeframe}"
        )


        with estado_lock:

            stats[
                "operacoes"
            ] += 1


            if resultado > 0:

                stats[
                    "wins"
                ] += 1


                stats[
                    "sequencia_loss_atual"
                ] = 0


                stats[
                    "lucro"
                ] = round(
                    stats[
                        "lucro"
                    ]
                    + resultado,
                    2
                )


                stats_timeframe[
                    chave_tf
                ][
                    "win"
                ] += 1


                stats_mercado[
                    mercado
                ][
                    "win"
                ] += 1


                registrar_win(
                    resultado
                )


                status = (
                    "✅ <b>WIN</b>"
                )


            elif resultado < 0:

                stats[
                    "losses"
                ] += 1


                stats[
                    "sequencia_loss_atual"
                ] += 1


                stats[
                    "prejuizo"
                ] = round(
                    stats[
                        "prejuizo"
                    ]
                    + abs(
                        resultado
                    ),
                    2
                )


                stats[
                    "maior_sequencia_loss"
                ] = max(
                    stats[
                        "maior_sequencia_loss"
                    ],
                    stats[
                        "sequencia_loss_atual"
                    ]
                )


                stats_timeframe[
                    chave_tf
                ][
                    "loss"
                ] += 1


                stats_mercado[
                    mercado
                ][
                    "loss"
                ] += 1


                registrar_loss(
                    valor
                )


                status = (
                    "❌ <b>LOSS</b>"
                )


            else:

                stats[
                    "draws"
                ] += 1


                status = (
                    "⚪ <b>DRAW</b>"
                )


        total = (
            stats[
                "wins"
            ]
            +
            stats[
                "losses"
            ]
        )


        assertividade = (
            (
                stats[
                    "wins"
                ]
                / total
            )
            * 100.0

            if total

            else 0.0
        )


        saldo_resultados = (
            stats[
                "lucro"
            ]
            -
            stats[
                "prejuizo"
            ]
        )


        enviar_telegram(
            f"{status}\n\n"

            f"💱 {ativo}\n"

            f"🌐 {mercado}\n"

            f"⏱ M{timeframe}\n"

            f"💵 Entrada: "
            f"R$ {valor:.2f}\n"

            f"💰 Resultado: "
            f"R$ {resultado:.2f}\n\n"

            f"✅ Wins: "
            f"{stats['wins']}\n"

            f"❌ Losses: "
            f"{stats['losses']}\n"

            f"⚪ Draws: "
            f"{stats['draws']}\n"

            f"🎯 Assertividade: "
            f"{assertividade:.2f}%\n"

            f"📉 Maior sequência LOSS: "
            f"{stats['maior_sequencia_loss']}\n\n"

            f"💵 Resultado acumulado: "
            f"R$ {saldo_resultados:.2f}\n"

            f"♻️ Dívida de recuperação: "
            f"R$ {gerenciamento['divida']:.2f}\n"

            f"➡️ Próxima entrada: "
            f"R$ {valor_entrada():.2f}\n"

            f"🔄 Próximo Gale: "
            f"{gerenciamento['gale_atual']}/"
            f"{gerenciamento['gales_permitidos']}"
        )


    except Exception:

        logger.exception(
            "Erro acompanhando resultado."
        )


    finally:

        with estado_lock:

            operacao_em_andamento = False


# ============================================================
# EXECUÇÃO PRACTICE
# ============================================================

def executar_operacao(
    ativo,
    timeframe,
    direcao
):

    global operacao_em_andamento


    # M15 permanece análise/sinal.
    #
    # Não tentamos enviar Digital M15
    # nesta versão.
    if timeframe not in (
        1,
        5
    ):

        return


    # Enquanto false:
    # Telegram recebe sinais,
    # mas nenhuma ordem é enviada.
    if not EXECUTAR_ORDENS:

        return


    with estado_lock:

        if operacao_em_andamento:

            logger.info(
                "Operação em andamento. "
                "Novo sinal não executado."
            )

            return


        operacao_em_andamento = True


    try:

        if not garantir_conexao():

            with estado_lock:
                operacao_em_andamento = False

            return


        valor = valor_entrada()


        logger.info(
            "Enviando PRACTICE | %s | M%s | %s | R$ %.2f",
            ativo,
            timeframe,
            direcao,
            valor
        )


        with api_lock:

            sucesso, order_id = (
                api.buy_digital_spot_v2(
                    ativo,
                    valor,
                    direcao,
                    timeframe
                )
            )


        if not sucesso:

            logger.warning(
                "Ordem recusada: %s",
                order_id
            )


            with estado_lock:
                operacao_em_andamento = False


            return


        enviar_telegram(
            "🟡 <b>ORDEM PRACTICE ABERTA</b>\n\n"

            f"💱 Ativo: "
            f"<b>{ativo}</b>\n"

            f"⏱ Tempo: "
            f"<b>M{timeframe}</b>\n"

            f"🎯 Direção: "
            f"<b>{direcao.upper()}</b>\n"

            f"💵 Valor: "
            f"<b>R$ {valor:.2f}</b>\n\n"

            f"🔄 Gale: "
            f"{gerenciamento['gale_atual']}/"
            f"{gerenciamento['gales_permitidos']}"
        )


        threading.Thread(
            target=aguardar_resultado_digital,
            args=(
                order_id,
                ativo,
                timeframe,
                valor
            ),
            daemon=True
        ).start()


    except Exception:

        logger.exception(
            "Erro executando operação."
        )


        stats[
            "erros"
        ] += 1


        with estado_lock:

            operacao_em_andamento = False


# ============================================================
# ANALISAR UM ATIVO
# ============================================================

def analisar_ativo(
    ativo,
    timeframe,
    segundos
):

    try:

        if not garantir_conexao():
            return False


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

            logger.debug(
                "Sem candles suficientes: %s M%s",
                ativo,
                timeframe
            )

            return False


        stats[
            "candles_recebidos"
        ] += 1


        logger.info(
            "CANDLES OK | %s | M%s | %s candles",
            ativo,
            timeframe,
            len(candles)
        )


        sinal = analisar_candles(
            candles
        )


        if not sinal:
            return True


        if not pode_enviar(
            ativo,
            timeframe,
            sinal[
                "direcao"
            ]
        ):

            return True


        with estado_lock:

            stats[
                "sinais"
            ] += 1


            stats[
                sinal[
                    "direcao"
                ]
            ] += 1


        logger.info(
            "SINAL | %s | M%s | %s | score=%s",
            ativo,
            timeframe,
            sinal[
                "direcao"
            ],
            sinal[
                "score"
            ]
        )


        enviar_telegram(
            mensagem_sinal(
                ativo,
                timeframe,
                sinal
            )
        )


        executar_operacao(
            ativo,
            timeframe,
            sinal[
                "direcao"
            ]
        )


        return True


    except Exception as erro:

        # Ativo fechado ou indisponível
        # não derruba o robô.
        logger.debug(
            "INDISPONÍVEL | %s | M%s | %s",
            ativo,
            timeframe,
            erro
        )


        return False


# ============================================================
# SCANNER PRINCIPAL
# ============================================================

def loop_scanner():

    # Aguarda Gunicorn terminar
    # a inicialização.
    time.sleep(
        5
    )


    while True:

        try:

            if not garantir_conexao():

                logger.warning(
                    "IQ desconectada. "
                    "Nova tentativa em 30 segundos."
                )

                time.sleep(
                    30
                )

                continue


            candidatos = (
                buscar_ativos_candidatos()
            )


            stats[
                "ciclos_scanner"
            ] += 1


            logger.info(
                "INÍCIO SCANNER | ciclo=%s | candidatos=%s",
                stats[
                    "ciclos_scanner"
                ],
                len(
                    candidatos
                )
            )


            ativos_com_dados = set()


            for ativo in candidatos:

                for timeframe, segundos in (
                    TIMEFRAMES.items()
                ):

                    recebeu = analisar_ativo(
                        ativo,
                        timeframe,
                        segundos
                    )


                    if recebeu:

                        ativos_com_dados.add(
                            ativo
                        )


                    # Pequeno intervalo para
                    # não bombardear a API.
                    time.sleep(
                        0.8
                    )


            logger.info(
                "FIM SCANNER | ativos com dados=%s | sinais=%s",
                len(
                    ativos_com_dados
                ),
                stats[
                    "sinais"
                ]
            )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception:

            logger.exception(
                "Erro geral no scanner."
            )


            stats[
                "erros"
            ] += 1


            time.sleep(
                20
            )


# ============================================================
# FLASK / RENDER
# ============================================================

def calcular_assertividade():

    total = (
        stats[
            "wins"
        ]
        +
        stats[
            "losses"
        ]
    )


    if total == 0:
        return 0.0


    return round(
        (
            stats[
                "wins"
            ]
            / total
        )
        * 100.0,
        2
    )


@app.route("/")
def home():

    conectado = False


    try:

        conectado = (
            api is not None
            and api.check_connect()
        )

    except Exception:

        conectado = False


    return jsonify({

        "robo":
            "IQ Normal + OTC",

        "versao":
            "2.0",

        "status":
            "online",

        "iq_conectada":
            conectado,

        "conta":
            "PRACTICE",

        "execucao_automatica":
            EXECUTAR_ORDENS,

        "mercados": [
            "NORMAL",
            "OTC"
        ],

        "timeframes": [
            "M1",
            "M5",
            "M15"
        ],

        "score_minimo":
            SCORE_MIN,

        "entrada_atual":
            valor_entrada(),

        "assertividade":
            calcular_assertividade(),

        "gerenciamento":
            gerenciamento,

        "estatisticas":
            stats,

        "por_timeframe":
            stats_timeframe,

        "por_mercado":
            stats_mercado,

        "iq_configurada":
            bool(
                IQ_EMAIL
                and IQ_PASSWORD
            ),

        "telegram_configurado":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            )
    })


@app.route("/health")
def health():

    conectado = False


    try:

        conectado = (
            api is not None
            and api.check_connect()
        )

    except Exception:

        pass


    return jsonify({

        "status":
            "ok",

        "versao":
            "IQ-V2",

        "iq_conectada":
            conectado,

        "conta":
            "PRACTICE",

        "execucao":
            (
                "PRACTICE AUTOMATICA"
                if EXECUTAR_ORDENS
                else "SOMENTE SINAIS"
            ),

        "candles_recebidos":
            stats[
                "candles_recebidos"
            ],

        "sinais":
            stats[
                "sinais"
            ]
    })


# ============================================================
# INICIALIZAÇÃO
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
            daemon=True,
            name="scanner-iq"
        ).start()


iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
            )
