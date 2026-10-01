import os
import time
import math
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# VERSÃO
# ============================================================

VERSAO = "IQ-3-FRENTES-V5-SEM-OPEN-TIME"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("iq-bot")


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

ENTRADA_BASE = float(os.getenv("ENTRADA_BASE", "2"))
SCORE_MIN = int(os.getenv("SCORE_MIN", "65"))

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "20")
)

INTERVALO_ENTRE_ATIVOS = float(
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.8")
)

CONTA_PERMITIDA = "PRACTICE"

TEMPO_BLOQUEIO = 300


# ============================================================
# ATIVOS
#
# Não usamos get_all_open_time().
# Tentamos ativos conhecidos individualmente.
# Se a IQ não fornecer candles ou rejeitar a ordem,
# o ativo/timeframe é ignorado/bloqueado temporariamente.
# ============================================================

ATIVOS_NORMAL = [
    "EURUSD",
    "GBPUSD",
    "EURGBP",
    "USDJPY",
    "AUDUSD",
    "USDCHF",
    "EURJPY",
    "GBPJPY",
    "USDCAD",
    "AUDCAD",
    "AUDJPY",
    "EURCAD",
    "NZDUSD"
]

ATIVOS_OTC = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "EURGBP-OTC",
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "USDCHF-OTC",
    "EURJPY-OTC",
    "GBPJPY-OTC",
    "USDCAD-OTC",
    "AUDCAD-OTC",
    "AUDJPY-OTC",
    "EURCAD-OTC",
    "NZDUSD-OTC"
]

ATIVOS = ATIVOS_NORMAL + ATIVOS_OTC


# ============================================================
# 3 FRENTES
# ============================================================

FRENTES = {
    "BANCA 1": {
        "timeframe": 1,
        "segundos": 60
    },
    "BANCA 2": {
        "timeframe": 5,
        "segundos": 300
    },
    "BANCA 3": {
        "timeframe": 15,
        "segundos": 900
    }
}


# ============================================================
# ESTADO
# ============================================================

api = None

api_lock = threading.RLock()
connect_lock = threading.Lock()
estado_lock = threading.RLock()

ativos_em_uso = set()

# (ativo, timeframe) -> timestamp de liberação
bloqueados = {}

# Guarda quais ativos conseguiram retornar candles
ativos_validos = set()

stats_global = {
    "wins": 0,
    "losses": 0,
    "win_direto": 0,
    "win_g1": 0,
    "win_g2": 0,
    "ordens_recusadas": 0
}

estado_frentes = {}

for banca in FRENTES:
    estado_frentes[banca] = {
        "ocupada": False,
        "ativo": None,
        "direcao": None,
        "score": None,
        "nivel": None,
        "order_id": None,

        "wins": 0,
        "losses": 0,

        "win_direto": 0,
        "win_g1": 0,
        "win_g2": 0,

        "ordens_recusadas": 0,
        "ultimo_sinal": None
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        r = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto
            },
            timeout=10
        )

        return r.ok

    except Exception as e:

        log.warning(
            "TELEGRAM ERRO | %s",
            e
        )

        return False


# ============================================================
# CONEXÃO
# ============================================================

def conectado():

    global api

    try:

        if api is None:
            return False

        with api_lock:
            return bool(api.check_connect())

    except Exception:
        return False


def conectar():

    global api

    if not IQ_EMAIL or not IQ_PASSWORD:

        log.error(
            "IQ_EMAIL/IQ_PASSWORD não configurados."
        )

        return False


    with connect_lock:

        # Double check depois de adquirir o lock.
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
            # CONTA DE TREINO OBRIGATÓRIA
            # ================================================

            nova.change_balance(
                CONTA_PERMITIDA
            )

            time.sleep(1)

            saldo = nova.get_balance()

            with api_lock:
                api = nova

            log.info(
                "IQ CONECTADA | PRACTICE | saldo=%s",
                saldo
            )

            telegram(
                "🤖 ROBÔ V5 ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: ${saldo}\n"
                "🏦 Banca 1: M1\n"
                "🏦 Banca 2: M5\n"
                "🏦 Banca 3: M15\n"
                f"🔥 Score mínimo: {SCORE_MIN}"
            )

            return True

        except Exception as e:

            log.exception(
                "ERRO CONEXÃO IQ | %s",
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

        with api_lock:

            api.change_balance(
                CONTA_PERMITIDA
            )

        return True

    except Exception as e:

        log.warning(
            "ERRO AO GARANTIR PRACTICE | %s",
            e
        )

        return False


# ============================================================
# BLOQUEIO DE INSTRUMENTO
# ============================================================

def chave_bloqueio(
    ativo,
    timeframe
):

    return (
        ativo,
        int(timeframe)
    )


def esta_bloqueado(
    ativo,
    timeframe
):

    chave = chave_bloqueio(
        ativo,
        timeframe
    )

    ate = bloqueados.get(chave)

    if ate is None:
        return False

    if time.time() >= ate:

        bloqueados.pop(
            chave,
            None
        )

        return False

    return True


def bloquear(
    ativo,
    timeframe,
    motivo
):

    bloqueados[
        chave_bloqueio(
            ativo,
            timeframe
        )
    ] = (
        time.time()
        + TEMPO_BLOQUEIO
    )

    log.warning(
        "BLOQUEADO | %s | M%s | %ss | %s",
        ativo,
        timeframe,
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

        agora = int(time.time())

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

        agora2 = int(time.time())

        for c in dados:

            try:

                inicio = int(
                    c.get("from", 0)
                )

                # Candle ainda em formação
                if inicio + segundos > agora2:
                    continue

                candles.append({
                    "from": inicio,
                    "open": float(c["open"]),
                    "close": float(c["close"]),
                    "max": float(c["max"]),
                    "min": float(c["min"])
                })

            except Exception:
                continue

        if len(candles) >= 60:

            with estado_lock:
                ativos_validos.add(ativo)

        return candles

    except Exception as e:

        texto = str(e)

        # Não fica imprimindo stack gigante
        # para ativo simplesmente inexistente.
        log.info(
            "ATIVO SEM CANDLES | %s | %s",
            ativo,
            texto
        )

        return []


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


def desvio_padrao(valores):

    if not valores:
        return 0

    m = media(valores)

    variancia = (
        sum(
            (x - m) ** 2
            for x in valores
        )
        / len(valores)
    )

    return math.sqrt(
        variancia
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

    k = 2 / (
        periodo + 1
    )

    for preco in valores[periodo:]:

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

        diferenca = (
            valores[i]
            - valores[i - 1]
        )

        if diferenca >= 0:

            ganhos.append(
                diferenca
            )

            perdas.append(0)

        else:

            ganhos.append(0)

            perdas.append(
                abs(diferenca)
            )


    ganho = media(ganhos)
    perda = media(perdas)

    if perda == 0:
        return 100

    rs = ganho / perda

    return (
        100
        - (
            100
            / (1 + rs)
        )
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
        return None

    return e12 - e26


# ============================================================
# SCORE
# ============================================================

def analisar_candles(candles):

    if len(candles) < 60:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    ultimo = candles[-1]

    preco = ultimo["close"]

    call = 0
    put = 0

    motivos_call = []
    motivos_put = []


    # ========================================================
    # EMA 20 / 50 = 20
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
            motivos_call.append("EMA")

        elif (
            e20 < e50
            and preco < e20
        ):

            put += 20
            motivos_put.append("EMA")


    # ========================================================
    # RSI = 15
    # ========================================================

    vrsi = rsi(
        closes
    )

    if vrsi is not None:

        if 50 <= vrsi <= 70:

            call += 15
            motivos_call.append("RSI")

        elif 30 <= vrsi < 50:

            put += 15
            motivos_put.append("RSI")


    # ========================================================
    # BOLLINGER = 10
    # ========================================================

    ult20 = closes[-20:]

    mm20 = media(ult20)

    dp = desvio_padrao(
        ult20
    )

    superior = (
        mm20 + 2 * dp
    )

    inferior = (
        mm20 - 2 * dp
    )

    if (
        preco > mm20
        and preco < superior
    ):

        call += 10
        motivos_call.append(
            "BOLLINGER"
        )

    elif (
        preco < mm20
        and preco > inferior
    ):

        put += 10
        motivos_put.append(
            "BOLLINGER"
        )


    # ========================================================
    # PRICE ACTION = 15
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
            corpo / amplitude
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
    # SUPORTE / RESISTÊNCIA = 10
    # ========================================================

    janela = candles[-20:-1]

    resistencia = max(
        c["max"]
        for c in janela
    )

    suporte = min(
        c["min"]
        for c in janela
    )

    distancia = (
        resistencia - suporte
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
    # BREAKOUT = 15
    # ========================================================

    janela_break = candles[-11:-1]

    max_ant = max(
        c["max"]
        for c in janela_break
    )

    min_ant = min(
        c["min"]
        for c in janela_break
    )

    if preco > max_ant:

        call += 15
        motivos_call.append(
            "BREAKOUT"
        )

    elif preco < min_ant:

        put += 15
        motivos_put.append(
            "BREAKOUT"
        )


    # ========================================================
    # MACD = 10
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
    # MOMENTUM = 5
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
    # DECISÃO
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

    # Evita sinal dividido
    if score - oposto < 15:
        return None


    return {
        "direcao": direcao,
        "score": score,
        "score_oposto": oposto,
        "motivos": motivos,
        "rsi": (
            round(vrsi, 2)
            if vrsi is not None
            else None
        )
    }


# ============================================================
# RESERVAR ATIVO
# ============================================================

def reservar_ativo(ativo):

    with estado_lock:

        if ativo in ativos_em_uso:
            return False

        ativos_em_uso.add(
            ativo
        )

        return True


def liberar_ativo(ativo):

    with estado_lock:

        ativos_em_uso.discard(
            ativo
        )


# ============================================================
# ORDEM
# ============================================================

def executar_ordem(
    ativo,
    valor,
    direcao,
    timeframe
):

    if not EXECUTAR_ORDENS:

        log.warning(
            "EXECUTAR_ORDENS=false"
        )

        return (
            False,
            "execucao_desativada"
        )


    if not garantir_practice():

        return (
            False,
            "sem_conexao"
        )


    if esta_bloqueado(
        ativo,
        timeframe
    ):

        return (
            False,
            "bloqueado"
        )


    log.info(
        "TENTANDO ORDEM | %s | M%s | %s | $%.2f",
        ativo,
        timeframe,
        direcao.upper(),
        valor
    )


    try:

        with api_lock:

            resposta = (
                api.buy_digital_spot_v2(
                    ativo,
                    float(valor),
                    direcao,
                    int(timeframe)
                )
            )


        log.info(
            "RESPOSTA ORDEM | %s | M%s | %s",
            ativo,
            timeframe,
            resposta
        )


        status = False
        order_id = None


        if isinstance(
            resposta,
            (tuple, list)
        ):

            if len(resposta) > 0:
                status = bool(
                    resposta[0]
                )

            if len(resposta) > 1:
                order_id = resposta[1]

        else:

            status = bool(
                resposta
            )


        if status and order_id:

            log.info(
                "ORDEM ACEITA | %s | M%s | id=%s",
                ativo,
                timeframe,
                order_id
            )

            return (
                True,
                order_id
            )


        stats_global[
            "ordens_recusadas"
        ] += 1


        motivo = str(
            order_id
        )


        log.warning(
            "ORDEM RECUSADA | %s | M%s | %s",
            ativo,
            timeframe,
            motivo
        )


        # Qualquer instrumento recusado fica
        # temporariamente fora da fila.
        bloquear(
            ativo,
            timeframe,
            motivo
        )


        return (
            False,
            motivo
        )


    except Exception as e:

        log.warning(
            "ERRO ORDEM | %s | M%s | %s",
            ativo,
            timeframe,
            e
        )

        bloquear(
            ativo,
            timeframe,
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
    order_id,
    timeout=1200
):

    inicio = time.time()

    while (
        time.time() - inicio
        < timeout
    ):

        if not garantir_conexao():

            time.sleep(2)
            continue

        try:

            with api_lock:

                resposta = (
                    api.check_win_digital_v2(
                        order_id
                    )
                )


            if isinstance(
                resposta,
                (tuple, list)
            ) and len(resposta) >= 2:

                terminou = resposta[0]
                valor = resposta[1]

                if terminou:

                    try:

                        lucro = float(
                            valor
                        )

                    except Exception:

                        lucro = 0.0


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


        except Exception as e:

            log.debug(
                "RESULTADO AGUARDANDO | %s | %s",
                order_id,
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
        wins + losses
    )

    if total == 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


# ============================================================
# REGISTRO
# ============================================================

def registrar_win(
    banca,
    nivel
):

    with estado_lock:

        stats_global[
            "wins"
        ] += 1

        estado_frentes[
            banca
        ][
            "wins"
        ] += 1


        if nivel == 0:

            stats_global[
                "win_direto"
            ] += 1

            estado_frentes[
                banca
            ][
                "win_direto"
            ] += 1


        elif nivel == 1:

            stats_global[
                "win_g1"
            ] += 1

            estado_frentes[
                banca
            ][
                "win_g1"
            ] += 1


        elif nivel == 2:

            stats_global[
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

        stats_global[
            "losses"
        ] += 1

        estado_frentes[
            banca
        ][
            "losses"
        ] += 1


# ============================================================
# CICLO $2 -> $4 -> $8
# ============================================================

def executar_ciclo(
    banca,
    ativo,
    direcao,
    score,
    timeframe
):

    valores = [
        ENTRADA_BASE,
        ENTRADA_BASE * 2,
        ENTRADA_BASE * 4
    ]

    niveis = [
        "ENTRADA",
        "G1",
        "G2"
    ]


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


    try:

        for nivel, valor in enumerate(
            valores
        ):

            with estado_lock:

                estado_frentes[
                    banca
                ][
                    "nivel"
                ] = nivel


            ok, order_id = executar_ordem(
                ativo,
                valor,
                direcao,
                timeframe
            )


            if not ok:

                with estado_lock:

                    estado_frentes[
                        banca
                    ][
                        "ordens_recusadas"
                    ] += 1


                log.warning(
                    "%s | %s NÃO EXECUTADA | %s",
                    banca,
                    niveis[nivel],
                    order_id
                )

                # Não conta como LOSS porque
                # dinheiro não entrou no mercado.
                return


            with estado_lock:

                estado_frentes[
                    banca
                ][
                    "order_id"
                ] = order_id


            # ================================================
            # TELEGRAM APENAS APÓS A ORDEM SER ACEITA
            # ================================================

            if nivel == 0:

                mercado = (
                    "OTC"
                    if ativo.endswith("-OTC")
                    else "NORMAL"
                )

                telegram(
                    f"📊 NOVO SINAL\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | {mercado}\n"
                    f"⏱ M{timeframe}\n"
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
                        "timeframe": timeframe,
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
                "RESULTADO | %s | %s | M%s | %s | %s | lucro=%s",
                banca,
                ativo,
                timeframe,
                niveis[nivel],
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
                    nome_resultado = "WIN"

                elif nivel == 1:
                    nome_resultado = "WIN G1"

                else:
                    nome_resultado = "WIN G2"


                w = stats_global[
                    "wins"
                ]

                l = stats_global[
                    "losses"
                ]


                telegram(
                    f"✅ {nome_resultado}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | M{timeframe}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: {taxa(w, l)}%"
                )

                return


            # ================================================
            # DRAW
            # ================================================

            if resultado == "draw":

                log.info(
                    "DRAW | %s | %s",
                    banca,
                    ativo
                )

                return


            # ================================================
            # TIMEOUT
            # ================================================

            if resultado == "timeout":

                log.warning(
                    "TIMEOUT RESULTADO | %s | %s",
                    banca,
                    ativo
                )

                return


            # ================================================
            # LOSS - vai para Gale
            # ================================================

            if nivel < 2:

                log.info(
                    "%s | LOSS %s | próximo=%s",
                    banca,
                    niveis[nivel],
                    niveis[nivel + 1]
                )

                time.sleep(1)

                continue


            # ================================================
            # LOSS COMPLETO
            # ================================================

            registrar_loss(
                banca
            )

            w = stats_global[
                "wins"
            ]

            l = stats_global[
                "losses"
            ]


            telegram(
                f"❌ LOSS\n"
                f"🏦 {banca}\n"
                f"💱 {ativo} | M{timeframe}\n"
                f"📊 {w} WIN / {l} LOSS\n"
                f"🎯 Assertividade: {taxa(w, l)}%"
            )

            return


    finally:

        liberar_ativo(
            ativo
        )

        with estado_lock:

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
                "nivel"
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

    config = FRENTES[
        banca
    ]

    timeframe = config[
        "timeframe"
    ]

    segundos = config[
        "segundos"
    ]


    log.info(
        "%s INICIADA | M%s",
        banca,
        timeframe
    )


    while True:

        try:

            if not garantir_conexao():

                log.warning(
                    "%s | aguardando conexão...",
                    banca
                )

                time.sleep(10)
                continue


            with estado_lock:

                ocupada = (
                    estado_frentes[
                        banca
                    ][
                        "ocupada"
                    ]
                )


            if ocupada:

                time.sleep(2)
                continue


            log.info(
                "%s | M%s | INICIANDO VARREDURA | %d ativos",
                banca,
                timeframe,
                len(ATIVOS)
            )


            melhor = None

            analisados = 0


            for ativo in ATIVOS:

                # ============================================
                # Já bloqueado nesse timeframe
                # ============================================

                if esta_bloqueado(
                    ativo,
                    timeframe
                ):
                    continue


                # ============================================
                # Outra banca usando
                # ============================================

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


                analise = analisar_candles(
                    candles
                )


                if analise is not None:

                    log.info(
                        "CANDIDATO | %s | %s | M%s | %s | score=%s | oposto=%s",
                        banca,
                        ativo,
                        timeframe,
                        analise["direcao"],
                        analise["score"],
                        analise["score_oposto"]
                    )


                    if (
                        melhor is None
                        or analise["score"]
                        > melhor["score"]
                    ):

                        melhor = {
                            "ativo": ativo,
                            "direcao": analise[
                                "direcao"
                            ],
                            "score": analise[
                                "score"
                            ],
                            "score_oposto": analise[
                                "score_oposto"
                            ],
                            "motivos": analise[
                                "motivos"
                            ]
                        }


                time.sleep(
                    INTERVALO_ENTRE_ATIVOS
                )


            log.info(
                "%s | M%s | VARREDURA CONCLUÍDA | válidos=%s | candidato=%s",
                banca,
                timeframe,
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


                if reservar_ativo(
                    ativo
                ):

                    log.info(
                        "SINAL APROVADO | %s | %s | M%s | %s | score=%s | motivos=%s",
                        banca,
                        ativo,
                        timeframe,
                        melhor["direcao"],
                        melhor["score"],
                        ",".join(
                            melhor["motivos"]
                        )
                    )


                    executar_ciclo(
                        banca,
                        ativo,
                        melhor["direcao"],
                        melhor["score"],
                        timeframe
                    )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception as e:

            log.exception(
                "ERRO SCANNER | %s | %s",
                banca,
                e
            )

            time.sleep(5)


# ============================================================
# WORKER PRINCIPAL
#
# IMPORTANTE:
# Flask/Gunicorn inicia primeiro.
# A IQ roda em thread separada.
# Isso evita o problema "No open ports detected".
# ============================================================

def worker_principal():

    log.info(
        "WORKER IQ INICIADO"
    )

    while not conectar():

        log.warning(
            "IQ indisponível. Nova tentativa em 10s."
        )

        time.sleep(10)


    atrasos = {
        "BANCA 1": 0,
        "BANCA 2": 3,
        "BANCA 3": 6
    }


    for banca in FRENTES:

        def iniciar_banca(
            nome=banca
        ):

            time.sleep(
                atrasos[nome]
            )

            scanner(
                nome
            )


        threading.Thread(
            target=iniciar_banca,
            daemon=True,
            name=f"scanner-{banca}"
        ).start()


# ============================================================
# WEB
# ============================================================

@app.route("/")
def home():

    saldo = None

    try:

        if conectado():

            with api_lock:
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


        estatisticas = {
            **stats_global,
            "assertividade": taxa(
                stats_global["wins"],
                stats_global["losses"]
            )
        }


        validos = sorted(
            ativos_validos
        )


    return jsonify({

        "versao": VERSAO,

        "status": "online",

        "iq_conectada": conectado(),

        "conta": CONTA_PERMITIDA,

        "saldo": saldo,

        "execucao": EXECUTAR_ORDENS,

        "score_min": SCORE_MIN,

        "entrada": ENTRADA_BASE,

        "g1": ENTRADA_BASE * 2,

        "g2": ENTRADA_BASE * 4,

        "ativos_configurados": len(
            ATIVOS
        ),

        "ativos_com_candles": validos,

        "ativos_em_uso": sorted(
            ativos_em_uso
        ),

        "bloqueados": len(
            bloqueados
        ),

        "estatisticas": estatisticas,

        "frentes": frentes

    })


@app.route("/health")
def health():

    # Não depende da IQ para responder 200.
    # Assim Render consegue verificar o serviço
    # mesmo se a API externa estiver lenta.

    return jsonify({
        "status": "ok",
        "versao": VERSAO
    }), 200


# ============================================================
# INICIALIZAÇÃO
# ============================================================

log.info(
    "================================================"
)

log.info(
    "%s",
    VERSAO
)

log.info(
    "CONTA TRAVADA = PRACTICE"
)

log.info(
    "EXECUTAR_ORDENS = %s",
    EXECUTAR_ORDENS
)

log.info(
    "GESTÃO = $%.2f -> $%.2f -> $%.2f",
    ENTRADA_BASE,
    ENTRADA_BASE * 2,
    ENTRADA_BASE * 4
)

log.info(
    "SCORE_MIN = %s",
    SCORE_MIN
)

log.info(
    "get_all_open_time = DESATIVADO"
)

log.info(
    "================================================"
)


# A inicialização da IQ NÃO bloqueia mais
# a inicialização do Gunicorn.
threading.Thread(
    target=worker_principal,
    daemon=True,
    name="worker-iq"
).start()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
