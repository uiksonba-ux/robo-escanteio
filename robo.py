import os
import time
import math
import logging
import threading
import requests

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# VERSÃO
# ============================================================

VERSAO = "IQ-V10-10-BANCAS-X3-2C-M1-M5-M15-DIGITAL"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-v10")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# SEGURANÇA
# ============================================================

# CONTA FIXA DE TREINO.
# NÃO transformar em variável de ambiente.

CONTA = "PRACTICE"

EXECUTAR_ORDENS = (
    os.getenv("EXECUTAR_ORDENS", "false").lower() == "true"
)


# ============================================================
# ESTRATÉGIA
# ============================================================

# ESTRATÉGIA VIGENTE MANTIDA SEM ALTERAÇÕES

SCORE_MIN = int(
    os.getenv("SCORE_MIN", "65")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "20")
)

INTERVALO_ENTRE_ATIVOS = float(
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.7")
)

TEMPO_BLOQUEIO = int(
    os.getenv("TEMPO_BLOQUEIO", "300")
)


# ============================================================
# HORÁRIO BRASIL
# ============================================================

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora_brasil():
    return datetime.now(FUSO_BRASIL)


def obter_entrada_base():
    """
    Entrada base fixa de R$ 2,00.
    """
    return 2.00


# ============================================================
# NOVA GESTÃO
# ============================================================

# Ciclo 1:
# entrada + G1 + G2
#
# Se ocorrer LOSS completo:
# entrada do Ciclo 2 =
# entrada do Ciclo 1 + 10% do LOSS do Ciclo 1
#
# Ciclo 2:
# entrada + G1 + G2
#
# Se o Ciclo 2 perder:
# - permanece no Ciclo 2
# - NÃO aumenta a entrada
# - NÃO cria Ciclo 3
#
# Repete até:
# - recuperar todo prejuízo; ou
# - completar 10 sinais de recuperação.

RECUPERACAO_PERCENTUAL = float(
    os.getenv(
        "RECUPERACAO_PERCENTUAL",
        "0.10"
    )
)

MAX_SINAIS_RECUPERACAO = int(
    os.getenv(
        "MAX_SINAIS_RECUPERACAO",
        "10"
    )
)

MAX_CICLOS_GESTAO = 2


# ============================================================
# GALE X3 - 2 GALES EM AMBOS OS CICLOS
# ============================================================

GALES_POR_CICLO = {
    1: 2,
    2: 2,
}


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAMES = {

    1: {
        "segundos": 60,
        "expiracao": 1,
    },

    5: {
        "segundos": 300,
        "expiracao": 5,
    },

    15: {
        "segundos": 900,
        "expiracao": 5,
    },
}


# ============================================================
# BANCAS
# ============================================================

BANCAS = [
    "BANCA 1",
    "BANCA 2",
    "BANCA 3",
    "BANCA 4",
    "BANCA 5",
    "BANCA 6",
    "BANCA 7",
    "BANCA 8",
    "BANCA 9",
    "BANCA 10",
]


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
# ESTADO GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()
connect_lock = threading.Lock()
estado_lock = threading.RLock()

ativos_em_uso = set()

bloqueados = {}

ativos_validos = set()


# ============================================================
# ESTATÍSTICAS
# ============================================================

stats = {

    "wins": 0,
    "losses": 0,

    "win_direto": 0,

    "win_g1": 0,
    "win_g2": 0,
    "win_g3": 0,
    "win_g4": 0,
    "win_g5": 0,

    "ordens_aceitas": 0,
    "ordens_recusadas": 0,
}


# ============================================================
# ESTADO DAS 10 BANCAS
# ============================================================

estado_frentes = {

    banca: {

        "ocupada": False,

        "ativo": None,
        "direcao": None,
        "score": None,

        "timeframe": None,
        "expiracao": None,

        "order_id": None,

        "wins": 0,
        "losses": 0,

        "win_direto": 0,

        "win_g1": 0,
        "win_g2": 0,
        "win_g3": 0,
        "win_g4": 0,
        "win_g5": 0,

        "ordens_aceitas": 0,
        "ordens_recusadas": 0,

        "ultimo_sinal": None,

        # Gestão individual
        "ciclo_gestao": 1,

        "entrada_atual": obter_entrada_base(),

        "prejuizo_acumulado": 0.0,

        "em_recuperacao": False,

        "sinais_recuperacao": 0,
    }

    for banca in BANCAS
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
                "text": texto,
            },

            timeout=10,
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

        return (
            api is not None
            and bool(api.check_connect())
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

            # PRACTICE OBRIGATÓRIO

            nova.change_balance(
                "PRACTICE"
            )

            time.sleep(1)

            saldo = nova.get_balance()

            api = nova

            entrada = obter_entrada_base()

            log.info(
                "IQ CONECTADA | PRACTICE | saldo=%s",
                saldo
            )

            telegram(
                "🤖 ROBÔ V10 ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: {saldo}\n"
                f"💵 Entrada base: {entrada:.2f}\n\n"
                "🏦 10 BANCAS INDEPENDENTES\n"
                "⏱ M1 + M5 + M15\n"
                "📊 SOMENTE DIGITAL\n"
                "📈 GALE X3\n"
                "🛡 2 GALES POR CICLO\n"
                "🔁 2 CICLOS\n"
                "♻️ RECUPERAÇÃO 10%\n\n"
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

    return (
        conectado()
        or conectar()
    )


def garantir_practice():

    if not garantir_conexao():
        return False

    try:

        with api_lock:

            api.change_balance(
                "PRACTICE"
            )

        return True

    except Exception as e:

        log.warning(
            "ERRO AO GARANTIR PRACTICE | %s",
            e
        )

        return False


def obter_saldo_atual():

    try:

        if not garantir_practice():
            return None

        with api_lock:

            saldo = api.get_balance()

        return round(
            float(saldo),
            2
        )

    except Exception as e:

        log.warning(
            "ERRO AO CONSULTAR SALDO | %s",
            e
        )

        return None


def texto_saldo():

    saldo = obter_saldo_atual()

    if saldo is None:
        return "indisponível"

    return f"{saldo:.2f}"


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
        "BLOQUEADO | %s | "
        "DIGITAL M%s | %ss | %s",

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
                    c.get("from", 0)
                )

                if (
                    inicio + segundos
                    > agora
                ):
                    continue

                candles.append({

                    "from":
                        inicio,

                    "open":
                        float(c["open"]),

                    "close":
                        float(c["close"]),

                    "max":
                        float(c["max"]),

                    "min":
                        float(c["min"]),
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

    m = media(lista)

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

        d = (
            valores[i]
            - valores[i - 1]
        )

        if d >= 0:

            ganhos.append(d)
            perdas.append(0)

        else:

            ganhos.append(0)
            perdas.append(
                abs(d)
            )

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
        return None

    return (
        e12 - e26
    )


# ============================================================
# ESTRATÉGIA ATUAL - SEM ALTERAÇÃO
# ============================================================

def analisar(candles):

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


    # EMA 20 / 50 = 20

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


    # RSI = 15

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


    # BOLLINGER = 10

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


    # PRICE ACTION = 15

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


    # SUPORTE / RESISTÊNCIA = 10

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


    # BREAKOUT = 15

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


    # MACD = 10

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


    # MOMENTUM = 5

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


    # DECISÃO ATUAL

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


    if (
        score < SCORE_MIN
        or score - oposto < 15
    ):

        return None


    return {

        "direcao":
            direcao,

        "score":
            score,

        "oposto":
            oposto,

        "motivos":
            motivos,
    }


# ============================================================
# ORDEM DIGITAL
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


    # PRACTICE OBRIGATÓRIO

    if not garantir_practice():

        return (
            False,
            "sem_conexao"
        )


    log.info(
        "BUY DIGITAL | "
        "%s | %s | M%s | %s | %.2f",

        banca,
        ativo,
        expiracao,
        direcao.upper(),
        valor
    )


    try:

        metodo = getattr(
            api,
            "buy_digital_spot_v2",
            None
        )

        nome_metodo = (
            "buy_digital_spot_v2"
        )


        if not callable(
            metodo
        ):

            metodo = getattr(
                api,
                "buy_digital_spot",
                None
            )

            nome_metodo = (
                "buy_digital_spot"
            )


        if not callable(
            metodo
        ):

            return (
                False,
                "biblioteca_sem_buy_digital_spot"
            )


        with api_lock:

            resposta = metodo(
                ativo,
                float(valor),
                direcao.lower(),
                int(expiracao),
            )


        log.info(
            "RESPOSTA %s | %s | %s",
            nome_metodo,
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

                order_id = (
                    resposta[1]
                )


        elif (
            isinstance(resposta, int)
            and not isinstance(resposta, bool)
        ):

            ok = resposta > 0
            order_id = resposta


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


        motivo = str(
            order_id
            if order_id is not None
            else resposta
        )


        bloquear(
            ativo,
            expiracao,
            motivo
        )


        return (
            False,
            motivo
        )


    except Exception as e:

        log.exception(
            "ERRO ORDEM DIGITAL | "
            "%s | M%s | %s",

            ativo,
            expiracao,
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
# RESULTADO DIGITAL
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


                    if lucro < 0:

                        return (
                            "loss",
                            lucro
                        )


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
# ESTATÍSTICAS
# ============================================================

def taxa(
    wins,
    losses
):

    total = (
        wins + losses
    )

    if total == 0:
        return 0

    return round(
        wins / total * 100,
        2
    )


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


        chave = (
            "win_direto"
            if nivel == 0
            else f"win_g{nivel}"
        )


        if chave in stats:

            stats[
                chave
            ] += 1


        if chave in estado_frentes[banca]:

            estado_frentes[
                banca
            ][
                chave
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
# GALE X3
# ============================================================

def valores_do_ciclo(
    entrada,
    quantidade_gales
):

    return [

        round(
            float(entrada)
            * (3 ** nivel),
            2
        )

        for nivel in range(
            quantidade_gales + 1
        )
        ]
    # ============================================================
# SINCRONIZA ENTRADA BASE
# ============================================================

def sincronizar_entrada_base_do_dia(
    banca
):

    entrada_base = (
        obter_entrada_base()
    )

    with estado_lock:

        dados = (
            estado_frentes[banca]
        )

        if (
            not dados["em_recuperacao"]
            and int(
                dados["ciclo_gestao"]
            ) == 1
        ):

            dados[
                "entrada_atual"
            ] = round(
                entrada_base,
                2
            )


# ============================================================
# RESET GESTÃO
# ============================================================

def resetar_gestao(
    banca,
    motivo
):

    entrada_base = (
        obter_entrada_base()
    )

    with estado_lock:

        dados = (
            estado_frentes[banca]
        )

        dados[
            "ciclo_gestao"
        ] = 1

        dados[
            "entrada_atual"
        ] = round(
            entrada_base,
            2
        )

        dados[
            "prejuizo_acumulado"
        ] = 0.0

        dados[
            "em_recuperacao"
        ] = False

        dados[
            "sinais_recuperacao"
        ] = 0

    log.info(
        "GESTÃO RESETADA | "
        "%s | entrada=%.2f | %s",

        banca,
        entrada_base,
        motivo
    )


# ============================================================
# LOSS COMPLETO DO CICLO
# ============================================================

def aplicar_loss_ciclo(
    banca,
    perda_ciclo
):

    perda_ciclo = round(
        abs(float(perda_ciclo)),
        2
    )

    with estado_lock:

        dados = (
            estado_frentes[banca]
        )

        ciclo_atual = int(
            dados[
                "ciclo_gestao"
            ]
        )

        # Soma o LOSS ao prejuízo total
        # que ainda precisa ser recuperado.

        dados[
            "prejuizo_acumulado"
        ] = round(
            dados[
                "prejuizo_acumulado"
            ]
            + perda_ciclo,
            2
        )

        dados[
            "em_recuperacao"
        ] = True


        # ====================================================
        # LOSS NO CICLO 1
        #
        # Calcula 10% SOMENTE aqui.
        #
        # Exemplo:
        # 2 + 6 + 18 = 26
        # 10% = 2,60
        # próxima entrada = 4,60
        # ====================================================

        if ciclo_atual == 1:

            acrescimo = round(
                perda_ciclo
                * RECUPERACAO_PERCENTUAL,
                2
            )

            dados[
                "entrada_atual"
            ] = round(
                float(
                    dados[
                        "entrada_atual"
                    ]
                )
                + acrescimo,
                2
            )

            dados[
                "ciclo_gestao"
            ] = 2


        # ====================================================
        # LOSS NO CICLO 2
        #
        # Permanece no Ciclo 2.
        # NÃO aumenta entrada.
        # NÃO recalcula os 10%.
        # ====================================================

        else:

            acrescimo = 0.0

            dados[
                "ciclo_gestao"
            ] = 2

            # entrada_atual NÃO é alterada


        return {

            "ciclo":
                dados[
                    "ciclo_gestao"
                ],

            "entrada":
                dados[
                    "entrada_atual"
                ],

            "prejuizo":
                dados[
                    "prejuizo_acumulado"
                ],

            "sinais":
                dados[
                    "sinais_recuperacao"
                ],

            "acrescimo":
                acrescimo,
        }


# ============================================================
# WIN DURANTE RECUPERAÇÃO
# ============================================================

def aplicar_win_recuperacao(
    banca,
    lucro
):

    lucro = max(
        0.0,
        round(
            float(lucro),
            2
        )
    )

    with estado_lock:

        dados = (
            estado_frentes[banca]
        )

        if not dados[
            "em_recuperacao"
        ]:

            return {
                "recuperado": True,
                "restante": 0.0,
                "sinais": 0,
            }


        # O lucro reduz o prejuízo acumulado.
        # A entrada do Ciclo 2 continua congelada.

        dados[
            "prejuizo_acumulado"
        ] = round(
            max(
                0.0,
                dados[
                    "prejuizo_acumulado"
                ]
                - lucro
            ),
            2
        )

        restante = (
            dados[
                "prejuizo_acumulado"
            ]
        )

        sinais = (
            dados[
                "sinais_recuperacao"
            ]
        )


    # Recuperou tudo:
    # volta ao Ciclo 1 / R$2.

    if restante <= 0:

        resetar_gestao(
            banca,
            "prejuizo_recuperado"
        )

        return {
            "recuperado": True,
            "restante": 0.0,
            "sinais": sinais,
        }


    return {
        "recuperado": False,
        "restante": restante,
        "sinais": sinais,
    }


# ============================================================
# CONTROLE DOS 10 SINAIS DE RECUPERAÇÃO
# ============================================================

def iniciar_sinal_gestao(
    banca
):

    with estado_lock:

        dados = (
            estado_frentes[banca]
        )

        if dados[
            "em_recuperacao"
        ]:

            if (
                dados[
                    "sinais_recuperacao"
                ]
                >= MAX_SINAIS_RECUPERACAO
            ):

                return False

            dados[
                "sinais_recuperacao"
            ] += 1

        return True


# ============================================================
# LIBERAR BANCA
# ============================================================

def liberar_banca(
    banca,
    ativo
):

    with estado_lock:

        ativos_em_uso.discard(
            ativo
        )

        dados = (
            estado_frentes[banca]
        )

        dados["ocupada"] = False
        dados["ativo"] = None
        dados["direcao"] = None
        dados["score"] = None
        dados["timeframe"] = None
        dados["expiracao"] = None
        dados["order_id"] = None


# ============================================================
# CICLO DA OPERAÇÃO
# ============================================================

def ciclo(
    banca,
    ativo,
    direcao,
    score,
    timeframe_analise,
    expiracao
):

    try:

        # ====================================================
        # SE JÁ COMPLETOU 10 SINAIS DE RECUPERAÇÃO
        # RESETA PARA CICLO 1 / R$2
        # ====================================================

        with estado_lock:

            limite = (
                estado_frentes[
                    banca
                ][
                    "em_recuperacao"
                ]
                and
                estado_frentes[
                    banca
                ][
                    "sinais_recuperacao"
                ]
                >= MAX_SINAIS_RECUPERACAO
            )

        if limite:

            resetar_gestao(
                banca,
                "limite_10_sinais"
            )


        # ====================================================
        # MANTÉM R$2 SOMENTE FORA DA RECUPERAÇÃO
        # ====================================================

        sincronizar_entrada_base_do_dia(
            banca
        )


        # ====================================================
        # CONTA O SINAL SE ESTIVER EM RECUPERAÇÃO
        # ====================================================

        if not iniciar_sinal_gestao(
            banca
        ):

            return


        with estado_lock:

            dados = (
                estado_frentes[banca]
            )

            ciclo_atual = int(
                dados[
                    "ciclo_gestao"
                ]
            )

            entrada_atual = round(
                float(
                    dados[
                        "entrada_atual"
                    ]
                ),
                2
            )

            em_recuperacao = bool(
                dados[
                    "em_recuperacao"
                ]
            )

            sinal_rec = int(
                dados[
                    "sinais_recuperacao"
                ]
            )

            prejuizo_antes = round(
                float(
                    dados[
                        "prejuizo_acumulado"
                    ]
                ),
                2
            )


        quantidade_gales = (
            GALES_POR_CICLO[
                ciclo_atual
            ]
        )


        # Sempre:
        # entrada -> G1 x3 -> G2 x3

        valores = valores_do_ciclo(
            entrada_atual,
            quantidade_gales
        )


        log.info(
            "GESTÃO | %s | M%s | "
            "ciclo=%s/2 | entrada=%.2f | "
            "gales=%s | X3 | recuperação=%s | "
            "sinal_rec=%s/%s | valores=%s",

            banca,
            timeframe_analise,
            ciclo_atual,
            entrada_atual,
            quantidade_gales,
            em_recuperacao,
            sinal_rec,
            MAX_SINAIS_RECUPERACAO,
            valores
        )


        perdas_deste_sinal = 0.0


        # ====================================================
        # ENTRADA + G1 + G2
        # ====================================================

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


            # =================================================
            # ORDEM RECUSADA
            #
            # Não consome tentativa de recuperação.
            # =================================================

            if not ok:

                log.warning(
                    "%s | ORDEM NÃO EXECUTADA | %s",
                    banca,
                    order_id
                )

                if em_recuperacao:

                    with estado_lock:

                        estado_frentes[
                            banca
                        ][
                            "sinais_recuperacao"
                        ] = max(
                            0,
                            estado_frentes[
                                banca
                            ][
                                "sinais_recuperacao"
                            ]
                            - 1
                        )

                return


            with estado_lock:

                estado_frentes[
                    banca
                ][
                    "order_id"
                ] = order_id


            # =================================================
            # TELEGRAM - NOVO SINAL
            # =================================================

            if nivel == 0:

                mercado = (
                    "OTC"
                    if ativo.endswith("-OTC")
                    else "NORMAL"
                )

                sequencia = " → ".join(
                    (
                        f"Entrada {v:.2f}"
                        if i == 0
                        else f"G{i} {v:.2f}"
                    )
                    for i, v
                    in enumerate(valores)
                )


                telegram(
                    "📊 NOVO SINAL DIGITAL\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | {mercado}\n"
                    f"📈 Análise: M{timeframe_analise}\n"
                    f"⏱ Digital: M{expiracao}\n"
                    f"🎯 {direcao.upper()}\n"
                    f"🔥 Score: {score}/100\n"
                    f"🔁 Ciclo: {ciclo_atual}/2\n"
                    f"🛡 Gales: 2\n"
                    f"📈 Multiplicador: X3\n"
                    f"💰 {sequencia}\n"
                    f"♻️ Recuperação: "
                    f"{'SIM' if em_recuperacao else 'NÃO'}"
                    + (
                        f" ({sinal_rec}/"
                        f"{MAX_SINAIS_RECUPERACAO})\n"
                        f"📉 A recuperar: "
                        f"{prejuizo_antes:.2f}"
                        if em_recuperacao
                        else ""
                    )
                )


                with estado_lock:

                    estado_frentes[
                        banca
                    ][
                        "ultimo_sinal"
                    ] = {

                        "ativo":
                            ativo,

                        "direcao":
                            direcao,

                        "score":
                            score,

                        "timeframe":
                            timeframe_analise,

                        "expiracao":
                            expiracao,

                        "ciclo":
                            ciclo_atual,

                        "entrada":
                            entrada_atual,

                        "gales":
                            quantidade_gales,

                        "multiplicador":
                            3,

                        "recuperacao":
                            em_recuperacao,

                        "sinal_recuperacao":
                            sinal_rec,

                        "data":
                            datetime.now(
                                timezone.utc
                            ).isoformat(),
                    }


            # =================================================
            # AGUARDA RESULTADO
            # =================================================

            resultado, lucro = (
                aguardar_resultado(
                    order_id
                )
            )


            log.info(
                "RESULTADO | %s | %s | "
                "M%s | nivel=%s | %s | %s",

                banca,
                ativo,
                timeframe_analise,
                nivel,
                resultado,
                lucro
            )


            # =================================================
            # WIN
            # =================================================

            if resultado == "win":

                registrar_win(
                    banca,
                    nivel
                )


                nome = (
                    "WIN"
                    if nivel == 0
                    else f"WIN G{nivel}"
                )


                rec = aplicar_win_recuperacao(
                    banca,
                    lucro
                )


                w = stats["wins"]
                l = stats["losses"]

                saldo_atual = (
                    texto_saldo()
                )


                telegram(
                    f"✅ {nome}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"⏱ M{timeframe_analise} / "
                    f"Digital M{expiracao}\n"
                    f"🔁 Ciclo: {ciclo_atual}/2\n"
                    f"💵 Lucro: {float(lucro):.2f}\n"
                    f"💰 Saldo atual PRACTICE: "
                    f"{saldo_atual}\n"
                    f"♻️ Restante recuperação: "
                    f"{rec['restante']:.2f}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
                )


                # Se chegou ao 10º sinal e ainda
                # existe prejuízo, reseta.

                with estado_lock:

                    atingiu_10 = (
                        estado_frentes[
                            banca
                        ][
                            "em_recuperacao"
                        ]
                        and
                        estado_frentes[
                            banca
                        ][
                            "sinais_recuperacao"
                        ]
                        >= MAX_SINAIS_RECUPERACAO
                    )


                if atingiu_10:

                    resetar_gestao(
                        banca,
                        "10_sinais_sem_recuperacao_total"
                    )


                return


            # =================================================
            # DRAW / TIMEOUT
            #
            # Não consome tentativa.
            # =================================================

            if resultado in (
                "draw",
                "timeout"
            ):

                if em_recuperacao:

                    with estado_lock:

                        estado_frentes[
                            banca
                        ][
                            "sinais_recuperacao"
                        ] = max(
                            0,
                            estado_frentes[
                                banca
                            ][
                                "sinais_recuperacao"
                            ]
                            - 1
                        )

                return


            # =================================================
            # LOSS
            # =================================================

            if resultado == "loss":

                # Mantém a lógica vigente:
                # contabiliza o valor da entrada perdida.

                perdas_deste_sinal = round(
                    perdas_deste_sinal
                    + abs(float(valor)),
                    2
                )


                # Ainda tem Gale disponível.

                if nivel < quantidade_gales:

                    log.info(
                        "%s | LOSS nível %s | G%s X3",
                        banca,
                        nivel,
                        nivel + 1
                    )

                    time.sleep(1)

                    continue


                # =================================================
                # LOSS COMPLETO
                # =================================================

                registrar_loss(
                    banca
                )


                gestao = aplicar_loss_ciclo(
                    banca,
                    perdas_deste_sinal
                )


                w = stats["wins"]
                l = stats["losses"]

                saldo_atual = (
                    texto_saldo()
                )


                if ciclo_atual == 1:

                    mensagem_gestao = (
                        f"➕ 10% aplicado uma vez: "
                        f"{gestao['acrescimo']:.2f}\n"
                        f"➡️ Próximo ciclo: 2/2\n"
                        f"💵 Entrada do Ciclo 2: "
                        f"{gestao['entrada']:.2f}"
                    )

                else:

                    mensagem_gestao = (
                        "🔒 Ciclo 2 mantido\n"
                        "➕ Novo aumento: NÃO\n"
                        f"💵 Entrada mantida: "
                        f"{gestao['entrada']:.2f}"
                    )


                telegram(
                    "❌ LOSS COMPLETO\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"⏱ M{timeframe_analise} / "
                    f"Digital M{expiracao}\n"
                    f"🔁 Ciclo encerrado: "
                    f"{ciclo_atual}/2\n"
                    f"📈 Gale: X3\n"
                    f"🛡 Gales: 2\n"
                    f"💸 LOSS do sinal: "
                    f"{perdas_deste_sinal:.2f}\n"
                    f"💰 Saldo atual PRACTICE: "
                    f"{saldo_atual}\n"
                    f"{mensagem_gestao}\n"
                    f"📉 Prejuízo acumulado: "
                    f"{gestao['prejuizo']:.2f}\n"
                    f"♻️ Recuperação: "
                    f"{gestao['sinais']}/"
                    f"{MAX_SINAIS_RECUPERACAO}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
                )


                # =================================================
                # SE FOI A 10ª TENTATIVA DE RECUPERAÇÃO
                # RESETA APÓS REGISTRAR O RESULTADO.
                # =================================================

                with estado_lock:

                    atingiu_10 = (
                        estado_frentes[
                            banca
                        ][
                            "em_recuperacao"
                        ]
                        and
                        estado_frentes[
                            banca
                        ][
                            "sinais_recuperacao"
                        ]
                        >= MAX_SINAIS_RECUPERACAO
                    )


                if atingiu_10:

                    resetar_gestao(
                        banca,
                        "10_sinais_sem_recuperacao_total"
                    )


                return


    finally:

        liberar_banca(
            banca,
            ativo
        )


# ============================================================
# RESERVAR BANCA
# ============================================================

def reservar_banca(
    ativo,
    direcao,
    score,
    timeframe,
    expiracao
):

    with estado_lock:

        if ativo in ativos_em_uso:
            return None


        for banca in BANCAS:

            dados = (
                estado_frentes[banca]
            )


            if not dados[
                "ocupada"
            ]:

                dados[
                    "ocupada"
                ] = True

                dados[
                    "ativo"
                ] = ativo

                dados[
                    "direcao"
                ] = direcao

                dados[
                    "score"
                ] = score

                dados[
                    "timeframe"
                ] = timeframe

                dados[
                    "expiracao"
                ] = expiracao


                ativos_em_uso.add(
                    ativo
                )


                return banca


    return None


# ============================================================
# DISPARAR SINAL
# ============================================================

def disparar_sinal(
    ativo,
    direcao,
    score,
    timeframe,
    expiracao
):

    banca = reservar_banca(
        ativo,
        direcao,
        score,
        timeframe,
        expiracao
    )


    if banca is None:
        return False


    log.info(
        "SINAL RESERVADO | "
        "%s | %s | M%s | "
        "Digital M%s | %s | score=%s",

        banca,
        ativo,
        timeframe,
        expiracao,
        direcao.upper(),
        score
    )


    thread = threading.Thread(

        target=ciclo,

        args=(
            banca,
            ativo,
            direcao,
            score,
            timeframe,
            expiracao,
        ),

        daemon=True,

        name=(
            f"operacao-{banca}-"
            f"M{timeframe}-{ativo}"
        )
    )


    thread.start()

    return True


# ============================================================
# SCANNER DE TIMEFRAME
# ============================================================

def scanner_timeframe(
    timeframe
):

    cfg = TIMEFRAMES[
        timeframe
    ]

    segundos = cfg[
        "segundos"
    ]

    expiracao = cfg[
        "expiracao"
    ]


    log.info(
        "SCANNER M%s INICIADO | "
        "Digital M%s",

        timeframe,
        expiracao
    )


    while True:

        try:

            if not garantir_conexao():

                time.sleep(10)

                continue


            log.info(
                "VARREDURA M%s | Digital M%s",
                timeframe,
                expiracao
            )


            for ativo in ATIVOS:

                with estado_lock:

                    existe_banca_livre = any(
                        not estado_frentes[
                            banca
                        ][
                            "ocupada"
                        ]
                        for banca in BANCAS
                    )


                if not existe_banca_livre:
                    break


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


                resultado = analisar(
                    candles
                )


                if resultado:

                    log.info(
                        "CANDIDATO | "
                        "%s | M%s | %s | "
                        "score=%s",

                        ativo,
                        timeframe,
                        resultado[
                            "direcao"
                        ],
                        resultado[
                            "score"
                        ]
                    )


                    reservado = disparar_sinal(
                        ativo,
                        resultado[
                            "direcao"
                        ],
                        resultado[
                            "score"
                        ],
                        timeframe,
                        expiracao
                    )


                    if reservado:

                        log.info(
                            "SINAL APROVADO E DISTRIBUÍDO | "
                            "%s | M%s",
                            ativo,
                            timeframe
                        )


                time.sleep(
                    INTERVALO_ENTRE_ATIVOS
                )


            time.sleep(
                INTERVALO_ANALISE
            )


        except Exception as e:

            log.exception(
                "SCANNER M%s ERRO | %s",
                timeframe,
                e
            )

            time.sleep(5)


# ============================================================
# SUPERVISOR
# ============================================================

scanner_threads = {}

scanner_threads_lock = (
    threading.RLock()
)


def iniciar_scanner(
    timeframe,
    atraso=0
):

    def alvo():

        if atraso:

            time.sleep(
                atraso
            )

        scanner_timeframe(
            timeframe
        )


    thread = threading.Thread(

        target=alvo,

        daemon=True,

        name=f"scanner-M{timeframe}"
    )


    thread.start()


    with scanner_threads_lock:

        scanner_threads[
            timeframe
        ] = thread


    log.info(
        "SCANNER M%s CRIADO | %s",
        timeframe,
        thread.name
    )


    return thread


def supervisor():

    log.info(
        "SUPERVISOR V10 INICIADO"
    )


    while True:

        try:

            with scanner_threads_lock:

                snapshot = dict(
                    scanner_threads
                )


            for timeframe in TIMEFRAMES:

                thread = snapshot.get(
                    timeframe
                )


                if (
                    thread is None
                    or not thread.is_alive()
                ):

                    log.error(
                        "SCANNER M%s PAROU | "
                        "REINICIANDO",
                        timeframe
                    )


                    iniciar_scanner(
                        timeframe,
                        0
                    )


            time.sleep(15)


        except Exception as e:

            log.exception(
                "SUPERVISOR ERRO | %s",
                e
            )

            time.sleep(5)


# ============================================================
# WORKER
# ============================================================

def worker():

    log.info(
        "WORKER V10 INICIADO"
    )


    while not conectar():

        time.sleep(10)


    iniciar_scanner(
        1,
        0
    )

    iniciar_scanner(
        5,
        2
    )

    iniciar_scanner(
        15,
        4
    )


    threading.Thread(
        target=supervisor,
        daemon=True,
        name="supervisor-scanners"
    ).start()


# ============================================================
# STATUS WEB
# ============================================================

@app.route("/")
def home():

    saldo = None


    try:

        if conectado():

            with api_lock:

                saldo = (
                    api.get_balance()
                )

    except Exception:

        pass


    with estado_lock:

        frentes = {

            nome: {

                **dados,

                "assertividade":
                    taxa(
                        dados["wins"],
                        dados["losses"]
                    ),
            }

            for nome, dados
            in estado_frentes.items()
        }


        geral = {

            **stats,

            "assertividade":
                taxa(
                    stats["wins"],
                    stats["losses"]
                ),
        }


    agora = agora_brasil()


    return jsonify({

        "versao":
            VERSAO,

        "status":
            "online",

        "iq_conectada":
            conectado(),

        "conta":
            "PRACTICE",

        "saldo":
            saldo,

        "execucao":
            EXECUTAR_ORDENS,

        "score_min":
            SCORE_MIN,

        "data_brasil":
            agora.strftime(
                "%d/%m/%Y %H:%M:%S"
            ),

        "dia_brasil":
            agora.day,

        "gestao": {

            "entrada_base":
                obter_entrada_base(),

            "entrada_base_regra":
                "fixa R$2 no ciclo 1",

            "multiplicador_gale":
                3,

            "gales_por_ciclo":
                GALES_POR_CICLO,

            "max_ciclos":
                2,

            "recuperacao_percentual":
                RECUPERACAO_PERCENTUAL,

            "regra_recuperacao":
                "10% aplicado somente na passagem do ciclo 1 para o ciclo 2",

            "entrada_ciclo_2":
                "mantida fixa mesmo após LOSS",

            "max_sinais_recuperacao":
                MAX_SINAIS_RECUPERACAO,
        },

        "mercado":
            "DIGITAL",

        "timeframes": {

            "M1":
                "Digital M1",

            "M5":
                "Digital M5",

            "M15":
                "Digital M5",
        },

        "distribuicao":
            "primeiro sinal aprovado -> primeira banca livre",

        "estatisticas":
            geral,

        "bancas":
            frentes,

        "ativos_em_uso":
            sorted(
                ativos_em_uso
            ),

        "ativos_com_candles":
            sorted(
                ativos_validos
            ),

        "bloqueados":
            len(
                bloqueados
            ),
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "versao":
            VERSAO,

        "conta":
            "PRACTICE",

        "mercado":
            "DIGITAL",

        "entrada_base":
            obter_entrada_base(),

        "multiplicador_gale":
            3,

        "gales_por_ciclo":
            2,

        "max_ciclos":
            2,

        "recuperacao_percentual":
            RECUPERACAO_PERCENTUAL,

        "max_sinais_recuperacao":
            MAX_SINAIS_RECUPERACAO,

        "dia_brasil":
            agora_brasil().day,

        "timeframes":
            [
                "M1",
                "M5",
                "M15"
            ],

        "bancas":
            10,
    }), 200


# ============================================================
# START
# ============================================================

entrada_inicial = (
    obter_entrada_base()
)


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
    "MERCADO = DIGITAL"
)

log.info(
    "TIMEFRAMES = M1 + M5 + M15"
)

log.info(
    "BANCAS = 10"
)

log.info(
    "EXECUTAR_ORDENS = %s",
    EXECUTAR_ORDENS
)

log.info(
    "ENTRADA BASE = %.2f",
    entrada_inicial
)

log.info(
    "GESTÃO | "
    "GALE X3 | "
    "2 GALES | "
    "2 CICLOS | "
    "RECUPERAÇÃO %.0f%% | "
    "CICLO 2 ENTRADA FIXA | "
    "MAX %s SINAIS",

    RECUPERACAO_PERCENTUAL * 100,

    MAX_SINAIS_RECUPERACAO
)

log.info(
    "DISTRIBUIÇÃO = "
    "M1/M5/M15 -> PRIMEIRA BANCA LIVRE"
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
