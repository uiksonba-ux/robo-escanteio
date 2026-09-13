import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = "https://api.5dollarfootballapi.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))

QTD_POR_RODADA = int(os.getenv("QTD_BILHETES", "8"))

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "600"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "300"))

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-escanteio/2.0"
}

# ============================================================
# CONTROLE
# ============================================================

lock = threading.RLock()

stats = {
    "gols_wins": 0,
    "gols_losses": 0,
    "cantos_wins": 0,
    "cantos_losses": 0,
    "total_sinais": 0,
}

# Evita sinais repetidos
entradas_pre = {}

# Sinais aguardando resultado
pendentes_pre = {}


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def primeiro_valor(*valores):
    for valor in valores:
        if valor is not None and valor != "":
            return valor
    return None


def numero(valor, padrao=0.0):
    try:
        if isinstance(valor, str):
            valor = valor.replace(",", ".").strip()

        return float(valor)

    except Exception:
        return padrao


def inteiro(valor, padrao=0):
    try:
        return int(float(numero(valor, padrao)))

    except Exception:
        return padrao


def nome_time(dado, padrao):
    if isinstance(dado, dict):
        return str(
            primeiro_valor(
                dado.get("name"),
                dado.get("short_name"),
                dado.get("title"),
                padrao
            )
        )

    return str(dado) if dado else padrao


# ============================================================
# DADOS DA PARTIDA
# ============================================================

def extrair_id(jogo):
    fixture = jogo.get("fixture", {}) or {}

    return str(
        primeiro_valor(
            jogo.get("id"),
            fixture.get("id"),
            jogo.get("fixture_id"),
            ""
        )
    )


def extrair_times(jogo):

    teams = jogo.get("teams", {}) or {}

    casa = nome_time(
        primeiro_valor(
            jogo.get("home_team"),
            jogo.get("home"),
            teams.get("home")
        ),
        "Casa"
    )

    fora = nome_time(
        primeiro_valor(
            jogo.get("away_team"),
            jogo.get("away"),
            teams.get("away")
        ),
        "Fora"
    )

    return casa, fora


def extrair_placar(jogo):

    gols = jogo.get("goals", {}) or {}
    score = jogo.get("score", {}) or {}

    casa = inteiro(
        primeiro_valor(
            jogo.get("home_score"),
            gols.get("home"),
            score.get("home"),
            0
        )
    )

    fora = inteiro(
        primeiro_valor(
            jogo.get("away_score"),
            gols.get("away"),
            score.get("away"),
            0
        )
    )

    return casa, fora


def extrair_cantos(jogo):

    cantos = primeiro_valor(
        jogo.get("corners"),
        jogo.get("corner")
    )

    if isinstance(cantos, (int, float, str)):
        return inteiro(cantos)

    if isinstance(cantos, dict):

        total = primeiro_valor(
            cantos.get("total"),
            cantos.get("value")
        )

        if total is not None:
            return inteiro(total)

        casa = inteiro(
            primeiro_valor(
                cantos.get("home"),
                0
            )
        )

        fora = inteiro(
            primeiro_valor(
                cantos.get("away"),
                0
            )
        )

        return casa + fora

    return 0


def extrair_status(jogo):

    fixture = jogo.get("fixture", {}) or {}

    status = fixture.get("status", {}) or {}

    return str(
        primeiro_valor(
            jogo.get("status"),
            status.get("short"),
            status.get("long"),
            ""
        )
    ).lower()


def eh_finalizado(jogo):

    status = extrair_status(jogo)

    palavras = [
        "ft",
        "finished",
        "finish",
        "aet",
        "pen"
    ]

    return any(palavra in status for palavra in palavras)


def eh_agendado(jogo):

    status = extrair_status(jogo)

    return any(
        palavra in status
        for palavra in [
            "scheduled",
            "not started",
            "ns"
        ]
    )


# ============================================================
# TELEGRAM
# ============================================================

def tg_msg(mensagem):

    if not TOKEN or not CHAT_ID:

        print(
            "TELEGRAM NÃO CONFIGURADO:",
            mensagem,
            flush=True
        )

        return False

    try:

        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        resposta = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML"
            },
            timeout=15
        )

        if resposta.status_code != 200:

            print(
                f"ERRO TELEGRAM HTTP {resposta.status_code}: "
                f"{resposta.text[:500]}",
                flush=True
            )

            return False

        return True

    except Exception as erro:

        print(
            f"ERRO TELEGRAM: {erro}",
            flush=True
        )

        return False


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        print(
            "ERRO: FIVE_DOLLAR_KEY não configurada.",
            flush=True
        )

        return None

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    try:

        resposta = requests.get(
            url,
            headers=HEADERS,
            params=params or {},
            timeout=25
        )

        print(
            f"API {endpoint} HTTP {resposta.status_code}",
            flush=True
        )

        if resposta.status_code != 200:

            print(
                f"RESPOSTA API: {resposta.text[:1000]}",
                flush=True
            )

            return None

        dados = resposta.json()

        return dados

    except Exception as erro:

        print(
            f"ERRO API {endpoint}: {erro}",
            flush=True
        )

        return None


# ============================================================
# FIXTURES
# ============================================================

def unix_hoje():

    agora = datetime.now(timezone.utc)

    inicio = agora.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    fim = inicio.timestamp() + 86400

    return int(inicio.timestamp()), int(fim)


def buscar_pre():

    inicio, fim = unix_hoje()

    dados = api_get(
        "fixtures",
        {
            "start_time": inicio,
            "end_time": fim,
            "status": "scheduled",
            "per_page": 100,
            "lang": "pt"
        }
    )

    if not dados:
        return []

    jogos = dados.get("data", [])

    if isinstance(jogos, dict):
        jogos = jogos.get("fixtures", [])

    if not isinstance(jogos, list):
        jogos = []

    print(
        f"PRE: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


# ============================================================
# ODDS
# ============================================================

def obter_bloco_odds(jogo_id, mercado):

    dados = api_get(
        f"fixtures/{jogo_id}/odds",
        {
            "market": mercado
        }
    )

    if not dados:
        return None

    data = dados.get("data", dados)

    if not isinstance(data, dict):
        return None

    # Formato com bookmakers
    bookmakers = data.get("bookmakers")

    if isinstance(bookmakers, list):

        for bookmaker in bookmakers:

            if not isinstance(bookmaker, dict):
                continue

            odds = bookmaker.get("odds", {})

            if not isinstance(odds, dict):
                continue

            if mercado == "goalline":
                bloco = odds.get("goal_line")

            elif mercado == "corner":
                bloco = odds.get("corner_line")

            else:
                bloco = None

            if bloco:
                return bloco

    # Formato direto
    odds = data.get("odds", {})

    if isinstance(odds, dict):

        if mercado == "goalline":
            return odds.get("goal_line")

        if mercado == "corner":
            return odds.get("corner_line")

    return None


def extrair_melhor_preco(bloco):

    if not isinstance(bloco, dict):
        return None

    # Preferimos closing.
    # Se ainda não existir, usamos opening.
    for momento in ["closing", "opening"]:

        dados = bloco.get(momento)

        if not isinstance(dados, dict):
            continue

        linha = numero(
            dados.get("line"),
            -999
        )

        over = numero(
            dados.get("over"),
            0
        )

        if linha == -999 or over <= 0:
            continue

        return {
            "linha": linha,
            "odd": over,
            "momento": momento
        }

    return None


# ============================================================
# FILTRO DE GOLS
# ============================================================

def filtra_over_gols(jogo):

    jogo_id = extrair_id(jogo)

    if not jogo_id:
        return None

    bloco = obter_bloco_odds(
        jogo_id,
        "goalline"
    )

    resultado = extrair_melhor_preco(bloco)

    if not resultado:
        return None

    linha = resultado["linha"]
    odd = resultado["odd"]

    # Mantemos as linhas desejadas
    linhas_permitidas = [
        0.5,
        1.5,
        2.5
    ]

    if not any(
        abs(linha - x) < 0.01
        for x in linhas_permitidas
    ):
        return None

    if not (
        ODD_MIN <= odd <= ODD_MAX
    ):
        return None

    return (
        odd,
        f"Over {linha:g} Gols",
        linha
    )


# ============================================================
# FILTRO DE ESCANTEIOS
# ============================================================

def filtra_over_cantos(jogo):

    jogo_id = extrair_id(jogo)

    if not jogo_id:
        return None

    bloco = obter_bloco_odds(
        jogo_id,
        "corner"
    )

    resultado = extrair_melhor_preco(bloco)

    if not resultado:
        return None

    linha = resultado["linha"]
    odd = resultado["odd"]

    linhas_permitidas = [
        6.5,
        7.5,
        8.5,
        9.0,
        9.5,
        10.0,
        10.5
    ]

    if not any(
        abs(linha - x) < 0.01
        for x in linhas_permitidas
    ):
        return None

    if not (
        ODD_MIN <= odd <= ODD_MAX
    ):
        return None

    return (
        odd,
        f"Over {linha:g} Escanteios",
        linha
    )


# ============================================================
# ANALISAR PRÉ-JOGO
# ============================================================

def analisar_pre(jogo):

    fid = extrair_id(jogo)

    if not fid:
        return False

    if not eh_agendado(jogo):
        return False

    casa, fora = extrair_times(jogo)

    gerou = False

    # --------------------------------------------------------
    # GOLS
    # --------------------------------------------------------

    key_gols = f"{fid}_GOLS"

    if key_gols not in entradas_pre:

        resultado = filtra_over_gols(jogo)

        if resultado:

            odd, mercado, linha = resultado

            entradas_pre[key_gols] = True

            pendentes_pre[key_gols] = {
                "fid": fid,
                "tipo": "GOLS",
                "home": casa,
                "away": fora,
                "odd": odd,
                "mercado": mercado,
                "linha": linha
            }

            stats["total_sinais"] += 1

            tg_msg(
                f"⚽ <b>PRE - {mercado.upper()}</b>\n\n"
                f"🏟️ {casa} x {fora}\n"
                f"💰 {mercado} @ {odd:.2f}"
            )

            gerou = True

    # --------------------------------------------------------
    # ESCANTEIOS
    # --------------------------------------------------------

    key_cantos = f"{fid}_CANTOS"

    if key_cantos not in entradas_pre:

        resultado = filtra_over_cantos(jogo)

        if resultado:

            odd, mercado, linha = resultado

            entradas_pre[key_cantos] = True

            pendentes_pre[key_cantos] = {
                "fid": fid,
                "tipo": "CANTOS",
                "home": casa,
                "away": fora,
                "odd": odd,
                "mercado": mercado,
                "linha": linha
            }

            stats["total_sinais"] += 1

            tg_msg(
                f"🚩 <b>PRE - {mercado.upper()}</b>\n\n"
                f"🏟️ {casa} x {fora}\n"
                f"💰 {mercado} @ {odd:.2f}"
            )

            gerou = True

    return gerou


# ============================================================
# RESULTADOS
# ============================================================

def buscar_jogo_por_id(fid):

    dados = api_get(
        f"fixtures/{fid}"
    )

    if not dados:
        return None

    jogo = dados.get("data")

    if isinstance(jogo, dict):
        return jogo

    return None


def finalizar(key, jogo):

    if key not in pendentes_pre:
        return

    if not eh_finalizado(jogo):
        return

    info = pendentes_pre.pop(key)

    casa_gols, fora_gols = extrair_placar(jogo)

    total_gols = casa_gols + fora_gols

    total_cantos = extrair_cantos(jogo)

    linha = numero(
        info.get("linha"),
        0
    )

    # --------------------------------------------------------
    # GOLS
    # --------------------------------------------------------

    if info["tipo"] == "GOLS":

        win = total_gols > linha

        if win:

            stats["gols_wins"] += 1

            emoji = "🟢"
            resultado = "GREEN"

        else:

            stats["gols_losses"] += 1

            emoji = "🔴"
            resultado = "RED"

        tg_msg(
            f"{emoji} <b>{resultado} - {info['mercado']}</b>\n"
            f"🏟️ {info['home']} x {info['away']}\n"
            f"⚽ Placar: {casa_gols} x {fora_gols}\n"
            f"⚽ Total: {total_gols} gols"
        )

    # --------------------------------------------------------
    # ESCANTEIOS
    # --------------------------------------------------------

    else:

        win = total_cantos > linha

        if win:

            stats["cantos_wins"] += 1

            emoji = "🟢"
            resultado = "GREEN"

        else:

            stats["cantos_losses"] += 1

            emoji = "🔴"
            resultado = "RED"

        tg_msg(
            f"{emoji} <b>{resultado} - {info['mercado']}</b>\n"
            f"🏟️ {info['home']} x {info['away']}\n"
            f"🚩 Total: {total_cantos} escanteios"
        )


# ============================================================
# ESTATÍSTICAS
# ============================================================

def resumo_stats():

    gw = stats["gols_wins"]
    gl = stats["gols_losses"]

    cw = stats["cantos_wins"]
    cl = stats["cantos_losses"]

    total_gols = gw + gl
    total_cantos = cw + cl

    if total_gols:
        aproveitamento_gols = (
            gw / total_gols
        ) * 100
    else:
        aproveitamento_gols = 0

    if total_cantos:
        aproveitamento_cantos = (
            cw / total_cantos
        ) * 100
    else:
        aproveitamento_cantos = 0

    return {
        "total_sinais": stats["total_sinais"],
        "gols": {
            "wins": gw,
            "losses": gl,
            "aproveitamento": round(
                aproveitamento_gols,
                2
            )
        },
        "cantos": {
            "wins": cw,
            "losses": cl,
            "aproveitamento": round(
                aproveitamento_cantos,
                2
            )
        },
        "pendentes": len(pendentes_pre)
    }


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def iniciar_bot():

    tg_msg(
        f"🤖 <b>TURBO ONLINE</b>\n\n"
        f"🎟️ Até {QTD_POR_RODADA} sinais por rodada\n"
        f"⏱️ Intervalo: {INTERVALO_PRE // 60} min\n"
        f"⚽ Over Gols + 🚩 Over Escanteios"
    )

    print(
        "====================================",
        flush=True
    )

    print(
        "BOT TURBO INICIADO",
        flush=True
    )

    print(
        f"QTD POR RODADA: {QTD_POR_RODADA}",
        flush=True
    )

    print(
        f"INTERVALO PRE: {INTERVALO_PRE}s",
        flush=True
    )

    print(
        "====================================",
        flush=True
    )

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:

        try:

            agora = time.time()

            # =================================================
            # BUSCAR NOVOS SINAIS
            # =================================================

            if agora - ultimo_pre >= INTERVALO_PRE:

                ultimo_pre = agora

                gerados = 0

                jogos = buscar_pre()

                for jogo in jogos:

                    if gerados >= QTD_POR_RODADA:
                        break

                    try:

                        if analisar_pre(jogo):

                            gerados += 1

                            time.sleep(1)

                    except Exception as erro:

                        print(
                            f"ERRO ANALISANDO JOGO: {erro}",
                            flush=True
                        )

                print(
                    f"TURBO: {gerados} SINAIS GERADOS",
                    flush=True
                )

            # =================================================
            # CONFERIR RESULTADOS
            # =================================================

            if (
                agora - ultimo_resultado
                >= INTERVALO_RESULTADOS
            ):

                ultimo_resultado = agora

                pendentes = list(
                    pendentes_pre.items()
                )

                for key, info in pendentes:

                    try:

                        fid = info["fid"]

                        jogo = buscar_jogo_por_id(
                            fid
                        )

                        if jogo and eh_finalizado(jogo):

                            finalizar(
                                key,
                                jogo
                            )

                            time.sleep(1)

                    except Exception as erro:

                        print(
                            f"ERRO RESULTADO {fid}: {erro}",
                            flush=True
                        )

            time.sleep(20)

        except Exception as erro:

            print(
                f"ERRO NO LOOP PRINCIPAL: {erro}",
                flush=True
            )

            time.sleep(30)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    dados = resumo_stats()

    return jsonify({
        "status": "TURBO ONLINE",
        "sinais_por_rodada": QTD_POR_RODADA,
        "intervalo_segundos": INTERVALO_PRE,
        "estatisticas": dados
    })


@app.route("/health")
def health():

    return jsonify({
        "online": True,
        "api_configurada": bool(API_KEY),
        "telegram_configurado": bool(
            TOKEN and CHAT_ID
        ),
        "pendentes": len(pendentes_pre)
    })


@app.route("/stats")
def stats_route():

    return jsonify(
        resumo_stats()
    )


# ============================================================
# THREAD
# ============================================================

def iniciar_thread():

    thread = threading.Thread(
        target=iniciar_bot,
        daemon=True
    )

    thread.start()


iniciar_thread()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
