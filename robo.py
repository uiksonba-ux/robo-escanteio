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
# V3 - VALIDAÇÃO DIGITAL + ATIVOS DIFERENTES
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-bot")


# ============================================================
# CONFIGURAÇÃO
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
    in ("true", "1", "yes", "sim", "on")
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

INTERVALO_ATIVOS = float(
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.8")
)

# Segurança
CONTA_PERMITIDA = "PRACTICE"


# ============================================================
# BANCAS
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
    },
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

        "ordens_recusadas": 0,

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


def lista_ativos():

    resultado = []

    for ativo in ATIVOS_BASE:

        resultado.append(ativo)
        resultado.append(ativo + "-OTC")

    return resultado


# ============================================================
# ESTADO
# ============================================================

api = None

api_lock = threading.RLock()
estado_lock = threading.RLock()

ativos_em_uso = set()

# Instrumentos que deram erro recentemente.
#
# Evita ficar tentando o mesmo instrumento inválido
# a cada 20 segundos.
instrumentos_bloqueados = {}

TEMPO_BLOQUEIO_INSTRUMENTO = 300


stats = {

    "sinais": 0,

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
        return

    try:

        requests.post(

            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",

            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },

            timeout=15
        )

    except Exception:

        log.exception(
            "Erro ao enviar Telegram"
        )


# ============================================================
# ESTATÍSTICAS
# ============================================================

def taxa(wins, losses):

    total = wins + losses

    if total == 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def taxa_frente(frente):

    return taxa(
        frente["wins"],
        frente["losses"]
    )


# ============================================================
# CONEXÃO
# ============================================================

def conectar():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        log.error(
            "IQ_EMAIL/IQ_PASSWORD ausentes."
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
            "CONNECT | ok=%r | motivo=%r",
            ok,
            motivo
        )

        if not ok:
            return False

        # ====================================================
        # TRAVA ABSOLUTA PRACTICE
        # ====================================================

        nova.change_balance(
            CONTA_PERMITIDA
        )

        time.sleep(1)

        try:

            saldo = nova.get_balance()

        except Exception:

            saldo = None

        with api_lock:

            api = nova

        log.info(
            "IQ CONECTADA | PRACTICE | saldo=%r",
            saldo
        )

        return True

    except Exception:

        stats["erros"] += 1

        log.exception(
            "Erro na conexão"
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


def garantir_practice():

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
            "Não foi possível selecionar PRACTICE"
        )

        return False


# ============================================================
# ATIVO EM USO
# ============================================================

def ativo_esta_em_uso(ativo):

    with estado_lock:

        return ativo in ativos_em_uso


def reservar_ativo(ativo):

    with estado_lock:

        if ativo in ativos_em_uso:
            return False

        ativos_em_uso.add(
            ativo
        )

        return True


def liberar_ativo(ativo):

    if not ativo:
        return

    with estado_lock:

        ativos_em_uso.discard(
            ativo
        )


# ============================================================
# BLOQUEIO TEMPORÁRIO DE INSTRUMENTO
# ============================================================

def bloquear_instrumento(
    ativo,
    timeframe,
    motivo
):

    chave = (
        ativo,
        timeframe
    )

    instrumentos_bloqueados[
        chave
    ] = {

        "ate":
            time.time()
            +
            TEMPO_BLOQUEIO_INSTRUMENTO,

        "motivo":
            str(motivo)
    }


def instrumento_bloqueado(
    ativo,
    timeframe
):

    chave = (
        ativo,
        timeframe
    )

    info = instrumentos_bloqueados.get(
        chave
    )

    if not info:
        return False

    if time.time() >= info["ate"]:

        instrumentos_bloqueados.pop(
            chave,
            None
        )

        return False

    return True


# ============================================================
# INDICADORES
# ============================================================

def media(valores):

    if not valores:
        return 0.0

    return sum(valores) / len(valores)


def ema(
    valores,
    periodo
):

    if len(valores) < periodo:
        return None

    k = 2 / (
        periodo + 1
    )

    resultado = media(
        valores[:periodo]
    )

    for preco in valores[periodo:]:

        resultado = (
            preco * k
            +
            resultado * (1 - k)
        )

    return resultado


def rsi(
    valores,
    periodo=14
):

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
        100 / (1 + rs)
    )


def desvio(
    valores
):

    if not valores:
        return 0.0

    m = media(
        valores
    )

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

    m = media(
        dados
    )

    d = desvio(
        dados
    )

    return (
        m - 2 * d,
        m,
        m + 2 * d
    )


def macd(
    valores
):

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

    return (
        e12 - e26
    )


# ============================================================
# CANDLES
# ============================================================

def normalizar_candles(
    candles
):

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
# SOMENTE VELAS FECHADAS
# ============================================================

def remover_vela_aberta(
    candles,
    segundos
):

    agora = int(
        time.time()
    )

    fechadas = []

    for candle in candles:

        inicio = candle.get(
            "from",
            0
        )

        if (
            inicio
            +
            segundos
            <=
            agora
        ):

            fechadas.append(
                candle
            )

    return fechadas


# ============================================================
# ANÁLISE
# ============================================================

def analisar(
    candles
):

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
    # EMA 20 / 50
    # ========================================================

    e20 = ema(
        closes,
        20
    )

    e50 = ema(
        closes,
        50
    )

    if (
        e20 is not None
        and e50 is not None
    ):

        if e20 > e50:
            call += 20

        elif e20 < e50:
            put += 20


    # ========================================================
    # RSI
    # ========================================================

    vrsi = rsi(
        closes
    )

    if vrsi <= 35:

        call += 15

    elif vrsi >= 65:

        put += 15


    # ========================================================
    # BOLLINGER
    # ========================================================

    inferior, meio, superior = (
        bollinger(
            closes
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


    # Evita sinal sem diferença clara.
    if abs(
        call - put
    ) < 15:

        return None


    if (
        call >= SCORE_MIN
        and call > put
    ):

        return {

            "direcao":
                "call",

            "score":
                call,

            "score_contra":
                put
        }


    if (
        put >= SCORE_MIN
        and put > call
    ):

        return {

            "direcao":
                "put",

            "score":
                put,

            "score_contra":
                call
        }


    return None


# ============================================================
# GESTÃO
# ============================================================

def valor_nivel(
    nivel
):

    return round(

        ENTRADA_BASE
        *
        (
            2 ** nivel
        ),

        2
    )


# ============================================================
# VALIDAÇÃO DIGITAL
# ============================================================

def validar_digital(
    ativo,
    timeframe
):

    """
    Validação conservadora.

    Não considera candle disponível como prova de que
    o instrumento Digital está negociável.

    Primeiro verificamos se a biblioteca consegue obter
    o retorno Digital para o ativo/expiração.

    Qualquer falha simplesmente faz o scanner ignorar
    o instrumento.
    """

    if instrumento_bloqueado(
        ativo,
        timeframe
    ):

        return False


    if not garantir_conexao():
        return False


    try:

        with api_lock:

            retorno = (
                api.get_digital_current_profit(
                    ativo,
                    timeframe
                )
            )


        log.debug(
            "DIGITAL CHECK | %s | M%s | retorno=%r",
            ativo,
            timeframe,
            retorno
        )


        if retorno is False:
            return False


        if retorno is None:
            return False


        try:

            retorno_num = float(
                retorno
            )

        except Exception:

            return False


        if retorno_num <= 0:
            return False


        return True


    except Exception as erro:

        log.debug(
            "DIGITAL INDISPONÍVEL | %s | M%s | %s",
            ativo,
            timeframe,
            erro
        )

        return False


# ============================================================
# COMPRAR
# ============================================================

def comprar(
    ativo,
    valor,
    direcao,
    timeframe
):

    if not EXECUTAR_ORDENS:

        return {

            "ok":
                False,

            "id":
                None,

            "motivo":
                "EXECUTAR_ORDENS=false"
        }


    if not garantir_practice():

        return {

            "ok":
                False,

            "id":
                None,

            "motivo":
                "PRACTICE indisponível"
        }


    # ========================================================
    # VERIFICA NOVAMENTE IMEDIATAMENTE ANTES DA COMPRA
    # ========================================================

    if not validar_digital(
        ativo,
        timeframe
    ):

        return {

            "ok":
                False,

            "id":
                None,

            "motivo":
                "instrumento Digital indisponível"
        }


    try:

        log.info(

            "TENTANDO ORDEM | %s | M%s | %s | $%.2f",

            ativo,

            timeframe,

            direcao,

            valor
        )


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

            "RESPOSTA ORDEM | %s | M%s | %r",

            ativo,

            timeframe,

            resposta
        )


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

            retorno = resposta[1]


            if (
                status
                and retorno
            ):

                stats[
                    "ordens_aceitas"
                ] += 1


                log.info(

                    "ORDEM ACEITA | %s | M%s | id=%s",

                    ativo,

                    timeframe,

                    retorno
                )


                return {

                    "ok":
                        True,

                    "id":
                        retorno,

                    "motivo":
                        None
                }


            stats[
                "ordens_recusadas"
            ] += 1


            motivo = str(
                retorno
            )


            # =================================================
            # ERRO QUE ENCONTRAMOS NO RENDER
            # =================================================

            if (
                "invalid instrument"
                in motivo.lower()
            ):

                bloquear_instrumento(

                    ativo,

                    timeframe,

                    motivo
                )


            log.warning(

                "ORDEM RECUSADA | %s | M%s | %r",

                ativo,

                timeframe,

                resposta
            )


            return {

                "ok":
                    False,

                "id":
                    None,

                "motivo":
                    motivo
            }


        stats[
            "ordens_recusadas"
        ] += 1


        return {

            "ok":
                False,

            "id":
                None,

            "motivo":
                repr(resposta)
        }


    except Exception as erro:

        stats[
            "erros"
        ] += 1


        log.exception(
            "Erro na compra | %s",
            ativo
        )


        return {

            "ok":
                False,

            "id":
                None,

            "motivo":
                f"{type(erro).__name__}: {erro}"
        }


# ============================================================
# RESULTADO
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

                time.sleep(2)
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

                        "RESULTADO | id=%s | lucro=%s",

                        order_id,

                        lucro
                    )


                    if lucro > 0:

                        return "win"


                    if lucro < 0:

                        return "loss"


                    return "draw"


        except Exception:

            log.exception(
                "Erro consultando resultado"
            )


        time.sleep(2)


    return "erro"


# ============================================================
# LIBERAR BANCA
# ============================================================

def liberar_frente(
    frente
):

    ativo = frente[
        "ativo"
    ]

    liberar_ativo(
        ativo
    )

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


# ============================================================
# TELEGRAM SINAL
# ============================================================

def enviar_sinal(
    frente
):

    ativo = frente[
        "ativo"
    ]

    mercado = (
        "OTC"
        if "-OTC" in ativo
        else "NORMAL"
    )


    telegram(

        f"📊 <b>{frente['nome']} - NOVO SINAL</b>\n\n"

        f"💱 <b>{ativo}</b> | {mercado}\n"

        f"⏱ M{frente['timeframe']}\n"

        f"🎯 <b>{frente['direcao'].upper()}</b>\n"

        f"🔥 Score: <b>{frente['score']}/100</b>\n\n"

        f"💰 ${valor_nivel(0):.2f} → "
        f"G1 ${valor_nivel(1):.2f} → "
        f"G2 ${valor_nivel(2):.2f}\n\n"

        f"🎯 Acerto da banca: "
        f"<b>{taxa_frente(frente):.2f}%</b>"
    )


# ============================================================
# RESULTADO FINAL
# ============================================================

def finalizar(
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

            resultado = (
                "✅ <b>WIN</b>"
            )


        elif nivel == 1:

            frente[
                "win_g1"
            ] += 1

            resultado = (
                "✅ <b>WIN G1</b>"
            )


        else:

            frente[
                "win_g2"
            ] += 1

            resultado = (
                "✅ <b>WIN G2</b>"
            )


    else:

        frente[
            "losses"
        ] += 1

        stats[
            "losses"
        ] += 1

        resultado = (
            "❌ <b>LOSS</b>"
        )


    telegram(

        f"{resultado}\n\n"

        f"🏦 {frente['nome']}\n"

        f"💱 {frente['ativo']}\n"

        f"⏱ M{frente['timeframe']}\n\n"

        f"📊 {frente['wins']} WIN / "
        f"{frente['losses']} LOSS\n"

        f"🎯 Acerto: "
        f"<b>{taxa_frente(frente):.2f}%</b>"
    )


    liberar_frente(
        frente
    )


# ============================================================
# OPERAR
# ============================================================

def operar(
    chave,
    sinal
):

    frente = FRENTES[
        chave
    ]


    try:

        # ====================================================
        # ENTRADA
        # ====================================================

        frente[
            "nivel"
        ] = 0


        ordem = comprar(

            frente[
                "ativo"
            ],

            valor_nivel(0),

            frente[
                "direcao"
            ],

            frente[
                "timeframe"
            ]
        )


        if not ordem["ok"]:

            frente[
                "ordens_recusadas"
            ] += 1


            log.warning(

                "%s | ENTRADA NÃO EXECUTADA | %s | %s",

                frente["nome"],

                frente["ativo"],

                ordem["motivo"]
            )


            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = ordem[
            "id"
        ]


        # Telegram somente depois de
        # a IQ confirmar a ordem.
        enviar_sinal(
            frente
        )


        resultado = esperar_resultado(

            ordem[
                "id"
            ],

            frente[
                "timeframe"
            ]
        )


        if resultado == "win":

            finalizar(
                frente,
                True,
                0
            )

            return


        if resultado != "loss":

            log.warning(

                "%s | Resultado entrada=%s",

                frente["nome"],

                resultado
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


        ordem = comprar(

            frente[
                "ativo"
            ],

            valor_nivel(1),

            frente[
                "direcao"
            ],

            frente[
                "timeframe"
            ]
        )


        if not ordem["ok"]:

            log.warning(
                "%s | G1 recusado | %s",
                frente["nome"],
                ordem["motivo"]
            )

            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = ordem[
            "id"
        ]


        resultado = esperar_resultado(

            ordem[
                "id"
            ],

            frente[
                "timeframe"
            ]
        )


        if resultado == "win":

            finalizar(
                frente,
                True,
                1
            )

            return


        if resultado != "loss":

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


        ordem = comprar(

            frente[
                "ativo"
            ],

            valor_nivel(2),

            frente[
                "direcao"
            ],

            frente[
                "timeframe"
            ]
        )


        if not ordem["ok"]:

            log.warning(
                "%s | G2 recusado | %s",
                frente["nome"],
                ordem["motivo"]
            )

            liberar_frente(
                frente
            )

            return


        frente[
            "order_id"
        ] = ordem[
            "id"
        ]


        resultado = esperar_resultado(

            ordem[
                "id"
            ],

            frente[
                "timeframe"
            ]
        )


        if resultado == "win":

            finalizar(
                frente,
                True,
                2
            )

            return


        if resultado == "loss":

            finalizar(
                frente,
                False,
                2
            )

            return


        liberar_frente(
            frente
        )


    except Exception:

        stats[
            "erros"
        ] += 1

        log.exception(
            "Erro operação %s",
            chave
        )

        liberar_frente(
            frente
        )


# ============================================================
# INICIAR SINAL
# ============================================================

def iniciar_sinal(
    chave,
    ativo,
    sinal
):

    frente = FRENTES[
        chave
    ]


    with estado_lock:

        if frente[
            "ocupada"
        ]:
            return False


        # ====================================================
        # NÃO DEIXA DUAS BANCAS USAREM O MESMO ATIVO
        # ====================================================

        if ativo in ativos_em_uso:

            log.info(
                "%s ignorou %s: ativo já utilizado por outra banca.",
                frente["nome"],
                ativo
            )

            return False


        agora = time.time()


        ultimo = frente[
            "ultimo_sinal"
        ].get(
            ativo,
            0
        )


        if (
            agora - ultimo
            <
            frente["segundos"]
        ):

            return False


        ativos_em_uso.add(
            ativo
        )


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

        frente[
            "nivel"
        ] = 0


        frente[
            "ultimo_sinal"
        ][
            ativo
        ] = agora


        stats[
            "sinais"
        ] += 1


    threading.Thread(

        target=operar,

        args=(
            chave,
            sinal
        ),

        daemon=True

    ).start()


    return True


# ============================================================
# SCANNER
# ============================================================

def scanner(
    chave
):

    frente = FRENTES[
        chave
    ]


    if frente[
        "ocupada"
    ]:
        return


    for ativo in lista_ativos():

        if frente[
            "ocupada"
        ]:
            return


        # Outra banca já está usando.
        if ativo_esta_em_uso(
            ativo
        ):
            continue


        # Instrumento já falhou recentemente.
        if instrumento_bloqueado(

            ativo,

            frente[
                "timeframe"
            ]
        ):

            continue


        try:

            if not garantir_conexao():
                return


            # =================================================
            # PRIMEIRO:
            # valida Digital.
            #
            # Assim evitamos fazer toda a análise de um ativo
            # que não pode receber a ordem.
            # =================================================

            if not validar_digital(

                ativo,

                frente[
                    "timeframe"
                ]
            ):

                continue


            with api_lock:

                candles = api.get_candles(

                    ativo,

                    frente[
                        "segundos"
                    ],

                    105,

                    time.time()
                )


            candles = normalizar_candles(
                candles
            )


            candles = remover_vela_aberta(

                candles,

                frente[
                    "segundos"
                ]
            )


            if len(candles) < 60:
                continue


            # Somente as últimas 100
            candles = candles[-100:]


            sinal = analisar(
                candles
            )


            if not sinal:
                continue


            log.info(

                "SINAL APROVADO | %s | %s | M%s | %s | score=%s",

                frente[
                    "nome"
                ],

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


            if iniciar_sinal(

                chave,

                ativo,

                sinal
            ):

                return


        except Exception as erro:

            log.debug(

                "Scanner ignorou %s | M%s | %s",

                ativo,

                frente[
                    "timeframe"
                ],

                erro
            )


        time.sleep(
            INTERVALO_ATIVOS
        )


# ============================================================
# LOOP
# ============================================================

def loop_frente(
    chave
):

    atrasos = {

        "BANCA_1": 5,

        "BANCA_2": 10,

        "BANCA_3": 15,
    }


    time.sleep(
        atrasos[
            chave
        ]
    )


    while True:

        try:

            scanner(
                chave
            )

        except Exception:

            stats[
                "erros"
            ] += 1

            log.exception(
                "Erro scanner %s",
                chave
            )


        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# STATUS
# ============================================================

@app.route("/")
def home():

    resultado_frentes = {}


    for chave, frente in (
        FRENTES.items()
    ):

        resultado_frentes[
            chave
        ] = {

            "timeframe":
                f"M{frente['timeframe']}",

            "ocupada":
                frente[
                    "ocupada"
                ],

            "ativo":
                frente[
                    "ativo"
                ],

            "direcao":
                frente[
                    "direcao"
                ],

            "score":
                frente[
                    "score"
                ],

            "nivel":
                frente[
                    "nivel"
                ],

            "wins":
                frente[
                    "wins"
                ],

            "losses":
                frente[
                    "losses"
                ],

            "win_direto":
                frente[
                    "win_direto"
                ],

            "win_g1":
                frente[
                    "win_g1"
                ],

            "win_g2":
                frente[
                    "win_g2"
                ],

            "assertividade":
                taxa_frente(
                    frente
                )
        }


    saldo = None


    try:

        if garantir_practice():

            with api_lock:

                saldo = (
                    api.get_balance()
                )

    except Exception:
        pass


    return jsonify({

        "robo":
            "IQ 3 FRENTES V3",

        "status":
            "online",

        "conta":
            "PRACTICE",

        "real_bloqueada":
            True,

        "saldo":
            saldo,

        "execucao":
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
        },

        "ativos_em_uso":
            list(
                ativos_em_uso
            ),

        "instrumentos_bloqueados":
            len(
                instrumentos_bloqueados
            ),

        "stats":
            stats,

        "assertividade_geral":
            taxa(
                stats["wins"],
                stats["losses"]
            ),

        "frentes":
            resultado_frentes
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
            "IQ-3-FRENTES-V3",

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
