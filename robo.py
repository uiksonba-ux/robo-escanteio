import os
import time
import math
import threading
import logging
import requests

from flask import Flask, jsonify
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# IQ OPTION - 3 FRENTES - PRACTICE
# ============================================================
#
# BANCA 1 = M1
# BANCA 2 = M5
# BANCA 3 = M15
#
# Entrada = $2
# G1      = $4
# G2      = $8
#
# A conta REAL não é utilizada.
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-3-frentes")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

EXECUTAR_ORDENS = (
    os.getenv("EXECUTAR_ORDENS", "false")
    .strip()
    .lower()
    in ("1", "true", "yes", "sim", "on")
)

ENTRADA_BASE = float(
    os.getenv("ENTRADA_BASE", "2")
)

SCORE_MIN = int(
    os.getenv("SCORE_MIN", "65")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "20")
)

INTERVALO_ENTRE_ATIVOS = float(
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.8")
)

# Nunca permitir REAL
CONTA = "PRACTICE"


# ============================================================
# FRENTES
# ============================================================

FRENTES = {

    "BANCA_1": {
        "nome": "BANCA 1",
        "timeframe": 1,
        "segundos": 60,
    },

    "BANCA_2": {
        "nome": "BANCA 2",
        "timeframe": 5,
        "segundos": 300,
    },

    "BANCA_3": {
        "nome": "BANCA 3",
        "timeframe": 15,
        "segundos": 900,
    }
}


for frente in FRENTES.values():

    frente.update({

        "ocupada": False,

        "ativo": None,

        "direcao": None,

        "score": None,

        "nivel": 0,

        "order_id": None,

        "wins": 0,

        "losses": 0,

        "win_direto": 0,

        "win_g1": 0,

        "win_g2": 0,

        "ultimo_sinal": {},
    })


# ============================================================
# ATIVOS
# ============================================================

ATIVOS_BASE = [

    "EURUSD",
    "EURGBP",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",

    "EURJPY",
    "GBPJPY",
    "USDCHF",
    "AUDCAD",
    "AUDJPY",

    "EURCAD",
    "USDCAD",
    "NZDUSD",
]


def ativos_candidatos():

    resultado = []

    for ativo in ATIVOS_BASE:

        resultado.append(ativo)

        resultado.append(
            ativo + "-OTC"
        )

    return resultado


# ============================================================
# ESTADO GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()
estado_lock = threading.RLock()

stats = {

    "sinais_encontrados": 0,

    "ordens_aceitas": 0,

    "ordens_recusadas": 0,

    "wins": 0,

    "losses": 0,

    "erros": 0,
}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False

    try:

        resposta = requests.post(

            (
                "https://api.telegram.org/bot"
                + TELEGRAM_TOKEN
                + "/sendMessage"
            ),

            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },

            timeout=15
        )

        return resposta.ok

    except Exception:

        log.exception(
            "Erro Telegram"
        )

        return False


# ============================================================
# TAXAS
# ============================================================

def taxa_frente(frente):

    total = (
        frente["wins"]
        +
        frente["losses"]
    )

    if total == 0:
        return 0.0

    return round(
        frente["wins"]
        / total
        * 100,
        2
    )


def taxa_geral():

    total = (
        stats["wins"]
        +
        stats["losses"]
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
# CONEXÃO IQ
# ============================================================

def conectar():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        log.error(
            "IQ_EMAIL ou IQ_PASSWORD não configurado."
        )

        return False

    try:

        log.info(
            "Conectando à IQ Option..."
        )

        nova = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        ok, motivo = nova.connect()

        log.info(
            "Resposta connect: ok=%s motivo=%r",
            ok,
            motivo
        )

        if not ok:
            return False

        # ====================================================
        # TRAVA PRACTICE
        # ====================================================

        nova.change_balance(
            "PRACTICE"
        )

        time.sleep(1)

        with api_lock:
            api = nova

        try:

            saldo = nova.get_balance()

        except Exception:

            saldo = None

        log.info(
            "IQ CONECTADA | PRACTICE | saldo=%r",
            saldo
        )

        return True

    except Exception:

        stats["erros"] += 1

        log.exception(
            "Erro conectar IQ"
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

    return conectar()


# ============================================================
# REFORÇAR PRACTICE
# ============================================================

def reforcar_practice():

    if not garantir_conexao():
        return False

    try:

        with api_lock:

            api.change_balance(
                "PRACTICE"
            )

        return True

    except Exception:

        log.exception(
            "Falha ao selecionar PRACTICE"
        )

        return False


# ============================================================
# MATEMÁTICA
# ============================================================

def media(valores):

    if not valores:
        return 0.0

    return sum(valores) / len(valores)


def ema(valores, periodo):

    if len(valores) < periodo:
        return None

    k = 2.0 / (periodo + 1)

    valor = media(
        valores[:periodo]
    )

    for preco in valores[periodo:]:

        valor = (
            preco * k
            +
            valor * (1 - k)
        )

    return valor


def rsi(valores, periodo=14):

    if len(valores) <= periodo:
        return 50.0

    dados = valores[
        -(periodo + 1):
    ]

    ganhos = []
    perdas = []

    for i in range(
        len(dados) - 1
    ):

        diferenca = (
            dados[i + 1]
            -
            dados[i]
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

    ganho = media(ganhos)
    perda = media(perdas)

    if perda == 0:

        if ganho > 0:
            return 100.0

        return 50.0

    rs = ganho / perda

    return (
        100
        -
        100 / (1 + rs)
    )


def desvio(valores):

    if not valores:
        return 0.0

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

    if len(valores) < periodo:

        return (
            None,
            None,
            None
        )

    dados = valores[
        -periodo:
    ]

    m = media(dados)
    d = desvio(dados)

    return (
        m - 2 * d,
        m,
        m + 2 * d
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

    if e12 is None or e26 is None:
        return 0.0

    return e12 - e26


# ============================================================
# CANDLES
# ============================================================

def normalizar(candles):

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
# MOTOR DE SCORE
# ============================================================

def analisar(candles):

    candles = normalizar(
        candles
    )

    if len(candles) < 60:
        return None

    opens = [
        c["open"]
        for c in candles
    ]

    closes = [
        c["close"]
        for c in candles
    ]

    highs = [
        c["max"]
        for c in candles
    ]

    lows = [
        c["min"]
        for c in candles
    ]

    call = 0
    put = 0


    # ========================================================
    # EMA
    # ========================================================

    e20 = ema(
        closes,
        20
    )

    e50 = ema(
        closes,
        50
    )

    if e20 is not None and e50 is not None:

        if e20 > e50:
            call += 20

        elif e20 < e50:
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
            closes,
            20
        )
    )

    preco = closes[-1]

    if inferior is not None:

        if preco <= inferior:
            call += 10

        elif preco >= superior:
            put += 10


    # ========================================================
    # PRICE ACTION
    # ========================================================

    amplitude = max(
        highs[-1]
        -
        lows[-1],
        0.00000001
    )

    corpo = abs(
        closes[-1]
        -
        opens[-1]
    )

    forca = (
        corpo
        /
        amplitude
    )

    if (
        closes[-1] > opens[-1]
        and forca >= 0.55
    ):

        call += 15

    elif (
        closes[-1] < opens[-1]
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
        -
        suporte,
        0.00000001
    )

    tolerancia = (
        faixa * 0.10
    )

    if (
        preco
        <=
        suporte + tolerancia
    ):
        call += 10

    if (
        preco
        >=
        resistencia - tolerancia
    ):
        put += 10


    # ========================================================
    # BREAKOUT
    # ========================================================

    if preco > resistencia:
        call += 15

    elif preco < suporte:
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
        >
        closes[-2]
        >
        closes[-3]
    ):

        call += 5

    elif (
        closes[-1]
        <
        closes[-2]
        <
        closes[-3]
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


    # ========================================================
    # DIFERENÇA MÍNIMA
    # ========================================================

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
# VALORES
# ============================================================

def valor_nivel(nivel):

    # nível 0 = 2
    # nível 1 = 4
    # nível 2 = 8

    return round(
        ENTRADA_BASE
        *
        (
            2 ** nivel
        ),
        2
    )


# ============================================================
# EXECUTAR ORDEM
# ============================================================

def comprar_digital(
    ativo,
    valor,
    direcao,
    timeframe
):

    if not EXECUTAR_ORDENS:

        return {
            "ok": False,
            "order_id": None,
            "motivo": "EXECUTAR_ORDENS=false"
        }


    if not reforcar_practice():

        return {
            "ok": False,
            "order_id": None,
            "motivo": "sem conexão PRACTICE"
        }


    try:

        log.info(
            "TENTANDO ORDEM | ativo=%s | valor=%.2f | direcao=%s | exp=%s",
            ativo,
            valor,
            direcao,
            timeframe
        )


        # ====================================================
        # IMPORTANTE
        #
        # Guardamos a resposta BRUTA para descobrir exatamente
        # o que a biblioteca devolve quando recusa.
        # ====================================================

        with api_lock:

            resposta = (
                api.buy_digital_spot_v2(
                    ativo,
                    valor,
                    direcao,
                    timeframe
                )
            )


        log.info(
            "RESPOSTA buy_digital_spot_v2 | ativo=%s | M%s | resposta=%r",
            ativo,
            timeframe,
            resposta
        )


        status = False
        order_id = None
        motivo = None


        if (
            isinstance(
                resposta,
                (tuple, list)
            )
            and len(resposta) >= 2
        ):

            status = bool(
                resposta[0]
            )

            order_id = resposta[1]


            if not status:

                motivo = repr(
                    resposta[1]
                )


        elif resposta:

            # Proteção para eventual mudança
            # no formato da biblioteca.
            status = True

            order_id = resposta


        if (
            status
            and order_id
        ):

            stats[
                "ordens_aceitas"
            ] += 1


            log.info(
                "ORDEM ACEITA | %s | M%s | %s | $%.2f | id=%s",
                ativo,
                timeframe,
                direcao,
                valor,
                order_id
            )


            return {
                "ok": True,
                "order_id": order_id,
                "motivo": None
            }


        stats[
            "ordens_recusadas"
        ] += 1


        log.warning(
            "ORDEM RECUSADA | %s | M%s | resposta=%r",
            ativo,
            timeframe,
            resposta
        )


        return {
            "ok": False,
            "order_id": None,
            "motivo": (
                motivo
                or repr(resposta)
            )
        }


    except Exception as erro:

        stats[
            "erros"
        ] += 1


        log.exception(
            "EXCEÇÃO AO CRIAR ORDEM | %s | M%s",
            ativo,
            timeframe
        )


        return {
            "ok": False,
            "order_id": None,
            "motivo": (
                f"{type(erro).__name__}: "
                f"{erro}"
            )
        }


# ============================================================
# ESPERAR RESULTADO
# ============================================================

def esperar_resultado(
    order_id,
    timeframe
):

    limite = (
        time.time()
        +
        timeframe * 60
        +
        180
    )


    while time.time() < limite:

        try:

            if not garantir_conexao():

                time.sleep(3)
                continue


            with api_lock:

                resposta = (
                    api.check_win_digital_v2(
                        order_id
                    )
                )


            if (
                isinstance(
                    resposta,
                    (tuple, list)
                )
                and len(resposta) >= 2
            ):

                status = resposta[0]

                lucro = resposta[1]


                if status:

                    try:

                        lucro = float(
                            lucro
                        )

                    except Exception:

                        lucro = 0.0


                    log.info(
                        "RESULTADO IQ | id=%s | lucro=%s",
                        order_id,
                        lucro
                    )


                    if lucro > 0:

                        return (
                            "win",
                            lucro
                        )


                    if lucro < 0:

                        return (
                            "loss",
                            lucro
                        )


                    return (
                        "draw",
                        lucro
                    )


        except Exception:

            log.exception(
                "Erro consultando resultado | id=%s",
                order_id
            )


        time.sleep(2)


    return (
        "erro",
        0.0
    )


# ============================================================
# TELEGRAM - SINAL CONFIRMADO
# ============================================================

def mensagem_sinal(
    frente,
    ativo
):

    mercado = (
        "OTC"
        if "-OTC" in ativo
        else "NORMAL"
    )


    telegram(

        f"📊 <b>{frente['nome']} - NOVO SINAL</b>\n\n"

        f"💱 <b>{ativo}</b>\n"

        f"🌐 {mercado}\n"

        f"⏱ M{frente['timeframe']}\n"

        f"🎯 <b>{frente['direcao'].upper()}</b>\n"

        f"🔥 Score: "
        f"<b>{frente['score']}/100</b>\n\n"

        f"💰 ${valor_nivel(0):.2f} → "
        f"G1 ${valor_nivel(1):.2f} → "
        f"G2 ${valor_nivel(2):.2f}\n\n"

        f"🎯 Acerto da banca: "
        f"<b>{taxa_frente(frente):.2f}%</b>"
    )


# ============================================================
# FINALIZAR
# ============================================================

def liberar_frente(frente):

    frente[
        "ocupada"
    ] = False

    frente[
        "ativo"
    ] = None

    frente[
        "direcao"
    ] = None

    frente[
        "score"
    ] = None

    frente[
        "nivel"
    ] = 0

    frente[
        "order_id"
    ] = None


def registrar_final(
    frente,
    ganhou,
    nivel
):

    if ganhou:

        frente[
            "wins"
        ] += 1

        stats[
            "wins"
        ] += 1


        if nivel == 0:

            frente[
                "win_direto"
            ] += 1


        elif nivel == 1:

            frente[
                "win_g1"
            ] += 1


        elif nivel == 2:

            frente[
                "win_g2"
            ] += 1


        if nivel == 0:

            titulo = (
                "✅ <b>WIN</b>"
            )

        else:

            titulo = (
                f"✅ <b>WIN G{nivel}</b>"
            )


    else:

        frente[
            "losses"
        ] += 1

        stats[
            "losses"
        ] += 1

        titulo = (
            "❌ <b>LOSS</b>"
        )


    telegram(

        f"{titulo}\n\n"

        f"🏦 {frente['nome']}\n"

        f"💱 {frente['ativo']} | "
        f"M{frente['timeframe']} | "
        f"{frente['direcao'].upper()}\n\n"

        f"📊 {frente['wins']} WIN / "
        f"{frente['losses']} LOSS\n"

        f"🎯 Acerto: "
        f"<b>{taxa_frente(frente):.2f}%</b>"
    )


    liberar_frente(
        frente
    )


# ============================================================
# OPERAR SINAL
# ============================================================

def operar_sinal(
    chave_frente,
    ativo,
    sinal
):

    frente = FRENTES[
        chave_frente
    ]


    try:

        # ====================================================
        # PRIMEIRA ENTRADA
        # ====================================================

        frente[
            "nivel"
        ] = 0


        primeira = comprar_digital(

            ativo,

            valor_nivel(0),

            sinal["direcao"],

            frente["timeframe"]
        )


        # ====================================================
        # SE A ORDEM NÃO FOI ACEITA:
        #
        # NÃO envia "NOVO SINAL".
        # ====================================================

        if not primeira["ok"]:

            log.warning(

                "%s | primeira ordem recusada | %s | motivo=%s",

                frente["nome"],

                ativo,

                primeira["motivo"]
            )


            telegram(

                f"⚠️ <b>{frente['nome']}</b>\n"

                f"Entrada não aceita pela plataforma.\n"

                f"💱 {ativo} | "
                f"M{frente['timeframe']}\n"

                f"🔎 Verificar log do Render."
            )


            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = primeira[
            "order_id"
        ]


        # Agora sim o sinal representa
        # uma entrada realmente aceita.
        mensagem_sinal(
            frente,
            ativo
        )


        # ====================================================
        # RESULTADO ENTRADA
        # ====================================================

        resultado, lucro = (
            esperar_resultado(

                primeira[
                    "order_id"
                ],

                frente[
                    "timeframe"
                ]
            )
        )


        if resultado == "win":

            registrar_final(
                frente,
                True,
                0
            )

            return


        if resultado == "draw":

            log.info(
                "%s | DRAW entrada",
                frente["nome"]
            )

            liberar_frente(
                frente
            )

            return


        if resultado == "erro":

            telegram(

                f"⚠️ <b>{frente['nome']}</b>\n"

                f"Não foi possível confirmar "
                f"o resultado da entrada.\n"

                f"💱 {ativo}"
            )

            liberar_frente(
                frente
            )

            return


        # ====================================================
        # G1
        # ====================================================

        frente[
            "nivel"
        ] = 1


        g1 = comprar_digital(

            ativo,

            valor_nivel(1),

            sinal["direcao"],

            frente["timeframe"]
        )


        if not g1["ok"]:

            telegram(

                f"⚠️ <b>{frente['nome']}</b>\n"

                f"G1 não foi aceito.\n"

                f"💱 {ativo}"
            )

            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = g1[
            "order_id"
        ]


        resultado, lucro = (
            esperar_resultado(

                g1[
                    "order_id"
                ],

                frente[
                    "timeframe"
                ]
            )
        )


        if resultado == "win":

            registrar_final(
                frente,
                True,
                1
            )

            return


        if resultado == "draw":

            liberar_frente(
                frente
            )

            return


        if resultado == "erro":

            telegram(

                f"⚠️ <b>{frente['nome']}</b>\n"

                f"Resultado do G1 não confirmado."
            )

            liberar_frente(
                frente
            )

            return


        # ====================================================
        # G2
        # ====================================================

        frente[
            "nivel"
        ] = 2


        g2 = comprar_digital(

            ativo,

            valor_nivel(2),

            sinal["direcao"],

            frente["timeframe"]
        )


        if not g2["ok"]:

            telegram(

                f"⚠️ <b>{frente['nome']}</b>\n"

                f"G2 não foi aceito.\n"

                f"💱 {ativo}"
            )

            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = g2[
            "order_id"
        ]


        resultado, lucro = (
            esperar_resultado(

                g2[
                    "order_id"
                ],

                frente[
                    "timeframe"
                ]
            )
        )


        if resultado == "win":

            registrar_final(
                frente,
                True,
                2
            )

            return


        if resultado == "loss":

            # Só agora é LOSS completo.
            registrar_final(
                frente,
                False,
                2
            )

            return


        # DRAW ou erro no G2
        telegram(

            f"⚠️ <b>{frente['nome']}</b>\n"

            f"Resultado final não confirmado.\n"

            f"💱 {ativo}"
        )

        liberar_frente(
            frente
        )


    except Exception:

        stats[
            "erros"
        ] += 1

        log.exception(
            "Erro em operar_sinal | %s",
            chave_frente
        )

        liberar_frente(
            frente
        )


# ============================================================
# CRIAR CANDIDATO
# ============================================================

def iniciar_operacao(
    chave_frente,
    ativo,
    sinal
):

    frente = FRENTES[
        chave_frente
    ]


    with estado_lock:

        if frente[
            "ocupada"
        ]:
            return


        agora = time.time()


        ultimo = (
            frente[
                "ultimo_sinal"
            ].get(
                ativo,
                0
            )
        )


        # Não repete imediatamente.
        if (
            agora - ultimo
            <
            frente["segundos"]
        ):
            return


        frente[
            "ultimo_sinal"
        ][
            ativo
        ] = agora


        frente[
            "ocupada"
        ] = True

        frente[
            "ativo"
        ] = ativo

        frente[
            "direcao"
        ] = sinal[
            "direcao"
        ]

        frente[
            "score"
        ] = sinal[
            "score"
        ]


        stats[
            "sinais_encontrados"
        ] += 1


    threading.Thread(

        target=operar_sinal,

        args=(
            chave_frente,
            ativo,
            sinal
        ),

        daemon=True

    ).start()


# ============================================================
# SCANNER DA FRENTE
# ============================================================

def analisar_frente(
    chave_frente
):

    frente = FRENTES[
        chave_frente
    ]


    if frente[
        "ocupada"
    ]:
        return


    for ativo in ativos_candidatos():

        if frente[
            "ocupada"
        ]:
            return


        try:

            if not garantir_conexao():
                return


            with api_lock:

                candles = (
                    api.get_candles(

                        ativo,

                        frente[
                            "segundos"
                        ],

                        100,

                        time.time()
                    )
                )


            candles = normalizar(
                candles
            )


            if len(candles) < 60:
                continue


            sinal = analisar(
                candles
            )


            if sinal:

                log.info(

                    "CANDIDATO | %s | %s | M%s | %s | score=%s",

                    frente["nome"],

                    ativo,

                    frente[
                        "timeframe"
                    ],

                    sinal[
                        "direcao"
                    ],

                    sinal[
                        "score"
                    ]
                )


                iniciar_operacao(
                    chave_frente,
                    ativo,
                    sinal
                )

                return


        except Exception as erro:

            log.debug(
                "Candle indisponível | %s | %s",
                ativo,
                erro
            )


        time.sleep(
            INTERVALO_ENTRE_ATIVOS
        )


# ============================================================
# LOOP DE CADA BANCA
# ============================================================

def loop_frente(
    chave_frente
):

    atrasos = {

        "BANCA_1": 5,

        "BANCA_2": 10,

        "BANCA_3": 15,
    }


    time.sleep(
        atrasos[
            chave_frente
        ]
    )


    while True:

        try:

            analisar_frente(
                chave_frente
            )

        except Exception:

            stats[
                "erros"
            ] += 1

            log.exception(
                "Erro scanner %s",
                chave_frente
            )


        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    frentes = {}


    for chave, frente in (
        FRENTES.items()
    ):

        frentes[
            chave
        ] = {

            "timeframe":
                f"M{frente['timeframe']}",

            "ocupada":
                frente["ocupada"],

            "ativo":
                frente["ativo"],

            "direcao":
                frente["direcao"],

            "nivel":
                frente["nivel"],

            "wins":
                frente["wins"],

            "losses":
                frente["losses"],

            "win_direto":
                frente["win_direto"],

            "win_g1":
                frente["win_g1"],

            "win_g2":
                frente["win_g2"],

            "assertividade":
                taxa_frente(
                    frente
                ),
        }


    saldo = None


    try:

        if garantir_conexao():

            with api_lock:

                # Reforça PRACTICE antes
                # de mostrar saldo.
                api.change_balance(
                    "PRACTICE"
                )

                saldo = api.get_balance()

    except Exception:
        pass


    return jsonify({

        "robo":
            "IQ 3 FRENTES V2",

        "status":
            "online",

        "conta":
            "PRACTICE",

        "real_bloqueada":
            True,

        "saldo_practice":
            saldo,

        "execucao_automatica":
            EXECUTAR_ORDENS,

        "score_minimo":
            SCORE_MIN,

        "gestao": {
            "entrada":
                valor_nivel(0),

            "g1":
                valor_nivel(1),

            "g2":
                valor_nivel(2),

            "exposicao_maxima_por_frente":
                round(
                    valor_nivel(0)
                    +
                    valor_nivel(1)
                    +
                    valor_nivel(2),
                    2
                )
        },

        "estatisticas":
            stats,

        "assertividade_geral":
            taxa_geral(),

        "frentes":
            frentes
    })


# ============================================================
# HEALTH
# ============================================================

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
            "IQ-3-FRENTES-V2",

        "iq_conectada":
            conectado,

        "conta":
            "PRACTICE",

        "real_bloqueada":
            True,

        "execucao":
            EXECUTAR_ORDENS
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


        for chave in FRENTES:

            threading.Thread(

                target=loop_frente,

                args=(
                    chave,
                ),

                daemon=True

            ).start()


iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
