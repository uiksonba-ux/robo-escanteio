import os
import time
import math
import logging
import threading
import hmac
import requests
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from deriv_client import DerivOptionsClient


# ============================================================
# VERSÃO
# ============================================================

VERSAO = "BASKARAIDO-DERIV-DEMO-DIGITAL-10B-REC100"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("baskaraido-deriv")


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

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "").strip()
DERIV_TOKEN = os.getenv("DERIV_TOKEN", "").strip()
DERIV_DEMO_ACCOUNT_ID = os.getenv("DERIV_DEMO_ACCOUNT_ID", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = env_int("PORT", 10000, 1)


# ============================================================
# SEGURANÇA
# ============================================================

# CONTA FIXA DE TREINO.
# NÃO transformar em variável de ambiente.
CONTA = "DEMO"

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

ENTRADA_MINIMA = 0.01

MULTIPLICADOR_GALE = 2.0  # multiplicador fixo da progressão

# Faixas de assertividade global (WINs / operações concluídas), compartilhadas pelas 10 bancas.
GALES_POR_CICLO = 4
MAX_GALES = 4

RECUPERACAO_PERCENTUAL = 1.5
PAYOUT_RECUPERACAO = env_float("PAYOUT_RECUPERACAO", 0.80, 0.01)

QTD_BANCAS = 10

# A assertividade define apenas a quantidade de gales; entrada-base fixa em BRL.
ENTRADA_BASE_BRL = 2.00



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
    """Entrada equivalente a R$ 2, convertida para a moeda da conta DEMO."""
    cliente = api
    valor = getattr(cliente, "base_stake", None) if cliente is not None else None
    if isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor) and valor > 0:
        return float(valor)
    return float(ENTRADA_BASE_BRL)


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
        "expiracao": 15,
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

# Catálogo dinâmico dos ativos disponíveis pela API Deriv Options.\nATIVOS = []


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
ultimo_saldo_demo = None
recuperacao_em_curso = None
operacoes_ativas = {}
ordens_pendentes = {}
ordens_liquidadas = set()
operacoes_concluidas = set()
envios_indeterminados = {}


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
            type(e).__name__
        )


# ============================================================
# CONEXÃO
# ============================================================

# Chamadas de leitura/conexão da biblioteca não oficial podem esperar sem limite.
# Cada chave tem um único worker; uma chamada atrasada nunca gera workers em série.
_chamadas_api_lock = threading.Lock()
_chamadas_api = {}
_cliente_conectando = None
_sessao_lock = threading.RLock()
PRAZO_CONSULTA_API = 5.0
PRAZO_ENVIO_DIGITAL = 10.0
PRAZO_RESULTADO_DIGITAL = 900.0


def chamada_api_limitada(chave, funcao, timeout=5.0, cliente=None):
    cliente = cliente if cliente is not None else api
    identificador = (id(cliente), chave)
    with _chamadas_api_lock:
        chamada = _chamadas_api.get(identificador)
        if chamada is None:
            chamada = {"evento": threading.Event(), "estado": None, "valor": None}
            _chamadas_api[identificador] = chamada

            def executar():
                try:
                    chamada["valor"] = funcao()
                    chamada["estado"] = "ok"
                except Exception as erro:
                    # Mensagens de erro da corretora podem conter dados de sessão.
                    chamada["valor"] = type(erro).__name__
                    chamada["estado"] = "erro"
                finally:
                    chamada["evento"].set()

            threading.Thread(target=executar, daemon=True,
                             name="iq-consulta-" + str(chave)[:30]).start()
    if not chamada["evento"].wait(max(0, timeout)):
        return "timeout", None
    with _chamadas_api_lock:
        if _chamadas_api.get(identificador) is chamada:
            _chamadas_api.pop(identificador, None)
    return chamada["estado"], chamada["valor"]


def conectado():
    if api is None:
        return False
    estado, resposta = chamada_api_limitada("check_connect", api.check_connect,
                                            timeout=1.0)
    return estado == "ok" and resposta is True


def _confirmar_practice_sincrono(cliente):
    # O adapter só abre o endpoint de opções DEMO e valida o account_id configurado.
    return cliente.get_balance_mode() == "DEMO"


def _confirmar_practice(cliente):
    confirmado = cliente is not None and cliente.check_connect() and _confirmar_practice_sincrono(cliente)
    if not confirmado:
        log.error("CONTA DERIV DEMO NAO CONFIRMADA")
    return confirmado


def _conectar_na_sessao(cliente):
    return cliente.connect()


class _EnvioNaoRealizado(Exception):
    """Falha anterior à única invocação: nenhuma ordem saiu desta tentativa."""


def _enviar_na_sessao_practice(cliente, funcao):
    with _sessao_lock:
        if api is not cliente or not _confirmar_practice_sincrono(cliente):
            raise _EnvioNaoRealizado()
        return funcao()


def _saldo_practice_sincrono(cliente, exigir_atual=True):
    with _sessao_lock:
        if (exigir_atual and api is not cliente) or not _confirmar_practice_sincrono(cliente):
            raise ValueError("demo_nao_confirmada")
        return cliente.get_balance()


def conectar():
    global api, ultimo_saldo_demo, _cliente_conectando, ATIVOS
    if not DERIV_APP_ID or not DERIV_TOKEN or not DERIV_DEMO_ACCOUNT_ID:
        log.error("DERIV_APP_ID, DERIV_TOKEN ou DERIV_DEMO_ACCOUNT_ID ausente.")
        return False
    if not connect_lock.acquire(timeout=1):
        log.warning("CONEXAO DERIV EM ANDAMENTO | nova tentativa adiada")
        return False
    try:
        if conectado():
            return True
        if _cliente_conectando is None:
            _cliente_conectando = DerivOptionsClient(
                DERIV_APP_ID, DERIV_TOKEN, DERIV_DEMO_ACCOUNT_ID,
                base_stake_brl=ENTRADA_BASE_BRL,
            )
        nova = _cliente_conectando
        estado, resposta = chamada_api_limitada(
            "connect", lambda: _conectar_na_sessao(nova), 30.0, nova
        )
        if estado != "ok":
            log.error("CONEXAO DERIV INDISPONIVEL | estado=%s", estado)
            if estado != "timeout":
                _cliente_conectando = None
            return False
        _cliente_conectando = None
        ok = isinstance(resposta, (tuple, list)) and len(resposta) >= 1 and resposta[0] is True
        if not ok or not _confirmar_practice(nova):
            return False

        estado, saldo = chamada_api_limitada("balance", nova.get_balance, PRAZO_CONSULTA_API, nova)
        if estado != "ok" or isinstance(saldo, bool):
            log.warning("SALDO DEMO INDISPONIVEL | estado=%s", estado)
            return False
        saldo = float(saldo)
        if not math.isfinite(saldo) or saldo < 0:
            return False

        estado, simbolos = chamada_api_limitada(
            "active_symbols", lambda: nova.get_active_symbols(refresh=True),
            PRAZO_CONSULTA_API, nova
        )
        if estado != "ok" or not isinstance(simbolos, dict) or not simbolos:
            log.error("CATALOGO DERIV INDISPONIVEL | nenhuma ordem será iniciada")
            return False
        ativos = sorted(simbolos)
        if not _sessao_lock.acquire(timeout=PRAZO_CONSULTA_API):
            log.error("SESSAO OCUPADA | publicacao_da_conexao_adiada")
            return False
        try:
            api = nova
            ATIVOS = ativos
        finally:
            _sessao_lock.release()
        with estado_lock:
            ultimo_saldo_demo = saldo

        entrada = obter_entrada_base(saldo)
        moeda = nova.currency
        log.info("DERIV CONECTADA | conta=DEMO confirmada | moeda=%s | ativos=%s", moeda, len(ativos))
        telegram(
            "🤖 Baskaraido Trader PRO ONLINE\n"
            "🧪 Corretora: Deriv | Conta: DEMO confirmada\n"
            f"💰 Saldo DEMO: {saldo:.2f} {moeda}\n"
            f"💵 Entrada-base: R$ {ENTRADA_BASE_BRL:.2f} (≈ {entrada:.2f} {moeda})\n"
            f"🏦 {QTD_BANCAS} bancas compartilhadas\n"
            "⏱ M1 + M5 + M15 | Opções digitais CALL/PUT\n"
            f"📊 Ativos autorizados sem Forex tradicional: {len(ativos)}\n"
            f"📈 GALE X{MULTIPLICADOR_GALE:g}\n"
            "🛡 Gales por assertividade: <50%=4; 50–59%=3; 60–69%=2; 70–79%=1; ≥80%=0\n"
            f"♻️ Recuperação global de {RECUPERACAO_PERCENTUAL:.0%} do prejuízo acumulado\n"
            f"🔥 Score mínimo: {SCORE_MIN} | Diferença mínima: {DIFERENCA_MINIMA}"
        )
        return True
    except Exception as erro:
        log.error("ERRO CONEXAO DERIV | tipo=%s", type(erro).__name__)
        return False
    finally:
        connect_lock.release()

def garantir_conexao():
    return conectado() or conectar()


def garantir_practice():
    return garantir_conexao() and _confirmar_practice(api)


def obter_saldo_atual():
    global ultimo_saldo_demo
    try:
        if not garantir_practice():
            return None
        cliente = api
        estado, resposta = chamada_api_limitada("balance", lambda: _saldo_practice_sincrono(cliente),
                                                PRAZO_CONSULTA_API, cliente)
        if estado != "ok" or isinstance(resposta, bool):
            log.warning("SALDO DEMO INDISPONIVEL | estado=%s", estado)
            return None
        saldo = float(resposta)
        if not math.isfinite(saldo) or saldo < 0:
            return None
        with estado_lock:
            ultimo_saldo_demo = saldo
        return round(saldo, 2)
    except Exception as erro:
        log.warning("SALDO DEMO INDISPONIVEL | tipo=%s", type(erro).__name__)
        return None


def texto_saldo():

    saldo = obter_saldo_atual()

    if saldo is None:
        return "indisponível"

    return f"{saldo:.2f} {getattr(api, 'currency', '')}".strip()


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

_candles_receptor_lock = threading.Lock()


def _consultar_candles_correlacionados(cliente, ativo, segundos, quantidade, agora):
    """Usa request_id do canal get-candles; None significa SDK incompatível."""
    import json
    import types

    baixo = getattr(cliente, "api", None)
    if not isinstance(getattr(baixo, "digital_option_placed_id", None), dict):
        return None
    websocket = getattr(baixo, "websocket", None)
    enviar = getattr(baixo, "send_websocket_request", None)
    if websocket is None or not callable(enviar):
        return None
    with _candles_receptor_lock:
        receptor = getattr(websocket, "_robo_candles_receptor", None)
        if receptor is None:
            original = getattr(websocket, "on_message", None)
            if not isinstance(original, types.MethodType):
                return None
            receptor = {"lock": threading.Lock(), "pedidos": {}}

            def receber(_websocket, mensagem):
                try:
                    # Só conserva candles dos nossos pedidos ainda pendentes.
                    # Não registra payloads de sessão ou de qualquer resposta.
                    dados = json.loads(mensagem)
                    if isinstance(dados, dict) and dados.get("name") == "candles":
                        chave = str(dados.get("request_id", ""))
                        with receptor["lock"]:
                            pedido = receptor["pedidos"].get(chave)
                            corpo = dados.get("msg")
                            if pedido is not None and isinstance(corpo, dict):
                                candles = corpo.get("candles")
                                if isinstance(candles, list):
                                    pedido["dados"] = candles
                                    pedido["evento"].set()
                except (ValueError, TypeError):
                    pass
                finally:
                    original(mensagem)

            # websocket-client 0.56.0 busca wss.on_message em cada frame e
            # invoca callback bound com apenas message. Preserva essa assinatura.
            websocket.on_message = types.MethodType(receber, websocket)
            websocket._robo_candles_receptor = receptor

    mapa = cliente.get_all_ACTIVES_OPCODE()
    if not isinstance(mapa, dict) or ativo not in mapa:
        return "ativo_sem_id_confirmado", None
    request_id = "robo-candles-" + str(time.monotonic_ns())
    pedido = {"evento": threading.Event(), "dados": None}
    with receptor["lock"]:
        receptor["pedidos"][request_id] = pedido
    try:
        corpo = {"name": "get-candles", "version": "2.0", "body": {
            "active_id": int(mapa[ativo]), "split_normalization": True,
            "size": segundos, "to": int(agora), "count": quantidade,
            "": mapa[ativo]}}

        def enviar_pedido():
            enviar(name="sendMessage", msg=corpo, request_id=request_id)
            return request_id

        estado, enviado = chamada_api_limitada("candles_raw:envio", enviar_pedido,
                                               PRAZO_CONSULTA_API, cliente)
        if estado != "ok":
            return estado, None
        if enviado != request_id:
            return "envio_anterior_concluido", None
        if not pedido["evento"].wait(PRAZO_CONSULTA_API):
            return "timeout_resposta_sem_request_id_confirmado", None
        return "ok", pedido["dados"]
    finally:
        with receptor["lock"]:
            receptor["pedidos"].pop(request_id, None)


def obter_candles(ativo, segundos, quantidade=100):
    if not garantir_conexao():
        return []
    try:
        cliente = api
        if not ativo_digital_disponivel(ativo):
            return []
        estado, dados = chamada_api_limitada(
            "candles:" + ativo + ":" + str(segundos),
            lambda: cliente.get_candles(ativo, segundos, quantidade, int(time.time())),
            PRAZO_CONSULTA_API, cliente
        )
        if estado != "ok" or not isinstance(dados, list):
            log.warning("CANDLES INDISPONIVEIS | ativo=%s | segundos=%s | estado=%s", ativo, segundos, estado)
            return []
        agora = int(time.time())
        candles = []
        for candle in dados:
            try:
                inicio = int(candle.get("from", 0))
                if inicio + segundos > agora:
                    continue
                candles.append({
                    "from": inicio, "open": float(candle["open"]),
                    "close": float(candle["close"]), "max": float(candle["max"]),
                    "min": float(candle["min"]),
                })
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
        if len(candles) >= 60:
            with estado_lock:
                ativos_validos.add(ativo)
        return candles
    except Exception as erro:
        log.debug("CANDLES DERIV ERRO | %s | %s", ativo, type(erro).__name__)
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
_disponibilidade_cache = {"dados": None, "ate": 0.0, "consultando": False, "tentar_apos": 0.0}

def listar_ativos_otc():
    """Retorna os símbolos abertos autorizados pelo catálogo Deriv Options."""
    cliente = api
    if cliente is None:
        return list(ATIVOS)
    try:
        return sorted(cliente.get_active_symbols())
    except Exception as erro:
        log.warning("CATALOGO DERIV INDISPONIVEL | tipo=%s", type(erro).__name__)
        return []


def ativo_digital_disponivel(ativo):
    """Confirma catálogo aberto para Options; contratos são revalidados antes da compra."""
    cliente = api
    if cliente is None or not isinstance(ativo, str):
        return None
    try:
        return bool(cliente.is_symbol_tradeable(ativo))
    except Exception:
        return None


_payout_cache = {}
_envios_digitais = {}
_clientes_por_ordem = {}
_envios_digitais_lock = threading.Lock()


def obter_payout_digital(ativo, expiracao):
    cliente = api
    if cliente is None:
        return None
    chave = (id(cliente), ativo, expiracao)
    registro = _payout_cache.get(chave)
    if registro and time.monotonic() < registro["ate"]:
        return registro["payout"]
    estado, payout = chamada_api_limitada(
        "payout:" + ativo + ":" + str(expiracao),
        lambda: cliente.get_digital_payout(ativo, expiracao),
        PRAZO_CONSULTA_API, cliente
    )
    if estado != "ok" or isinstance(payout, bool):
        return None
    try:
        payout = float(payout)
    except (TypeError, ValueError, OverflowError):
        return None
    # Deriv adapter returns net profit per stake as a ratio (e.g. 0.82), not percent.
    if not math.isfinite(payout) or payout <= 0:
        return None
    _payout_cache[chave] = {"payout": payout, "ate": time.monotonic() + 15}
    return payout

def _motivo_recusa_seguro(resposta):
    # Não registrar payload arbitrário, que pode incluir dados da conta/sessão.
    codigos = {"error_place_digital_order", "insufficient_funds", "not_enough_money",
               "instrument_not_found", "instrument_expired", "instrument_suspended"}
    mensagens = {"rejected", "not enough money", "insufficient funds", "instrument not found"}
    if isinstance(resposta, dict):
        codigo = resposta.get("code")
        mensagem = resposta.get("message")
        return ((codigo if isinstance(codigo, str) and codigo in codigos else "recusa_deriv") +
                (":" + mensagem if isinstance(mensagem, str) and mensagem in mensagens else ""))
    return "recusa_iq_option"


def _normalizar_confirmacao(resposta):
    if isinstance(resposta, (tuple, list)) and len(resposta) >= 2:
        ok, identificador = resposta[:2]
        if ok is False:
            return False, _motivo_recusa_seguro(identificador)
        if ok is not True:
            return None, "resposta_de_envio_sem_confirmacao"
    elif isinstance(resposta, int) and not isinstance(resposta, bool):
        identificador = resposta
        if identificador <= 0:
            return False, "recusa_iq_option"
    elif isinstance(resposta, dict):
        if isinstance(resposta.get("code"), str) and resposta.get("code") in {"error_place_digital_order", "insufficient_funds", "not_enough_money",
                                    "instrument_not_found", "instrument_expired", "instrument_suspended"}:
            return False, _motivo_recusa_seguro(resposta)
        return None, "resposta_de_envio_sem_confirmacao"
    else:
        return None, "resposta_de_envio_sem_confirmacao"
    valido = ((isinstance(identificador, int) and not isinstance(identificador, bool) and identificador > 0)
              or (isinstance(identificador, str) and identificador.isdigit() and int(identificador) > 0))
    return (True, identificador) if valido else (None, "identificador_de_ordem_nao_confirmado")


def consultar_confirmacao_envio(banca):
    with _envios_digitais_lock:
        envio = _envios_digitais.get(banca)
    if envio is None:
        return None, "envio_sem_registro"
    if envio.get("falha_envio"):
        return None, "excecao_no_envio_sem_confirmacao"
    if "resposta_terminal" in envio:
        ok, resultado = _normalizar_confirmacao(envio["resposta_terminal"])
    elif envio["modo"] == "raw_v2":
        if envio.get("request_id") is None:
            estado, request_id = chamada_api_limitada(envio["chave"], envio["funcao"],
                                                       timeout=0, cliente=envio["cliente"])
            if estado != "ok":
                if estado == "erro" and request_id == "_EnvioNaoRealizado":
                    with _envios_digitais_lock:
                        _envios_digitais.pop(banca, None)
                    return False, "practice_ou_sessao_alterada_antes_do_envio"
                if estado == "erro":
                    envio["falha_envio"] = True
                return None, "envio_v2_sem_confirmacao"
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                envio["falha_envio"] = True
                return None, "request_id_nao_confirmado"
            envio["request_id"] = request_id
        if envio["request_id"] in envio.get("request_ids_anteriores", ()):
            envio["falha_envio"] = True
            return None, "request_id_reutilizado_sem_confirmacao"
        respostas = envio["cliente"].api.digital_option_placed_id
        resposta = respostas.get(envio["request_id"])
        if resposta is None:
            return None, "aguardando_confirmacao_iq_option"
        ok, resultado = _normalizar_confirmacao(resposta)
    else:
        estado, resposta = chamada_api_limitada(envio["chave"], envio["funcao"],
                                               timeout=0, cliente=envio["cliente"])
        if estado != "ok":
            if estado == "erro" and resposta == "_EnvioNaoRealizado":
                with _envios_digitais_lock:
                    _envios_digitais.pop(banca, None)
                return False, "practice_ou_sessao_alterada_antes_do_envio"
            if estado == "erro":
                envio["falha_envio"] = True
            return None, "envio_v2_sem_confirmacao"
        envio["resposta_terminal"] = resposta
        ok, resultado = _normalizar_confirmacao(resposta)
    if ok is not None:
        with _envios_digitais_lock:
            _envios_digitais.pop(banca, None)
        if ok:
            _clientes_por_ordem[resultado] = envio["cliente"]
            with estado_lock:
                stats["ordens_aceitas"] += 1
                estado_frentes[banca]["ordens_aceitas"] += 1
            log.info("ORDEM ACEITA | banca=%s | ativo=%s | order_id=%s | conta=DEMO",
                     banca, envio["ativo"], resultado)
        else:
            with estado_lock:
                stats["ordens_recusadas"] += 1
                estado_frentes[banca]["ordens_recusadas"] += 1
            log.warning("ORDEM RECUSADA | banca=%s | ativo=%s | motivo=%s",
                        banca, envio["ativo"], resultado)
            bloquear(envio["ativo"], envio["expiracao"], resultado)
    return ok, resultado


def _preparar_envio_v2(cliente, ativo, valor, direcao, expiracao):
    baixo = getattr(cliente, "api", None)
    # Protocolo v2 verificado no SDK. Acesso direto evita o while/pass sem
    # timeout de buy_digital_spot_v2; não muda produto nem reenvia a ordem.
    if isinstance(getattr(baixo, "digital_option_placed_id", None), dict):
        mapa = cliente.get_all_ACTIVES_OPCODE()
        if not isinstance(mapa, dict) or ativo not in mapa:
            return None, "ativo_sem_id_confirmado"
        timestamp = float(baixo.timesync.server_timestamp)
        if not math.isfinite(timestamp) or abs(timestamp - time.time()) > 120:
            return None, "relogio_da_sessao_desatualizado"
        periodo = expiracao * 60
        vencimento = (int(timestamp) // periodo + 1) * periodo
        if vencimento - timestamp <= 30:
            vencimento += periodo
        data = datetime.fromtimestamp(vencimento, timezone.utc).strftime("%Y%m%d%H%M")
        ativo_id = str(mapa[ativo])
        instrumento = ("do" + ativo_id + "A" + data[:8] + "D" + data[8:] +
                       "00T" + str(expiracao) + "M" + ("C" if direcao == "call" else "P") + "SPT")
        log.info("INSTRUMENTO DIGITAL V2 | ativo=%s | expiracao=%s | vencimento_utc=%s",
                 ativo, expiracao, datetime.fromtimestamp(vencimento, timezone.utc).isoformat())
        return {"modo": "raw_v2", "funcao": lambda: baixo.place_digital_option_v2(
                    instrumento, ativo_id, valor), "request_id": None,
                 "request_ids_anteriores": frozenset(baixo.digital_option_placed_id)}, None
    metodo = getattr(cliente, "buy_digital_spot_v2", None)
    if not callable(metodo):
        return None, "biblioteca_sem_buy_digital_spot_v2"
    # Compatibilidade com versões sem o canal v2 público: worker single-flight.
    return {"modo": "wrapper_v2", "funcao": lambda: metodo(ativo, valor, direcao, expiracao)}, None


def executar_ordem(banca, ativo, valor, direcao, expiracao):
    def recusar(motivo):
        log.warning("ORDEM NAO ENVIADA | banca=%s | ativo=%s | motivo=%s", banca, ativo, motivo)
        return False, motivo
    with _envios_digitais_lock:
        pendente = banca in _envios_digitais
    if pendente:
        return consultar_confirmacao_envio(banca)
    if not EXECUTAR_ORDENS:
        return recusar("EXECUTAR_ORDENS=false")
    try:
        ativo = str(ativo).strip().upper()
        direcao = str(direcao).strip().lower()
        valor = round(float(valor), 2)
        expiracao = int(expiracao)
    except (TypeError, ValueError, OverflowError):
        return recusar("parametros_invalidos")
    if not math.isfinite(valor):
        return recusar("valor_ou_saldo_invalido")
    if direcao not in ("call", "put"):
        return recusar("direcao_invalida")
    if expiracao not in (1, 5, 15):
        return recusar("expiracao_digital_invalida")
    if esta_bloqueado(ativo, expiracao):
        return recusar("ativo_bloqueado")
    if not garantir_practice():
        return recusar("sem_conexao")
    cliente = api
    estado, saldo = chamada_api_limitada("balance", lambda: _saldo_practice_sincrono(cliente),
                                         PRAZO_CONSULTA_API, cliente)
    try:
        saldo = float(saldo) if estado == "ok" and not isinstance(saldo, bool) else float("nan")
    except (TypeError, ValueError, OverflowError):
        saldo = float("nan")
    if not math.isfinite(saldo) or saldo < 0:
        return recusar("saldo_indisponivel")
    if valor > saldo or valor < max(ENTRADA_MINIMA, float(getattr(cliente, "base_stake", ENTRADA_MINIMA))):
        return recusar("saldo_insuficiente")
    try:
        disponibilidade = ativo_digital_disponivel(ativo)
    except Exception as erro:
        log.warning("DISPONIBILIDADE DIGITAL INDISPONIVEL | tipo=%s", type(erro).__name__)
        return recusar("disponibilidade_digital_nao_confirmada")
    if disponibilidade is False:
        bloquear(ativo, expiracao, "instrumento Options fechado na Deriv")
        return recusar("instrumento_digital_fechado")
    if disponibilidade is not True:
        return recusar("disponibilidade_digital_nao_confirmada")
    try:
        payout = obter_payout_digital(ativo, expiracao)
    except Exception as erro:
        log.warning("PAYOUT DIGITAL INDISPONIVEL | tipo=%s", type(erro).__name__)
        return recusar("payout_digital_nao_confirmado")
    if payout is None:
        return recusar("payout_digital_nao_confirmado")
    if payout <= 0:
        return recusar("payout_digital_indisponivel")
    try:
        envio, motivo = _preparar_envio_v2(cliente, ativo, valor, direcao, expiracao)
    except Exception as erro:
        log.warning("PREFLIGHT DIGITAL FALHOU | tipo=%s", type(erro).__name__)
        return recusar("instrumento_ou_sessao_invalido")
    if envio is None:
        return recusar(motivo)
    funcao_envio = envio["funcao"]
    def invocar_envio():
        log.info("ORDEM ENVIADA | banca=%s | ativo=%s | direcao=%s | expiracao=%s | valor=%.2f | payout=%.2f | conta=DEMO",
                 banca, ativo, direcao, expiracao, valor, payout)
        return funcao_envio()
    envio.update(cliente=cliente, ativo=ativo, expiracao=expiracao, chave="envio_v2:" + banca,
                 funcao=lambda: _enviar_na_sessao_practice(cliente, invocar_envio))
    # Reconfirmar o modo após consultas e imediatamente antes da única chamada.
    if not garantir_practice():
        return recusar("practice_nao_confirmada_antes_do_envio")
    with _envios_digitais_lock:
        _envios_digitais[banca] = envio
    estado, resposta = chamada_api_limitada(envio["chave"], envio["funcao"],
                                           PRAZO_ENVIO_DIGITAL, envio["cliente"])
    if estado == "erro" and resposta == "_EnvioNaoRealizado":
        with _envios_digitais_lock:
            _envios_digitais.pop(banca, None)
        return recusar("practice_ou_sessao_alterada_antes_do_envio")
    if estado == "ok":
        if envio["modo"] == "raw_v2":
            if not isinstance(resposta, (str, int)) or isinstance(resposta, bool):
                envio["falha_envio"] = True
                return None, "request_id_nao_confirmado"
            envio["request_id"] = resposta
        else:
            # A consulta terminal compartilha o normalizador e o bookkeeping.
            envio["resposta_terminal"] = resposta
            ok, resultado = consultar_confirmacao_envio(banca)
            if ok is None:
                log.error("ORDEM INDETERMINADA | banca=%s | motivo=%s | nao_reenviar=sim", banca, resultado)
            return ok, resultado
        inicio = time.monotonic()
        while time.monotonic() - inicio < PRAZO_ENVIO_DIGITAL:
            ok, resultado = consultar_confirmacao_envio(banca)
            if ok is not None:
                return ok, resultado
            time.sleep(0.1)
    if estado == "erro":
        envio["falha_envio"] = True
    log.error("ORDEM INDETERMINADA | banca=%s | ativo=%s | estado=%s | nao_reenviar=sim",
              banca, ativo, estado)
    return None, "envio_v2_sem_confirmacao"


# ============================================================
# RESULTADO DIGITAL
# ============================================================

def _ler_resultado_digital(cliente, order_id):
    baixo = getattr(cliente, "api", None)
    if isinstance(getattr(baixo, "order_async", None), dict):
        dados = baixo.order_async.get(order_id, {}).get("position-changed", {}).get("msg")
        if not isinstance(dados, dict) or dados.get("status") != "closed":
            return False, None
        if dados.get("close_reason") == "expired":
            try:
                if isinstance(dados["close_profit"], bool) or isinstance(dados["invest"], bool):
                    return False, None
                return True, float(dados["close_profit"]) - float(dados["invest"])
            except (KeyError, TypeError, ValueError):
                return False, None
        if dados.get("close_reason") == "default":
            return True, dados.get("pnl_realized")
        return False, None
    estado, resposta = chamada_api_limitada("resultado:" + str(order_id),
        lambda: cliente.check_win_digital_v2(order_id), PRAZO_CONSULTA_API, cliente)
    return resposta if estado == "ok" else (False, None)


def aguardar_resultado(order_id):
    inicio = time.monotonic()
    while time.monotonic() - inicio < PRAZO_RESULTADO_DIGITAL:
        try:
            if not garantir_conexao():
                time.sleep(2)
                continue
            cliente_original = _clientes_por_ordem.get(order_id)
            resposta = _ler_resultado_digital(cliente_original or api, order_id)
            fechado = isinstance(resposta, (tuple, list)) and len(resposta) >= 2 and resposta[0] is True
            if not fechado and cliente_original is not None and cliente_original is not api:
                resposta = _ler_resultado_digital(api, order_id)
            if isinstance(resposta, (tuple, list)) and len(resposta) >= 2 and resposta[0] is True:
                if isinstance(resposta[1], bool):
                    raise ValueError("resultado booleano")
                lucro = float(resposta[1])
                if not math.isfinite(lucro):
                    raise ValueError("resultado nao finito")
                resultado = "win" if lucro > 0 else "loss" if lucro < 0 else "draw"
                log.info("RESULTADO %s | order_id=%s | lucro=%.2f | fonte=DERIV",
                         resultado.upper(), order_id, lucro)
                _clientes_por_ordem.pop(order_id, None)
                return resultado, lucro
        except Exception as erro:
            log.warning("RESULTADO AINDA NAO CONFIRMADO | order_id=%s | tipo=%s",
                         order_id, type(erro).__name__)
        time.sleep(1)
    log.warning("RESULTADO PENDENTE | order_id=%s | prazo_esgotado_sem_liquidacao", order_id)
    return "timeout", 0


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


def gales_por_assertividade(percentual):
    """Faixas globais: 0-49:4, 50-59:3, 60-69:2, 70-79:1, 80-100:0."""
    valor = max(0.0, min(100.0, float(percentual)))
    if valor < 50: return 4
    if valor < 60: return 3
    if valor < 70: return 2
    if valor < 80: return 1
    return 0


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


def calcular_alvo_recuperacao(prejuizo):
    """Aplica o percentual configurado ao prejuízo global confirmado."""
    valor = Decimal(str(prejuizo))
    percentual = Decimal(str(RECUPERACAO_PERCENTUAL))
    if not valor.is_finite() or not percentual.is_finite():
        raise ValueError("Alvo de recuperação inválido")
    return float(max(Decimal("0"), valor) * percentual)


def calcular_entrada_recuperacao(prejuizo, saldo=None, payout=None):
    """Calcula a entrada fixa mais o alvo de recuperação pelo payout informado."""
    alvo = max(Decimal("0"), Decimal(str(prejuizo)))
    taxa_payout = Decimal(str(PAYOUT_RECUPERACAO if payout is None else payout))
    if not taxa_payout.is_finite() or taxa_payout <= 0:
        raise ValueError("Payout de recuperação inválido")
    adicional = (alvo / taxa_payout).quantize(
        Decimal("0.01"), rounding=ROUND_UP)
    return float(Decimal(str(obter_entrada_base(saldo))) + adicional)


def snapshot_gestao_global():
    with estado_lock:
        return {
            "entrada_base": obter_entrada_base(),
            "entrada_percentual": None,
            "entrada_minima": ENTRADA_MINIMA,
            "saldo_practice_referencia": ultimo_saldo_demo,
            "resultado_liquido_global": resultado_global_centavos / 100,
            "prejuizo_global": max(0, -resultado_global_centavos) / 100,
            "recuperacao_em_curso": recuperacao_em_curso,
            "entradas_globais_aceitas": entradas_globais_aceitas,
            "assertividade_global": taxa_ponderada(stats["wins"], stats["losses"]),
            "gales_proximos": gales_por_assertividade(taxa_ponderada(stats["wins"], stats["losses"])),
            "sequencia_gales": [4, 3, 2, 1, 0],
            "entradas_por_gale": {nivel: obter_entrada_base() for nivel in range(MAX_GALES + 1)},
            "multiplicador_gale": MULTIPLICADOR_GALE,
            "recuperacao_percentual": RECUPERACAO_PERCENTUAL,
            "payout_recuperacao": PAYOUT_RECUPERACAO,
            "envio_indeterminado": dict(next(iter(envios_indeterminados.values()))) if envios_indeterminados else None,
            "envios_indeterminados": [dict(item) for item in envios_indeterminados.values()],
            "envios_indeterminados_count": len(envios_indeterminados),
            "ordens_pendentes": len(ordens_pendentes),
            "ciclos": "ilimitados",
            "regra_gales": "Faixas de assertividade global: <50%=4, <60%=3, <70%=2, <80%=1, >=80%=0 gales",
            "regra_recuperacao": f"{RECUPERACAO_PERCENTUAL:.0%} do prejuízo líquido global; uma recuperação por vez",
            "wins_progressao": vitorias_progressao,
            "losses_progressao": perdas_progressao,
        }


def marcar_envio_indeterminado(banca, ativo, nivel, valor, motivo):
    """Isola a banca e o ativo até confirmar se a ordem foi aceita."""
    with estado_lock:
        envios_indeterminados[banca] = {"banca": banca, "ativo": ativo, "nivel": nivel,
                                        "valor": valor, "motivo": str(motivo)}
        estado_frentes[banca]["envio_indeterminado"] = True
    log.error("ENVIO INDETERMINADO | %s | esta banca e este ativo isolados; demais bancas continuam", banca)


def enviar_ordem_gerida(banca, ativo, direcao, expiracao, nivel=0):
    """Escolha, envio e confirmação serializados; a recusa não consome posição."""
    global entradas_globais_aceitas, recuperacao_em_curso, ultimo_saldo_demo
    with contador_entrada_lock:
        with estado_lock:
            contexto = operacoes_ativas.get(banca)
            if banca in envios_indeterminados:
                log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=envio_indeterminado | reconciliação em andamento", banca, ativo)
                return False, "banca_com_envio_indeterminado", contexto
            if any(o["banca"] == banca for o in ordens_pendentes.values()):
                log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=ordem_ja_pendente", banca, ativo)
                return False, "ordem_ja_pendente", contexto
            if (nivel == 0 and contexto is not None) or (nivel > 0 and contexto is None):
                log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=operacao_incompativel", banca, ativo)
                return False, "operacao_incompativel", contexto
            if nivel > 0 and (nivel != contexto["ultimo_nivel"] + 1 or nivel > contexto["gales"]):
                log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=nivel_invalido | nivel=%s", banca, ativo, nivel)
                return False, "nivel_invalido", contexto

        if not garantir_practice():
            log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=sem_conexao_practice", banca, ativo)
            return False, "sem_conexao_practice", contexto
        try:
            saldo = float(obter_saldo_atual())
            if not math.isfinite(saldo) or saldo < 0:
                raise ValueError("saldo inválido")
        except Exception:
            log.warning("SALDO DEMO INDISPONÍVEL | %s", banca)
            return False, "saldo_indisponivel", contexto

        # A consulta pode bloquear até seu prazo; nunca segurar estado_lock
        # durante I/O. O ledger é relido abaixo antes de reservar a dívida.
        with estado_lock:
            consultar_payout = nivel == 0 and recuperacao_em_curso is None and resultado_global_centavos < 0
        payout_real = None
        payout_efetivo = None
        if consultar_payout:
            try:
                resposta_payout = obter_payout_digital(ativo, expiracao)
                if isinstance(resposta_payout, bool):
                    raise ValueError("payout booleano")
                payout_real = float(resposta_payout)
                if not math.isfinite(payout_real) or not 0 < payout_real <= 1:
                    raise ValueError("payout inválido")
                payout_efetivo = min(PAYOUT_RECUPERACAO, payout_real)
            except Exception:
                payout_real = None

        with estado_lock:
            ultimo_saldo_demo = saldo
            if nivel == 0:
                assertividade_atual = taxa_ponderada(stats["wins"], stats["losses"])
                posicao = gales_por_assertividade(assertividade_atual)
                alvo = calcular_alvo_recuperacao(max(0, -resultado_global_centavos) / 100) if recuperacao_em_curso is None else 0.0
                if alvo > 0 and payout_efetivo is None:
                    log.warning("RECUPERACAO ADIADA | banca=%s | ativo=%s | motivo=payout_nao_confirmado | prejuizo=%.2f | divida_preservada=true", banca, ativo, alvo)
                    alvo = 0.0
                contexto = {"posicao": posicao, "numero": entradas_globais_aceitas + len(reservas_pendentes) + 1,
                            "gales": posicao, "assertividade_global": assertividade_atual,
                            "base": obter_entrada_base(saldo),
                            "entrada": calcular_entrada_recuperacao(alvo, saldo, payout_efetivo)
                            if alvo > 0 else obter_entrada_base(saldo),
                            "prejuizo_alocado": alvo, "resultado_centavos": 0,
                            "payout_recuperacao": payout_efetivo if alvo > 0 else None,
                            "payout_real": payout_real,
                            "primeira_ordem": None, "ultimo_nivel": -1, "ativo": ativo}
            valor = valores_do_ciclo(contexto["entrada"], nivel)[nivel]
            # Conservador enquanto a atualização de saldo da corretora pode estar atrasada.
            comprometido = sum(o["valor"] for o in ordens_pendentes.values())
            disponivel = saldo - comprometido - sum(reservas_pendentes.values())
            if nivel == 0 and contexto["prejuizo_alocado"] > 0 and valor > disponivel:
                # O alvo continua integralmente reservado. Não alocar uma recuperação
                # que não cabe permite aos sinais normais continuarem pela base;
                # somente resultados confirmados podem reduzir o ledger.
                log.warning("RECUPERACAO ADIADA | banca=%s | ativo=%s | motivo=saldo_insuficiente_para_recuperacao_integral | prejuizo=%.2f | entrada_recuperacao=%.2f | disponivel=%.2f | entrada_normal=%.2f | divida_preservada=true",
                            banca, ativo, contexto["prejuizo_alocado"], valor, disponivel, contexto["base"])
                contexto["entrada"] = contexto["base"]
                contexto["prejuizo_alocado"] = 0.0
                contexto["payout_recuperacao"] = None
                valor = contexto["base"]
            if valor > disponivel or valor < ENTRADA_MINIMA:
                log.warning("SINAL BLOQUEADO | banca=%s | ativo=%s | motivo=saldo_insuficiente | nivel=%s | valor=%.2f | saldo=%.2f | comprometido=%.2f | disponivel=%.2f", banca, ativo, nivel, valor, saldo, comprometido, disponivel)
                return False, "saldo_insuficiente", operacoes_ativas.get(banca)
            if nivel == 0 and contexto["prejuizo_alocado"] > 0:
                recuperacao_em_curso = banca
                log.info("RECUPERACAO DIMENSIONADA | banca=%s | ativo=%s | prejuizo_alvo=%.2f | entrada=%.2f | payout_real=%.4f | payout_recuperacao=%.4f | alvo=%.0f%% | conta=DEMO", banca, ativo, contexto["prejuizo_alocado"], valor, contexto["payout_real"], contexto["payout_recuperacao"], RECUPERACAO_PERCENTUAL * 100)
            reservas_pendentes[banca] = valor
            log.info("SALDO RESERVADO | banca=%s | ativo=%s | nivel=%s | valor=%.2f | saldo=%.2f | disponivel=%.2f | recuperacao=%.2f | conta=DEMO", banca, ativo, nivel, valor, saldo, disponivel, contexto["prejuizo_alocado"])

        try:
            ok, order_id = executar_ordem(banca, ativo, valor, direcao, expiracao)
        except Exception as erro:
            ok, order_id = None, type(erro).__name__

        with estado_lock:
            if ok is None:
                # Reserva financeira e lease permanecem enquanto o envio é incerto.
                marcar_envio_indeterminado(banca, ativo, nivel, valor, order_id)
                envios_indeterminados[banca]["contexto"] = dict(contexto)
                return None, order_id, contexto
            if not ok:
                reservas_pendentes.pop(banca, None)
                if nivel == 0 and recuperacao_em_curso == banca:
                    recuperacao_em_curso = None
                log.warning("RESERVA LIBERADA | banca=%s | ativo=%s | nivel=%s | motivo=ordem_nao_aceita", banca, ativo, nivel)
                return False, order_id, operacoes_ativas.get(banca)
            return confirmar_ordem_gerida(banca, ativo, nivel, valor, order_id, contexto)


def confirmar_ordem_gerida(banca, ativo, nivel, valor, order_id, contexto):
    """Confirmação imediata ou tardia compartilha a mesma transição contábil."""
    global entradas_globais_aceitas
    with estado_lock:
        if not isinstance(order_id, (int, str)) or isinstance(order_id, bool) or not order_id or order_id in ordens_pendentes or order_id in ordens_liquidadas:
            marcar_envio_indeterminado(banca, ativo, nivel, valor, "identificador de ordem inválido ou repetido")
            envios_indeterminados[banca]["contexto"] = dict(contexto)
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


def reconciliar_envio_gerido(banca):
    """Consulta a requisição original; timeout nunca libera uma ordem incerta."""
    global recuperacao_em_curso
    with contador_entrada_lock:
        with estado_lock:
            pendente = envios_indeterminados.get(banca)
            if not pendente:
                return False, "sem_envio_indeterminado", operacoes_ativas.get(banca)
            contexto = operacoes_ativas.get(banca) if pendente["nivel"] > 0 else pendente.get("contexto")
            if contexto is None:
                return None, "reconciliacao_manual_necessaria", None
        try:
            ok, resposta = consultar_confirmacao_envio(banca)
        except Exception as erro:
            log.warning("CONFIRMACAO PENDENTE | banca=%s | motivo=%s", banca, type(erro).__name__)
            return None, "confirmacao_indisponivel", contexto
        with estado_lock:
            if ok is None:
                return None, resposta, contexto
            if ok:
                confirmado = confirmar_ordem_gerida(banca, pendente["ativo"], pendente["nivel"],
                                                    pendente["valor"], resposta, contexto)
                if confirmado[0] is not True:
                    return confirmado
                log.info("ORDEM ACEITA TARDIA | banca=%s | ativo=%s | order_id=%s | conta=DEMO", banca, pendente["ativo"], resposta)
            else:
                reservas_pendentes.pop(banca, None)
                if pendente["nivel"] == 0 and recuperacao_em_curso == banca:
                    recuperacao_em_curso = None
                confirmado = (False, resposta, operacoes_ativas.get(banca))
                log.warning("ORDEM RECUSADA TARDIA | banca=%s | ativo=%s | reserva_liberada=true | conta=DEMO", banca, pendente["ativo"])
            envios_indeterminados.pop(banca, None)
            estado_frentes[banca]["envio_indeterminado"] = False
            return confirmado


def aguardar_confirmacao_envio_gerido(banca):
    while True:
        ok, resposta, contexto = reconciliar_envio_gerido(banca)
        if ok is not None:
            return ok, resposta, contexto
        log.warning("CONFIRMACAO PENDENTE | banca=%s | reserva_mantida=true | mesmo_envio=true", banca)
        time.sleep(5)


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
        log.info("OPERACAO CONCILIADA | banca=%s | primeira_ordem=%s | resultado_liquido=%.2f | resultado_global=%.2f | prejuizo_global=%.2f", banca, contexto["primeira_ordem"], contexto["resultado_centavos"] / 100, resultado_global_centavos / 100, max(0, -resultado_global_centavos) / 100)
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
            log.warning("RESULTADO PENDENTE | order_id=%s | motivo=%s | reconciliando_mesma_ordem=true", order_id, type(erro).__name__)
        log.warning("ORDEM SEM RESULTADO CONFIRMADO | order_id=%s | banca e ativo mantidos reservados; outras bancas podem executar sinais normais", order_id)
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
        if ok is None:
            ok, order_id, contexto = aguardar_confirmacao_envio_gerido(banca)
        if not ok:
            return
        gales = contexto["gales"]
        valores = valores_do_ciclo(contexto["entrada"], gales)
        log.info("GESTÃO GLOBAL | %s | operação=%s | posição=%s | gales=%s | base=%.2f | entrada=%.2f | X2 | recuperação=%.2f",
                 banca, contexto["numero"], contexto["posicao"], gales,
                 contexto["base"], contexto["entrada"], contexto["prejuizo_alocado"])
        with estado_lock:
            estado_frentes[banca]["ultimo_sinal"] = {
                "ativo": ativo, "direcao": direcao, "score": score,
                "timeframe": timeframe_analise, "expiracao": expiracao,
                "ciclo": contexto["numero"], "posicao_global": contexto["posicao"],
                "entrada": contexto["entrada"], "gales": gales,
                "multiplicador": MULTIPLICADOR_GALE,
                "recuperacao": contexto["prejuizo_alocado"] > 0,
                "prejuizo_antes": contexto["prejuizo_alocado"],
                "data": datetime.now(timezone.utc).isoformat()}
        sequencia = " → ".join(f"{'Entrada' if i == 0 else f'G{i}'} {getattr(api, 'currency', 'USD')} {v:.2f}"
                               for i, v in enumerate(valores))
        telegram("📊 NOVO SINAL | Baskaraido Trader PRO\n"
                 f"🏦 {banca}\n💱 {ativo}\n🎯 {direcao.upper()}\n"
                 f"📈 Análise M{timeframe_analise} / Digital M{expiracao}\n🔥 Score: {score}/100\n"
                 f"🔢 Operação global: {contexto['numero']} / posição {contexto['posicao']}\n"
                 f"🛡 Gales: {gales} | X2\n💰 {sequencia}\n"
                 f"♻️ Recuperação global reservada: {getattr(api, 'currency', 'USD')} {contexto['prejuizo_alocado']:.2f}\n🧪 Deriv DEMO")

        for nivel in range(gales + 1):
            if nivel > 0:
                ok, order_id, _ = enviar_ordem_gerida(banca, ativo, direcao, expiracao, nivel)
                if ok is None:
                    ok, order_id, _ = aguardar_confirmacao_envio_gerido(banca)
                if not ok:
                    log.warning("CICLO INTERROMPIDO | %s | %s", banca, order_id)
                    return
            resultado, lucro = aguardar_resultado_confirmado(order_id)
            if not registrar_resultado_ordem(order_id, resultado, lucro):
                return
            log.info("RESULTADO %s | banca=%s | ativo=%s | order_id=%s | M%s | nivel=%s | lucro=%.2f | conta=DEMO",
                     resultado.upper(), banca, ativo, order_id, timeframe_analise, nivel, lucro)
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
            telegram(f"🤖 Deriv DEMO | {'✅ WIN' if resultado == 'win' else '❌ LOSS COMPLETO' if resultado == 'loss' else '➖ EMPATE'}\n"
                     f"🏦 {banca} | {ativo}\n⏱ M{timeframe_analise} / Digital M{expiracao}\n"
                     f"💵 Resultado líquido do sinal: {getattr(api, 'currency', 'USD')} {liquido:.2f}\n"
                     f"📉 Prejuízo líquido global: {getattr(api, 'currency', 'USD')} {gestao['prejuizo_global']:.2f}\n"
                     f"🛡 Próxima posição global: {gestao['gales_proximos']} gales\n"
                     f"📊 Global: {stats['wins']} WIN / {stats['losses']} LOSS\n"
                     f"🎯 Assertividade: {taxa_ponderada(stats['wins'], stats['losses'])}%\n"
                     f"💰 Saldo Deriv DEMO: {texto_saldo()} {getattr(api, 'currency', '')}")
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
        if ativo in ativos_em_uso:
            motivo = "ativo_em_uso"
            log.warning("SINAL SEM RESERVA | ativo=%s | M%s | motivo=%s", ativo, timeframe, motivo)
            return None
        for banca in BANCAS:
            dados = estado_frentes[banca]
            if dados["ocupada"]:
                continue
            dados.update({"ocupada": True, "ativo": ativo, "direcao": direcao,
                          "score": score, "timeframe": timeframe, "expiracao": expiracao})
            ativos_em_uso.add(ativo)
            return banca
        log.warning("SINAL SEM RESERVA | ativo=%s | M%s | motivo=sem_banca_livre", ativo, timeframe)
    return None

def disparar_sinal(ativo, direcao, score, timeframe, expiracao):
    ativo = str(ativo).strip().upper()
    log.info("SINAL APROVADO | ativo=%s | direcao=%s | M%s | expiracao=%s | score=%s", ativo, direcao, timeframe, expiracao, score)
    banca = reservar_banca(ativo, direcao, score, timeframe, expiracao)
    if banca is None:
        return False
    log.info("SINAL APROVADO E DISTRIBUÍDO | banca=%s | ativo=%s | direcao=%s | M%s | expiracao=%s | reserva_banca=true", banca, ativo, direcao, timeframe, expiracao)
    try:
        threading.Thread(target=ciclo, args=(banca, ativo, direcao, score, timeframe, expiracao),
                         daemon=True, name=f"operacao-{banca}-M{timeframe}-{ativo}").start()
    except Exception as erro:
        liberar_banca(banca, ativo)
        log.error("SINAL NAO INICIADO | banca=%s | ativo=%s | motivo=falha_thread | tipo=%s", banca, ativo, type(erro).__name__)
        return False
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

            with scanner_threads_lock:
                scanner_atividade[timeframe] = time.monotonic()

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

                with scanner_threads_lock:
                    scanner_atividade[timeframe] = time.monotonic()

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

                    disparar_sinal(
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

                time.sleep(
                    INTERVALO_ENTRE_ATIVOS
                )

            time.sleep(
                INTERVALO_ANALISE
            )

        except Exception as e:

            log.error(
                "SCANNER M%s ERRO | %s",
                timeframe,
                type(e).__name__
            )

            time.sleep(5)


# ============================================================
# SUPERVISOR
# ============================================================

scanner_threads = {}
scanner_atividade = {}

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
        scanner_atividade.setdefault(timeframe, time.monotonic())

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

    ultimo_alerta = {}
    ultima_telemetria = None
    while True:

        try:
            instante = time.monotonic()
            if ultima_telemetria is None or instante - ultima_telemetria >= 60:
                with estado_lock:
                    log.info("ESTADO EXECUCAO | conta=DEMO | habilitada=%s | aceitas=%s | recusadas=%s | wins=%s | losses=%s | pendentes=%s | envios_indeterminados=%s | prejuizo_global=%.2f | recuperacao_banca=%s",
                             EXECUTAR_ORDENS, stats["ordens_aceitas"], stats["ordens_recusadas"],
                             stats["wins"], stats["losses"], len(ordens_pendentes),
                             bool(envios_indeterminados), max(0, -resultado_global_centavos) / 100,
                             recuperacao_em_curso)
                ultima_telemetria = instante

            with scanner_threads_lock:

                snapshot = dict(
                    scanner_threads
                )
                atividade = dict(scanner_atividade)

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
                else:
                    agora = time.monotonic()
                    atraso = agora - atividade.get(timeframe, agora)
                    limite = max(120, INTERVALO_ANALISE * 3)
                    if atraso > limite and agora - ultimo_alerta.get(timeframe, 0) >= 60:
                        # Thread viva não é prova de progresso. Não cria outra
                        # varredura concorrente sobre a sessão ainda ocupada.
                        log.error("SCANNER SEM PROGRESSO | M%s | segundos=%.0f | thread_viva=true",
                                  timeframe, atraso)
                        ultimo_alerta[timeframe] = agora

            time.sleep(15)

        except Exception as e:

            log.error(
                "SUPERVISOR ERRO | %s",
                type(e).__name__
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

        "deriv_conectada":
            conectado(),

        "conta":
            "DEMO",

        "moeda": getattr(api, "currency", None),

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
                "Digital M15",
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
# API DE MONITORAMENTO - SOMENTE LEITURA / DEMO
# Não habilita ordens e não expõe credenciais da Deriv/Telegram.
# Token deve ser configurado em MONITOR_API_TOKEN no servidor.
# ============================================================

@app.route("/api/v1/monitor", methods=["GET"])
def monitor_practice():
    token = os.getenv("MONITOR_API_TOKEN", "").strip()
    supplied = request.headers.get("Authorization", "")
    if not token:
        return jsonify({"erro": "monitoramento_nao_configurado"}), 503
    if not hmac.compare_digest(supplied, "Bearer " + token):
        return jsonify({"erro": "nao_autorizado"}), 401

    with estado_lock:
        geral = {
            "wins": int(stats["wins"]),
            "losses": int(stats["losses"]),
            "assertividade": taxa_ponderada(stats["wins"], stats["losses"]),
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

    return jsonify({
        "schema_version": 1,
        "conta": "DEMO",
        "modo": "somente_leitura",
        "origem": "robo_practice_memoria_volatil",
        "persistencia": "nao_persistido_reinicia_com_processo",
        "atualizado_em": agora_brasil().isoformat(),
        "estatisticas": geral,
        "bancas": frentes,
    }), 200


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    with estado_lock:
        estatisticas = dict(stats)
    with scanner_threads_lock:
        instante = time.monotonic()
        scanners = {str(tf): {"thread_viva": thread.is_alive(),
                             "segundos_sem_progresso": round(max(0, instante - scanner_atividade.get(tf, instante)), 1)}
                    for tf, thread in scanner_threads.items()}
    return jsonify({

        "status":
            "ok",

        "versao":
            VERSAO,

        "conta":
            "DEMO",

        "mercado":
            "DIGITAL",

        **snapshot_gestao_global(),
        "estatisticas_ordens": estatisticas,
        "scanners": scanners,
        "persistencia": "memoria_volatil_reinicia_com_processo",

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
    "CONTA = DEMO"
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
    "ENTRADA BASE FIXA = R$%.2f (convertida para moeda DEMO)",
    entrada_inicial
)

log.info(
    "GESTÃO | "
    "GALE X%s | "
    "GALES POR ASSERTIVIDADE GLOBAL <50=4, 50-59=3, 60-69=2, 70-79=1, >=80=0; INICIAL=%s | "
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



