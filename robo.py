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

VERSAO = "IQ-3-FRENTES-V4-DIGITAL-DINAMICO"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("iq-bot")


# ============================================================
# VARIÁVEIS DE AMBIENTE
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
    os.getenv("INTERVALO_ENTRE_ATIVOS", "0.8")
)

TEMPO_BLOQUEIO_ATIVO = 300

CONTA_PERMITIDA = "PRACTICE"


# ============================================================
# CONFIGURAÇÃO DAS 3 FRENTES
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
# ESTADO GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()
connect_lock = threading.Lock()
estado_lock = threading.RLock()

ativos_em_uso = set()

instrumentos_bloqueados = {}

ativos_digitais_cache = []
ativos_digitais_cache_time = 0

CACHE_ATIVOS_SEGUNDOS = 60


stats_global = {
    "wins": 0,
    "losses": 0,
    "win_direto": 0,
    "win_g1": 0,
    "win_g2": 0,
    "ordens_recusadas": 0
}


estado_frentes = {}

for nome in FRENTES:
    estado_frentes[nome] = {
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
            "Erro Telegram: %s",
            e
        )

        return False


# ============================================================
# CONEXÃO IQ OPTION
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
            "IQ_EMAIL ou IQ_PASSWORD não configurados."
        )
        return False

    with connect_lock:

        if conectado():
            return True

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
                "CONNECT | ok=%s | motivo=%s",
                ok,
                motivo
            )

            if not ok:
                return False

            # ================================================
            # TRAVA ABSOLUTA NA CONTA PRACTICE
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
                "🤖 ROBÔ V4 ONLINE\n"
                "🧪 Conta: PRACTICE\n"
                f"💰 Saldo: ${saldo}\n"
                "🏦 3 frentes: M1 / M5 / M15\n"
                f"🔥 Score mínimo: {SCORE_MIN}"
            )

            return True

        except Exception as e:
            log.exception(
                "Erro ao conectar IQ Option: %s",
                e
            )

            return False


def garantir_conexao():
    if conectado():
        return True

    return conectar()


def garantir_practice():
    """
    Reforça que nenhuma operação seja executada
    fora da conta de treinamento.
    """

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
            "Falha ao garantir PRACTICE: %s",
            e
        )

        return False


# ============================================================
# DESCOBERTA DOS ATIVOS DIGITAL
# ============================================================

def limpar_nome_ativo(nome):
    if not isinstance(nome, str):
        return None

    nome = nome.strip().upper()

    if not nome:
        return None

    return nome


def descobrir_ativos_digitais(forcar=False):
    """
    Consulta a IQ Option e retorna somente instrumentos
    DIGITAL marcados como abertos.

    Essa lista substitui a antiga lista fixa.
    """

    global ativos_digitais_cache
    global ativos_digitais_cache_time

    agora = time.time()

    if (
        not forcar
        and ativos_digitais_cache
        and agora - ativos_digitais_cache_time
        < CACHE_ATIVOS_SEGUNDOS
    ):
        return list(ativos_digitais_cache)

    if not garantir_conexao():
        return []

    try:

        log.info(
            "Consultando ativos DIGITAL disponíveis..."
        )

        with api_lock:
            open_time = api.get_all_open_time()

        if not isinstance(open_time, dict):
            log.warning(
                "get_all_open_time retornou formato inválido: %s",
                type(open_time)
            )
            return list(ativos_digitais_cache)

        digital = open_time.get(
            "digital",
            {}
        )

        if not isinstance(digital, dict):
            log.warning(
                "Lista DIGITAL inválida."
            )
            return list(ativos_digitais_cache)

        abertos = []

        for nome, dados in digital.items():

            ativo = limpar_nome_ativo(
                nome
            )

            if not ativo:
                continue

            try:
                aberto = bool(
                    dados.get(
                        "open",
                        False
                    )
                )

            except Exception:
                aberto = False

            if aberto:
                abertos.append(
                    ativo
                )

        abertos = sorted(
            set(abertos)
        )

        if abertos:

            ativos_digitais_cache = abertos
            ativos_digitais_cache_time = agora

            log.info(
                "ATIVOS DIGITAL DISPONÍVEIS (%d): %s",
                len(abertos),
                ", ".join(abertos)
            )

        else:

            log.warning(
                "Nenhum ativo DIGITAL aberto retornado pela IQ."
            )

        return list(abertos)

    except Exception as e:

        log.warning(
            "Erro ao consultar ativos DIGITAL: %s",
            e
        )

        # Se já temos cache, não derruba o robô
        return list(
            ativos_digitais_cache
        )


# ============================================================
# BLOQUEIO TEMPORÁRIO DE INSTRUMENTOS
# ============================================================

def chave_bloqueio(
    ativo,
    timeframe
):
    return (
        ativo,
        int(timeframe)
    )


def bloquear_instrumento(
    ativo,
    timeframe,
    motivo=""
):
    chave = chave_bloqueio(
        ativo,
        timeframe
    )

    instrumentos_bloqueados[chave] = (
        time.time()
        + TEMPO_BLOQUEIO_ATIVO
    )

    log.warning(
        "INSTRUMENTO BLOQUEADO | %s | M%s | %ss | %s",
        ativo,
        timeframe,
        TEMPO_BLOQUEIO_ATIVO,
        motivo
    )


def instrumento_bloqueado(
    ativo,
    timeframe
):
    chave = chave_bloqueio(
        ativo,
        timeframe
    )

    ate = instrumentos_bloqueados.get(
        chave
    )

    if not ate:
        return False

    if time.time() >= ate:
        instrumentos_bloqueados.pop(
            chave,
            None
        )
        return False

    return True


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

        fim = int(
            time.time()
        )

        with api_lock:
            candles = api.get_candles(
                ativo,
                segundos,
                quantidade,
                fim
            )

        if not candles:
            return []

        candles_validos = []

        agora = int(
            time.time()
        )

        for c in candles:

            try:

                inicio = int(
                    c.get(
                        "from",
                        0
                    )
                )

                # Remove candle ainda em formação
                if inicio + segundos > agora:
                    continue

                abertura = float(
                    c["open"]
                )

                fechamento = float(
                    c["close"]
                )

                maxima = float(
                    c["max"]
                )

                minima = float(
                    c["min"]
                )

                candles_validos.append({
                    "from": inicio,
                    "open": abertura,
                    "close": fechamento,
                    "max": maxima,
                    "min": minima
                })

            except Exception:
                continue

        return candles_validos

    except Exception as e:

        log.debug(
            "Candles indisponíveis | %s | %s",
            ativo,
            e
        )

        return []


# ============================================================
# INDICADORES
# ============================================================

def media(
    valores
):
    if not valores:
        return 0

    return sum(valores) / len(valores)


def desvio_padrao(
    valores
):
    if not valores:
        return 0

    m = media(
        valores
    )

    variancia = sum(
        (x - m) ** 2
        for x in valores
    ) / len(valores)

    return math.sqrt(
        variancia
    )


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

    for i in range(
        len(valores) - periodo,
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
            perdas.append(
                0
            )

        else:
            ganhos.append(
                0
            )
            perdas.append(
                abs(diferenca)
            )

    ganho_medio = media(
        ganhos
    )

    perda_media = media(
        perdas
    )

    if perda_media == 0:
        return 100

    rs = (
        ganho_medio
        / perda_media
    )

    return (
        100
        - (
            100
            / (1 + rs)
        )
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
        return None

    return e12 - e26


# ============================================================
# ANÁLISE / SCORE
# ============================================================

def analisar_candles(
    candles
):
    if len(candles) < 60:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    ultimo = candles[-1]

    preco = ultimo["close"]

    score_call = 0
    score_put = 0

    motivos_call = []
    motivos_put = []


    # ========================================================
    # 1. EMA 20 / EMA 50 - 20 pontos
    # ========================================================

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    if ema20 is not None and ema50 is not None:

        if ema20 > ema50 and preco > ema20:
            score_call += 20
            motivos_call.append(
                "EMA"
            )

        elif ema20 < ema50 and preco < ema20:
            score_put += 20
            motivos_put.append(
                "EMA"
            )


    # ========================================================
    # 2. RSI - 15 pontos
    # ========================================================

    valor_rsi = rsi(
        closes,
        14
    )

    if valor_rsi is not None:

        if 50 <= valor_rsi <= 70:
            score_call += 15
            motivos_call.append(
                "RSI"
            )

        elif 30 <= valor_rsi < 50:
            score_put += 15
            motivos_put.append(
                "RSI"
            )


    # ========================================================
    # 3. BOLLINGER - 10 pontos
    # ========================================================

    ultimos20 = closes[-20:]

    mm20 = media(
        ultimos20
    )

    dp20 = desvio_padrao(
        ultimos20
    )

    superior = (
        mm20
        + 2 * dp20
    )

    inferior = (
        mm20
        - 2 * dp20
    )

    if preco > mm20 and preco < superior:
        score_call += 10
        motivos_call.append(
            "BOLLINGER"
        )

    elif preco < mm20 and preco > inferior:
        score_put += 10
        motivos_put.append(
            "BOLLINGER"
        )


    # ========================================================
    # 4. PRICE ACTION - 15 pontos
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

            if ultimo["close"] > ultimo["open"]:
                score_call += 15
                motivos_call.append(
                    "PRICE ACTION"
                )

            elif ultimo["close"] < ultimo["open"]:
                score_put += 15
                motivos_put.append(
                    "PRICE ACTION"
                )


    # ========================================================
    # 5. SUPORTE / RESISTÊNCIA - 10 pontos
    # ========================================================

    janela_sr = candles[-20:-1]

    resistencia = max(
        c["max"]
        for c in janela_sr
    )

    suporte = min(
        c["min"]
        for c in janela_sr
    )

    distancia_total = (
        resistencia
        - suporte
    )

    if distancia_total > 0:

        posicao = (
            (preco - suporte)
            / distancia_total
        )

        if posicao <= 0.30:
            score_call += 10
            motivos_call.append(
                "SUPORTE"
            )

        elif posicao >= 0.70:
            score_put += 10
            motivos_put.append(
                "RESISTÊNCIA"
            )


    # ========================================================
    # 6. BREAKOUT - 15 pontos
    # ========================================================

    janela_break = candles[-11:-1]

    max_anterior = max(
        c["max"]
        for c in janela_break
    )

    min_anterior = min(
        c["min"]
        for c in janela_break
    )

    if preco > max_anterior:
        score_call += 15
        motivos_call.append(
            "BREAKOUT"
        )

    elif preco < min_anterior:
        score_put += 15
        motivos_put.append(
            "BREAKOUT"
        )


    # ========================================================
    # 7. MACD - 10 pontos
    # ========================================================

    valor_macd = macd(
        closes
    )

    if valor_macd is not None:

        if valor_macd > 0:
            score_call += 10
            motivos_call.append(
                "MACD"
            )

        elif valor_macd < 0:
            score_put += 10
            motivos_put.append(
                "MACD"
            )


    # ========================================================
    # 8. MOMENTUM - 5 pontos
    # ========================================================

    if len(closes) >= 6:

        momentum = (
            closes[-1]
            - closes[-6]
        )

        if momentum > 0:
            score_call += 5
            motivos_call.append(
                "MOMENTUM"
            )

        elif momentum < 0:
            score_put += 5
            motivos_put.append(
                "MOMENTUM"
            )


    # ========================================================
    # DECISÃO
    # ========================================================

    if score_call >= score_put:

        direcao = "call"
        score = score_call
        score_oposto = score_put
        motivos = motivos_call

    else:

        direcao = "put"
        score = score_put
        score_oposto = score_call
        motivos = motivos_put


    # Exige vantagem mínima
    if (
        score < SCORE_MIN
        or score - score_oposto < 15
    ):
        return None


    return {
        "direcao": direcao,
        "score": score,
        "score_oposto": score_oposto,
        "motivos": motivos,
        "rsi": (
            round(
                valor_rsi,
                2
            )
            if valor_rsi is not None
            else None
        )
    }


# ============================================================
# RESERVA DE ATIVO
# ============================================================

def reservar_ativo(
    ativo
):
    with estado_lock:

        if ativo in ativos_em_uso:
            return False

        ativos_em_uso.add(
            ativo
        )

        return True


def liberar_ativo(
    ativo
):
    with estado_lock:
        ativos_em_uso.discard(
            ativo
        )


# ============================================================
# ORDEM DIGITAL
# ============================================================

def executar_ordem(
    ativo,
    valor,
    direcao,
    timeframe
):
    if not EXECUTAR_ORDENS:

        log.warning(
            "EXECUTAR_ORDENS=false | operação não enviada."
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


    if instrumento_bloqueado(
        ativo,
        timeframe
    ):

        return (
            False,
            "instrumento_bloqueado"
        )


    log.info(
        "TENTANDO ORDEM | ativo=%s | valor=%.2f | direcao=%s | exp=%s",
        ativo,
        valor,
        direcao,
        timeframe
    )


    try:

        with api_lock:

            resposta = api.buy_digital_spot_v2(
                ativo,
                float(valor),
                direcao,
                int(timeframe)
            )


        log.info(
            "RESPOSTA buy_digital_spot_v2 | ativo=%s | M%s | resposta=%s",
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

            if len(resposta) >= 1:
                status = bool(
                    resposta[0]
                )

            if len(resposta) >= 2:
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


        motivo = order_id


        stats_global[
            "ordens_recusadas"
        ] += 1


        texto_motivo = str(
            motivo
        ).lower()


        if (
            "invalid instrument"
            in texto_motivo
            or "rejected"
            in texto_motivo
        ):

            bloquear_instrumento(
                ativo,
                timeframe,
                texto_motivo
            )


        log.warning(
            "ORDEM RECUSADA | %s | M%s | motivo=%s",
            ativo,
            timeframe,
            motivo
        )


        return (
            False,
            motivo
        )


    except Exception as e:

        log.exception(
            "Erro ao executar ordem | %s | M%s | %s",
            ativo,
            timeframe,
            e
        )

        return (
            False,
            str(e)
        )


# ============================================================
# RESULTADO DIGITAL
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


            if (
                isinstance(
                    resposta,
                    (tuple, list)
                )
                and len(resposta) >= 2
            ):

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
                        return "win", lucro

                    elif lucro < 0:
                        return "loss", lucro

                    else:
                        return "draw", lucro


        except Exception as e:

            log.debug(
                "Aguardando resultado %s: %s",
                order_id,
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

def taxa_acerto(
    wins,
    losses
):
    total = (
        wins + losses
    )

    if total <= 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def registrar_win(
    banca,
    nivel
):
    with estado_lock:

        stats_global[
            "wins"
        ] += 1

        estado_frentes[banca][
            "wins"
        ] += 1


        if nivel == 0:

            stats_global[
                "win_direto"
            ] += 1

            estado_frentes[banca][
                "win_direto"
            ] += 1

        elif nivel == 1:

            stats_global[
                "win_g1"
            ] += 1

            estado_frentes[banca][
                "win_g1"
            ] += 1

        elif nivel == 2:

            stats_global[
                "win_g2"
            ] += 1

            estado_frentes[banca][
                "win_g2"
            ] += 1


def registrar_loss(
    banca
):
    with estado_lock:

        stats_global[
            "losses"
        ] += 1

        estado_frentes[banca][
            "losses"
        ] += 1


# ============================================================
# CICLO ENTRY + G1 + G2
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


    nomes = [
        "ENTRADA",
        "G1",
        "G2"
    ]


    with estado_lock:

        estado_frentes[banca][
            "ocupada"
        ] = True

        estado_frentes[banca][
            "ativo"
        ] = ativo

        estado_frentes[banca][
            "direcao"
        ] = direcao

        estado_frentes[banca][
            "score"
        ] = score


    try:

        primeira_aceita = False


        for nivel, valor in enumerate(
            valores
        ):

            with estado_lock:

                estado_frentes[banca][
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

                    estado_frentes[banca][
                        "ordens_recusadas"
                    ] += 1


                # Se a primeira ordem não foi aceita,
                # não existe sinal real e não fazemos Gale.
                if nivel == 0:

                    log.warning(
                        "%s | primeira ordem recusada | %s | motivo=%s",
                        banca,
                        ativo,
                        order_id
                    )

                    return


                log.warning(
                    "%s | %s recusado | %s | motivo=%s",
                    banca,
                    nomes[nivel],
                    ativo,
                    order_id
                )

                return


            with estado_lock:

                estado_frentes[banca][
                    "order_id"
                ] = order_id


            # Só envia sinal depois que a primeira
            # ordem realmente foi aceita pela IQ.
            if nivel == 0:

                primeira_aceita = True

                mercado = (
                    "OTC"
                    if "-OTC" in ativo
                    else "NORMAL"
                )

                telegram(
                    f"📊 NOVO SINAL - {banca}\n"
                    f"💱 {ativo} | {mercado}\n"
                    f"⏱ M{timeframe}\n"
                    f"🎯 {direcao.upper()}\n"
                    f"🔥 Score: {score}/100\n"
                    f"💰 ${valores[0]:.2f} → "
                    f"G1 ${valores[1]:.2f} → "
                    f"G2 ${valores[2]:.2f}"
                )


                with estado_lock:

                    estado_frentes[banca][
                        "ultimo_sinal"
                    ] = {
                        "ativo": ativo,
                        "direcao": direcao,
                        "score": score,
                        "timeframe": timeframe,
                        "timestamp": datetime.now(
                            timezone.utc
                        ).isoformat()
                    }


            resultado, lucro = aguardar_resultado(
                order_id
            )


            log.info(
                "RESULTADO | %s | %s | M%s | %s | %s | lucro=%s",
                banca,
                ativo,
                timeframe,
                nomes[nivel],
                resultado,
                lucro
            )


            if resultado == "win":

                registrar_win(
                    banca,
                    nivel
                )

                if nivel == 0:
                    texto_resultado = "WIN"

                elif nivel == 1:
                    texto_resultado = "WIN G1"

                else:
                    texto_resultado = "WIN G2"


                wins = stats_global[
                    "wins"
                ]

                losses = stats_global[
                    "losses"
                ]

                taxa = taxa_acerto(
                    wins,
                    losses
                )


                telegram(
                    f"✅ {texto_resultado}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | M{timeframe}\n"
                    f"📊 {wins} WIN / {losses} LOSS\n"
                    f"🎯 Assertividade: {taxa}%"
                )

                return


            if resultado == "draw":

                log.info(
                    "%s | DRAW | repetindo mesmo nível",
                    banca
                )

                # Nesta versão, empate encerra o ciclo
                # sem contar como loss.
                telegram(
                    f"➖ DRAW\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | M{timeframe}"
                )

                return


            if resultado == "timeout":

                log.warning(
                    "%s | timeout aguardando resultado.",
                    banca
                )

                return


            # LOSS
            # Se ainda houver Gale, continua.
            if nivel < 2:

                log.info(
                    "%s | LOSS %s | preparando %s",
                    banca,
                    nomes[nivel],
                    nomes[nivel + 1]
                )

                time.sleep(1)

                continue


            # =================================================
            # LOSS FINAL
            # =================================================

            registrar_loss(
                banca
            )

            wins = stats_global[
                "wins"
            ]

            losses = stats_global[
                "losses"
            ]

            taxa = taxa_acerto(
                wins,
                losses
            )


            telegram(
                f"❌ LOSS\n"
                f"🏦 {banca}\n"
                f"💱 {ativo} | M{timeframe}\n"
                f"📊 {wins} WIN / {losses} LOSS\n"
                f"🎯 Assertividade: {taxa}%"
            )

            return


    finally:

        liberar_ativo(
            ativo
        )

        with estado_lock:

            estado_frentes[banca][
                "ocupada"
            ] = False

            estado_frentes[banca][
                "ativo"
            ] = None

            estado_frentes[banca][
                "direcao"
            ] = None

            estado_frentes[banca][
                "score"
            ] = None

            estado_frentes[banca][
                "nivel"
            ] = None

            estado_frentes[banca][
                "order_id"
            ] = None


# ============================================================
# SCANNER DE CADA BANCA
# ============================================================

def scanner_banca(
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
                    "%s | sem conexão.",
                    banca
                )

                time.sleep(10)
                continue


            with estado_lock:

                ocupada = estado_frentes[
                    banca
                ][
                    "ocupada"
                ]


            if ocupada:

                time.sleep(2)
                continue


            ativos = descobrir_ativos_digitais()


            if not ativos:

                log.warning(
                    "%s | nenhum ativo DIGITAL disponível.",
                    banca
                )

                time.sleep(
                    INTERVALO_ANALISE
                )

                continue


            log.info(
                "%s | M%s | analisando %d ativos DIGITAL",
                banca,
                timeframe,
                len(ativos)
            )


            melhor = None


            for ativo in ativos:

                if instrumento_bloqueado(
                    ativo,
                    timeframe
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


                analise = analisar_candles(
                    candles
                )


                if analise:

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
                "%s | erro scanner: %s",
                banca,
                e
            )

            time.sleep(5)


# ============================================================
# STATUS WEB
# ============================================================

@app.route("/")
def home():

    try:

        conectado_agora = conectado()

        saldo = None

        if conectado_agora:

            try:

                with api_lock:
                    saldo = api.get_balance()

            except Exception:
                saldo = None


        with estado_lock:

            frentes_status = {}

            for nome, dados in estado_frentes.items():

                frentes_status[nome] = {
                    **dados,
                    "taxa": taxa_acerto(
                        dados["wins"],
                        dados["losses"]
                    )
                }


            global_status = {
                **stats_global,
                "taxa": taxa_acerto(
                    stats_global["wins"],
                    stats_global["losses"]
                )
            }


        ativos_cache = list(
            ativos_digitais_cache
        )


        return jsonify({

            "versao": VERSAO,

            "status": "online",

            "iq_conectada": conectado_agora,

            "conta": CONTA_PERMITIDA,

            "saldo": saldo,

            "executar_ordens": EXECUTAR_ORDENS,

            "entrada_base": ENTRADA_BASE,

            "gestao": [
                ENTRADA_BASE,
                ENTRADA_BASE * 2,
                ENTRADA_BASE * 4
            ],

            "score_min": SCORE_MIN,

            "ativos_digitais_abertos": ativos_cache,

            "quantidade_ativos_digitais": len(
                ativos_cache
            ),

            "ativos_em_uso": list(
                ativos_em_uso
            ),

            "instrumentos_bloqueados": len(
                instrumentos_bloqueados
            ),

            "estatisticas": global_status,

            "frentes": frentes_status

        })


    except Exception as e:

        return jsonify({
            "status": "erro",
            "erro": str(e)
        }), 500


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "versao": VERSAO,
        "conta": CONTA_PERMITIDA
    })


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def iniciar():

    log.info(
        "================================================"
    )

    log.info(
        "%s",
        VERSAO
    )

    log.info(
        "CONTA TRAVADA: %s",
        CONTA_PERMITIDA
    )

    log.info(
        "EXECUTAR_ORDENS=%s",
        EXECUTAR_ORDENS
    )

    log.info(
        "ENTRADA: %.2f -> %.2f -> %.2f",
        ENTRADA_BASE,
        ENTRADA_BASE * 2,
        ENTRADA_BASE * 4
    )

    log.info(
        "SCORE_MIN=%s",
        SCORE_MIN
    )

    log.info(
        "================================================"
    )


    conectar()


    # Primeira descoberta de ativos
    try:
        descobrir_ativos_digitais(
            forcar=True
        )
    except Exception as e:

        log.warning(
            "Primeira consulta de ativos falhou: %s",
            e
        )


    atrasos = {
        "BANCA 1": 0,
        "BANCA 2": 3,
        "BANCA 3": 6
    }


    for banca in FRENTES:

        def iniciar_thread(
            nome=banca
        ):

            time.sleep(
                atrasos[nome]
            )

            scanner_banca(
                nome
            )


        threading.Thread(
            target=iniciar_thread,
            daemon=True,
            name=banca
        ).start()


# ============================================================
# START
# ============================================================

iniciar()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
            )
