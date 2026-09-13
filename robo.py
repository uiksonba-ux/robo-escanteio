import os
import json
import time
import threading
import requests

from datetime import datetime
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÕES
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "Robo-Escanteios/2.0"
}

ARQUIVO_DADOS = "bot_data.json"
ARQUIVO_HISTORICO = "historico.json"

INTERVALO_LIVE = 60
INTERVALO_PRE = 1800
INTERVALO_RESULTADOS = 300

MINUTO_LIVE_INICIAL = 20
MINUTO_LIVE_FINAL = 45

MIN_CANTOS_LIVE = 4

ODD_FAVORITO_MAXIMA = 1.70
ODD_FAVORITO_FORTE = 1.50

ODD_OVER25_MINIMA = 1.70
ODD_OVER25_MAXIMA = 2.50


# ============================================================
# DADOS E MEMÓRIA
# ============================================================

lock = threading.RLock()

stats = {
    "live_wins": 0,
    "live_losses": 0,
    "live_voids": 0,
    "pre_wins": 0,
    "pre_losses": 0,
    "total_sinais": 0
}

entradas_live = {}
entradas_pre = {}

pendentes_live = {}
pendentes_pre = {}

historico = []

bot_thread_iniciado = False


# ============================================================
# FUNÇÕES DE ARQUIVOS
# ============================================================

def carregar_json(caminho, padrao):
    """
    Carrega um arquivo JSON.
    Se não existir ou estiver inválido, retorna o padrão.
    """
    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)

        return dados

    except Exception as erro:
        print(f"ERRO AO CARREGAR {caminho}: {erro}", flush=True)
        return padrao


def salvar_json(caminho, dados):
    """
    Salva JSON com arquivo temporário para reduzir risco de corrupção.
    """
    temporario = f"{caminho}.tmp"

    try:
        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, caminho)

    except Exception as erro:
        print(f"ERRO AO SALVAR {caminho}: {erro}", flush=True)

        try:
            if os.path.exists(temporario):
                os.remove(temporario)
        except Exception:
            pass


def carregar_dados():
    global stats
    global entradas_live
    global entradas_pre
    global pendentes_live
    global pendentes_pre
    global historico

    dados = carregar_json(ARQUIVO_DADOS, {})

    stats_carregadas = dados.get("stats", {})

    for chave, valor in stats_carregadas.items():
        stats[chave] = valor

    entradas_live = dados.get("entradas_live", {})
    entradas_pre = dados.get("entradas_pre", {})

    pendentes_live = dados.get("pendentes_live", {})
    pendentes_pre = dados.get("pendentes_pre", {})

    historico = carregar_json(ARQUIVO_HISTORICO, [])

    if not isinstance(historico, list):
        historico = []


def salvar_dados():
    dados = {
        "stats": stats,
        "entradas_live": entradas_live,
        "entradas_pre": entradas_pre,
        "pendentes_live": pendentes_live,
        "pendentes_pre": pendentes_pre
    }

    with lock:
        salvar_json(ARQUIVO_DADOS, dados)
        salvar_json(ARQUIVO_HISTORICO, historico)


carregar_dados()


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hora_atual():
    return datetime.now().strftime("%H:%M")


def numero(valor, padrao=0.0):
    try:
        if valor is None:
            return padrao

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


def primeiro_valor(*valores):
    for valor in valores:
        if valor is not None and valor != "":
            return valor

    return None


def nome_time(valor, padrao="Desconhecido"):
    if isinstance(valor, dict):
        return primeiro_valor(
            valor.get("name"),
            valor.get("team_name"),
            valor.get("short_name"),
            valor.get("title"),
            padrao
        )

    if valor:
        return str(valor)

    return padrao


def extrair_id(jogo):
    fixture = jogo.get("fixture", {}) or {}

    valor = primeiro_valor(
        jogo.get("id"),
        jogo.get("fixture_id"),
        fixture.get("id")
    )

    if valor is None:
        return None

    return str(valor)


def extrair_times(jogo):
    teams = jogo.get("teams", {}) or {}

    casa = primeiro_valor(
        jogo.get("home_team"),
        jogo.get("home"),
        teams.get("home")
    )

    fora = primeiro_valor(
        jogo.get("away_team"),
        jogo.get("away"),
        teams.get("away")
    )

    return (
        nome_time(casa, "Casa"),
        nome_time(fora, "Fora")
    )


def extrair_placar(jogo):
    goals = jogo.get("goals", {}) or {}
    score = jogo.get("score", {}) or {}

    casa = primeiro_valor(
        jogo.get("home_score"),
        jogo.get("home_goals"),
        goals.get("home"),
        score.get("home"),
        0
    )

    fora = primeiro_valor(
        jogo.get("away_score"),
        jogo.get("away_goals"),
        goals.get("away"),
        score.get("away"),
        0
    )

    return inteiro(casa), inteiro(fora)


def extrair_minuto(jogo):
    fixture = jogo.get("fixture", {}) or {}
    status = fixture.get("status", {}) or {}

    minuto = primeiro_valor(
        jogo.get("elapsed"),
        jogo.get("minute"),
        status.get("elapsed"),
        0
    )

    return inteiro(minuto)


def extrair_status(jogo):
    fixture = jogo.get("fixture", {}) or {}
    status = fixture.get("status", {}) or {}

    valor = primeiro_valor(
        jogo.get("status"),
        jogo.get("state"),
        status.get("short"),
        status.get("long"),
        ""
    )

    return str(valor).lower()


def jogo_finalizado(jogo):
    status = extrair_status(jogo)

    status_finais = [
        "ft",
        "finished",
        "complete",
        "completed",
        "aet",
        "after extra time",
        "pen",
        "penalties"
    ]

    return any(item in status for item in status_finais)


def extrair_cantos(jogo):
    """
    Tenta interpretar diferentes formatos de escanteios.
    """

    cantos = primeiro_valor(
        jogo.get("corners"),
        jogo.get("corner"),
        jogo.get("corner_kicks"),
        jogo.get("statistics", {}).get("corners")
        if isinstance(jogo.get("statistics"), dict)
        else None,
        0
    )

    if isinstance(cantos, dict):
        total = primeiro_valor(
            cantos.get("total"),
            cantos.get("value"),
            cantos.get("count")
        )

        if total is not None:
            return inteiro(total)

        casa = primeiro_valor(
            cantos.get("home"),
            cantos.get("local"),
            cantos.get("team1"),
            cantos.get("1"),
            0
        )

        fora = primeiro_valor(
            cantos.get("away"),
            cantos.get("visitor"),
            cantos.get("team2"),
            cantos.get("2"),
            0
        )

        return inteiro(casa) + inteiro(fora)

    if isinstance(cantos, list):
        total = 0

        for item in cantos:
            if not isinstance(item, dict):
                continue

            nome = str(
                primeiro_valor(
                    item.get("name"),
                    item.get("type"),
                    item.get("stat"),
                    ""
                )
            ).lower()

            if "corner" not in nome and "canto" not in nome:
                continue

            valor = primeiro_valor(
                item.get("total"),
                item.get("value"),
                item.get("count"),
                0
            )

            total += inteiro(valor)

        return total

    return inteiro(cantos)


def taxa(wins, losses):
    total = wins + losses

    if total <= 0:
        return 0.0

    return round((wins / total) * 100, 2)


def taxa_live():
    return taxa(
        stats.get("live_wins", 0),
        stats.get("live_losses", 0)
    )


def taxa_pre():
    return taxa(
        stats.get("pre_wins", 0),
        stats.get("pre_losses", 0)
    )


def taxa_geral():
    return taxa(
        stats.get("live_wins", 0) + stats.get("pre_wins", 0),
        stats.get("live_losses", 0) + stats.get("pre_losses", 0)
    )


def texto_odds(odd):
    return f"{numero(odd):.2f}"


# ============================================================
# TELEGRAM
# ============================================================

def tg_msg(mensagem):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM NÃO CONFIGURADO", flush=True)
        print(mensagem, flush=True)
        return False

    try:
        resposta = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
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
                f"{resposta.text}",
                flush=True
            )
            return False

        dados = resposta.json()

        if not dados.get("ok"):
            print(
                f"TELEGRAM RECUSOU A MENSAGEM: {dados}",
                flush=True
            )
            return False

        return True

    except Exception as erro:
        print(f"ERRO AO ENVIAR TELEGRAM: {erro}", flush=True)
        return False


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):
    try:
        resposta = requests.get(
            f"{BASE_API}{endpoint}",
            headers=HEADERS,
            params=params or {},
            timeout=25
        )

        print(
            f"API {endpoint} | HTTP {resposta.status_code}",
            flush=True
        )

        if resposta.status_code != 200:
            print(
                f"RESPOSTA API: {resposta.text[:500]}",
                flush=True
            )
            return []

        dados = resposta.json()

        if isinstance(dados, list):
            return dados

        if not isinstance(dados, dict):
            return []

        resultado = primeiro_valor(
            dados.get("data"),
            dados.get("response"),
            dados.get("results"),
            []
        )

        if isinstance(resultado, dict):
            resultado = primeiro_valor(
                resultado.get("data"),
                resultado.get("response"),
                resultado.get("fixtures"),
                resultado.get("matches"),
                []
            )

        if isinstance(resultado, list):
            return resultado

        return []

    except Exception as erro:
        print(
            f"ERRO API {endpoint}: {erro}",
            flush=True
        )
        return []


def buscar_live():
    jogos = api_get("/livescores")

    print(
        f"LIVE: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


def buscar_pre():
    jogos = api_get(
        "/fixtures",
        params={
            "date": "today"
        }
    )

    print(
        f"PRÉ: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


def buscar_finalizados():
    jogos = api_get(
        "/fixtures",
        params={
            "date": "today",
            "status": "finished"
        }
    )

    print(
        f"FINALIZADOS: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


# ============================================================
# ODDS E FILTROS
# ============================================================

def listar_odds(jogo):
    odds = primeiro_valor(
        jogo.get("odds"),
        jogo.get("pre_odds"),
        jogo.get("bookmakers"),
        []
    )

    if isinstance(odds, dict):
        odds = primeiro_valor(
            odds.get("data"),
            odds.get("markets"),
            odds.get("odds"),
            []
        )

    if not isinstance(odds, list):
        return []

    resultado = []

    for item in odds:
        if isinstance(item, dict):
            resultado.append(item)

    return resultado


def get_favorito_info(jogo):
    """
    Identifica favorito pelo menor preço no mercado vencedor.
    """

    try:
        odds = listar_odds(jogo)

        home_odd = None
        away_odd = None

        for odd in odds:
            texto = " ".join([
                str(odd.get("market", "")),
                str(odd.get("name", "")),
                str(odd.get("selection", "")),
                str(odd.get("team", "")),
                str(odd.get("side", "")),
                str(odd.get("label", "")),
                str(odd.get("value", ""))
            ]).lower()

            preco = primeiro_valor(
                odd.get("odd"),
                odd.get("price"),
                odd.get("value_odd"),
                odd.get("decimal"),
                0
            )

            preco = numero(preco)

            if preco < 1.01:
                continue

            mercado_vencedor = (
                "winner" in texto
                or "match result" in texto
                or "match winner" in texto
                or "moneyline" in texto
                or "1x2" in texto
                or texto.strip() in ["1", "2", "home", "away"]
            )

            if not mercado_vencedor:
                continue

            eh_casa = (
                "home" in texto
                or "casa" in texto
                or "team1" in texto
                or texto.strip() == "1"
                or texto.endswith(" 1")
            )

            eh_fora = (
                "away" in texto
                or "fora" in texto
                or "visitor" in texto
                or "team2" in texto
                or texto.strip() == "2"
                or texto.endswith(" 2")
            )

            if eh_casa:
                if home_odd is None or preco < home_odd:
                    home_odd = preco

            elif eh_fora:
                if away_odd is None or preco < away_odd:
                    away_odd = preco

        home_odd = home_odd if home_odd is not None else 10.0
        away_odd = away_odd if away_odd is not None else 10.0

        if (
            home_odd <= ODD_FAVORITO_FORTE
            and home_odd < away_odd
        ):
            return "HOME", home_odd, home_odd, away_odd

        if (
            away_odd <= ODD_FAVORITO_FORTE
            and away_odd < home_odd
        ):
            return "AWAY", away_odd, home_odd, away_odd

        if (
            home_odd <= ODD_FAVORITO_MAXIMA
            and home_odd < away_odd
        ):
            return "HOME", home_odd, home_odd, away_odd

        if (
            away_odd <= ODD_FAVORITO_MAXIMA
            and away_odd < home_odd
        ):
            return "AWAY", away_odd, home_odd, away_odd

        return None, 0.0, home_odd, away_odd

    except Exception as erro:
        print(
            f"ERRO AO IDENTIFICAR FAVORITO: {erro}",
            flush=True
        )

        return None, 0.0, 10.0, 10.0


def filtra_over25(jogo):
    """
    Aceita somente Over 2.5 gols.
    """

    try:
        odds = listar_odds(jogo)

        for odd in odds:
            texto = " ".join([
                str(odd.get("market", "")),
                str(odd.get("name", "")),
                str(odd.get("selection", "")),
                str(odd.get("label", "")),
                str(odd.get("value", ""))
            ]).lower()

            preco = primeiro_valor(
                odd.get("odd"),
                odd.get("price"),
                odd.get("value_odd"),
                odd.get("decimal"),
                0
            )

            preco = numero(preco)

            if preco < ODD_OVER25_MINIMA:
                continue

            if preco > ODD_OVER25_MAXIMA:
                continue

            eh_over25 = (
                "over 2.5" in texto
                or "over2.5" in texto
                or "over 2,5" in texto
                or "mais de 2.5" in texto
                or "mais de 2,5" in texto
            )

            eh_mercado_gols = (
                "goal" in texto
                or "goals" in texto
                or "gol" in texto
                or "gols" in texto
                or "total" in texto
            )

            if eh_over25 and eh_mercado_gols:
                mercado = primeiro_valor(
                    odd.get("market"),
                    odd.get("name"),
                    odd.get("selection"),
                    "Over 2.5 Gols"
                )

                return preco, str(mercado)

        return None

    except Exception as erro:
        print(
            f"ERRO NO FILTRO OVER 2.5: {erro}",
            flush=True
        )

        return None


# ============================================================
# HISTÓRICO
# ============================================================

def registrar_historico(tipo, fid, info, resultado, detalhes):
    registro = {
        "id": fid,
        "tipo": tipo,
        "data": agora(),
        "casa": info.get("home", ""),
        "fora": info.get("away", ""),
        "odd": info.get("odd", 0),
        "mercado": info.get("mercado", ""),
        "resultado": resultado,
        "detalhes": detalhes
    }

    with lock:
        historico.append(registro)

        # Mantém no máximo 2.000 registros
        if len(historico) > 2000:
            del historico[:-2000]


# ============================================================
# ANÁLISE LIVE
# ============================================================

def analisar_live(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return

    with lock:
        if fid in entradas_live:
            return

    minuto = extrair_minuto(jogo)

    if not (
        MINUTO_LIVE_INICIAL
        <= minuto
        <= MINUTO_LIVE_FINAL
    ):
        return

    casa, fora = extrair_times(jogo)
    gols_casa, gols_fora = extrair_placar(jogo)

    favorito, odd_favorito, odd_casa, odd_fora = (
        get_favorito_info(jogo)
    )

    if not favorito:
        return

    cantos = extrair_cantos(jogo)

    if cantos < MIN_CANTOS_LIVE:
        return

    condicao = False
    descricao = ""

    if favorito == "HOME":
        diferenca = gols_fora - gols_casa

        if diferenca == 0:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                "favorito da casa empatando"
            )

        elif 1 <= diferenca <= 2:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito da casa perdendo {diferenca}"
            )

    elif favorito == "AWAY":
        diferenca = gols_casa - gols_fora

        if diferenca == 0:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                "favorito visitante empatando"
            )

        elif 1 <= diferenca <= 2:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito visitante perdendo {diferenca}"
            )

    if not condicao:
        return

    info = {
        "home": casa,
        "away": fora,
        "over": 9.0,
        "hora": hora_atual(),
        "data": agora(),
        "fav": favorito,
        "odd": odd_favorito,
        "cantos_entrada": cantos,
        "placar_entrada": f"{gols_casa}x{gols_fora}",
        "minuto": minuto,
        "mercado": "Over 9.0 Escanteios"
    }

    with lock:
        entradas_live[fid] = True
        pendentes_live[fid] = info
        stats["total_sinais"] += 1
        salvar_dados()

    mensagem = (
        "🔥 <b>LIVE - OVER 9.0 ESCANTEIOS</b>\n\n"
        f"🏟️ {casa} x {fora}\n"
        f"⏰ Minuto: {minuto}'\n"
        f"📊 Placar: {descricao}\n"
        f"⭐ Favorito: {favorito} @ "
        f"{texto_odds(odd_favorito)}\n"
        f"🚩 Cantos atuais: {cantos}\n\n"
        "👉 <b>ENTRADA: Over 9.0 FT</b>\n"
        "⚠️ 9 escanteios = VOID"
    )

    tg_msg(mensagem)

    print(
        f"SINAL LIVE ENVIADO: {casa} x {fora}",
        flush=True
    )


# ============================================================
# ANÁLISE PRÉ-JOGO
# ============================================================

def analisar_pre(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return

    with lock:
        if fid in entradas_pre:
            return

    resultado = filtra_over25(jogo)

    if not resultado:
        return

    odd_valor, mercado = resultado

    casa, fora = extrair_times(jogo)

    info = {
        "home": casa,
        "away": fora,
        "over": 2.5,
        "hora": hora_atual(),
        "data": agora(),
        "odd": odd_valor,
        "mercado": mercado
    }

    with lock:
        entradas_pre[fid] = True
        pendentes_pre[fid] = info
        stats["total_sinais"] += 1
        salvar_dados()

    mensagem = (
        "⚽ <b>PRÉ-JOGO - OVER 2.5 GOLS</b>\n\n"
        f"🏟️ {casa} x {fora}\n"
        f"💰 Mercado: {mercado}\n"
        f"💵 Odd: {texto_odds(odd_valor)}\n\n"
        "👉 <b>ENTRADA: Mais de 2.5 gols</b>"
    )

    tg_msg(mensagem)

    print(
        f"SINAL PRÉ ENVIADO: {casa} x {fora}",
        flush=True
    )


# ============================================================
# FECHAMENTO DOS RESULTADOS
# ============================================================

def fechar_live(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return

    with lock:
        if fid not in pendentes_live:
            return

        info = pendentes_live.pop(fid)

    total_cantos = extrair_cantos(jogo)
    gols_casa, gols_fora = extrair_placar(jogo)
    total_gols = gols_casa + gols_fora

    if total_cantos >= 10:
        stats["live_wins"] += 1
        resultado = "GREEN"
        emoji = "🟢"

    elif total_cantos == 9:
        stats["live_voids"] += 1
        resultado = "VOID"
        emoji = "⚪"

    else:
        stats["live_losses"] += 1
        resultado = "RED"
        emoji = "🔴"

    detalhes = (
        f"{total_cantos} escanteios | "
        f"{total_gols} gols"
    )

    registrar_historico(
        "LIVE",
        fid,
        info,
        resultado,
        detalhes
    )

    salvar_dados()

    mensagem = (
        f"{emoji} <b>{resultado} - LIVE OVER 9.0</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"🚩 Escanteios finais: {total_cantos}\n"
        f"⚽ Gols: {total_gols}\n"
        f"Entrada: Over 9.0\n\n"
        f"📊 Live: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V\n"
        f"🎯 Aproveitamento: {taxa_live()}%"
    )

    tg_msg(mensagem)

    print(
        f"RESULTADO LIVE: {info['home']} x "
        f"{info['away']} = {resultado}",
        flush=True
    )


def fechar_pre(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return

    with lock:
        if fid not in pendentes_pre:
            return

        info = pendentes_pre.pop(fid)

    gols_casa, gols_fora = extrair_placar(jogo)
    total_gols = gols_casa + gols_fora

    if total_gols >= 3:
        stats["pre_wins"] += 1
        resultado = "GREEN"
        emoji = "🟢"

    else:
        stats["pre_losses"] += 1
        resultado = "RED"
        emoji = "🔴"

    detalhes = f"{total_gols} gols"

    registrar_historico(
        "PRE",
        fid,
        info,
        resultado,
        detalhes
    )

    salvar_dados()

    mensagem = (
        f"{emoji} <b>{resultado} - PRE OVER 2.5</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"⚽ Gols finais: {total_gols}\n"
        f"Entrada: Over 2.5 @ "
        f"{texto_odds(info['odd'])}\n\n"
        f"📊 Pré: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L\n"
        f"🎯 Aproveitamento: {taxa_pre()}%"
    )

    tg_msg(mensagem)

    print(
        f"RESULTADO PRÉ: {info['home']} x "
        f"{info['away']} = {resultado}",
        flush=True
    )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def iniciar_bot():
    global bot_thread_iniciado

    with lock:
        if bot_thread_iniciado:
            print("BOT JÁ ESTÁ INICIADO", flush=True)
            return

        bot_thread_iniciado = True

    mensagem_inicio = (
        "🤖 <b>ROBÔ DE ESCANTEIOS ONLINE</b>\n\n"
        f"📊 Live: {stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V\n"
        f"🎯 Taxa Live: {taxa_live()}%\n\n"
        f"📊 Pré: {stats['pre_wins']}W-"
        f"{stats['pre_losses']}L\n"
        f"🎯 Taxa Pré: {taxa_pre()}%\n\n"
        "✅ Monitoramento iniciado"
    )

    tg_msg(mensagem_inicio)

    print(
        "BOT INICIADO - AGUARDANDO JOGOS...",
        flush=True
    )

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:
        try:
            # ------------------------------------------------
            # JOGOS AO VIVO
            # ------------------------------------------------

            jogos_live = buscar_live()

            for jogo in jogos_live:
                try:
                    analisar_live(jogo)

                except Exception as erro:
                    print(
                        f"ERRO AO ANALISAR LIVE: {erro}",
                        flush=True
                    )

            # ------------------------------------------------
            # JOGOS PRÉ
            # ------------------------------------------------

            if time.time() - ultimo_pre >= INTERVALO_PRE:
                ultimo_pre = time.time()

                jogos_pre = buscar_pre()

                sinais_enviados = 0
                limite_sinais_pre = 3

                for jogo in jogos_pre:
                    if sinais_enviados >= limite_sinais_pre:
                        break

                    try:
                        antes = stats["total_sinais"]

                        analisar_pre(jogo)

                        depois = stats["total_sinais"]

                        if depois > antes:
                            sinais_enviados += 1

                    except Exception as erro:
                        print(
                            f"ERRO AO ANALISAR PRÉ: {erro}",
                            flush=True
                        )

            # ------------------------------------------------
            # RESULTADOS FINALIZADOS
            # ------------------------------------------------

            if time.time() - ultimo_resultado >= INTERVALO_RESULTADOS:
                ultimo_resultado = time.time()

                with lock:
                    existem_pendentes = (
                        bool(pendentes_live)
                        or bool(pendentes_pre)
                    )

                if existem_pendentes:
                    finalizados = buscar_finalizados()

                    for jogo in finalizados:
                        try:
                            if not jogo_finalizado(jogo):
                                continue

                            fechar_live(jogo)
                            fechar_pre(jogo)

                        except Exception as erro:
                            print(
                                f"ERRO AO FECHAR RESULTADO: {erro}",
                                flush=True
                            )

            time.sleep(INTERVALO_LIVE)

        except Exception as erro:
            print(
                f"ERRO GERAL NO LOOP DO BOT: {erro}",
                flush=True
            )

            time.sleep(30)


# ============================================================
# PAINEL FLASK
# ============================================================

@app.route("/")
def home():
    return (
        "Online | "
        f"Live: {stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V "
        f"({taxa_live()}%) | "
        f"Pré: {stats['pre_wins']}W-"
        f"{stats['pre_losses']}L "
        f"({taxa_pre()}%) | "
        f"Geral: {taxa_geral()}% | "
        f"Sinais: {stats['total_sinais']}"
    )


@app.route("/stats")
def painel_stats():
    with lock:
        return jsonify({
            "status": "online",
            "stats": stats,
            "taxa_live": taxa_live(),
            "taxa_pre": taxa_pre(),
            "taxa_geral": taxa_geral(),
            "pendentes_live": len(pendentes_live),
            "pendentes_pre": len(pendentes_pre),
            "entradas_live": len(entradas_live),
            "entradas_pre": len(entradas_pre)
        })


@app.route("/historico")
def painel_historico():
    with lock:
        return jsonify({
            "total": len(historico),
            "historico": historico[-100:]
        })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_thread_iniciado": bot_thread_iniciado,
        "data": agora()
    })


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def iniciar_thread_bot():
    thread = threading.Thread(
        target=iniciar_bot,
        daemon=True,
        name="bot-escanteios"
    )

    thread.start()


iniciar_thread_bot()


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=porta,
        debug=False
    )
