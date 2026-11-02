import os
import time
import math
import logging
import threading
import hmac
import uuid
import requests
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option
from practice_band_config import read_remote_snapshot, publish_telemetry, select_band


# ============================================================
# VERSÃO
# ============================================================

VERSAO = "IQ-DIGITAL-10B-GLOBAL-0A9-REC100-PRACTICE"

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

    if not math.isfinite(valor):
        valor = float(padrao)
        log.warning("%s não finito; usando padrão %s", nome, padrao)

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

ENTRADA_MINIMA = 2.0

MULTIPLICADOR_GALE = 2.0  # multiplicador fixo da progressão

# A posição da ordem inicial aceita determina os gales do sinal: 0..9, 0..9.
GALES_POR_CICLO = 0
MAX_GALES = 2
LIMIAR_ASSERTIVIDADE_SEM_GALE = 60.0

# Padrão local da branch PRACTICE. Configurações autenticadas do monitor de
# testes substituem estes valores somente ao iniciar uma nova operação.
FAIXAS_ASSERTIVIDADE_ATIVAS = True
FAIXAS_ASSERTIVIDADE = (
    (0.0, 50.0, 4, 2.0),
    (50.0, 60.0, 3, 2.0),
    (60.0, 70.0, 2, 2.0),
    (70.0, 80.0, 1, 2.0),
    (80.0, 100.000001, 0, 2.0),
)

def gestao_por_assertividade(wins, losses):
    """Seleciona a gestão global pela janela de assertividade das 10 bancas."""
    if not FAIXAS_ASSERTIVIDADE_ATIVAS:
        return (MAX_GALES if wins + losses <= 0 or
                (100.0 * wins / (wins + losses)) < LIMIAR_ASSERTIVIDADE_SEM_GALE
                else 0), MULTIPLICADOR_GALE
    # Sem amostra, a faixa conservadora inicial é a primeira.
    total = max(0, wins) + max(0, losses)
    percentual = 0.0 if total == 0 else 100.0 * max(0, wins) / total
    for minimo, maximo, gales, multiplicador in FAIXAS_ASSERTIVIDADE:
        if minimo <= percentual < maximo:
            return gales, multiplicador
    raise ValueError("assertividade fora das faixas configuradas")

def gestao_sincronizada_practice(wins, losses, remote_config=None):
    """Only used for new orders; never changes an existing operation context."""
    return select_band(wins, losses, remote_config,
                       gestao_por_assertividade(wins, losses))

def gales_por_assertividade(wins, losses):
    return gestao_por_assertividade(wins, losses)[0]

RECUPERACAO_PERCENTUAL = 1.0
PAYOUT_RECUPERACAO = env_float("PAYOUT_RECUPERACAO", 0.80, 0.01)

QTD_BANCAS = 10
PERCENTUAL_ENTRADA = 0.01


# ============================================================
# HORÁRIO BRASIL
# ============================================================

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora_brasil():
    return datetime.now(FUSO_BRASIL)


# ============================================================
# ENTRADA BASE
# ============================================================

def obter_entrada_base(saldo=None):
    if saldo is None:
        saldo = ultimo_saldo_practice or 0
    valor = Decimal(str(saldo))
    if not valor.is_finite():
        raise ValueError("Saldo PRACTICE não finito")
    return float(max(Decimal(str(ENTRADA_MINIMA)),
                     valor * Decimal(str(PERCENTUAL_ENTRADA))).quantize(
                         Decimal("0.01"), rounding=ROUND_HALF_UP))


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

# Quatro ativos OTC removidos após logs da biblioteca: "not found on consts".
# Não se cria mapeamento de IDs sem confirmação da corretora.
ATIVOS = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "EURGBP-OTC",
    "USDJPY-OTC",
    "USDCHF-OTC",
    "EURJPY-OTC",
    "GBPJPY-OTC",
    "AUDCAD-OTC",
    "NZDUSD-OTC",
]


# ============================================================
# ESTADO GLOBAL
# ============================================================

api = None

api_lock = threading.RLock()
connect_lock = threading.Lock()
estado_lock = threading.RLock()
config_practice_lock = threading.Lock()
telemetria_practice_lock = threading.Lock()
config_practice_remota = None
sessao_monitor_practice = str(uuid.uuid4())
sequencia_monitor_practice = 0

ativos_em_uso = set()
bloqueados = {}
ativos_validos = set()
contador_sinais = 0
reservas_pendentes = {}  # Reservas ainda não enviadas à corretora
vitorias_progressao = 0
perdas_progressao = 0
entradas_globais_aceitas = 0
entradas_normais_desde_reset = 0
wins_base_ponderada = 0
losses_base_ponderada = 0
contador_entrada_lock = threading.Lock()
resultado_global_centavos = 0
ultimo_saldo_practice = None
recuperacao_em_curso = None
operacoes_ativas = {}
ordens_pendentes = {}
ordens_liquidadas = set()
operacoes_concluidas = set()
envio_indeterminado = None


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
        "wins_base_janela": 0,
        "losses_base_janela": 0,
        "entradas_janela": 0,

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

        "entrada_atual": ENTRADA_MINIMA,
        "gales_reativos": GALES_POR_CICLO,

        "prejuizo_acumulado": 0.0,
        "entradas_recuperacao_restantes": 0,
        "recuperacao_estendida": False,

        "em_recuperacao": False,
        "ciclo_em_execucao": False,
        "resultado_liquido": 0.0,
        "envio_indeterminado": False,
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

    global api, ultimo_saldo_practice

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
            with estado_lock:
                ultimo_saldo_practice = float(saldo)

            api = nova

            entrada = obter_entrada_base(saldo)

            log.info(
                "IQ CONECTADA | "
                "PRACTICE | saldo=%s",
                saldo
            )

            telegram(
                "🤖 ROBÔ IQ DIGITAL ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: {saldo}\n"
                f"💵 Entrada-base: 1% do saldo PRACTICE, mínimo R${ENTRADA_MINIMA:.2f}; atual R${entrada:.2f}\n"
                f"🏦 {QTD_BANCAS} bancas; contador e prejuízo líquido globais\n"
                "⏱ M1 + M5 + M15\n"
                "📊 SOMENTE DIGITAL\n"
                f"📈 GALE X{MULTIPLICADOR_GALE:g}\n"
                "🛡 Assertividade global abaixo de 60%: até 2 gales X2; a partir de 60%: sem gale\n"
                "♻️ Recuperação de 100% do prejuízo líquido global, reservada a uma operação por vez\n"
                "♾️ CICLOS ILIMITADOS\n"
                f"📈 Multiplicador fixo nesta versão: X{MULTIPLICADOR_GALE:g}\n\n"
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

    global ultimo_saldo_practice
    try:

        if not garantir_practice():
            return None

        with api_lock:

            saldo = api.get_balance()

        with estado_lock:
            ultimo_saldo_practice = float(saldo)
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


# Consulta de disponibilidade em thread daemon: a biblioteca não oficial
# pode bloquear nesta chamada. Cache impede consultas concorrentes repetidas.
_disponibilidade_lock = threading.Lock()
_disponibilidade_cache = {"dados": None, "ate": 0.0, "consultando": False}

def listar_ativos_otc():
    """Retorna todos os instrumentos digitais OTC abertos, de qualquer categoria.
    Inclui ações (como Google/Amazon), índices e moedas quando publicados pela API.
    """
    with _disponibilidade_lock:
        dados = _disponibilidade_cache["dados"]
        valido = dados is not None and time.time() < _disponibilidade_cache["ate"]
    if valido:
        return sorted(
            ativo for ativo, info in dados.items()
            if isinstance(ativo, str)
            and "OTC" in ativo.upper()
            and isinstance(info, dict)
            and info.get("open") is True
        )
    ativo_digital_disponivel(ATIVOS[0])
    return list(ATIVOS)

def ativo_digital_disponivel(ativo):
    agora = time.time()
    with _disponibilidade_lock:
        dados = _disponibilidade_cache["dados"]
        if dados is not None and agora < _disponibilidade_cache["ate"]:
            info = dados.get(ativo)
            if isinstance(info, dict) and "open" in info:
                return bool(info["open"])
            return None
        if not _disponibilidade_cache["consultando"]:
            _disponibilidade_cache["consultando"] = True
            def consultar():
                try:
                    # Sem api_lock: evita travar as operações caso a API bloqueie.
                    resposta = api.get_all_open_time()
                    digital = resposta.get("digital", {}) if isinstance(resposta, dict) else {}
                    if not isinstance(digital, dict):
                        digital = {}
                    with _disponibilidade_lock:
                        _disponibilidade_cache.update(dados=digital, ate=time.time() + 120)
                    log.info("DISPONIBILIDADE DIGITAL | instrumentos=%s", len(digital))
                except Exception as e:
                    log.warning("DISPONIBILIDADE DIGITAL INDISPONIVEL | %s", e)
                finally:
                    with _disponibilidade_lock:
                        _disponibilidade_cache["consultando"] = False
            threading.Thread(target=consultar, daemon=True, name="consulta-digital").start()
    # Sem confirmação de disponibilidade, não presumimos que esteja fechado.
    return None


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
    try:
        with api_lock:
            saldo_disponivel = float(api.get_balance())
    except Exception:
        return (False, "saldo_indisponivel")
    if not math.isfinite(valor) or not math.isfinite(saldo_disponivel):
        return (False, "valor_ou_saldo_invalido")
    if valor > saldo_disponivel or valor < ENTRADA_MINIMA:
        log.warning("%s | ORDEM BLOQUEADA POR SALDO | valor=%.2f | saldo=%.2f", banca, valor, saldo_disponivel)
        return (False, "saldo_insuficiente")

    if direcao not in ("call", "put"):
        return (False, f"direcao_invalida:{direcao}")

    # Consulta assíncrona com cache: nunca bloqueia a thread de operações.
    disponibilidade = ativo_digital_disponivel(ativo)
    if disponibilidade is False:
        log.info("DIGITAL FECHADO | %s | M%s | ordem ignorada", ativo, expiracao)
        bloquear(ativo, expiracao, "instrumento digital fechado na IQ Option")
        return (False, "instrumento_digital_fechado")
    if disponibilidade is None:
        log.info("DISPONIBILIDADE DIGITAL NAO CONFIRMADA | %s | validacao pela resposta da ordem", ativo)
    if expiracao not in (1, 5, 15):
        motivo = f"expiracao_digital_invalida:{expiracao}"
        log.warning("EXPIRACAO DIGITAL INVALIDA | %s | %s", ativo, expiracao)
        return (False, motivo)

    # Diagnóstico seguro: não envia ordem adicional.
    # Registra contexto mínimo para distinguir instrumento inválido de rejeição.
    log.info(
        "DIAGNOSTICO DIGITAL | banca=%s | ativo=%s | direcao=%s | expiracao=%s | valor=%.2f | conta=PRACTICE",
        banca, ativo, direcao, expiracao, valor
    )

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

        log.info("DIAGNOSTICO RESPOSTA | ativo=%s | expiracao=%s | tipo=%s", ativo, expiracao, type(resposta).__name__)
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

        recusa_confirmada = (
            isinstance(resposta, (tuple, list)) and len(resposta) >= 1
            and resposta[0] is False
        ) or (isinstance(resposta, int) and not isinstance(resposta, bool) and resposta <= 0)
        if not recusa_confirmada:
            return (None, "resposta_de_envio_sem_confirmacao")

        motivo = str(order_id if order_id is not None else resposta)

    except Exception as e:
        motivo = str(e)
        log.exception(
            "ERRO buy_digital_spot_v2 | %s | %s",
            ativo, e
        )
        return (None, "excecao_no_envio_sem_confirmacao")

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

                    except (TypeError, ValueError):
                        log.warning("RESULTADO FINANCEIRO INVÁLIDO | aguardando confirmação da mesma ordem")
                        time.sleep(1)
                        continue

                    if not math.isfinite(lucro):
                        log.warning("RESULTADO FINANCEIRO NÃO FINITO | aguardando confirmação da mesma ordem")
                        time.sleep(1)
                        continue

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


def taxa_ponderada(wins, losses):
    """Taxa real das ordens desde o ultimo reset de 100 entradas iniciais."""
    return taxa(max(0, wins - wins_base_ponderada),
                max(0, losses - losses_base_ponderada))


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
# GALE FIXO X2
# ============================================================

def valores_do_ciclo(
    entrada,
    quantidade_gales=None,
    multiplicador=None
):

    if multiplicador is None:
        multiplicador = MULTIPLICADOR_GALE

    if quantidade_gales is None:
        quantidade_gales = GALES_POR_CICLO

    return [

        round(
            float(entrada)
            * (
                multiplicador
                ** nivel
            ),
            2
        )

        for nivel in range(
            quantidade_gales + 1
        )
    ]


# ============================================================
# GESTÃO GLOBAL E CICLO
# ============================================================

def em_centavos(valor):
    numero = Decimal(str(valor))
    if not numero.is_finite():
        raise ValueError("Valor financeiro não finito")
    return int((numero * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def prejuizo_global_atual():
    with estado_lock:
        return max(0, -resultado_global_centavos) / 100


def calcular_entrada_recuperacao(prejuizo, saldo=None):
    """Base percentual + alvo de 100% da dívida ao payout configurado."""
    alvo = max(Decimal("0"), Decimal(str(prejuizo)))
    adicional = (alvo / Decimal(str(PAYOUT_RECUPERACAO))).quantize(
        Decimal("0.01"), rounding=ROUND_UP)
    return float(Decimal(str(obter_entrada_base(saldo))) + adicional)


def snapshot_gestao_global():
    with estado_lock:
        proxima_gestao = gestao_sincronizada_practice(
            max(0, stats["wins"] - wins_base_ponderada),
            max(0, stats["losses"] - losses_base_ponderada),
            config_practice_remota.config if config_practice_remota else None)
        return {
            "entrada_base": obter_entrada_base(),
            "entrada_percentual": PERCENTUAL_ENTRADA,
            "entrada_minima": ENTRADA_MINIMA,
            "saldo_practice_referencia": ultimo_saldo_practice,
            "resultado_liquido_global": resultado_global_centavos / 100,
            "prejuizo_global": max(0, -resultado_global_centavos) / 100,
            "recuperacao_em_curso": recuperacao_em_curso,
            "entradas_globais_aceitas": entradas_globais_aceitas,
            "gales_proximos": proxima_gestao[0],
            "sequencia_gales": [proxima_gestao[0]],
            "limiar_assertividade_sem_gale": LIMIAR_ASSERTIVIDADE_SEM_GALE,
            "multiplicador_gale": proxima_gestao[1],
            "recuperacao_percentual": RECUPERACAO_PERCENTUAL,
            "payout_recuperacao": PAYOUT_RECUPERACAO,
            "envio_indeterminado": dict(envio_indeterminado) if envio_indeterminado else None,
            "ordens_pendentes": len(ordens_pendentes),
            "ciclos": "ilimitados",
            "regra_gales": "Assertividade global abaixo de 60%: até 2 gales X2; 60% ou mais: sem gale",
            "regra_recuperacao": "100% do prejuízo líquido global; reserva exclusiva por operação",
            "wins_progressao": vitorias_progressao,
            "losses_progressao": perdas_progressao,
        }


def marcar_envio_indeterminado(banca, ativo, nivel, valor, motivo):
    """Sem confirmação de recusa, impede reutilização da posição e da dívida."""
    global envio_indeterminado
    with estado_lock:
        envio_indeterminado = {"banca": banca, "ativo": ativo, "nivel": nivel,
                               "valor": valor, "motivo": str(motivo)}
        estado_frentes[banca]["envio_indeterminado"] = True
    log.error("ENVIO INDETERMINADO | %s | novas ordens pausadas; reconciliar com a corretora", banca)


def credenciais_monitor_practice():
    return (os.getenv("PRACTICE_FAIXAS_URL", "").strip(),
            os.getenv("MONITOR_ROBOT_TOKEN", "").strip() or
            os.getenv("PRACTICE_FAIXAS_TOKEN", "").strip())


def carregar_config_practice():
    """Retém a última versão validada durante falhas temporárias do monitor."""
    global config_practice_remota
    url, token = credenciais_monitor_practice()
    if CONTA != "PRACTICE" or not url or not token:
        return None
    # Serializa consultas concorrentes para uma resposta antiga não substituir
    # uma revisão recém-carregada; nunca mantém locks de ordens durante a rede.
    with config_practice_lock:
        snapshot = read_remote_snapshot(url, token)
        with estado_lock:
            if snapshot is not None:
                config_practice_remota = snapshot
            return config_practice_remota


def sincronizar_monitor_practice():
    """Observador do supervisor existente: configurações e métricas, sem ordens."""
    global sequencia_monitor_practice
    url, token = credenciais_monitor_practice()
    if CONTA != "PRACTICE" or not url or not token:
        return False
    with telemetria_practice_lock:
        carregar_config_practice()
        with estado_lock:
            sequencia_monitor_practice += 1
            payload = snapshot_monitor_practice()
            payload.update(robot_session=sessao_monitor_practice,
                           sequence=sequencia_monitor_practice)
        return publish_telemetry(url, token, payload)


def enviar_ordem_gerida(banca, ativo, direcao, expiracao, nivel=0):
    """Escolha, envio e confirmação serializados; a recusa não consome posição."""
    global entradas_globais_aceitas, recuperacao_em_curso, ultimo_saldo_practice
    # Atualiza antes dos locks de operação. Gales usam exclusivamente o contexto
    # aceito da ordem inicial, mesmo se a configuração remota mudar no meio.
    config_remota = None
    snapshot_remoto = None
    if nivel == 0:
        snapshot_remoto = carregar_config_practice()
        config_remota = snapshot_remoto.config if snapshot_remoto else None
    with contador_entrada_lock:
        with estado_lock:
            contexto = operacoes_ativas.get(banca)
            if envio_indeterminado:
                return False, "gestao_pausada_envio_indeterminado", contexto
            if any(o["banca"] == banca for o in ordens_pendentes.values()):
                return False, "ordem_ja_pendente", contexto
            if (nivel == 0 and contexto is not None) or (nivel > 0 and contexto is None):
                return False, "operacao_incompativel", contexto
            if nivel > 0 and (nivel != contexto["ultimo_nivel"] + 1 or nivel > contexto["gales"]):
                return False, "nivel_invalido", contexto

        if not garantir_practice():
            return False, "sem_conexao_practice", contexto
        try:
            with api_lock:
                saldo = float(api.get_balance())
            if not math.isfinite(saldo) or saldo < 0:
                raise ValueError("saldo inválido")
        except Exception:
            log.warning("SALDO PRACTICE INDISPONÍVEL | %s", banca)
            return False, "saldo_indisponivel", contexto

        with estado_lock:
            ultimo_saldo_practice = saldo
            if nivel == 0:
                # O fetch pode ter aguardado outro envio; usa a versão mais
                # recente já validada ao criar o contexto da nova operação.
                if snapshot_remoto is not None:
                    snapshot_remoto = config_practice_remota
                    config_remota = snapshot_remoto.config
                posicao = entradas_globais_aceitas % QTD_BANCAS
                alvo = max(0, -resultado_global_centavos) / 100 if recuperacao_em_curso is None else 0.0
                gales, multiplicador = gestao_sincronizada_practice(
                    max(0, stats["wins"] - wins_base_ponderada),
                    max(0, stats["losses"] - losses_base_ponderada), config_remota)
                contexto = {"posicao": posicao, "numero": entradas_globais_aceitas + 1,
                            "gales": gales, "multiplicador": multiplicador,
                            "config_revision": snapshot_remoto.revision if snapshot_remoto else None,
                            "config_id": snapshot_remoto.config_id if snapshot_remoto else None,
                            "base": obter_entrada_base(saldo),
                            "entrada": calcular_entrada_recuperacao(alvo, saldo),
                            "prejuizo_alocado": alvo, "resultado_centavos": 0,
                            "primeira_ordem": None, "ultimo_nivel": -1, "ativo": ativo}
            valor = valores_do_ciclo(contexto["entrada"], nivel, contexto.get("multiplicador", MULTIPLICADOR_GALE))[nivel]
            # Conservador enquanto a atualização de saldo da corretora pode estar atrasada.
            comprometido = sum(o["valor"] for o in ordens_pendentes.values())
            disponivel = saldo - comprometido - sum(reservas_pendentes.values())
            if valor > disponivel or valor < ENTRADA_MINIMA:
                log.warning("SALDO INSUFICIENTE | %s | valor=%.2f disponível=%.2f", banca, valor, disponivel)
                return False, "saldo_insuficiente", operacoes_ativas.get(banca)
            if nivel == 0 and contexto["prejuizo_alocado"] > 0:
                recuperacao_em_curso = banca
            reservas_pendentes[banca] = valor

        try:
            ok, order_id = executar_ordem(banca, ativo, valor, direcao, expiracao)
        except Exception as erro:
            ok, order_id = None, str(erro)

        with estado_lock:
            if ok is None:
                # Reserva financeira e lease permanecem enquanto o envio é incerto.
                marcar_envio_indeterminado(banca, ativo, nivel, valor, order_id)
                return None, order_id, contexto
            if not ok:
                reservas_pendentes.pop(banca, None)
                if nivel == 0 and recuperacao_em_curso == banca:
                    recuperacao_em_curso = None
                return False, order_id, operacoes_ativas.get(banca)
            if not isinstance(order_id, (int, str)) or isinstance(order_id, bool) or not order_id or order_id in ordens_pendentes or order_id in ordens_liquidadas:
                marcar_envio_indeterminado(banca, ativo, nivel, valor, "identificador de ordem inválido ou repetido")
                return None, "order_id_invalido_ou_repetido", contexto
            reservas_pendentes.pop(banca, None)
            if nivel == 0:
                entradas_globais_aceitas += 1
                contexto["primeira_ordem"] = order_id
                operacoes_ativas[banca] = contexto
                dados = estado_frentes[banca]
                dados["entradas_janela"] += 1
                if dados["entradas_janela"] >= 100:
                    dados["wins_base_janela"] = dados["wins"]
                    dados["losses_base_janela"] = dados["losses"]
                    dados["entradas_janela"] = 0
            contexto["ultimo_nivel"] = nivel
            ordens_pendentes[order_id] = {"banca": banca, "valor": valor, "nivel": nivel}
            estado_frentes[banca].update(order_id=order_id, entrada_atual=contexto["entrada"],
                                         gales_reativos=contexto["gales"],
                                         ciclo_gestao=contexto["numero"],
                                         em_recuperacao=contexto["prejuizo_alocado"] > 0)
            return True, order_id, dict(contexto)


def registrar_resultado_ordem(order_id, resultado, lucro):
    """Cada ordem é conciliada uma vez; só o sinal encerrado entra no ledger."""
    valor = em_centavos(lucro)
    if resultado not in ("win", "loss", "draw") or (resultado == "win" and valor <= 0) or (resultado == "loss" and valor >= 0) or (resultado == "draw" and valor != 0):
        raise ValueError("Resultado de ordem inconsistente")
    with estado_lock:
        if order_id in ordens_liquidadas:
            return False
        ordem = ordens_pendentes[order_id]
        contexto = operacoes_ativas[ordem["banca"]]
        contexto["resultado_centavos"] += valor
        ordens_liquidadas.add(order_id)
        ordens_pendentes.pop(order_id)
        estado_frentes[ordem["banca"]]["order_id"] = None
        return True


def finalizar_operacao_global(banca):
    """Soma P&L líquido uma vez e libera a recuperação, sem apagar créditos."""
    global resultado_global_centavos, recuperacao_em_curso
    with estado_lock:
        contexto = operacoes_ativas.get(banca)
        if contexto is None or estado_frentes[banca]["envio_indeterminado"] or any(o["banca"] == banca for o in ordens_pendentes.values()):
            return None
        if contexto["primeira_ordem"] not in operacoes_concluidas:
            resultado_global_centavos += contexto["resultado_centavos"]
            operacoes_concluidas.add(contexto["primeira_ordem"])
            dados = estado_frentes[banca]
            local = em_centavos(dados["resultado_liquido"]) + contexto["resultado_centavos"]
            dados["resultado_liquido"] = local / 100
            dados["prejuizo_acumulado"] = max(0, -local) / 100
        operacoes_ativas.pop(banca)
        if recuperacao_em_curso == banca:
            recuperacao_em_curso = None
        estado_frentes[banca]["em_recuperacao"] = False
        return snapshot_gestao_global()


def liberar_banca(banca, ativo):
    with estado_lock:
        dados = estado_frentes[banca]
        if banca in operacoes_ativas or dados["envio_indeterminado"] or any(o["banca"] == banca for o in ordens_pendentes.values()):
            return False
        dados.update(ocupada=False, ativo=None, direcao=None, score=None, timeframe=None,
                     expiracao=None, order_id=None, ciclo_em_execucao=False)
        ativos_em_uso.discard(ativo)
        return True


def aguardar_resultado_confirmado(order_id):
    """Timeout não encerra a posição; reconcilia sempre o mesmo identificador."""
    while True:
        try:
            resultado, lucro = aguardar_resultado(order_id)
            if resultado in ("win", "loss", "draw"):
                # Valida antes de entregar o resultado ao ciclo.
                valor = em_centavos(lucro)
                if ((resultado == "win" and valor > 0) or
                        (resultado == "loss" and valor < 0) or
                        (resultado == "draw" and valor == 0)):
                    return resultado, valor / 100
        except Exception as erro:
            log.warning("RESULTADO PENDENTE | reconciliação da mesma ordem | %s", erro)
        log.warning("ORDEM SEM RESULTADO CONFIRMADO | banca e ativo mantidos reservados")
        time.sleep(5)


def ciclo(banca, ativo, direcao, score, timeframe_analise, expiracao):
    ativo = str(ativo).strip().upper()
    global vitorias_progressao, perdas_progressao
    with estado_lock:
        dados = estado_frentes[banca]
        if not dados["ocupada"] or dados["ativo"] != ativo or dados["ciclo_em_execucao"]:
            return
        dados["ciclo_em_execucao"] = True
    try:
        ok, order_id, contexto = enviar_ordem_gerida(banca, ativo, direcao, expiracao)
        if not ok:
            return
        gales = contexto["gales"]
        valores = valores_do_ciclo(contexto["entrada"], gales, contexto.get("multiplicador", MULTIPLICADOR_GALE))
        log.info("GESTÃO GLOBAL | %s | operação=%s | posição=%s | gales=%s | base=%.2f | entrada=%.2f | X2 | recuperação=%.2f",
                 banca, contexto["numero"], contexto["posicao"], gales,
                 contexto["base"], contexto["entrada"], contexto["prejuizo_alocado"])
        with estado_lock:
            estado_frentes[banca]["ultimo_sinal"] = {
                "ativo": ativo, "direcao": direcao, "score": score,
                "timeframe": timeframe_analise, "expiracao": expiracao,
                "ciclo": contexto["numero"], "posicao_global": contexto["posicao"],
                "entrada": contexto["entrada"], "gales": gales,
                "multiplicador": contexto.get("multiplicador", MULTIPLICADOR_GALE),
                "recuperacao": contexto["prejuizo_alocado"] > 0,
                "prejuizo_antes": contexto["prejuizo_alocado"],
                "data": datetime.now(timezone.utc).isoformat()}
        sequencia = " → ".join(f"{'Entrada' if i == 0 else f'G{i}'} R${v:.2f}"
                               for i, v in enumerate(valores))
        telegram("📊 NOVO SINAL DIGITAL\n"
                 f"🏦 {banca}\n💱 {ativo}\n🎯 {direcao.upper()}\n"
                 f"📈 Análise M{timeframe_analise} / Digital M{expiracao}\n🔥 Score: {score}/100\n"
                 f"🔢 Operação global: {contexto['numero']} / posição {contexto['posicao']}\n"
                 f"🛡 Gales: {gales} | X2\n💰 {sequencia}\n"
                 f"♻️ Recuperação global reservada: R${contexto['prejuizo_alocado']:.2f}\n🧪 PRACTICE")

        for nivel in range(gales + 1):
            if nivel > 0:
                ok, order_id, _ = enviar_ordem_gerida(banca, ativo, direcao, expiracao, nivel)
                if not ok:
                    log.warning("CICLO INTERROMPIDO | %s | %s", banca, order_id)
                    return
            resultado, lucro = aguardar_resultado_confirmado(order_id)
            if not registrar_resultado_ordem(order_id, resultado, lucro):
                return
            log.info("RESULTADO | %s | %s | M%s | nivel=%s | %s | %.2f",
                     banca, ativo, timeframe_analise, nivel, resultado, lucro)
            if resultado == "loss" and nivel < gales:
                time.sleep(1)
                continue
            if resultado == "win":
                registrar_win(banca, nivel)
                with estado_lock:
                    vitorias_progressao += 1
            elif resultado == "loss":
                registrar_loss(banca)
                with estado_lock:
                    perdas_progressao += 1
            with estado_lock:
                liquido = operacoes_ativas[banca]["resultado_centavos"] / 100
            gestao = finalizar_operacao_global(banca)
            telegram(f"{'✅ WIN' if resultado == 'win' else '❌ LOSS COMPLETO' if resultado == 'loss' else '➖ EMPATE'}\n"
                     f"🏦 {banca} | {ativo}\n⏱ M{timeframe_analise} / Digital M{expiracao}\n"
                     f"💵 Resultado líquido do sinal: R${liquido:.2f}\n"
                     f"📉 Prejuízo líquido global: R${gestao['prejuizo_global']:.2f}\n"
                     f"🛡 Próxima operação: {gestao['gales_proximos']} gales (faixa de assertividade)\n"
                     f"📊 Global: {stats['wins']} WIN / {stats['losses']} LOSS\n"
                     f"🎯 Assertividade: {taxa_ponderada(stats['wins'], stats['losses'])}%\n"
                     f"💰 Saldo PRACTICE: {texto_saldo()}")
            return
    finally:
        # Se existe ordem incerta, não libera posições nem a recuperação.
        finalizar_operacao_global(banca)
        liberar_banca(banca, ativo)


# ============================================================
# RESERVAR BANCA
# ============================================================

def reservar_banca(ativo, direcao, score, timeframe, expiracao):
    ativo = str(ativo).strip().upper()
    with estado_lock:
        if envio_indeterminado or ativo in ativos_em_uso:
            return None
        for banca in BANCAS:
            dados = estado_frentes[banca]
            if dados["ocupada"]:
                continue
            dados.update({"ocupada": True, "ativo": ativo, "direcao": direcao,
                          "score": score, "timeframe": timeframe, "expiracao": expiracao})
            ativos_em_uso.add(ativo)
            return banca
    return None

def disparar_sinal(ativo, direcao, score, timeframe, expiracao):
    ativo = str(ativo).strip().upper()
    banca = reservar_banca(ativo, direcao, score, timeframe, expiracao)
    if banca is None:
        return False
    log.info("SINAL RESERVADO | %s | %s | %s | M%s", banca, ativo, direcao, timeframe)
    threading.Thread(target=ciclo, args=(banca, ativo, direcao, score, timeframe, expiracao),
                     daemon=True, name=f"operacao-{banca}-M{timeframe}-{ativo}").start()
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

            for ativo in listar_ativos_otc():

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

            sincronizar_monitor_practice()
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
                taxa_ponderada(
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

        "gestao": snapshot_gestao_global(),

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
                "10 bancas paralelas; sequência de gales e recuperação globais"
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
# API DE MONITORAMENTO - SOMENTE LEITURA / PRACTICE
# Não habilita ordens e não expõe credenciais da IQ/Telegram.
# Token deve ser configurado em MONITOR_API_TOKEN no servidor.
# ============================================================

@app.route("/api/v1/monitor", methods=["GET"])
def monitor_practice():
    token = os.getenv("MONITOR_API_TOKEN", "").strip()
    supplied = request.headers.get("Authorization", "")
    if not token:
        return jsonify({"erro": "monitoramento_nao_configurado"}), 503
    if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
        return jsonify({"erro": "nao_autorizado"}), 401

    return jsonify(snapshot_monitor_practice()), 200


def snapshot_monitor_practice():
    """Snapshot atômico compartilhado pela API de leitura e pela telemetria."""
    with estado_lock:
        geral = {
            "wins": int(stats["wins"]),
            "losses": int(stats["losses"]),
            "assertividade": taxa(stats["wins"], stats["losses"]),
        }
        frentes = {
            nome: {
                "wins": int(dados.get("wins", 0)),
                "losses": int(dados.get("losses", 0)),
                # O robô não mantém saldo individual verificável por banca.
                "saldo": None,
            }
            for nome, dados in estado_frentes.items()
        }

        payload = {
            "schema_version": 1,
            "conta": "PRACTICE",
            "modo": "somente_leitura",
            "origem": "robo_practice_memoria_volatil",
            "persistencia": "nao_persistido_reinicia_com_processo",
            "atualizado_em": agora_brasil().isoformat(),
            "estatisticas": geral,
            "bancas": frentes,
        }
        if config_practice_remota and config_practice_remota.revision is not None:
            payload.update(config_revision=config_practice_remota.revision,
                           config_id=config_practice_remota.config_id)
        return payload


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

        **snapshot_gestao_global(),

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
    "ENTRADA BASE = 1%% do saldo PRACTICE; mínimo R$%.2f",
    entrada_inicial
)

log.info(
    "GESTÃO | "
    "GALE X%s | "
    "GALES GLOBAIS 0..9; INICIAL=%s | "
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



