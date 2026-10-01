import os
import time
import math
import logging
import threading
import requests

from datetime import datetime, timezone
from flask import Flask, jsonify
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# VERSÃO
# ============================================================

VERSAO = "IQ-3-FRENTES-V6-INSTRUMENT-ID"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-v6")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

EXECUTAR_ORDENS = (
    os.getenv("EXECUTAR_ORDENS", "false").lower() == "true"
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
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.7")
)

TEMPO_BLOQUEIO = 300

CONTA = "PRACTICE"


# ============================================================
# ATIVOS
# ============================================================

ATIVOS = [
    "EURUSD",
    "GBPUSD",
    "EURGBP",
    "USDJPY",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "EURJPY",
    "GBPJPY",
    "AUDCAD",
    "AUDJPY",
    "EURCAD",
    "NZDUSD",

    "EURUSD-OTC",
    "GBPUSD-OTC",
    "EURGBP-OTC",
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "USDCHF-OTC",
    "USDCAD-OTC",
    "EURJPY-OTC",
    "GBPJPY-OTC",
    "AUDCAD-OTC",
    "AUDJPY-OTC",
    "EURCAD-OTC",
    "NZDUSD-OTC",
]


# ============================================================
# FRENTES
#
# timeframe_analise:
# candles utilizados na estratégia
#
# expiracao:
# contrato DIGITAL efetivamente comprado
#
# A API Digital documenta 1 ou 5 minutos.
# ============================================================

FRENTES = {

    "BANCA 1": {
        "timeframe_analise": 1,
        "segundos": 60,
        "expiracao": 1,
    },

    "BANCA 2": {
        "timeframe_analise": 5,
        "segundos": 300,
        "expiracao": 5,
    },

    "BANCA 3": {
        "timeframe_analise": 15,
        "segundos": 900,

        # Analisa M15, mas executa DIGITAL M5.
        "expiracao": 5,
    },
}


# ============================================================
# ESTADO GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()
connect_lock = threading.Lock()
estado_lock = threading.RLock()

ativos_em_uso = set()

bloqueados = {}

ativos_validos = set()

stats = {
    "wins": 0,
    "losses": 0,
    "win_direto": 0,
    "win_g1": 0,
    "win_g2": 0,
    "ordens_aceitas": 0,
    "ordens_recusadas": 0,
}

estado_frentes = {}

for nome in FRENTES:

    estado_frentes[nome] = {

        "ocupada": False,

        "ativo": None,

        "direcao": None,

        "score": None,

        "order_id": None,

        "wins": 0,

        "losses": 0,

        "win_direto": 0,

        "win_g1": 0,

        "win_g2": 0,

        "ordens_aceitas": 0,

        "ordens_recusadas": 0,

        "ultimo_sinal": None,
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        return

    try:

        requests.post(
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": texto
            },
            timeout=10
        )

    except Exception as e:

        log.warning(
            "TELEGRAM ERRO | %s",
            e
        )


# ============================================================
# CONEXÃO
# ============================================================

def conectado():

    global api

    try:

        if api is None:
            return False

        return bool(
            api.check_connect()
        )

    except Exception:

        return False


def conectar():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        log.error(
            "IQ_EMAIL/IQ_PASSWORD ausentes."
        )

        return False


    with connect_lock:

        if conectado():
            return True

        try:

            log.info(
                "CONECTANDO IQ OPTION..."
            )

            nova = IQ_Option(
                IQ_EMAIL,
                IQ_PASSWORD
            )

            ok, motivo = nova.connect()

            log.info(
                "CONNECT | ok=%s | motivo=%s",
                ok,
                motivo
            )

            if not ok:
                return False

            # ================================================
            # CONTA DE TREINO TRAVADA
            # ================================================

            nova.change_balance(
                "PRACTICE"
            )

            time.sleep(1)

            saldo = nova.get_balance()

            api = nova

            log.info(
                "IQ CONECTADA | PRACTICE | saldo=%s",
                saldo
            )

            telegram(
                "🤖 ROBÔ V6 ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: ${saldo}\n\n"
                "🏦 Banca 1: análise M1 / Digital M1\n"
                "🏦 Banca 2: análise M5 / Digital M5\n"
                "🏦 Banca 3: análise M15 / Digital M5\n\n"
                f"🔥 Score mínimo: {SCORE_MIN}"
            )

            return True

        except Exception as e:

            log.exception(
                "ERRO CONEXÃO | %s",
                e
            )

            return False


def garantir_conexao():

    if conectado():
        return True

    return conectar()


def garantir_practice():

    if not garantir_conexao():
        return False

    try:

        api.change_balance(
            "PRACTICE"
        )

        return True

    except Exception:

        return False


# ============================================================
# BLOQUEIO
# ============================================================

def chave_bloqueio(
    ativo,
    expiracao
):

    return (
        ativo,
        expiracao
    )


def esta_bloqueado(
    ativo,
    expiracao
):

    chave = chave_bloqueio(
        ativo,
        expiracao
    )

    limite = bloqueados.get(
        chave
    )

    if limite is None:
        return False

    if time.time() >= limite:

        bloqueados.pop(
            chave,
            None
        )

        return False

    return True


def bloquear(
    ativo,
    expiracao,
    motivo
):

    bloqueados[
        chave_bloqueio(
            ativo,
            expiracao
        )
    ] = (
        time.time()
        + TEMPO_BLOQUEIO
    )

    log.warning(
        "BLOQUEADO | %s | DIGITAL M%s | %ss | %s",
        ativo,
        expiracao,
        TEMPO_BLOQUEIO,
        motivo
    )


# ============================================================
# CANDLES
# ============================================================

def obter_candles(
    ativo,
    segundos,
    quantidade=100
):

    if not garantir_conexao():
        return []

    try:

        agora = int(
            time.time()
        )

        with api_lock:

            dados = api.get_candles(
                ativo,
                segundos,
                quantidade,
                agora
            )

        if not dados:
            return []

        candles = []

        agora = int(
            time.time()
        )

        for c in dados:

            try:

                inicio = int(
                    c.get(
                        "from",
                        0
                    )
                )

                # ignora candle aberto
                if inicio + segundos > agora:
                    continue

                candles.append({

                    "from": inicio,

                    "open": float(
                        c["open"]
                    ),

                    "close": float(
                        c["close"]
                    ),

                    "max": float(
                        c["max"]
                    ),

                    "min": float(
                        c["min"]
                    )
                })

            except Exception:
                pass

        if len(candles) >= 60:

            with estado_lock:

                ativos_validos.add(
                    ativo
                )

        return candles

    except Exception as e:

        log.debug(
            "CANDLES ERRO | %s | %s",
            ativo,
            e
        )

        return []


# ============================================================
# INDICADORES
# ============================================================

def media(lista):

    if not lista:
        return 0

    return (
        sum(lista)
        / len(lista)
    )


def desvio(lista):

    if not lista:
        return 0

    m = media(
        lista
    )

    return math.sqrt(
        sum(
            (x - m) ** 2
            for x in lista
        )
        / len(lista)
    )


def ema(
    valores,
    periodo
):

    if len(valores) < periodo:
        return None

    resultado = media(
        valores[:periodo]
    )

    k = (
        2
        / (periodo + 1)
    )

    for preco in valores[
        periodo:
    ]:

        resultado = (
            preco * k
            + resultado * (1 - k)
        )

    return resultado


def rsi(
    valores,
    periodo=14
):

    if len(valores) < periodo + 1:
        return None

    ganhos = []
    perdas = []

    inicio = (
        len(valores)
        - periodo
    )

    for i in range(
        inicio,
        len(valores)
    ):

        d = (
            valores[i]
            - valores[i - 1]
        )

        if d >= 0:

            ganhos.append(d)
            perdas.append(0)

        else:

            ganhos.append(0)
            perdas.append(abs(d))

    g = media(
        ganhos
    )

    p = media(
        perdas
    )

    if p == 0:
        return 100

    rs = g / p

    return (
        100
        - 100 / (1 + rs)
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

    if (
        e12 is None
        or e26 is None
    ):

        return None

    return (
        e12 - e26
    )


# ============================================================
# ANÁLISE
# ============================================================

def analisar(
    candles
):

    if len(candles) < 60:
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    ultimo = candles[-1]

    preco = ultimo[
        "close"
    ]

    call = 0
    put = 0

    motivos_call = []
    motivos_put = []


    # ========================================================
    # EMA 20/50 - 20
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

        if (
            e20 > e50
            and preco > e20
        ):

            call += 20
            motivos_call.append(
                "EMA"
            )

        elif (
            e20 < e50
            and preco < e20
        ):

            put += 20
            motivos_put.append(
                "EMA"
            )


    # ========================================================
    # RSI - 15
    # ========================================================

    vrsi = rsi(
        closes
    )

    if vrsi is not None:

        if 50 <= vrsi <= 70:

            call += 15
            motivos_call.append(
                "RSI"
            )

        elif 30 <= vrsi < 50:

            put += 15
            motivos_put.append(
                "RSI"
            )


    # ========================================================
    # BOLLINGER - 10
    # ========================================================

    ult20 = closes[-20:]

    mm = media(
        ult20
    )

    dp = desvio(
        ult20
    )

    superior = (
        mm + 2 * dp
    )

    inferior = (
        mm - 2 * dp
    )

    if (
        preco > mm
        and preco < superior
    ):

        call += 10
        motivos_call.append(
            "BOLLINGER"
        )

    elif (
        preco < mm
        and preco > inferior
    ):

        put += 10
        motivos_put.append(
            "BOLLINGER"
        )


    # ========================================================
    # PRICE ACTION - 15
    # ========================================================

    corpo = abs(
        ultimo["close"]
        - ultimo["open"]
    )

    amplitude = (
        ultimo["max"]
        - ultimo["min"]
    )

    if amplitude > 0:

        proporcao = (
            corpo
            / amplitude
        )

        if proporcao >= 0.55:

            if (
                ultimo["close"]
                > ultimo["open"]
            ):

                call += 15

                motivos_call.append(
                    "PRICE ACTION"
                )

            elif (
                ultimo["close"]
                < ultimo["open"]
            ):

                put += 15

                motivos_put.append(
                    "PRICE ACTION"
                )


    # ========================================================
    # SUPORTE/RESISTÊNCIA - 10
    # ========================================================

    janela = candles[
        -20:-1
    ]

    resistencia = max(
        x["max"]
        for x in janela
    )

    suporte = min(
        x["min"]
        for x in janela
    )

    distancia = (
        resistencia
        - suporte
    )

    if distancia > 0:

        posicao = (
            (preco - suporte)
            / distancia
        )

        if posicao <= 0.30:

            call += 10

            motivos_call.append(
                "SUPORTE"
            )

        elif posicao >= 0.70:

            put += 10

            motivos_put.append(
                "RESISTENCIA"
            )


    # ========================================================
    # BREAKOUT - 15
    # ========================================================

    janela_break = candles[
        -11:-1
    ]

    max_anterior = max(
        x["max"]
        for x in janela_break
    )

    min_anterior = min(
        x["min"]
        for x in janela_break
    )

    if preco > max_anterior:

        call += 15

        motivos_call.append(
            "BREAKOUT"
        )

    elif preco < min_anterior:

        put += 15

        motivos_put.append(
            "BREAKOUT"
        )


    # ========================================================
    # MACD - 10
    # ========================================================

    vm = macd(
        closes
    )

    if vm is not None:

        if vm > 0:

            call += 10

            motivos_call.append(
                "MACD"
            )

        elif vm < 0:

            put += 10

            motivos_put.append(
                "MACD"
            )


    # ========================================================
    # MOMENTUM - 5
    # ========================================================

    momentum = (
        closes[-1]
        - closes[-6]
    )

    if momentum > 0:

        call += 5

        motivos_call.append(
            "MOMENTUM"
        )

    elif momentum < 0:

        put += 5

        motivos_put.append(
            "MOMENTUM"
        )


    # ========================================================
    # RESULTADO
    # ========================================================

    if call >= put:

        direcao = "call"

        score = call

        oposto = put

        motivos = motivos_call

    else:

        direcao = "put"

        score = put

        oposto = call

        motivos = motivos_put


    if score < SCORE_MIN:
        return None

    if (
        score - oposto
        < 15
    ):

        return None


    return {

        "direcao": direcao,

        "score": score,

        "oposto": oposto,

        "motivos": motivos,
    }


# ============================================================
# ESCOLHER INSTRUMENT_ID REAL
# ============================================================

def obter_instrumento_digital(
    ativo,
    direcao,
    expiracao
):

    if expiracao not in (
        1,
        5
    ):

        return (
            None,
            None,
            "expiracao_invalida"
        )


    if not garantir_practice():

        return (
            None,
            None,
            "sem_conexao"
        )


    log.info(
        "STRIKE SUBSCRIBE | %s | M%s",
        ativo,
        expiracao
    )


    try:

        with api_lock:

            api.subscribe_strike_list(
                ativo,
                expiracao
            )


        # Dá tempo para websocket receber
        # a primeira atualização.
        time.sleep(1.5)


        # IMPORTANTE:
        # get_realtime_strike_list() da biblioteca
        # pode ficar esperando dados.
        #
        # Portanto fazemos a leitura em uma thread
        # com timeout para não congelar a banca.

        resultado = {
            "data": None,
            "erro": None
        }


        def ler_strikes():

            try:

                resultado[
                    "data"
                ] = api.get_realtime_strike_list(
                    ativo,
                    expiracao
                )

            except Exception as e:

                resultado[
                    "erro"
                ] = str(e)


        t = threading.Thread(
            target=ler_strikes,
            daemon=True
        )

        t.start()

        t.join(
            timeout=8
        )


        if t.is_alive():

            log.warning(
                "STRIKE TIMEOUT | %s | M%s",
                ativo,
                expiracao
            )

            return (
                None,
                None,
                "strike_timeout"
            )


        if resultado["erro"]:

            return (
                None,
                None,
                resultado["erro"]
            )


        strikes = resultado[
            "data"
        ]


        if not isinstance(
            strikes,
            dict
        ) or not strikes:

            return (
                None,
                None,
                "sem_strikes"
            )


        candidatos = []


        for preco, lados in strikes.items():

            try:

                lado = lados.get(
                    direcao
                )

                if not lado:
                    continue


                instrument_id = lado.get(
                    "id"
                )

                profit = lado.get(
                    "profit"
                )


                if not instrument_id:
                    continue


                try:

                    preco_float = float(
                        preco
                    )

                except Exception:

                    preco_float = 0


                candidatos.append({

                    "preco": preco,

                    "preco_float": preco_float,

                    "id": instrument_id,

                    "profit": profit
                })

            except Exception:

                continue


        if not candidatos:

            return (
                None,
                None,
                "sem_instrumento_direcao"
            )


        # ====================================================
        # ESCOLHER STRIKE MAIS PRÓXIMO DO PREÇO ATUAL
        # ====================================================

        preco_atual = None


        try:

            candles = obter_candles(
                ativo,
                60,
                3
            )

            if candles:

                preco_atual = candles[
                    -1
                ][
                    "close"
                ]

        except Exception:

            pass


        if preco_atual is not None:

            candidatos.sort(
                key=lambda x: abs(
                    x["preco_float"]
                    - preco_atual
                )
            )

        else:

            # Sem preço atual, escolhe o strike
            # central em vez de um extremo.
            candidatos.sort(
                key=lambda x: x[
                    "preco_float"
                ]
            )

            meio = (
                len(candidatos)
                // 2
            )

            candidato = candidatos[
                meio
            ]

            log.info(
                "INSTRUMENTO REAL | %s | M%s | %s | strike=%s | profit=%s | id=%s",
                ativo,
                expiracao,
                direcao.upper(),
                candidato["preco"],
                candidato["profit"],
                candidato["id"]
            )

            return (
                candidato["id"],
                candidato["profit"],
                None
            )


        candidato = candidatos[0]


        log.info(
            "INSTRUMENTO REAL | %s | M%s | %s | strike=%s | profit=%s | id=%s",
            ativo,
            expiracao,
            direcao.upper(),
            candidato["preco"],
            candidato["profit"],
            candidato["id"]
        )


        return (
            candidato["id"],
            candidato["profit"],
            None
        )


    except Exception as e:

        log.warning(
            "ERRO STRIKE | %s | M%s | %s",
            ativo,
            expiracao,
            e
        )

        return (
            None,
            None,
            str(e)
        )


# ============================================================
# ORDEM DIGITAL V6
# ============================================================

def executar_ordem(
    banca,
    ativo,
    valor,
    direcao,
    expiracao
):

    if not EXECUTAR_ORDENS:

        return (
            False,
            "EXECUTAR_ORDENS=false"
        )


    if esta_bloqueado(
        ativo,
        expiracao
    ):

        return (
            False,
            "ativo_bloqueado"
        )


    instrument_id, profit, erro = (
        obter_instrumento_digital(
            ativo,
            direcao,
            expiracao
        )
    )


    if not instrument_id:

        log.warning(
            "SEM INSTRUMENTO | %s | M%s | %s",
            ativo,
            expiracao,
            erro
        )

        bloquear(
            ativo,
            expiracao,
            erro
        )

        return (
            False,
            erro
        )


    if not garantir_practice():

        return (
            False,
            "sem_conexao"
        )


    log.info(
        "COMPRANDO DIGITAL | %s | %s | M%s | %s | $%.2f",
        banca,
        ativo,
        expiracao,
        direcao.upper(),
        valor
    )


    try:

        with api_lock:

            resposta = api.buy_digital(
                float(valor),
                instrument_id
            )


        log.info(
            "RESPOSTA BUY_DIGITAL | %s | %s",
            ativo,
            resposta
        )


        ok = False
        order_id = None


        if isinstance(
            resposta,
            (tuple, list)
        ):

            if len(resposta) >= 1:

                ok = bool(
                    resposta[0]
                )

            if len(resposta) >= 2:

                order_id = resposta[1]


        if (
            ok
            and order_id
        ):

            with estado_lock:

                stats[
                    "ordens_aceitas"
                ] += 1

                estado_frentes[
                    banca
                ][
                    "ordens_aceitas"
                ] += 1


            log.info(
                "ORDEM ACEITA !!! | %s | %s | M%s | id=%s",
                banca,
                ativo,
                expiracao,
                order_id
            )


            return (
                True,
                order_id
            )


        with estado_lock:

            stats[
                "ordens_recusadas"
            ] += 1

            estado_frentes[
                banca
            ][
                "ordens_recusadas"
            ] += 1


        bloquear(
            ativo,
            expiracao,
            str(resposta)
        )


        return (
            False,
            str(resposta)
        )


    except Exception as e:

        log.warning(
            "BUY_DIGITAL ERRO | %s | %s",
            ativo,
            e
        )


        bloquear(
            ativo,
            expiracao,
            str(e)
        )


        return (
            False,
            str(e)
        )


# ============================================================
# RESULTADO
# ============================================================

def aguardar_resultado(
    order_id
):

    inicio = time.time()

    while (
        time.time() - inicio
        < 900
    ):

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

                fechado = resposta[0]

                valor = resposta[1]


                if fechado:

                    try:

                        lucro = float(
                            valor
                        )

                    except Exception:

                        lucro = 0


                    if lucro > 0:

                        return (
                            "win",
                            lucro
                        )

                    elif lucro < 0:

                        return (
                            "loss",
                            lucro
                        )

                    else:

                        return (
                            "draw",
                            0
                        )


        except Exception as e:

            log.debug(
                "CHECK RESULTADO | %s",
                e
            )


        time.sleep(1)


    return (
        "timeout",
        0
    )


# ============================================================
# TAXA
# ============================================================

def taxa(
    wins,
    losses
):

    total = (
        wins
        + losses
    )

    if total == 0:
        return 0

    return round(
        wins / total * 100,
        2
    )


# ============================================================
# RESULTADOS
# ============================================================

def registrar_win(
    banca,
    nivel
):

    with estado_lock:

        stats[
            "wins"
        ] += 1

        estado_frentes[
            banca
        ][
            "wins"
        ] += 1


        if nivel == 0:

            stats[
                "win_direto"
            ] += 1

            estado_frentes[
                banca
            ][
                "win_direto"
            ] += 1


        elif nivel == 1:

            stats[
                "win_g1"
            ] += 1

            estado_frentes[
                banca
            ][
                "win_g1"
            ] += 1


        elif nivel == 2:

            stats[
                "win_g2"
            ] += 1

            estado_frentes[
                banca
            ][
                "win_g2"
            ] += 1


def registrar_loss(
    banca
):

    with estado_lock:

        stats[
            "losses"
        ] += 1

        estado_frentes[
            banca
        ][
            "losses"
        ] += 1


# ============================================================
# CICLO
# ============================================================

def ciclo(
    banca,
    ativo,
    direcao,
    score,
    timeframe_analise,
    expiracao
):

    valores = [

        ENTRADA_BASE,

        ENTRADA_BASE * 2,

        ENTRADA_BASE * 4
    ]


    try:

        with estado_lock:

            estado_frentes[
                banca
            ][
                "ocupada"
            ] = True

            estado_frentes[
                banca
            ][
                "ativo"
            ] = ativo

            estado_frentes[
                banca
            ][
                "direcao"
            ] = direcao

            estado_frentes[
                banca
            ][
                "score"
            ] = score


        for nivel, valor in enumerate(
            valores
        ):


            ok, order_id = executar_ordem(
                banca,
                ativo,
                valor,
                direcao,
                expiracao
            )


            # ================================================
            # NÃO HOUVE APOSTA
            # ================================================

            if not ok:

                log.warning(
                    "%s | ORDEM NÃO EXECUTADA | %s",
                    banca,
                    order_id
                )

                # Não conta como LOSS.
                return


            with estado_lock:

                estado_frentes[
                    banca
                ][
                    "order_id"
                ] = order_id


            # ================================================
            # SINAL SOMENTE DEPOIS DA ORDEM ACEITA
            # ================================================

            if nivel == 0:

                mercado = (
                    "OTC"
                    if ativo.endswith(
                        "-OTC"
                    )
                    else "NORMAL"
                )


                telegram(
                    "📊 NOVO SINAL\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | {mercado}\n"
                    f"📈 Análise: M{timeframe_analise}\n"
                    f"⏱ Digital: M{expiracao}\n"
                    f"🎯 {direcao.upper()}\n"
                    f"🔥 Score: {score}/100\n"
                    f"💰 ${valores[0]:.2f} → "
                    f"G1 ${valores[1]:.2f} → "
                    f"G2 ${valores[2]:.2f}"
                )


                with estado_lock:

                    estado_frentes[
                        banca
                    ][
                        "ultimo_sinal"
                    ] = {

                        "ativo": ativo,

                        "direcao": direcao,

                        "score": score,

                        "analise": timeframe_analise,

                        "expiracao": expiracao,

                        "data": datetime.now(
                            timezone.utc
                        ).isoformat()
                    }


            resultado, lucro = (
                aguardar_resultado(
                    order_id
                )
            )


            log.info(
                "RESULTADO | %s | %s | nivel=%s | %s | lucro=%s",
                banca,
                ativo,
                nivel,
                resultado,
                lucro
            )


            # ================================================
            # WIN
            # ================================================

            if resultado == "win":

                registrar_win(
                    banca,
                    nivel
                )


                if nivel == 0:

                    nome = "WIN"

                elif nivel == 1:

                    nome = "WIN G1"

                else:

                    nome = "WIN G2"


                w = stats["wins"]
                l = stats["losses"]


                telegram(
                    f"✅ {nome}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
                )


                return


            # ================================================
            # DRAW/TIMEOUT
            # ================================================

            if resultado in (
                "draw",
                "timeout"
            ):

                log.warning(
                    "%s | %s | ciclo encerrado sem LOSS",
                    banca,
                    resultado
                )

                return


            # ================================================
            # LOSS - tenta próximo Gale
            # ================================================

            if (
                resultado == "loss"
                and nivel < 2
            ):

                log.info(
                    "%s | LOSS nível %s | iniciando próximo Gale",
                    banca,
                    nivel
                )

                time.sleep(1)

                continue


            # ================================================
            # LOSS FINAL
            # ================================================

            if (
                resultado == "loss"
                and nivel == 2
            ):

                registrar_loss(
                    banca
                )


                w = stats["wins"]
                l = stats["losses"]


                telegram(
                    "❌ LOSS\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
                )


                return


    finally:

        with estado_lock:

            ativos_em_uso.discard(
                ativo
            )

            estado_frentes[
                banca
            ][
                "ocupada"
            ] = False

            estado_frentes[
                banca
            ][
                "ativo"
            ] = None

            estado_frentes[
                banca
            ][
                "direcao"
            ] = None

            estado_frentes[
                banca
            ][
                "score"
            ] = None

            estado_frentes[
                banca
            ][
                "order_id"
            ] = None


# ============================================================
# SCANNER
# ============================================================

def scanner(
    banca
):

    cfg = FRENTES[
        banca
    ]

    tf = cfg[
        "timeframe_analise"
    ]

    segundos = cfg[
        "segundos"
    ]

    expiracao = cfg[
        "expiracao"
    ]


    log.info(
        "%s INICIADA | análise=M%s | digital=M%s",
        banca,
        tf,
        expiracao
    )


    while True:

        try:

            if not garantir_conexao():

                time.sleep(10)

                continue


            melhor = None

            analisados = 0


            log.info(
                "%s | VARREDURA | análise M%s | Digital M%s",
                banca,
                tf,
                expiracao
            )


            for ativo in ATIVOS:


                if esta_bloqueado(
                    ativo,
                    expiracao
                ):

                    continue


                with estado_lock:

                    if ativo in ativos_em_uso:
                        continue


                candles = obter_candles(
                    ativo,
                    segundos,
                    100
                )


                if len(candles) < 60:

                    time.sleep(
                        INTERVALO_ENTRE_ATIVOS
                    )

                    continue


                analisados += 1


                resultado = analisar(
                    candles
                )


                if resultado:

                    log.info(
                        "CANDIDATO | %s | %s | análise=M%s | digital=M%s | %s | score=%s",
                        banca,
                        ativo,
                        tf,
                        expiracao,
                        resultado[
                            "direcao"
                        ],
                        resultado[
                            "score"
                        ]
                    )


                    if (
                        melhor is None
                        or resultado[
                            "score"
                        ] > melhor[
                            "score"
                        ]
                    ):

                        melhor = {

                            "ativo": ativo,

                            "direcao": resultado[
                                "direcao"
                            ],

                            "score": resultado[
                                "score"
                            ],

                            "motivos": resultado[
                                "motivos"
                            ]
                        }


                time.sleep(
                    INTERVALO_ENTRE_ATIVOS
                )


            log.info(
                "%s | FIM VARREDURA | válidos=%s | melhor=%s",
                banca,
                analisados,
                (
                    melhor["ativo"]
                    if melhor
                    else "nenhum"
                )
            )


            if melhor:

                ativo = melhor[
                    "ativo"
                ]


                with estado_lock:

                    if ativo in ativos_em_uso:

                        time.sleep(
                            INTERVALO_ANALISE
                        )

                        continue


                    ativos_em_uso.add(
                        ativo
                    )


                log.info(
                    "SINAL APROVADO | %s | %s | análise=M%s | digital=M%s | %s | score=%s",
                    banca,
                    ativo,
                    tf,
                    expiracao,
                    melhor["direcao"],
                    melhor["score"]
                )


                ciclo(
                    banca,
                    ativo,
                    melhor["direcao"],
                    melhor["score"],
                    tf,
                    expiracao
                )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception as e:

            log.exception(
                "SCANNER ERRO | %s | %s",
                banca,
                e
            )

            time.sleep(5)


# ============================================================
# WORKER
# ============================================================

def worker():

    log.info(
        "WORKER V6 INICIADO"
    )


    while not conectar():

        time.sleep(10)


    for indice, banca in enumerate(
        FRENTES
    ):


        def iniciar(
            nome=banca,
            atraso=indice * 3
        ):

            time.sleep(
                atraso
            )

            scanner(
                nome
            )


        threading.Thread(
            target=iniciar,
            daemon=True,
            name=f"scanner-{banca}"
        ).start()


# ============================================================
# STATUS WEB
# ============================================================

@app.route("/")
def home():

    saldo = None

    try:

        if conectado():

            saldo = api.get_balance()

    except Exception:

        pass


    with estado_lock:

        frentes = {}


        for nome, dados in estado_frentes.items():

            frentes[nome] = {

                **dados,

                "assertividade": taxa(
                    dados["wins"],
                    dados["losses"]
                )
            }


        geral = {

            **stats,

            "assertividade": taxa(
                stats["wins"],
                stats["losses"]
            )
        }


    return jsonify({

        "versao": VERSAO,

        "status": "online",

        "iq_conectada": conectado(),

        "conta": "PRACTICE",

        "saldo": saldo,

        "execucao": EXECUTAR_ORDENS,

        "score_min": SCORE_MIN,

        "gestao": {

            "entrada": ENTRADA_BASE,

            "g1": ENTRADA_BASE * 2,

            "g2": ENTRADA_BASE * 4
        },

        "frequencias": {

            "BANCA 1": "M1 -> Digital M1",

            "BANCA 2": "M5 -> Digital M5",

            "BANCA 3": "M15 -> Digital M5"
        },

        "estatisticas": geral,

        "frentes": frentes,

        "ativos_com_candles": sorted(
            ativos_validos
        ),

        "bloqueados": len(
            bloqueados
        )
    })


@app.route("/health")
def health():

    return jsonify({

        "status": "ok",

        "versao": VERSAO

    }), 200


# ============================================================
# START
# ============================================================

log.info(
    "==============================================="
)

log.info(
    VERSAO
)

log.info(
    "CONTA = PRACTICE"
)

log.info(
    "EXECUTAR_ORDENS = %s",
    EXECUTAR_ORDENS
)

log.info(
    "ENTRADA = %.2f -> %.2f -> %.2f",
    ENTRADA_BASE,
    ENTRADA_BASE * 2,
    ENTRADA_BASE * 4
)

log.info(
    "MÉTODO = strike list + instrument_id + buy_digital"
)

log.info(
    "==============================================="
)


threading.Thread(
    target=worker,
    daemon=True,
    name="worker-iq"
).start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
