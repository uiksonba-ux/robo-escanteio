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
VERSAO = "IQ-3-FRENTES-V9-DIGITAL-SPOT-SUPERVISOR"
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("iq-v9")

# ============================================================
# CONFIGURAÇÕES
# ============================================================
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Servidor
PORT = int(os.getenv("PORT", "10000"))

# Segurança: conta de treino fixa. Não transformar em variável de ambiente.
CONTA = "PRACTICE"
EXECUTAR_ORDENS = os.getenv("EXECUTAR_ORDENS", "false").lower() == "true"

# Estratégia
SCORE_MIN = int(os.getenv("SCORE_MIN", "65"))
INTERVALO_ANALISE = int(os.getenv("INTERVALO_ANALISE", "20"))
INTERVALO_ENTRE_ATIVOS = float(os.getenv("INTERVALO_ENTRE_ATIVOS", "0.7"))
TEMPO_BLOQUEIO = int(os.getenv("TEMPO_BLOQUEIO", "300"))

# Gestão V8
ENTRADA_BASE = float(os.getenv("ENTRADA_BASE", "2"))
RECUPERACAO_PERCENTUAL = float(os.getenv("RECUPERACAO_PERCENTUAL", "0.10"))
MAX_SINAIS_RECUPERACAO = int(os.getenv("MAX_SINAIS_RECUPERACAO", "10"))
MAX_CICLOS_GESTAO = 4

# Ciclo 1 = entrada + 2 Gales
# Ciclo 2 = entrada + 3 Gales
# Ciclo 3 = entrada + 4 Gales
# Ciclo 4 = entrada + 5 Gales
GALES_POR_CICLO = {
    1: 2,
    2: 3,
    3: 4,
    4: 5,
}

ATIVOS = [
    "EURUSD", "GBPUSD", "EURGBP", "USDJPY", "AUDUSD", "USDCHF",
    "USDCAD", "EURJPY", "GBPJPY", "AUDCAD", "AUDJPY", "EURCAD", "NZDUSD",
    "EURUSD-OTC", "GBPUSD-OTC", "EURGBP-OTC", "USDJPY-OTC",
    "AUDUSD-OTC", "USDCHF-OTC", "USDCAD-OTC", "EURJPY-OTC",
    "GBPJPY-OTC", "AUDCAD-OTC", "AUDJPY-OTC", "EURCAD-OTC", "NZDUSD-OTC",
]

FRENTES = {
    "BANCA 1": {"timeframe_analise": 1, "segundos": 60, "expiracao": 1},
    "BANCA 2": {"timeframe_analise": 5, "segundos": 300, "expiracao": 5},
    "BANCA 3": {"timeframe_analise": 15, "segundos": 900, "expiracao": 5},
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
    "win_g3": 0,
    "win_g4": 0,
    "win_g5": 0,
    "ordens_aceitas": 0,
    "ordens_recusadas": 0,
}

estado_frentes = {
    nome: {
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
        "win_g3": 0,
        "win_g4": 0,
        "win_g5": 0,
        "ordens_aceitas": 0,
        "ordens_recusadas": 0,
        "ultimo_sinal": None,

        # Gestão independente por banca
        "ciclo_gestao": 1,
        "entrada_atual": ENTRADA_BASE,
        "prejuizo_acumulado": 0.0,
        "em_recuperacao": False,
        "sinais_recuperacao": 0,
    }
    for nome in FRENTES
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
            data={"chat_id": CHAT_ID, "text": texto},
            timeout=10,
        )
    except Exception as e:
        log.warning("TELEGRAM ERRO | %s", e)

# ============================================================
# CONEXÃO
# ============================================================
def conectado():
    global api
    try:
        return api is not None and bool(api.check_connect())
    except Exception:
        return False

def conectar():
    global api

    if not IQ_EMAIL or not IQ_PASSWORD:
        log.error("IQ_EMAIL/IQ_PASSWORD ausentes.")
        return False

    with connect_lock:
        if conectado():
            return True

        try:
            log.info("CONECTANDO IQ OPTION...")
            nova = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            ok, motivo = nova.connect()
            log.info("CONNECT | ok=%s | motivo=%s", ok, motivo)

            if not ok:
                return False

            nova.change_balance("PRACTICE")
            time.sleep(1)
            saldo = nova.get_balance()
            api = nova

            log.info("IQ CONECTADA | PRACTICE | saldo=%s", saldo)
            telegram(
                "🤖 ROBÔ V9 ONLINE\n"
                "🧪 CONTA: PRACTICE\n"
                f"💰 Saldo: ${saldo}\n\n"
                "🏦 Banca 1: análise M1 / Digital M1\n"
                "🏦 Banca 2: análise M5 / Digital M5\n"
                "🏦 Banca 3: análise M15 / Digital M5\n\n"
                f"🔥 Score mínimo: {SCORE_MIN}"
            )
            return True

        except Exception as e:
            log.exception("ERRO CONEXÃO | %s", e)
            return False

def garantir_conexao():
    return conectado() or conectar()

def garantir_practice():
    if not garantir_conexao():
        return False
    try:
        api.change_balance("PRACTICE")
        return True
    except Exception:
        return False

# ============================================================
# BLOQUEIO
# ============================================================
def chave_bloqueio(ativo, expiracao):
    return ativo, expiracao

def esta_bloqueado(ativo, expiracao):
    chave = chave_bloqueio(ativo, expiracao)
    limite = bloqueados.get(chave)

    if limite is None:
        return False

    if time.time() >= limite:
        bloqueados.pop(chave, None)
        return False

    return True

def bloquear(ativo, expiracao, motivo):
    bloqueados[chave_bloqueio(ativo, expiracao)] = time.time() + TEMPO_BLOQUEIO
    log.warning(
        "BLOQUEADO | %s | DIGITAL M%s | %ss | %s",
        ativo, expiracao, TEMPO_BLOQUEIO, motivo
    )

# ============================================================
# CANDLES
# ============================================================
def obter_candles(ativo, segundos, quantidade=100):
    if not garantir_conexao():
        return []

    try:
        agora = int(time.time())
        with api_lock:
            dados = api.get_candles(ativo, segundos, quantidade, agora)

        if not dados:
            return []

        candles = []
        agora = int(time.time())

        for c in dados:
            try:
                inicio = int(c.get("from", 0))
                if inicio + segundos > agora:
                    continue

                candles.append({
                    "from": inicio,
                    "open": float(c["open"]),
                    "close": float(c["close"]),
                    "max": float(c["max"]),
                    "min": float(c["min"]),
                })
            except Exception:
                pass

        if len(candles) >= 60:
            with estado_lock:
                ativos_validos.add(ativo)

        return candles

    except Exception as e:
        log.debug("CANDLES ERRO | %s | %s", ativo, e)
        return []

# ============================================================
# INDICADORES
# ============================================================
def media(lista):
    return 0 if not lista else sum(lista) / len(lista)

def desvio(lista):
    if not lista:
        return 0
    m = media(lista)
    return math.sqrt(sum((x - m) ** 2 for x in lista) / len(lista))

def ema(valores, periodo):
    if len(valores) < periodo:
        return None

    resultado = media(valores[:periodo])
    k = 2 / (periodo + 1)

    for preco in valores[periodo:]:
        resultado = preco * k + resultado * (1 - k)

    return resultado

def rsi(valores, periodo=14):
    if len(valores) < periodo + 1:
        return None

    ganhos, perdas = [], []
    inicio = len(valores) - periodo

    for i in range(inicio, len(valores)):
        d = valores[i] - valores[i - 1]
        if d >= 0:
            ganhos.append(d)
            perdas.append(0)
        else:
            ganhos.append(0)
            perdas.append(abs(d))

    g = media(ganhos)
    p = media(perdas)

    if p == 0:
        return 100

    rs = g / p
    return 100 - 100 / (1 + rs)

def macd(valores):
    e12 = ema(valores, 12)
    e26 = ema(valores, 26)

    if e12 is None or e26 is None:
        return None

    return e12 - e26

# ============================================================
# ANÁLISE
# ============================================================
def analisar(candles):
    if len(candles) < 60:
        return None

    closes = [x["close"] for x in candles]
    ultimo = candles[-1]
    preco = ultimo["close"]

    call = 0
    put = 0
    motivos_call = []
    motivos_put = []

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20 is not None and e50 is not None:
        if e20 > e50 and preco > e20:
            call += 20
            motivos_call.append("EMA")
        elif e20 < e50 and preco < e20:
            put += 20
            motivos_put.append("EMA")

    vrsi = rsi(closes)
    if vrsi is not None:
        if 50 <= vrsi <= 70:
            call += 15
            motivos_call.append("RSI")
        elif 30 <= vrsi < 50:
            put += 15
            motivos_put.append("RSI")

    ult20 = closes[-20:]
    mm = media(ult20)
    dp = desvio(ult20)
    superior = mm + 2 * dp
    inferior = mm - 2 * dp

    if preco > mm and preco < superior:
        call += 10
        motivos_call.append("BOLLINGER")
    elif preco < mm and preco > inferior:
        put += 10
        motivos_put.append("BOLLINGER")

    corpo = abs(ultimo["close"] - ultimo["open"])
    amplitude = ultimo["max"] - ultimo["min"]

    if amplitude > 0:
        proporcao = corpo / amplitude

        if proporcao >= 0.55:
            if ultimo["close"] > ultimo["open"]:
                call += 15
                motivos_call.append("PRICE ACTION")
            elif ultimo["close"] < ultimo["open"]:
                put += 15
                motivos_put.append("PRICE ACTION")

    janela = candles[-20:-1]
    resistencia = max(x["max"] for x in janela)
    suporte = min(x["min"] for x in janela)
    distancia = resistencia - suporte

    if distancia > 0:
        posicao = (preco - suporte) / distancia

        if posicao <= 0.30:
            call += 10
            motivos_call.append("SUPORTE")
        elif posicao >= 0.70:
            put += 10
            motivos_put.append("RESISTENCIA")

    janela_break = candles[-11:-1]
    max_anterior = max(x["max"] for x in janela_break)
    min_anterior = min(x["min"] for x in janela_break)

    if preco > max_anterior:
        call += 15
        motivos_call.append("BREAKOUT")
    elif preco < min_anterior:
        put += 15
        motivos_put.append("BREAKOUT")

    vm = macd(closes)

    if vm is not None:
        if vm > 0:
            call += 10
            motivos_call.append("MACD")
        elif vm < 0:
            put += 10
            motivos_put.append("MACD")

    momentum = closes[-1] - closes[-6]

    if momentum > 0:
        call += 5
        motivos_call.append("MOMENTUM")
    elif momentum < 0:
        put += 5
        motivos_put.append("MOMENTUM")

    if call >= put:
        direcao, score, oposto, motivos = "call", call, put, motivos_call
    else:
        direcao, score, oposto, motivos = "put", put, call, motivos_put

    if score < SCORE_MIN or score - oposto < 15:
        return None

    return {
        "direcao": direcao,
        "score": score,
        "oposto": oposto,
        "motivos": motivos,
    }

# ============================================================
# V9 - ORDEM DIGITAL SPOT
# ============================================================
def executar_ordem(banca, ativo, valor, direcao, expiracao):
    if not EXECUTAR_ORDENS:
        return False, "EXECUTAR_ORDENS=false"

    if esta_bloqueado(ativo, expiracao):
        return False, "ativo_bloqueado"

    if not garantir_practice():
        return False, "sem_conexao"

    log.info(
        "V9 BUY DIGITAL SPOT | %s | %s | M%s | %s | $%.2f",
        banca, ativo, expiracao, direcao.upper(), valor
    )

    try:
        metodo = getattr(api, "buy_digital_spot_v2", None)
        nome_metodo = "buy_digital_spot_v2"

        if not callable(metodo):
            metodo = getattr(api, "buy_digital_spot", None)
            nome_metodo = "buy_digital_spot"

        if not callable(metodo):
            return False, "biblioteca_sem_buy_digital_spot"

        with api_lock:
            resposta = metodo(
                ativo,
                float(valor),
                direcao.lower(),
                int(expiracao),
            )

        log.info(
            "V9 RESPOSTA %s | %s | %s",
            nome_metodo, ativo, resposta
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
                "ORDEM ACEITA | %s | %s | M%s | id=%s | PRACTICE",
                ativo, direcao.upper(), expiracao, order_id
            )
            return True, order_id

        with estado_lock:
            stats["ordens_recusadas"] += 1
            estado_frentes[banca]["ordens_recusadas"] += 1

        motivo = str(order_id if order_id is not None else resposta)
        log.warning(
            "ORDEM RECUSADA | %s | M%s | %s",
            ativo, expiracao, motivo
        )
        bloquear(ativo, expiracao, motivo)
        return False, motivo

    except Exception as e:
        log.exception(
            "V9 ERRO ORDEM DIGITAL | %s | M%s | %s",
            ativo, expiracao, e
        )
        bloquear(ativo, expiracao, str(e))
        return False, str(e)

# ============================================================
# RESULTADO
# ============================================================
def aguardar_resultado(order_id):
    inicio = time.time()

    while time.time() - inicio < 900:
        try:
            if not garantir_conexao():
                time.sleep(2)
                continue

            with api_lock:
                resposta = api.check_win_digital_v2(order_id)

            if (
                isinstance(resposta, (tuple, list))
                and len(resposta) >= 2
            ):
                fechado, valor = resposta[0], resposta[1]

                if fechado:
                    try:
                        lucro = float(valor)
                    except Exception:
                        lucro = 0

                    if lucro > 0:
                        return "win", lucro
                    if lucro < 0:
                        return "loss", lucro
                    return "draw", 0

        except Exception as e:
            log.debug("CHECK RESULTADO | %s", e)

        time.sleep(1)

    return "timeout", 0

# ============================================================
# ESTATÍSTICAS E GESTÃO V8
# ============================================================
def taxa(wins, losses):
    total = wins + losses
    return 0 if total == 0 else round(wins / total * 100, 2)


def registrar_win(banca, nivel):
    with estado_lock:
        stats["wins"] += 1
        estado_frentes[banca]["wins"] += 1

        chave = "win_direto" if nivel == 0 else f"win_g{nivel}"

        if chave in stats:
            stats[chave] += 1

        if chave in estado_frentes[banca]:
            estado_frentes[banca][chave] += 1


def registrar_loss(banca):
    with estado_lock:
        stats["losses"] += 1
        estado_frentes[banca]["losses"] += 1


def valores_do_ciclo(entrada, quantidade_gales):
    """
    Gale 2x:
    entrada, G1, G2... até a quantidade permitida no ciclo.
    Ex.: R$2 + 2 Gales => 2, 4, 8.
    """
    return [
        round(float(entrada) * (2 ** nivel), 2)
        for nivel in range(quantidade_gales + 1)
    ]


def resetar_gestao(banca, motivo):
    with estado_lock:
        dados = estado_frentes[banca]
        dados["ciclo_gestao"] = 1
        dados["entrada_atual"] = round(ENTRADA_BASE, 2)
        dados["prejuizo_acumulado"] = 0.0
        dados["em_recuperacao"] = False
        dados["sinais_recuperacao"] = 0

    log.info(
        "GESTÃO RESETADA | %s | entrada=%.2f | motivo=%s",
        banca, ENTRADA_BASE, motivo
    )


def aplicar_loss_ciclo(banca, perda_ciclo):
    """
    LOSS completo:
    - soma a perda ao prejuízo acumulado;
    - entra/continua em recuperação;
    - a próxima entrada = entrada atual + 10% do LOSS deste ciclo;
    - avança no máximo até o ciclo 4;
    - ciclo 1/2/3/4 possuem 2/3/4/5 Gales.
    """
    perda_ciclo = round(abs(float(perda_ciclo)), 2)

    with estado_lock:
        dados = estado_frentes[banca]

        dados["prejuizo_acumulado"] = round(
            dados["prejuizo_acumulado"] + perda_ciclo,
            2
        )
        dados["em_recuperacao"] = True

        ciclo_atual = int(dados["ciclo_gestao"])
        dados["ciclo_gestao"] = min(
            ciclo_atual + 1,
            MAX_CICLOS_GESTAO
        )

        acrescimo = round(
            perda_ciclo * RECUPERACAO_PERCENTUAL,
            2
        )

        dados["entrada_atual"] = round(
            float(dados["entrada_atual"]) + acrescimo,
            2
        )

        return {
            "ciclo": dados["ciclo_gestao"],
            "entrada": dados["entrada_atual"],
            "prejuizo": dados["prejuizo_acumulado"],
            "sinais": dados["sinais_recuperacao"],
            "acrescimo": acrescimo,
        }


def aplicar_win_recuperacao(banca, lucro):
    """
    WIN durante recuperação:
    - abate somente o lucro positivo do prejuízo acumulado;
    - se zerar, volta ao Ciclo 1 / entrada base;
    - se ainda houver prejuízo, permanece na recuperação.
    """
    lucro = max(0.0, round(float(lucro), 2))

    with estado_lock:
        dados = estado_frentes[banca]

        if not dados["em_recuperacao"]:
            return {
                "recuperado": True,
                "restante": 0.0,
                "sinais": 0,
            }

        dados["prejuizo_acumulado"] = round(
            max(0.0, dados["prejuizo_acumulado"] - lucro),
            2
        )

        restante = dados["prejuizo_acumulado"]
        sinais = dados["sinais_recuperacao"]

    if restante <= 0:
        resetar_gestao(banca, "prejuizo_recuperado")
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


def iniciar_sinal_gestao(banca):
    """
    Conta no máximo 10 SINAIS enquanto a banca estiver em recuperação.
    O sinal que causou o primeiro LOSS ainda era o ciclo normal e não entra
    nessa contagem. Os próximos sinais de recuperação contam 1..10.
    """
    with estado_lock:
        dados = estado_frentes[banca]

        if dados["em_recuperacao"]:
            if dados["sinais_recuperacao"] >= MAX_SINAIS_RECUPERACAO:
                return False

            dados["sinais_recuperacao"] += 1

        return True


# ============================================================
# CICLO V8 - 4 CICLOS / 10 SINAIS DE RECUPERAÇÃO
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
        # Se já completou os 10 sinais de recuperação, encerra a recuperação
        # e recomeça em R$2 antes de abrir outro sinal.
        with estado_lock:
            limite_atingido = (
                estado_frentes[banca]["em_recuperacao"]
                and estado_frentes[banca]["sinais_recuperacao"]
                >= MAX_SINAIS_RECUPERACAO
            )

        if limite_atingido:
            resetar_gestao(banca, "limite_10_sinais")
            telegram(
                f"🔄 RECUPERAÇÃO ENCERRADA\n"
                f"🏦 {banca}\n"
                f"Limite de {MAX_SINAIS_RECUPERACAO} sinais atingido.\n"
                f"Nova sequência: Ciclo 1 / entrada {ENTRADA_BASE:.2f}"
            )

        if not iniciar_sinal_gestao(banca):
            return

        with estado_lock:
            dados = estado_frentes[banca]
            ciclo_atual = int(dados["ciclo_gestao"])
            entrada_atual = round(float(dados["entrada_atual"]), 2)
            em_recuperacao = bool(dados["em_recuperacao"])
            sinal_rec = int(dados["sinais_recuperacao"])
            prejuizo_antes = round(float(dados["prejuizo_acumulado"]), 2)

            estado_frentes[banca]["ocupada"] = True
            estado_frentes[banca]["ativo"] = ativo
            estado_frentes[banca]["direcao"] = direcao
            estado_frentes[banca]["score"] = score

        quantidade_gales = GALES_POR_CICLO[ciclo_atual]
        valores = valores_do_ciclo(
            entrada_atual,
            quantidade_gales
        )

        log.info(
            "GESTÃO | %s | ciclo=%s/4 | entrada=%.2f | gales=%s | "
            "recuperacao=%s | sinal_rec=%s/10 | prejuizo=%.2f | valores=%s",
            banca,
            ciclo_atual,
            entrada_atual,
            quantidade_gales,
            em_recuperacao,
            sinal_rec,
            prejuizo_antes,
            valores,
        )

        perdas_deste_sinal = 0.0

        for nivel, valor in enumerate(valores):
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
                    banca, order_id
                )

                # Ordem recusada não deve consumir um dos 10 sinais.
                if em_recuperacao:
                    with estado_lock:
                        estado_frentes[banca]["sinais_recuperacao"] = max(
                            0,
                            estado_frentes[banca]["sinais_recuperacao"] - 1
                        )
                return

            with estado_lock:
                estado_frentes[banca]["order_id"] = order_id

            if nivel == 0:
                mercado = "OTC" if ativo.endswith("-OTC") else "NORMAL"
                sequencia = " → ".join(
                    (
                        f"Entrada {v:.2f}"
                        if i == 0
                        else f"G{i} {v:.2f}"
                    )
                    for i, v in enumerate(valores)
                )

                telegram(
                    "📊 NOVO SINAL\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo} | {mercado}\n"
                    f"📈 Análise: M{timeframe_analise}\n"
                    f"⏱ Digital: M{expiracao}\n"
                    f"🎯 {direcao.upper()}\n"
                    f"🔥 Score: {score}/100\n"
                    f"🔁 Ciclo gestão: {ciclo_atual}/4\n"
                    f"🛡 Gales: {quantidade_gales}\n"
                    f"💰 {sequencia}\n"
                    f"♻️ Recuperação: "
                    f"{'SIM' if em_recuperacao else 'NÃO'}"
                    + (
                        f" ({sinal_rec}/{MAX_SINAIS_RECUPERACAO})\n"
                        f"📉 A recuperar: {prejuizo_antes:.2f}"
                        if em_recuperacao
                        else ""
                    )
                )

                with estado_lock:
                    estado_frentes[banca]["ultimo_sinal"] = {
                        "ativo": ativo,
                        "direcao": direcao,
                        "score": score,
                        "analise": timeframe_analise,
                        "expiracao": expiracao,
                        "ciclo_gestao": ciclo_atual,
                        "entrada": entrada_atual,
                        "gales": quantidade_gales,
                        "recuperacao": em_recuperacao,
                        "sinal_recuperacao": sinal_rec,
                        "data": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }

            resultado, lucro = aguardar_resultado(order_id)

            log.info(
                "RESULTADO | %s | %s | ciclo=%s | nivel=%s | %s | lucro=%s",
                banca, ativo, ciclo_atual, nivel, resultado, lucro
            )

            if resultado == "win":
                registrar_win(banca, nivel)

                nome = "WIN" if nivel == 0 else f"WIN G{nivel}"
                w = stats["wins"]
                l = stats["losses"]

                rec = aplicar_win_recuperacao(
                    banca,
                    lucro
                )

                telegram(
                    f"✅ {nome}\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"🔁 Ciclo: {ciclo_atual}/4\n"
                    f"💵 Lucro informado: {float(lucro):.2f}\n"
                    f"♻️ Restante recuperação: {rec['restante']:.2f}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: {taxa(w, l)}%"
                )

                # Se foi o 10º sinal e ainda não recuperou tudo,
                # encerra a recuperação conforme a regra definida.
                with estado_lock:
                    atingiu_10 = (
                        estado_frentes[banca]["em_recuperacao"]
                        and estado_frentes[banca]["sinais_recuperacao"]
                        >= MAX_SINAIS_RECUPERACAO
                    )

                if atingiu_10:
                    resetar_gestao(
                        banca,
                        "10_sinais_sem_recuperacao_total"
                    )

                return

            if resultado in ("draw", "timeout"):
                log.warning(
                    "%s | %s | ciclo encerrado sem contabilizar LOSS",
                    banca, resultado
                )

                # Não houve resultado válido: não consome sinal de recuperação.
                if em_recuperacao:
                    with estado_lock:
                        estado_frentes[banca]["sinais_recuperacao"] = max(
                            0,
                            estado_frentes[banca]["sinais_recuperacao"] - 1
                        )
                return

            if resultado == "loss":
                perdas_deste_sinal = round(
                    perdas_deste_sinal + abs(float(valor)),
                    2
                )

                if nivel < quantidade_gales:
                    log.info(
                        "%s | LOSS nível %s | iniciando G%s",
                        banca, nivel, nivel + 1
                    )
                    time.sleep(1)
                    continue

                # LOSS completo do sinal/ciclo
                registrar_loss(banca)
                gestao = aplicar_loss_ciclo(
                    banca,
                    perdas_deste_sinal
                )

                w = stats["wins"]
                l = stats["losses"]

                telegram(
                    "❌ LOSS COMPLETO\n"
                    f"🏦 {banca}\n"
                    f"💱 {ativo}\n"
                    f"🔁 Ciclo encerrado: {ciclo_atual}/4\n"
                    f"💸 LOSS do ciclo: {perdas_deste_sinal:.2f}\n"
                    f"➕ 10% somado à próxima entrada: "
                    f"{gestao['acrescimo']:.2f}\n"
                    f"➡️ Próximo ciclo: {gestao['ciclo']}/4\n"
                    f"💰 Próxima entrada: {gestao['entrada']:.2f}\n"
                    f"📉 Prejuízo acumulado: {gestao['prejuizo']:.2f}\n"
                    f"♻️ Sinais recuperação: "
                    f"{gestao['sinais']}/{MAX_SINAIS_RECUPERACAO}\n"
                    f"📊 {w} WIN / {l} LOSS\n"
                    f"🎯 Assertividade: {taxa(w, l)}%"
                )

                # Se este foi o 10º sinal de recuperação e terminou em LOSS,
                # encerra a sequência e volta à base.
                with estado_lock:
                    atingiu_10 = (
                        estado_frentes[banca]["em_recuperacao"]
                        and estado_frentes[banca]["sinais_recuperacao"]
                        >= MAX_SINAIS_RECUPERACAO
                    )

                if atingiu_10:
                    resetar_gestao(
                        banca,
                        "10_sinais_sem_recuperacao_total"
                    )

                return

    finally:
        with estado_lock:
            ativos_em_uso.discard(ativo)
            estado_frentes[banca]["ocupada"] = False
            estado_frentes[banca]["ativo"] = None
            estado_frentes[banca]["direcao"] = None
            estado_frentes[banca]["score"] = None
            estado_frentes[banca]["order_id"] = None


# ============================================================
# SCANNER
# ============================================================
def scanner(banca):
    cfg = FRENTES[banca]
    tf = cfg["timeframe_analise"]
    segundos = cfg["segundos"]
    expiracao = cfg["expiracao"]

    log.info(
        "%s INICIADA | análise=M%s | digital=M%s",
        banca, tf, expiracao
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
                banca, tf, expiracao
            )

            for ativo in ATIVOS:
                if esta_bloqueado(ativo, expiracao):
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
                    time.sleep(INTERVALO_ENTRE_ATIVOS)
                    continue

                analisados += 1
                resultado = analisar(candles)

                if resultado:
                    log.info(
                        "CANDIDATO | %s | %s | análise=M%s | "
                        "digital=M%s | %s | score=%s",
                        banca,
                        ativo,
                        tf,
                        expiracao,
                        resultado["direcao"],
                        resultado["score"],
                    )

                    if (
                        melhor is None
                        or resultado["score"] > melhor["score"]
                    ):
                        melhor = {
                            "ativo": ativo,
                            "direcao": resultado["direcao"],
                            "score": resultado["score"],
                            "motivos": resultado["motivos"],
                        }

                time.sleep(INTERVALO_ENTRE_ATIVOS)

            log.info(
                "%s | FIM VARREDURA | válidos=%s | melhor=%s",
                banca,
                analisados,
                melhor["ativo"] if melhor else "nenhum",
            )

            if melhor:
                ativo = melhor["ativo"]

                with estado_lock:
                    if ativo in ativos_em_uso:
                        time.sleep(INTERVALO_ANALISE)
                        continue

                    ativos_em_uso.add(ativo)

                log.info(
                    "SINAL APROVADO | %s | %s | análise=M%s | "
                    "digital=M%s | %s | score=%s",
                    banca,
                    ativo,
                    tf,
                    expiracao,
                    melhor["direcao"],
                    melhor["score"],
                )

                ciclo(
                    banca,
                    ativo,
                    melhor["direcao"],
                    melhor["score"],
                    tf,
                    expiracao,
                )

            time.sleep(INTERVALO_ANALISE)

        except Exception as e:
            log.exception(
                "SCANNER ERRO | %s | %s",
                banca, e
            )
            time.sleep(5)

# ============================================================
# WORKER / SUPERVISOR V9
# ============================================================
scanner_threads = {}
scanner_threads_lock = threading.RLock()


def iniciar_scanner_supervisionado(banca, atraso=0):
    def alvo():
        if atraso:
            time.sleep(atraso)
        scanner(banca)

    thread = threading.Thread(
        target=alvo,
        daemon=True,
        name=f"scanner-{banca}",
    )
    thread.start()

    with scanner_threads_lock:
        scanner_threads[banca] = thread

    log.info(
        "V9 SUPERVISOR | scanner iniciado | %s | thread=%s",
        banca, thread.name
    )
    return thread


def supervisor():
    log.info("V9 SUPERVISOR INICIADO")

    while True:
        try:
            with scanner_threads_lock:
                snapshot = dict(scanner_threads)

            for banca in FRENTES:
                thread = snapshot.get(banca)
                if thread is None or not thread.is_alive():
                    log.error(
                        "V9 SUPERVISOR | scanner parado | %s | reiniciando",
                        banca
                    )
                    iniciar_scanner_supervisionado(banca, 0)

            time.sleep(15)

        except Exception as e:
            log.exception("V9 SUPERVISOR ERRO | %s", e)
            time.sleep(5)


def worker():
    log.info("WORKER V9 INICIADO")

    while not conectar():
        time.sleep(10)

    for indice, banca in enumerate(FRENTES):
        iniciar_scanner_supervisionado(banca, indice * 3)

    threading.Thread(
        target=supervisor,
        daemon=True,
        name="supervisor-scanners",
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
        frentes = {
            nome: {
                **dados,
                "assertividade": taxa(
                    dados["wins"],
                    dados["losses"]
                ),
            }
            for nome, dados in estado_frentes.items()
        }

        geral = {
            **stats,
            "assertividade": taxa(
                stats["wins"],
                stats["losses"]
            ),
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
            "entrada_base": ENTRADA_BASE,
            "recuperacao_percentual": RECUPERACAO_PERCENTUAL,
            "max_sinais_recuperacao": MAX_SINAIS_RECUPERACAO,
            "max_ciclos": MAX_CICLOS_GESTAO,
            "gales_por_ciclo": GALES_POR_CICLO,
            "regra": "proxima entrada = entrada atual + 10% do LOSS completo anterior",
        },
        "frequencias": {
            "BANCA 1": "M1 -> Digital M1",
            "BANCA 2": "M5 -> Digital M5",
            "BANCA 3": "M15 -> Digital M5",
        },
        "estatisticas": geral,
        "frentes": frentes,
        "ativos_com_candles": sorted(ativos_validos),
        "bloqueados": len(bloqueados),
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "versao": VERSAO,
    }), 200

# ============================================================
# START
# ============================================================
log.info("===============================================")
log.info(VERSAO)
log.info("CONTA = PRACTICE")
log.info("EXECUTAR_ORDENS = %s", EXECUTAR_ORDENS)
log.info(
    "GESTÃO = base=%.2f | recuperação=%.0f%% | ciclos=%s | gales=2/3/4/5 | máximo=%s sinais",
    ENTRADA_BASE,
    RECUPERACAO_PERCENTUAL * 100,
    MAX_CICLOS_GESTAO,
    MAX_SINAIS_RECUPERACAO,
)
log.info("MÉTODO V9 = buy_digital_spot_v2 + fallback buy_digital_spot + supervisor + gestão 4 ciclos")
log.info("===============================================")

threading.Thread(
    target=worker,
    daemon=True,
    name="worker-iq",
).start()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
    )
