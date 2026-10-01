import os
import time
import math
import threading
import logging
from collections import defaultdict

import requests
from flask import Flask, jsonify

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# IQ OPTION - 3 FRENTES PRACTICE
#
# FRENTE 1 = M1
# FRENTE 2 = M5
# FRENTE 3 = M15
#
# Entrada: $2
# G1:      $4
# G2:      $8
#
# CONTA REAL BLOQUEADA
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-3-frentes")


# ============================================================
# CONFIG
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
    == "true"
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


# ============================================================
# TRAVA ABSOLUTA
# ============================================================

CONTA_PERMITIDA = "PRACTICE"

# Não existe seleção REAL neste código.
# Toda reconexão volta obrigatoriamente para PRACTICE.


# ============================================================
# 3 FRENTES
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

    ativos = []

    for ativo in ATIVOS_BASE:

        # NORMAL
        ativos.append(ativo)

        # OTC
        ativos.append(
            f"{ativo}-OTC"
        )

    return ativos


# ============================================================
# ESTADO
# ============================================================

api = None

api_lock = threading.RLock()
estado_lock = threading.RLock()


for chave in FRENTES:

    FRENTES[chave].update({

        "ocupada": False,

        "ativo": None,

        "direcao": None,

        "nivel": 0,

        "wins": 0,

        "losses": 0,

        "win_direto": 0,

        "win_g1": 0,

        "win_g2": 0,

        "ultimo_sinal": {},

        "operacao_id": None,

    })


stats_geral = {

    "sinais": 0,

    "wins": 0,

    "losses": 0,

    "ordens": 0,

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
            (
                f"https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/sendMessage"
            ),
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },
            timeout=15
        )

    except Exception:

        log.exception(
            "Erro Telegram"
        )


# ============================================================
# ASSERTIVIDADE
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
        stats_geral["wins"]
        +
        stats_geral["losses"]
    )

    if total == 0:
        return 0.0

    return round(
        stats_geral["wins"]
        / total
        * 100,
        2
    )


# ============================================================
# IQ OPTION
# ============================================================

def conectar():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        log.error(
            "IQ_EMAIL/IQ_PASSWORD ausentes."
        )

        return False

    try:

        nova = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD
        )

        ok, motivo = nova.connect()

        if not ok:

            log.error(
                "IQ conexão recusada: %s",
                motivo
            )

            return False

        # ====================================================
        # TRAVA PRACTICE
        # ====================================================

        nova.change_balance(
            "PRACTICE"
        )

        with api_lock:
            api = nova

        log.info(
            "IQ conectada - PRACTICE"
        )

        return True

    except Exception:

        log.exception(
            "Erro conectando IQ"
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
# INDICADORES
# ============================================================

def media(lista):

    if not lista:
        return 0

    return (
        sum(lista)
        /
        len(lista)
    )


def ema(lista, periodo):

    if len(lista) < periodo:
        return None

    k = 2 / (periodo + 1)

    valor = media(
        lista[:periodo]
    )

    for preco in lista[periodo:]:

        valor = (
            preco * k
            +
            valor * (1 - k)
        )

    return valor


def rsi(lista, periodo=14):

    if len(lista) <= periodo:
        return 50

    dados = lista[
        -(periodo + 1):
    ]

    ganhos = []
    perdas = []

    for i in range(
        len(dados) - 1
    ):

        d = (
            dados[i + 1]
            -
            dados[i]
        )

        ganhos.append(
            max(d, 0)
        )

        perdas.append(
            max(-d, 0)
        )

    g = media(ganhos)
    p = media(perdas)

    if p == 0:

        if g > 0:
            return 100

        return 50

    rs = g / p

    return (
        100
        -
        100 / (1 + rs)
    )


def desvio(lista):

    if not lista:
        return 0

    m = media(lista)

    return math.sqrt(
        media([
            (x - m) ** 2
            for x in lista
        ])
    )


def bollinger(lista, periodo=20):

    if len(lista) < periodo:

        return (
            None,
            None,
            None
        )

    dados = lista[
        -periodo:
    ]

    m = media(dados)
    d = desvio(dados)

    return (
        m - 2 * d,
        m,
        m + 2 * d
    )


def macd(lista):

    e12 = ema(lista, 12)
    e26 = ema(lista, 26)

    if e12 is None or e26 is None:
        return 0

    return e12 - e26


# ============================================================
# CANDLES
# ============================================================

def normalizar(candles):

    resultado = []

    if not candles:
        return resultado

    for c in candles:

        try:

            resultado.append({

                "open":
                    float(c["open"]),

                "close":
                    float(c["close"]),

                "max":
                    float(c["max"]),

                "min":
                    float(c["min"]),

                "from":
                    int(
                        c.get(
                            "from",
                            0
                        )
                    ),
            })

        except Exception:
            continue

    resultado.sort(
        key=lambda x: x["from"]
    )

    return resultado


# ============================================================
# MOTOR DE SCORE
# ============================================================

def analisar(candles):

    candles = normalizar(candles)

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


    # EMA 20/50
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20 is not None and e50 is not None:

        if e20 > e50:
            call += 20

        elif e20 < e50:
            put += 20


    # RSI
    r = rsi(closes)

    if r <= 35:
        call += 15

    elif r >= 65:
        put += 15


    # Bollinger
    inferior, _, superior = (
        bollinger(closes)
    )

    preco = closes[-1]

    if inferior is not None:

        if preco <= inferior:
            call += 10

        elif preco >= superior:
            put += 10


    # Price Action
    amplitude = max(
        highs[-1] - lows[-1],
        0.00000001
    )

    corpo = abs(
        closes[-1] - opens[-1]
    )

    forca = corpo / amplitude

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


    # Suporte / resistência
    resistencia = max(
        highs[-11:-1]
    )

    suporte = min(
        lows[-11:-1]
    )

    faixa = max(
        resistencia - suporte,
        0.00000001
    )

    tolerancia = (
        faixa * 0.10
    )

    if (
        preco
        <= suporte + tolerancia
    ):
        call += 10

    if (
        preco
        >= resistencia - tolerancia
    ):
        put += 10


    # Breakout
    if preco > resistencia:
        call += 15

    elif preco < suporte:
        put += 15


    # MACD
    m = macd(closes)

    if m > 0:
        call += 10

    elif m < 0:
        put += 10


    # Momentum
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


    call = min(call, 100)
    put = min(put, 100)


    # Evita empate técnico
    if abs(call - put) < 15:
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
# VALORES DOS GALES
# ============================================================

def valor_nivel(nivel):

    return round(
        ENTRADA_BASE
        *
        (
            2 ** nivel
        ),
        2
    )


# ============================================================
# MENSAGEM SINAL
# ============================================================

def enviar_sinal(
    frente,
    ativo,
    sinal
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
        f"🎯 <b>{sinal['direcao'].upper()}</b>\n"
        f"🔥 Score: <b>{sinal['score']}/100</b>\n\n"

        f"💰 $2.00 → "
        f"G1 $4.00 → "
        f"G2 $8.00\n\n"

        f"🎯 Acerto da banca: "
        f"<b>{taxa_frente(frente):.2f}%</b>"
    )


# ============================================================
# RESULTADO FINAL
# ============================================================

def resultado_final(
    frente,
    nivel,
    ganhou
):

    with estado_lock:

        if ganhou:

            frente["wins"] += 1
            stats_geral["wins"] += 1

            if nivel == 0:
                frente["win_direto"] += 1

            elif nivel == 1:
                frente["win_g1"] += 1

            elif nivel == 2:
                frente["win_g2"] += 1

        else:

            frente["losses"] += 1
            stats_geral["losses"] += 1


        ativo = frente["ativo"]
        direcao = frente["direcao"]

        if ganhou:

            if nivel == 0:
                titulo = "✅ <b>WIN</b>"

            else:
                titulo = (
                    f"✅ <b>WIN G{nivel}</b>"
                )

        else:

            titulo = "❌ <b>LOSS</b>"


        taxa = taxa_frente(
            frente
        )


        telegram(

            f"{titulo}\n\n"

            f"🏦 {frente['nome']}\n"

            f"💱 {ativo} | "
            f"M{frente['timeframe']} | "
            f"{direcao.upper()}\n\n"

            f"📊 {frente['wins']} WIN / "
            f"{frente['losses']} LOSS\n"

            f"🎯 Acerto: "
            f"<b>{taxa:.2f}%</b>"
        )


        frente["ocupada"] = False
        frente["ativo"] = None
        frente["direcao"] = None
        frente["nivel"] = 0
        frente["operacao_id"] = None


# ============================================================
# EXECUTAR DIGITAL
# ============================================================

def comprar_digital(
    ativo,
    valor,
    direcao,
    timeframe
):

    if not EXECUTAR_ORDENS:

        log.warning(
            "EXECUTAR_ORDENS=false"
        )

        return False, None


    if not garantir_conexao():

        return False, None


    try:

        # Reforça PRACTICE antes
        # de qualquer ordem.
        with api_lock:

            api.change_balance(
                "PRACTICE"
            )

            status, order_id = (
                api.buy_digital_spot_v2(
                    ativo,
                    valor,
                    direcao,
                    timeframe
                )
            )


        if status:

            stats_geral[
                "ordens"
            ] += 1

            log.info(
                "ORDEM PRACTICE | %s | $%.2f | %s | M%s | id=%s",
                ativo,
                valor,
                direcao,
                timeframe,
                order_id
            )

            return True, order_id


        log.warning(
            "Ordem recusada | %s | M%s",
            ativo,
            timeframe
        )

        return False, None


    except Exception:

        stats_geral[
            "erros"
        ] += 1

        log.exception(
            "Erro ordem"
        )

        return False, None


# ============================================================
# ESPERAR RESULTADO IQ
# ============================================================

def esperar_resultado(
    order_id,
    timeframe
):

    # Margem suficiente para
    # expiração + resposta da API.
    limite = (
        time.time()
        +
        timeframe * 60
        +
        120
    )


    while time.time() < limite:

        try:

            if not garantir_conexao():

                time.sleep(3)
                continue


            with api_lock:

                status, lucro = (
                    api.check_win_digital_v2(
                        order_id
                    )
                )


            if status:

                try:
                    lucro = float(lucro)

                except Exception:
                    lucro = 0.0


                if lucro > 0:
                    return "win", lucro

                if lucro < 0:
                    return "loss", lucro

                return "draw", lucro


        except Exception:
            pass


        time.sleep(2)


    return "erro", 0.0


# ============================================================
# CICLO ENTRADA / G1 / G2
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

        direcao = sinal[
            "direcao"
        ]

        timeframe = frente[
            "timeframe"
        ]


        # ====================================================
        # ENTRADA + G1 + G2
        # ====================================================

        for nivel in range(3):

            frente[
                "nivel"
            ] = nivel

            valor = valor_nivel(
                nivel
            )


            log.info(
                "%s | nível=%s | %s | $%.2f",
                frente["nome"],
                nivel,
                ativo,
                valor
            )


            ok, order_id = comprar_digital(
                ativo,
                valor,
                direcao,
                timeframe
            )


            if not ok:

                telegram(
                    f"⚠️ <b>{frente['nome']}</b>\n"
                    f"Ordem não executada.\n"
                    f"{ativo} | M{timeframe}"
                )

                frente[
                    "ocupada"
                ] = False

                return


            frente[
                "operacao_id"
            ] = order_id


            resultado, lucro = (
                esperar_resultado(
                    order_id,
                    timeframe
                )
            )


            log.info(
                "%s | resultado=%s | lucro=%s",
                frente["nome"],
                resultado,
                lucro
            )


            # =================================================
            # WIN
            # =================================================

            if resultado == "win":

                resultado_final(
                    frente,
                    nivel,
                    True
                )

                return


            # =================================================
            # DRAW
            #
            # Repete o mesmo nível.
            # Não sobe Gale.
            # =================================================

            if resultado == "draw":

                log.info(
                    "%s | DRAW",
                    frente["nome"]
                )

                # Para manter a lógica
                # simples, encerra sem
                # contabilizar W/L.
                frente[
                    "ocupada"
                ] = False

                return


            # =================================================
            # ERRO
            # =================================================

            if resultado == "erro":

                telegram(
                    f"⚠️ <b>{frente['nome']}</b>\n"
                    f"Não foi possível confirmar "
                    f"o resultado da operação."
                )

                frente[
                    "ocupada"
                ] = False

                return


            # Se LOSS e ainda existe
            # Gale, o loop continua.


        # ====================================================
        # PERDEU ENTRADA + G1 + G2
        # ====================================================

        resultado_final(
            frente,
            2,
            False
        )


    except Exception:

        stats_geral[
            "erros"
        ] += 1

        log.exception(
            "Erro ciclo %s",
            frente["nome"]
        )

        frente[
            "ocupada"
        ] = False


# ============================================================
# NOVO SINAL
# ============================================================

def novo_sinal(
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


        chave = ativo

        agora = time.time()

        ultimo = (
            frente[
                "ultimo_sinal"
            ].get(
                chave,
                0
            )
        )


        # Evita repetir o mesmo ativo
        # imediatamente.
        if (
            agora - ultimo
            <
            frente["segundos"]
        ):
            return


        frente[
            "ultimo_sinal"
        ][
            chave
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

        stats_geral[
            "sinais"
        ] += 1


    enviar_sinal(
        frente,
        ativo,
        sinal
    )


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
# ANALISAR UMA FRENTE
# ============================================================

def analisar_frente(
    chave_frente
):

    frente = FRENTES[
        chave_frente
    ]


    # Uma operação por frente.
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

                candles = api.get_candles(
                    ativo,
                    frente[
                        "segundos"
                    ],
                    100,
                    time.time()
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
                    "%s | SINAL | %s | %s | score=%s",
                    frente["nome"],
                    ativo,
                    sinal["direcao"],
                    sinal["score"]
                )


                novo_sinal(
                    chave_frente,
                    ativo,
                    sinal
                )

                return


        except Exception:
            continue


# ============================================================
# LOOP INDIVIDUAL DE CADA BANCA
# ============================================================

def loop_frente(
    chave_frente
):

    frente = FRENTES[
        chave_frente
    ]


    # Pequena diferença para as
    # três threads não consultarem
    # tudo exatamente juntas.
    atrasos = {
        "BANCA_1": 5,
        "BANCA_2": 10,
        "BANCA_3": 15
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

            stats_geral[
                "erros"
            ] += 1

            log.exception(
                "Erro %s",
                frente["nome"]
            )


        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# API WEB
# ============================================================

@app.route("/")
def home():

    dados_frentes = {}


    for chave, frente in (
        FRENTES.items()
    ):

        dados_frentes[
            chave
        ] = {

            "nome":
                frente[
                    "nome"
                ],

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
                ),
        }


    return jsonify({

        "robo":
            "IQ 3 FRENTES",

        "status":
            "online",

        "conta":
            "PRACTICE",

        "real_bloqueada":
            True,

        "execucao":
            EXECUTAR_ORDENS,

        "entrada":
            ENTRADA_BASE,

        "g1":
            ENTRADA_BASE * 2,

        "g2":
            ENTRADA_BASE * 4,

        "score_minimo":
            SCORE_MIN,

        "geral":
            stats_geral,

        "assertividade_geral":
            taxa_geral(),

        "frentes":
            dados_frentes
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

        "iq_conectada":
            conectado,

        "conta":
            "PRACTICE",

        "execucao":
            EXECUTAR_ORDENS,

        "versao":
            "3-FRENTES-V1"
    })


# ============================================================
# INICIAR
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
                args=(chave,),
                daemon=True
            ).start()


iniciar_threads()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
                    )
