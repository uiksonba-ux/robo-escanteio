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

VERSAO = "IQ-V10-DIGITAL-V2-G5-X2-PRACTICE"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-v10")


# ============================================================
# HELPERS DE CONFIGURAÇÃO DO RENDER
# ============================================================

def env_float(nome, padrao, minimo=None):
    try:
        valor = float(os.getenv(nome, str(padrao)))
    except (TypeError, ValueError):
        valor = float(padrao)
        log.warning("%s inválido; usando padrão %s", nome, padrao)

    if minimo is not None and valor < minimo:
        log.warning("%s abaixo do mínimo; usando %s", nome, minimo)
        valor = minimo

    return valor


def env_int(nome, padrao, minimo=None, maximo=None):
    try:
        valor = int(os.getenv(nome, str(padrao)))
    except (TypeError, ValueError):
        valor = int(padrao)
        log.warning("%s inválido; usando padrão %s", nome, padrao)

    if minimo is not None and valor < minimo:
        log.warning("%s abaixo do mínimo; usando %s", nome, minimo)
        valor = minimo

    if maximo is not None and valor > maximo:
        log.warning("%s acima do máximo; usando %s", nome, maximo)
        valor = maximo

    return valor


def env_bool(nome, padrao=False):
    valor = os.getenv(
        nome,
        "true" if padrao else "false"
    ).strip().lower()

    return valor in ("1", "true", "yes", "on", "sim")


# ============================================================
# CONFIGURAÇÕES
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = env_int("PORT", 10000, 1)


# ============================================================
# SEGURANÇA
# ============================================================

# CONTA FIXA DE TREINO.
# NÃO transformar em variável de ambiente.
CONTA = "PRACTICE"

EXECUTAR_ORDENS = env_bool(
    "EXECUTAR_ORDENS",
    False
)


# ============================================================
# ESTRATÉGIA
# ============================================================

# Estratégia vigente mantida.
# Apenas parâmetros operacionais ficam configuráveis no Render.

SCORE_MIN = env_int(
    "SCORE_MIN",
    65,
    0,
    100
)

DIFERENCA_MINIMA = env_int(
    "DIFERENCA_MINIMA",
    15,
    0,
    100
)

INTERVALO_ANALISE = env_int(
    "INTERVALO_ANALISE",
    20,
    1
)

INTERVALO_ENTRE_ATIVOS = env_float(
    "INTERVALO_ENTRE_ATIVOS",
    0.7,
    0.1
)

TEMPO_BLOQUEIO = env_int(
    "TEMPO_BLOQUEIO",
    300,
    1
)


# ============================================================
# GESTÃO PELO RENDER
# ============================================================

ENTRADA_BASE = env_float(
    "ENTRADA_BASE",
    2.00,
    0.01
)

MULTIPLICADOR_GALE = env_float(
    "MULTIPLICADOR_GALE",
    2.0,
    1.0
)

# Até 5 gales por ciclo.
GALES_POR_CICLO = env_int(
    "MAX_GALES",
    5,
    0,
    5
)

RECUPERACAO_PERCENTUAL = env_float(
    "RECUPERACAO_PERCENTUAL",
    0.01,
    0.0
)

QTD_BANCAS = env_int(
    "QTD_BANCAS",
    10,
    1,
    20
)


# ============================================================
# HORÁRIO BRASIL
# ============================================================

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora_brasil():
    return datetime.now(FUSO_BRASIL)


# ============================================================
# ENTRADA BASE
# ============================================================

def obter_entrada_base():
    return round(
        ENTRADA_BASE,
        2
    )


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
    f"BANCA {i}"
    for i in range(
        1,
        QTD_BANCAS + 1
    )
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
# ESTADO DAS BANCAS
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

        "ciclo_gestao": 1,

        "entrada_atual":
            obter_entrada_base(),

        "prejuizo_acumulado": 0.0,

        "em_recuperacao": False,
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
                "IQ CONECTADA | "
                "PRACTICE | saldo=%s",
                saldo
            )

            telegram(
                "🤖 ROBÔ V10 ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: {saldo}\n"
                f"💵 Entrada base: {entrada:.2f}\n\n"
                f"🏦 {QTD_BANCAS} BANCAS INDEPENDENTES\n"
                "⏱ M1 + M5 + M15\n"
                "📊 SOMENTE DIGITAL\n"
                f"📈 GALE X{MULTIPLICADOR_GALE:g}\n"
                f"🛡 {GALES_POR_CICLO} GALES POR CICLO\n"
                "♾️ CICLOS ILIMITADOS\n"
                f"♻️ RECUPERAÇÃO "
                f"{RECUPERACAO_PERCENTUAL * 100:g}%\n\n"
                f"🔥 Score mínimo: {SCORE_MIN}\n"
                f"↔️ Diferença mínima: {DIFERENCA_MINIMA}"
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
# ESTRATÉGIA ATUAL
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
            motivos_call.append("EMA")

        elif (
            e20 < e50
            and preco < e20
        ):

            put += 20
            motivos_put.append("EMA")

    # RSI = 15
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
        motivos_call.append("BOLLINGER")

    elif (
        preco < mm
        and preco > inferior
    ):

        put += 10
        motivos_put.append("BOLLINGER")

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
                motivos_call.append("PRICE ACTION")

            elif (
                ultimo["close"]
                < ultimo["open"]
            ):

                put += 15
                motivos_put.append("PRICE ACTION")

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
            motivos_call.append("SUPORTE")

        elif posicao >= 0.70:

            put += 10
            motivos_put.append("RESISTENCIA")

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
        motivos_call.append("BREAKOUT")

    elif preco < min_anterior:

        put += 15
        motivos_put.append("BREAKOUT")

    # MACD = 10
    vm = macd(
        closes
    )

    if vm is not None:

        if vm > 0:

            call += 10
            motivos_call.append("MACD")

        elif vm < 0:

            put += 10
            motivos_put.append("MACD")

    # MOMENTUM = 5
    momentum = (
        closes[-1]
        - closes[-6]
    )

    if momentum > 0:

        call += 5
        motivos_call.append("MOMENTUM")

    elif momentum < 0:

        put += 5
        motivos_put.append("MOMENTUM")

    # DECISÃO
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
        or score - oposto < DIFERENCA_MINIMA
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
        return (False, "EXECUTAR_ORDENS=false")

    if esta_bloqueado(ativo, expiracao):
        return (False, "ativo_bloqueado")

    # PRACTICE OBRIGATÓRIO
    if not garantir_practice():
        return (False, "sem_conexao")

    ativo = str(ativo).strip().upper()
    direcao = str(direcao).strip().lower()
    valor = round(float(valor), 2)
    expiracao = int(expiracao)

    if direcao not in ("call", "put"):
        return (False, f"direcao_invalida:{direcao}")

    log.info(
        "BUY DIGITAL V2 | %s | %s | M%s | %s | %.2f",
        banca, ativo, expiracao, direcao.upper(), valor
    )

    # IMPORTANTE: não usa buy_digital_spot() clássico.
    # Nesta biblioteca ele pode bloquear a thread indefinidamente.
    metodo = getattr(api, "buy_digital_spot_v2", None)

    if not callable(metodo):
        motivo = "biblioteca_sem_buy_digital_spot_v2"
        with estado_lock:
            stats["ordens_recusadas"] += 1
            estado_frentes[banca]["ordens_recusadas"] += 1
        bloquear(ativo, expiracao, motivo)
        return (False, motivo)

    try:
        with api_lock:
            resposta = metodo(
                ativo,
                valor,
                direcao,
                expiracao,
            )

        log.info(
            "RESPOSTA buy_digital_spot_v2 | %s | %s",
            ativo, resposta
        )

        ok = False
        order_id = None

        if isinstance(resposta, (tuple, list)):
            if len(resposta) >= 1:
                ok = bool(resposta[0])
            if len(resposta) >= 2:
                order_id = resposta[1]
        elif isinstance(resposta, int) and not isinstance(resposta, bool):
            ok = resposta > 0
            order_id = resposta

        if ok and order_id:
            with estado_lock:
                stats["ordens_aceitas"] += 1
                estado_frentes[banca]["ordens_aceitas"] += 1

            log.info(
                "ORDEM DIGITAL ACEITA | V2 | %s | ID=%s",
                ativo, order_id
            )
            return (True, order_id)

        motivo = str(order_id if order_id is not None else resposta)

    except Exception as e:
        motivo = str(e)
        log.exception(
            "ERRO buy_digital_spot_v2 | %s | %s",
            ativo, e
        )

    with estado_lock:
        stats["ordens_recusadas"] += 1
        estado_frentes[banca]["ordens_recusadas"] += 1

    bloquear(ativo, expiracao, motivo)
    return (False, motivo)


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
# GALE CONFIGURÁVEL PELO RENDER
# ============================================================

def valores_do_ciclo(
    entrada,
    quantidade_gales=None
):

    if quantidade_gales is None:
        quantidade_gales = GALES_POR_CICLO

    return [

        round(
            float(entrada)
            * (
                MULTIPLICADOR_GALE
                ** nivel
            ),
            2
        )

        for nivel in range(
            quantidade_gales + 1
        )
    ]


# ============================================================
# SINCRONIZA ENTRADA BASE
# ============================================================

def sincronizar_entrada_base_do_dia(banca):

    entrada_base = obter_entrada_base()

    with estado_lock:

        dados = estado_frentes[banca]

        if (
            not dados["em_recuperacao"]
            and int(dados["ciclo_gestao"]) == 1
        ):

            dados["entrada_atual"] = round(
                entrada_base,
                2
            )


# ============================================================
# RESET DA GESTÃO
# ============================================================

def resetar_gestao(
    banca,
    motivo
):

    entrada_base = obter_entrada_base()

    with estado_lock:

        dados = estado_frentes[banca]

        dados["ciclo_gestao"] = 1

        dados["entrada_atual"] = round(
            entrada_base,
            2
        )

        dados["prejuizo_acumulado"] = 0.0

        dados["em_recuperacao"] = False

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

        dados = estado_frentes[banca]

        dados["prejuizo_acumulado"] = round(
            float(
                dados["prejuizo_acumulado"]
            )
            + perda_ciclo,
            2
        )

        dados["em_recuperacao"] = True

        acrescimo = round(
            perda_ciclo
            * RECUPERACAO_PERCENTUAL,
            2
        )

        dados["entrada_atual"] = round(
            float(
                dados["entrada_atual"]
            )
            + acrescimo,
            2
        )

        dados["ciclo_gestao"] = (
            int(
                dados["ciclo_gestao"]
            )
            + 1
        )

        return {

            "ciclo":
                dados["ciclo_gestao"],

            "entrada":
                dados["entrada_atual"],

            "prejuizo":
                dados["prejuizo_acumulado"],

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

        dados = estado_frentes[banca]

        if not dados["em_recuperacao"]:

            return {
                "recuperado": True,
                "restante": 0.0,
            }

        dados["prejuizo_acumulado"] = round(
            max(
                0.0,
                float(
                    dados["prejuizo_acumulado"]
                )
                - lucro
            ),
            2
        )

        restante = round(
            float(
                dados["prejuizo_acumulado"]
            ),
            2
        )

    if restante <= 0:

        resetar_gestao(
            banca,
            "prejuizo_recuperado"
        )

        return {
            "recuperado": True,
            "restante": 0.0,
        }

    return {
        "recuperado": False,
        "restante": restante,
    }


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

        dados = estado_frentes[banca]

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

        sincronizar_entrada_base_do_dia(
            banca
        )

        with estado_lock:

            dados = estado_frentes[banca]

            ciclo_atual = int(
                dados["ciclo_gestao"]
            )

            entrada_atual = round(
                float(
                    dados["entrada_atual"]
                ),
                2
            )

            em_recuperacao = bool(
                dados["em_recuperacao"]
            )

            prejuizo_antes = round(
                float(
                    dados["prejuizo_acumulado"]
                ),
                2
            )

        quantidade_gales = (
            GALES_POR_CICLO
        )

        valores = valores_do_ciclo(
            entrada_atual,
            quantidade_gales
        )

        log.info(
            "GESTÃO | %s | M%s | "
            "ciclo=%s | entrada=%.2f | "
            "gales=%s | X%s | "
            "recuperacao=%s | "
            "prejuizo=%.2f | valores=%s",

            banca,
            timeframe_analise,
            ciclo_atual,
            entrada_atual,
            quantidade_gales,
            f"{MULTIPLICADOR_GALE:g}",
            em_recuperacao,
            prejuizo_antes,
            valores
        )

        perdas_deste_sinal = 0.0

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

            if not ok:

                log.warning(
                    "%s | ORDEM NÃO EXECUTADA | %s",
                    banca,
                    order_id
                )

                return

            with estado_lock:

                estado_frentes[
                    banca
                ][
                    "order_id"
                ] = order_id

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
                    f"🔁 Ciclo: {ciclo_atual}\n"
                    f"♾️ Ciclos: ILIMITADOS\n"
                    f"🛡 Gales: {quantidade_gales}\n"
                    f"📈 Multiplicador: "
                    f"X{MULTIPLICADOR_GALE:g}\n"
                    f"💰 {sequencia}\n"
                    f"♻️ Recuperação: "
                    f"{'SIM' if em_recuperacao else 'NÃO'}\n"
                    f"📉 A recuperar: "
                    f"{prejuizo_antes:.2f}"
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
                            MULTIPLICADOR_GALE,

                        "recuperacao":
                            em_recuperacao,

                        "prejuizo_antes":
                            prejuizo_antes,

                        "data":
                            datetime.now(
                                timezone.utc
                            ).isoformat(),
                    }

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

                with estado_lock:

                    w = stats["wins"]
                    l = stats["losses"]

                saldo_atual = (
                    texto_saldo()
                )

                if rec["recuperado"]:

                    texto_recuperacao = (
                        "✅ Recuperação concluída\n"
                        "🔄 Próximo sinal: "
                        f"Ciclo 1 / R$"
                        f"{obter_entrada_base():.2f}"
                    )

                else:

                    texto_recuperacao = (
                        "♻️ Recuperação continua\n"
                        f"📉 Restante: "
                        f"{rec['restante']:.2f}\n"
                        f"➡️ Ciclo permanece: "
                        f"{ciclo_atual}"
                    )

                telegram(
                    f"✅ {nome}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"⏱ M{timeframe_analise} / "
                    f"Digital M{expiracao}\n"
                    f"🔁 Ciclo: {ciclo_atual}\n"
                    f"💵 Lucro: "
                    f"{float(lucro):.2f}\n"
                    f"💰 Saldo atual PRACTICE: "
                    f"{saldo_atual}\n"
                    f"{texto_recuperacao}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
                )

                return

            if resultado in (
                "draw",
                "timeout"
            ):

                log.warning(
                    "%s | %s | "
                    "GESTÃO NÃO ALTERADA",
                    banca,
                    resultado.upper()
                )

                return

            if resultado == "loss":

                perdas_deste_sinal = round(
                    perdas_deste_sinal
                    + abs(float(valor)),
                    2
                )

                if nivel < quantidade_gales:

                    log.info(
                        "%s | LOSS nível %s | "
                        "PRÓXIMO G%s X%s",

                        banca,
                        nivel,
                        nivel + 1,
                        f"{MULTIPLICADOR_GALE:g}"
                    )

                    time.sleep(1)

                    continue

                registrar_loss(
                    banca
                )

                gestao = aplicar_loss_ciclo(
                    banca,
                    perdas_deste_sinal
                )

                with estado_lock:

                    w = stats["wins"]
                    l = stats["losses"]

                saldo_atual = (
                    texto_saldo()
                )

                telegram(
                    "❌ LOSS COMPLETO\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"⏱ M{timeframe_analise} / "
                    f"Digital M{expiracao}\n"
                    f"🔁 Ciclo encerrado: "
                    f"{ciclo_atual}\n"
                    f"📈 Gale: "
                    f"X{MULTIPLICADOR_GALE:g}\n"
                    f"🛡 Gales: "
                    f"{quantidade_gales}\n"
                    f"💸 LOSS do ciclo: "
                    f"{perdas_deste_sinal:.2f}\n"
                    f"➕ Recuperação "
                    f"{RECUPERACAO_PERCENTUAL * 100:g}%: "
                    f"{gestao['acrescimo']:.2f}\n"
                    f"➡️ Próximo ciclo: "
                    f"{gestao['ciclo']}\n"
                    f"💵 Próxima entrada: "
                    f"{gestao['entrada']:.2f}\n"
                    f"📉 Prejuízo acumulado: "
                    f"{gestao['prejuizo']:.2f}\n"
                    f"♾️ Limite de ciclos: "
                    f"NENHUM\n"
                    f"💰 Saldo PRACTICE: "
                    f"{saldo_atual}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: "
                    f"{taxa(w, l)}%"
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

            dados = estado_frentes[
                banca
            ]

            if not dados["ocupada"]:

                dados["ocupada"] = True
                dados["ativo"] = ativo
                dados["direcao"] = direcao
                dados["score"] = score
                dados["timeframe"] = timeframe
                dados["expiracao"] = expiracao

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
                "VARREDURA M%s | "
                "Digital M%s",

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
                            "SINAL APROVADO E "
                            "DISTRIBUÍDO | "
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

        ativos_uso = sorted(
            ativos_em_uso
        )

        ativos_candles = sorted(
            ativos_validos
        )

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

        "diferenca_minima":
            DIFERENCA_MINIMA,

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
                (
                    f"R${obter_entrada_base():.2f} "
                    "fora da recuperação"
                ),

            "multiplicador_gale":
                MULTIPLICADOR_GALE,

            "gales_por_ciclo":
                GALES_POR_CICLO,

            "ciclos":
                "ilimitados",

            "recuperacao_percentual":
                RECUPERACAO_PERCENTUAL,

            "regra_recuperacao":
                (
                    f"{RECUPERACAO_PERCENTUAL * 100:g}% "
                    "do LOSS do ciclo adicionado "
                    "à entrada do próximo ciclo"
                ),

            "reset":
                (
                    f"Ciclo 1 / R$"
                    f"{obter_entrada_base():.2f} após "
                    "recuperar todo o prejuízo"
                ),
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
            (
                "primeiro sinal aprovado -> "
                "primeira banca livre"
            ),

        "estatisticas":
            geral,

        "bancas":
            frentes,

        "ativos_em_uso":
            ativos_uso,

        "ativos_com_candles":
            ativos_candles,

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
            MULTIPLICADOR_GALE,

        "gales_por_ciclo":
            GALES_POR_CICLO,

        "ciclos":
            "ilimitados",

        "recuperacao_percentual":
            RECUPERACAO_PERCENTUAL,

        "score_min":
            SCORE_MIN,

        "diferenca_minima":
            DIFERENCA_MINIMA,

        "execucao":
            EXECUTAR_ORDENS,

        "dia_brasil":
            agora_brasil().day,

        "timeframes":
            [
                "M1",
                "M5",
                "M15"
            ],

        "bancas":
            QTD_BANCAS,
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
    "BANCAS = %s",
    QTD_BANCAS
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
    "GALE X%s | "
    "%s GALES POR CICLO | "
    "CICLOS ILIMITADOS | "
    "RECUPERAÇÃO %g%%",

    f"{MULTIPLICADOR_GALE:g}",
    GALES_POR_CICLO,
    RECUPERACAO_PERCENTUAL * 100
)

log.info(
    "FILTROS | SCORE_MIN=%s | DIFERENCA_MINIMA=%s",
    SCORE_MIN,
    DIFERENCA_MINIMA
)

log.info(
    "DISTRIBUIÇÃO = "
    "M1/M5/M15 -> PRIMEIRA BANCA LIVRE"
)

log.info(
    "==============================================="
)


# ============================================================
# INICIA WORKER
# ============================================================

threading.Thread(
    target=worker,
    daemon=True,
    name="worker-iq"
).start()


# ============================================================
# FLASK
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
