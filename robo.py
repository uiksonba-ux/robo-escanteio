import os
import time
import math
import threading
import logging
from datetime import datetime

import requests
from flask import Flask, jsonify

try:
    from iqoptionapi.stable_api import IQ_Option
except Exception:
    IQ_Option = None


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-iq")


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
        "1", "true", "yes", "sim", "on"
    )


# ------------------------------------------------------------
# RENDER / TELEGRAM
# ------------------------------------------------------------

PORT = env_int("PORT", 10000)

TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN")
CHAT_ID = env_str("CHAT_ID")


# ------------------------------------------------------------
# IQ OPTION
# ------------------------------------------------------------

IQ_EMAIL = env_str("IQ_EMAIL")
IQ_PASSWORD = env_str("IQ_PASSWORD")

# PRACTICE por segurança
IQ_BALANCE = env_str(
    "IQ_BALANCE",
    "PRACTICE"
).upper()

# False = só sinais
# True = executa automaticamente na PRACTICE
EXECUTAR_ORDENS = env_bool(
    "EXECUTAR_ORDENS",
    False
)


# ------------------------------------------------------------
# FILTROS
# ------------------------------------------------------------

SCORE_MIN = env_int(
    "SCORE_MIN",
    65
)

INTERVALO_ANALISE = env_int(
    "INTERVALO_ANALISE",
    20
)

MIN_PAYOUT = env_float(
    "MIN_PAYOUT",
    0.70
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
            "EURUSD,EURGBP,GBPUSD,USDJPY,"
            "AUDUSD,EURJPY,GBPJPY,USDCHF,"
            "AUDCAD,AUDJPY,EURCAD"
        )
    ).split(",")
    if ativo.strip()
]


# ============================================================
# GERENCIAMENTO
# ============================================================

ENTRADA_BASE = env_float(
    "ENTRADA_BASE",
    2.00
)

MULTIPLICADOR_GALE = 2.0

GALES_INICIAIS = 2

MAX_GALES = 5

RECUPERACAO_PERCENTUAL = 0.10


gerenciamento = {
    "entrada_original": ENTRADA_BASE,
    "entrada_base_atual": ENTRADA_BASE,
    "gales_permitidos": GALES_INICIAIS,
    "gale_atual": 0,
    "divida": 0.0,
    "em_recuperacao": False
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

    "maior_sequencia_loss": 0,
    "sequencia_loss_atual": 0,

    "erros": 0
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
# CONTROLE
# ============================================================

api = None

api_lock = threading.RLock()

estado_lock = threading.RLock()

ultimo_sinal = {}

operacao_em_andamento = False


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não configurado."
        )

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

        if not resposta.ok:

            logger.warning(
                "Telegram HTTP %s",
                resposta.status_code
            )

        return resposta.ok

    except Exception:

        logger.exception(
            "Erro enviando Telegram."
        )

        return False


# ============================================================
# IQ OPTION
# ============================================================

def conectar_iq():

    global api

    if IQ_Option is None:

        logger.error(
            "Biblioteca iqoptionapi não carregada."
        )

        return False

    if not IQ_EMAIL or not IQ_PASSWORD:

        logger.warning(
            "IQ_EMAIL/IQ_PASSWORD não configurados."
        )

        return False

    try:

        with api_lock:

            api = IQ_Option(
                IQ_EMAIL,
                IQ_PASSWORD
            )

            ok, motivo = api.connect()

            if not ok:

                logger.error(
                    "Falha IQ Option: %s",
                    motivo
                )

                api = None

                return False

            # Segurança:
            # esta versão força PRACTICE
            api.change_balance(
                "PRACTICE"
            )

        logger.info(
            "IQ Option conectada em PRACTICE."
        )

        enviar_telegram(
            "🟢 <b>ROBÔ CONECTADO</b>\n\n"
            "Conta: PRACTICE\n"
            "Mercados: Normal + OTC\n"
            "Tempos: M1 / M5 / M15\n"
            f"Score mínimo: {SCORE_MIN}"
        )

        return True

    except Exception:

        logger.exception(
            "Erro conectando IQ Option."
        )

        api = None

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
# INDICADORES
# ============================================================

def media(valores):

    if not valores:
        return 0

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
            * (1 - multiplicador)
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
            max(diferenca, 0)
        )

        perdas.append(
            max(-diferenca, 0)
        )

    ganho_medio = media(
        ganhos
    )

    perda_media = media(
        perdas
    )

    if perda_media == 0:

        return 100.0

    rs = (
        ganho_medio
        / perda_media
    )

    return (
        100
        -
        (
            100
            / (1 + rs)
        )
    )


def desvio_padrao(valores):

    if not valores:
        return 0

    m = media(valores)

    variancia = media([
        (x - m) ** 2
        for x in valores
    ])

    return math.sqrt(
        variancia
    )


def bollinger(
    valores,
    periodo=20
):

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
        -
        2 * desvio
    )

    superior = (
        centro
        +
        2 * desvio
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

        return 0

    return (
        e12
        - e26
    )


# ============================================================
# ESTRATÉGIA
# ============================================================

def analisar_candles(candles):

    if not candles:
        return None

    if len(candles) < 60:
        return None

    candles = sorted(
        candles,
        key=lambda x: x.get(
            "from",
            0
        )
    )

    opens = [
        float(c["open"])
        for c in candles
    ]

    closes = [
        float(c["close"])
        for c in candles
    ]

    highs = [
        float(c["max"])
        for c in candles
    ]

    lows = [
        float(c["min"])
        for c in candles
    ]


    score_call = 0

    score_put = 0

    motivos_call = []

    motivos_put = []


    # --------------------------------------------------------
    # EMA 20 / 50
    # --------------------------------------------------------

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
                "EMA tendência alta"
            )

        elif ema20 < ema50:

            score_put += 20

            motivos_put.append(
                "EMA tendência baixa"
            )


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    valor_rsi = rsi(
        closes,
        14
    )

    if valor_rsi <= 35:

        score_call += 15

        motivos_call.append(
            f"RSI {valor_rsi:.1f}"
        )

    elif valor_rsi >= 65:

        score_put += 15

        motivos_put.append(
            f"RSI {valor_rsi:.1f}"
        )


    # --------------------------------------------------------
    # BOLLINGER
    # --------------------------------------------------------

    inferior, centro, superior = (
        bollinger(
            closes,
            20
        )
    )

    ultimo = closes[-1]

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


    # --------------------------------------------------------
    # PRICE ACTION
    # --------------------------------------------------------

    abertura = opens[-1]

    fechamento = closes[-1]

    maxima = highs[-1]

    minima = lows[-1]

    corpo = abs(
        fechamento
        - abertura
    )

    amplitude = max(
        maxima - minima,
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
            "Price Action alta"
        )

    elif (
        fechamento < abertura
        and forca_corpo >= 0.55
    ):

        score_put += 15

        motivos_put.append(
            "Price Action baixa"
        )


    # --------------------------------------------------------
    # SUPORTE / RESISTÊNCIA
    # --------------------------------------------------------

    resistencia = max(
        highs[-11:-1]
    )

    suporte = min(
        lows[-11:-1]
    )

    tolerancia = (
        max(
            resistencia - suporte,
            0.00000001
        )
        * 0.10
    )

    if (
        ultimo
        <= suporte + tolerancia
    ):

        score_call += 10

        motivos_call.append(
            "Região de suporte"
        )

    if (
        ultimo
        >= resistencia - tolerancia
    ):

        score_put += 10

        motivos_put.append(
            "Região de resistência"
        )


    # --------------------------------------------------------
    # ROMPIMENTO
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    if (
        closes[-1]
        > closes[-2]
        > closes[-3]
    ):

        score_call += 5

        motivos_call.append(
            "Momentum alta"
        )

    elif (
        closes[-1]
        < closes[-2]
        < closes[-3]
    ):

        score_put += 5

        motivos_put.append(
            "Momentum baixa"
        )


    score_call = min(
        score_call,
        100
    )

    score_put = min(
        score_put,
        100
    )


    # --------------------------------------------------------
    # DECISÃO
    # --------------------------------------------------------

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
            "direcao": "call",
            "score": score_call,
            "score_contra": score_put,
            "rsi": round(
                valor_rsi,
                2
            ),
            "motivos": motivos_call
        }


    if (
        score_put >= SCORE_MIN
        and score_put > score_call
    ):

        return {
            "direcao": "put",
            "score": score_put,
            "score_contra": score_call,
            "rsi": round(
                valor_rsi,
                2
            ),
            "motivos": motivos_put
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

    if gerenciamento[
        "em_recuperacao"
    ]:

        gerenciamento[
            "divida"
        ] = max(
            0.0,
            gerenciamento[
                "divida"
            ]
            - lucro
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

        else:

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
    ] += valor_perdido

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


    # Ciclo completo perdido
    gerenciamento[
        "em_recuperacao"
    ] = True

    gerenciamento[
        "gale_atual"
    ] = 0

    if (
        gerenciamento[
            "gales_permitidos"
        ]
        < MAX_GALES
    ):

        gerenciamento[
            "gales_permitidos"
        ] += 1


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
# ATIVOS
# ============================================================

def buscar_ativos_abertos():

    if not garantir_conexao():

        return []

    encontrados = set()

    try:

        with api_lock:

            dados = (
                api.get_all_open_time()
            )

        for mercado in (
            "digital",
            "turbo",
            "binary"
        ):

            tabela = (
                dados.get(
                    mercado,
                    {}
                )
                or {}
            )

            for ativo, info in (
                tabela.items()
            ):

                if not info.get(
                    "open"
                ):

                    continue

                ativo_base = (
                    ativo.replace(
                        "-OTC",
                        ""
                    )
                )

                if (
                    ativo_base
                    in ATIVOS_BASE
                ):

                    encontrados.add(
                        ativo
                    )

    except Exception:

        logger.exception(
            "Erro buscando ativos."
        )

    return sorted(
        encontrados
    )


# ============================================================
# SINAIS
# ============================================================

def pode_enviar(
    ativo,
    timeframe
):

    chave = (
        f"{ativo}:M{timeframe}"
    )

    agora = int(
        time.time()
    )

    limite = (
        timeframe
        * 60
    )

    anterior = ultimo_sinal.get(
        chave,
        0
    )

    if (
        agora - anterior
        < limite
    ):

        return False

    ultimo_sinal[
        chave
    ] = agora

    return True


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

    motivos = ", ".join(
        sinal[
            "motivos"
        ][:5]
    )

    return (
        "📊 <b>NOVO SINAL</b>\n\n"
        f"💱 Ativo: <b>{ativo}</b>\n"
        f"🌐 Mercado: <b>{mercado}</b>\n"
        f"⏱ Tempo: <b>M{timeframe}</b>\n"
        f"🎯 Direção: <b>{sinal['direcao'].upper()}</b>\n"
        f"🔥 Score: <b>{sinal['score']}/100</b>\n"
        f"↔ Contra: {sinal['score_contra']}/100\n"
        f"📈 RSI: {sinal['rsi']}\n\n"
        f"💰 Próxima entrada: "
        f"R$ {valor_entrada():.2f}\n"
        f"🔄 Gale: "
        f"{gerenciamento['gale_atual']}/"
        f"{gerenciamento['gales_permitidos']}\n"
        f"♻️ Recuperação: "
        f"{'SIM' if gerenciamento['em_recuperacao'] else 'NÃO'}\n\n"
        f"🔎 {motivos}"
    )


# ============================================================
# RESULTADO DIGITAL
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
            timeframe * 60
            +
            90
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

            time.sleep(2)


        if resultado is None:

            enviar_telegram(
                "⚠️ Não consegui confirmar "
                "o resultado da operação."
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
                ] += resultado

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

                status = "✅ WIN"


            elif resultado < 0:

                stats[
                    "losses"
                ] += 1

                stats[
                    "sequencia_loss_atual"
                ] += 1

                stats[
                    "prejuizo"
                ] += abs(
                    resultado
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

                status = "❌ LOSS"


            else:

                stats[
                    "draws"
                ] += 1

                status = "⚪ DRAW"


        total = (
            stats["wins"]
            +
            stats["losses"]
        )

        assertividade = (
            (
                stats["wins"]
                / total
            )
            * 100
            if total
            else 0
        )


        enviar_telegram(
            f"{status}\n\n"
            f"💱 {ativo}\n"
            f"⏱ M{timeframe}\n"
            f"💵 Entrada: R$ {valor:.2f}\n"
            f"💰 Resultado: R$ {resultado:.2f}\n\n"
            f"✅ Wins: {stats['wins']}\n"
            f"❌ Losses: {stats['losses']}\n"
            f"🎯 Assertividade: "
            f"{assertividade:.2f}%\n"
            f"📉 Maior sequência LOSS: "
            f"{stats['maior_sequencia_loss']}\n\n"
            f"♻️ Dívida recuperação: "
            f"R$ {gerenciamento['divida']:.2f}\n"
            f"➡️ Próxima entrada: "
            f"R$ {valor_entrada():.2f}"
        )


    except Exception:

        logger.exception(
            "Erro acompanhando resultado."
        )

    finally:

        operacao_em_andamento = False


# ============================================================
# EXECUÇÃO
# ============================================================

def executar_operacao(
    ativo,
    timeframe,
    direcao
):

    global operacao_em_andamento


    # M15 permanece sinal/análise.
    if timeframe not in (
        1,
        5
    ):

        return


    if not EXECUTAR_ORDENS:

        return


    # Proteção desta versão.
    # Nunca envia ordem REAL.
    if IQ_BALANCE == "REAL":

        logger.error(
            "Execução REAL bloqueada."
        )

        return


    with estado_lock:

        if operacao_em_andamento:

            return

        operacao_em_andamento = True


    try:

        if not garantir_conexao():

            operacao_em_andamento = False

            return


        valor = valor_entrada()


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

            operacao_em_andamento = False

            logger.warning(
                "Operação recusada."
            )

            return


        enviar_telegram(
            "🟡 <b>ORDEM PRACTICE ABERTA</b>\n\n"
            f"Ativo: {ativo}\n"
            f"Tempo: M{timeframe}\n"
            f"Direção: {direcao.upper()}\n"
            f"Valor: R$ {valor:.2f}"
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

        operacao_em_andamento = False

        stats[
            "erros"
        ] += 1

        logger.exception(
            "Erro executando operação."
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

        with api_lock:

            candles = api.get_candles(
                ativo,
                segundos,
                100,
                time.time()
            )


        sinal = analisar_candles(
            candles
        )

        if not sinal:

            return


        if not pode_enviar(
            ativo,
            timeframe
        ):

            return


        with estado_lock:

            stats[
                "sinais"
            ] += 1

            stats[
                sinal["direcao"]
            ] += 1


        texto = mensagem_sinal(
            ativo,
            timeframe,
            sinal
        )

        enviar_telegram(
            texto
        )


        logger.info(
            "%s | M%s | %s | score=%s",
            ativo,
            timeframe,
            sinal[
                "direcao"
            ],
            sinal[
                "score"
            ]
        )


        executar_operacao(
            ativo,
            timeframe,
            sinal[
                "direcao"
            ]
        )


    except Exception:

        stats[
            "erros"
        ] += 1

        logger.exception(
            "Erro analisando %s M%s",
            ativo,
            timeframe
        )


def loop_scanner():

    time.sleep(5)

    while True:

        try:

            if not garantir_conexao():

                time.sleep(30)

                continue


            ativos = (
                buscar_ativos_abertos()
            )


            logger.info(
                "Ativos encontrados: %s",
                len(ativos)
            )


            for ativo in ativos:

                for timeframe, segundos in (
                    TIMEFRAMES.items()
                ):

                    analisar_ativo(
                        ativo,
                        timeframe,
                        segundos
                    )

                    time.sleep(
                        0.5
                    )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception:

            logger.exception(
                "Erro no scanner."
            )

            time.sleep(20)


# ============================================================
# FLASK / RENDER
# ============================================================

@app.route("/")
def home():

    total = (
        stats["wins"]
        +
        stats["losses"]
    )

    taxa = (
        round(
            stats["wins"]
            / total
            * 100,
            2
        )
        if total
        else 0
    )

    return jsonify({
        "robo": "IQ Digital/Binario",
        "status": "online",

        "conta": "PRACTICE",

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

        "entrada":
            valor_entrada(),

        "gerenciamento":
            gerenciamento,

        "assertividade":
            taxa,

        "stats":
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
        "status": "ok",
        "versao": "IQ-DIGITAL-V1",
        "iq_conectada": conectado,
        "execucao": (
            "PRACTICE"
            if EXECUTAR_ORDENS
            else "SINAIS"
        )
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
