import os
import json
import time
import threading
import tempfile
from datetime import datetime

import requests
from flask import Flask, jsonify

# =========================================================
# CONFIGURAÇÕES
# =========================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-escanteios/1.0"
}

ARQUIVO_STATS = "stats.json"
ARQUIVO_HISTORICO = "historico.json"

PORT = int(os.getenv("PORT", "10000"))

# Filtros LIVE
MINUTO_INICIAL_LIVE = 20
MINUTO_FINAL_LIVE = 45
MIN_CANTOS_LIVE = 4
ODD_FAVORITO_MAXIMA = 1.70

# Filtros PRÉ-JOGO
ODD_OVER_MINIMA = 1.70
ODD_OVER_MAXIMA = 2.50
INTERVALO_PRE = 1800       # 30 minutos
INTERVALO_RESULTADOS = 300 # 5 minutos
INTERVALO_LIVE = 60        # 1 minuto

lock = threading.RLock()

# =========================================================
# ESTATÍSTICAS
# =========================================================

STATS_PADRAO = {
    "live_wins": 0,
    "live_losses": 0,
    "live_voids": 0,
    "pre_wins": 0,
    "pre_losses": 0,
    "total_sinais": 0
}

stats = STATS_PADRAO.copy()


def carregar_json(arquivo, padrao):
    if not os.path.exists(arquivo):
        return padrao.copy() if isinstance(padrao, dict) else []

    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            dados = json.load(f)

        if isinstance(padrao, dict):
            resultado = padrao.copy()
            if isinstance(dados, dict):
                resultado.update(dados)
            return resultado

        return dados if isinstance(dados, list) else padrao

    except Exception as erro:
        print(f"ERRO AO CARREGAR {arquivo}: {erro}", flush=True)
        return padrao.copy() if isinstance(padrao, dict) else []


stats = carregar_json(ARQUIVO_STATS, STATS_PADRAO)
historico = carregar_json(ARQUIVO_HISTORICO, [])

entradas_live = {}
entradas_pre = {}
pendentes_live = {}
pendentes_pre = {}


def salvar_json_seguro(arquivo, dados):
    """
    Salva usando arquivo temporário para evitar corromper
    o JSON caso o processo seja interrompido.
    """
    try:
        pasta = os.path.dirname(arquivo) or "."
        fd, temporario = tempfile.mkstemp(
            prefix="tmp_",
            suffix=".json",
            dir=pasta
        )

        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                dados,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, arquivo)

    except Exception as erro:
        print(f"ERRO AO SALVAR {arquivo}: {erro}", flush=True)


def salvar_dados():
    with lock:
        salvar_json_seguro(ARQUIVO_STATS, stats)
        salvar_json_seguro(ARQUIVO_HISTORICO, historico)


def taxa(vitorias, derrotas):
    total = vitorias + derrotas

    if total == 0:
        return 0.0

    return round((vitorias / total) * 100, 2)


def registrar_historico(tipo, resultado, jogo, informacoes=None):
    if informacoes is None:
        informacoes = {}

    registro = {
        "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tipo": tipo,
        "resultado": resultado,
        "fixture_id": extrair_id(jogo),
        "casa": extrair_times(jogo)[0],
        "fora": extrair_times(jogo)[1],
        "informacoes": informacoes
    }

    with lock:
        historico.append(registro)

        # Mantém no máximo os últimos 2.000 resultados
        if len(historico) > 2000:
            del historico[:-2000]

        salvar_dados()


# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================

def primeiro_valor(*valores):
    for valor in valores:
        if valor is not None and valor != "":
            return valor

    return None


def numero(valor, padrao=0.0):
    try:
        if valor is None or valor == "":
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


def nome_time(time_data, padrao):
    if isinstance(time_data, dict):
        return str(
            primeiro_valor(
                time_data.get("name"),
                time_data.get("short_name"),
                time_data.get("title"),
                padrao
            )
        )

    if time_data:
        return str(time_data)

    return padrao


def extrair_id(jogo):
    fixture = jogo.get("fixture", {})

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
    goals = jogo.get("goals", {}) or {}
    scores = jogo.get("score", {}) or {}

    casa = inteiro(
        primeiro_valor(
            jogo.get("home_score"),
            jogo.get("home_goals"),
            goals.get("home"),
            scores.get("home"),
            0
        )
    )

    fora = inteiro(
        primeiro_valor(
            jogo.get("away_score"),
            jogo.get("away_goals"),
            goals.get("away"),
            scores.get("away"),
            0
        )
    )

    return casa, fora


def extrair_minuto(jogo):
    fixture = jogo.get("fixture", {}) or {}
    status = fixture.get("status", {}) or {}

    return inteiro(
        primeiro_valor(
            jogo.get("elapsed"),
            jogo.get("minute"),
            status.get("elapsed"),
            0
        )
    )


def extrair_status(jogo):
    fixture = jogo.get("fixture", {}) or {}
    status = fixture.get("status", {}) or {}

    valor = primeiro_valor(
        jogo.get("status"),
        status.get("short"),
        status.get("long"),
        ""
    )

    return str(valor).lower()


def eh_finalizado(jogo):
    status = extrair_status(jogo)

    status_finais = [
        "ft",
        "finished",
        "finalizado",
        "aet",
        "pen",
        "after extra time"
    ]

    return any(item in status for item in status_finais)


def extrair_cantos(jogo):
    """
    Aceita vários formatos possíveis da API:
    - corners: 8
    - corners: {"total": 8}
    - corners: {"home": 4, "away": 4}
    - statistics com corner kicks
    """

    corners = primeiro_valor(
        jogo.get("corners"),
        jogo.get("corner"),
        jogo.get("escanteios"),
        jogo.get("cantos")
    )

    if isinstance(corners, (int, float, str)):
        return inteiro(corners)

    if isinstance(corners, dict):
        total = primeiro_valor(
            corners.get("total"),
            corners.get("value"),
            corners.get("count")
        )

        if total is not None:
            return inteiro(total)

        casa = primeiro_valor(
            corners.get("home"),
            corners.get("local"),
            corners.get("casa"),
            0
        )

        fora = primeiro_valor(
            corners.get("away"),
            corners.get("visitor"),
            corners.get("visitante"),
            corners.get("fora"),
            0
        )

        return inteiro(casa) + inteiro(fora)

    statistics = jogo.get("statistics", [])

    if isinstance(statistics, list):
        total = 0

        for equipe in statistics:
            if not isinstance(equipe, dict):
                continue

            estatisticas_equipe = (
                equipe.get("statistics")
                or equipe.get("stats")
                or []
            )

            if not isinstance(estatisticas_equipe, list):
                continue

            for item in estatisticas_equipe:
                if not isinstance(item, dict):
                    continue

                nome = str(
                    primeiro_valor(
                        item.get("type"),
                        item.get("name"),
                        ""
                    )
                ).lower()

                if (
                    "corner" in nome
                    or "escante" in nome
                    or "canto" in nome
                ):
                    total += inteiro(
                        primeiro_valor(
                            item.get("value"),
                            item.get("total"),
                            0
                        )
                    )

        return total

    return 0


def extrair_odds(jogo):
    odds = primeiro_valor(
        jogo.get("odds"),
        jogo.get("pre_odds"),
        jogo.get("bets"),
        jogo.get("markets"),
        []
    )

    if isinstance(odds, dict):
        odds = (
            odds.get("data")
            or odds.get("odds")
            or odds.get("markets")
            or odds.get("bets")
            or []
        )

    if not isinstance(odds, list):
        return []

    resultado = []

    for item in odds:
        if isinstance(item, dict):
            resultado.append(item)

    return resultado


def texto_odds(odd):
    partes = [
        odd.get("market"),
        odd.get("name"),
        odd.get("selection"),
        odd.get("label"),
        odd.get("outcome"),
        odd.get("team"),
        odd.get("side"),
        odd.get("line"),
        odd.get("handicap")
    ]

    return " ".join(
        str(parte)
        for parte in partes
        if parte is not None
    ).lower()


def valor_odd(odd):
    return numero(
        primeiro_valor(
            odd.get("odd"),
            odd.get("price"),
            odd.get("value"),
            odd.get("rate"),
            odd.get("decimal")
        ),
        0
    )


# =========================================================
# TELEGRAM
# =========================================================

def tg_msg(mensagem):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_TOKEN ou CHAT_ID não configurado.", flush=True)
        return

    try:
        resposta = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

        if resposta.status_code != 200:
            print(
                f"ERRO TELEGRAM {resposta.status_code}: "
                f"{resposta.text}",
                flush=True
            )

    except Exception as erro:
        print(f"ERRO TELEGRAM: {erro}", flush=True)


# =========================================================
# API
# =========================================================

def api_get(endpoint, params=None):
    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    try:
        resposta = requests.get(
            url,
            headers=HEADERS,
            params=params or {},
            timeout=25
        )

        texto = resposta.text[:1000]

        print(
            f"API {url} | HTTP {resposta.status_code}",
            flush=True
        )

        if resposta.status_code != 200:
            print(f"RESPOSTA API: {texto}", flush=True)
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
            resultado = (
                resultado.get("data")
                or resultado.get("fixtures")
                or resultado.get("response")
                or []
            )

        return resultado if isinstance(resultado, list) else []

    except Exception as erro:
        print(f"ERRO API {endpoint}: {erro}", flush=True)
        return []


def buscar_live():
    """
    O endpoint /livescores estava retornando 404.
    Por isso usamos /fixtures com status=live.
    """

    jogos = api_get(
        "fixtures",
        {
            "status": "live"
        }
    )

    print(
        f"LIVE: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


def buscar_pre():
    jogos = api_get(
        "fixtures",
        {
            "date": "today",
            "status": "scheduled"
        }
    )

    print(
        f"PRE: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


def buscar_finalizados():
    jogos = api_get(
        "fixtures",
        {
            "date": "today",
            "status": "finished"
        }
    )

    print(
        f"FINALIZADOS: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


# =========================================================
# FILTRO DE FAVORITO
# =========================================================

def get_favorito_info(jogo):
    odds = extrair_odds(jogo)

    home_odd = 99.0
    away_odd = 99.0

    for odd in odds:
        texto = texto_odds(odd)
        preco = valor_odd(odd)

        if preco < 1.01:
            continue

        mercado_vencedor = (
            "winner" in texto
            or "match winner" in texto
            or "1x2" in texto
            or "moneyline" in texto
            or "vencedor" in texto
            or texto.strip() in ["1", "2"]
        )

        if not mercado_vencedor:
            continue

        lado = str(
            primeiro_valor(
                odd.get("team"),
                odd.get("side"),
                odd.get("selection"),
                odd.get("outcome"),
                odd.get("name"),
                ""
            )
        ).lower()

        if (
            "home" in lado
            or "casa" in lado
            or lado.strip() == "1"
            or "home" in texto
        ):
            home_odd = min(home_odd, preco)

        elif (
            "away" in lado
            or "fora" in lado
            or "visitante" in lado
            or lado.strip() == "2"
            or "away" in texto
        ):
            away_odd = min(away_odd, preco)

    if home_odd <= ODD_FAVORITO_MAXIMA and home_odd < away_odd:
        return "HOME", home_odd, home_odd, away_odd

    if away_odd <= ODD_FAVORITO_MAXIMA and away_odd < home_odd:
        return "AWAY", away_odd, home_odd, away_odd

    return None, 0, home_odd, away_odd


# =========================================================
# FILTRO CORRETO OVER 2.5
# =========================================================

def filtra_over25(jogo):
    """
    Aceita somente:
    - Over 2.5
    - Mais de 2.5 gols

    Não aceita:
    - Over 2.0
    - Over 2
    - Over 1.5
    - Over 3.5

    Odd entre 1.70 e 2.50.
    """

    odds = extrair_odds(jogo)

    for odd in odds:
        texto = texto_odds(odd)
        preco = valor_odd(odd)

        if preco <= 0:
            continue

        texto = (
            texto
            .replace(",", ".")
            .replace("over2.5", "over 2.5")
            .replace("over2,5", "over 2.5")
            .replace("maisde2.5", "mais de 2.5")
            .replace("maisde2,5", "mais de 2.5")
        )

        tem_over = (
            "over" in texto
            or "mais de" in texto
            or "mais_de" in texto
        )

        tem_gols = (
            "goal" in texto
            or "goals" in texto
            or "gol" in texto
            or "gols" in texto
        )

        # O ponto principal da correção:
        # exige especificamente a linha 2.5
        tem_linha_25 = (
            "2.5" in texto
            or "2,5" in texto
            or "2 5" in texto
        )

        # Bloqueia linhas diferentes
        linha_incorreta = (
            "1.5" in texto
            or "1,5" in texto
            or "2.0" in texto
            or "2,0" in texto
            or "3.5" in texto
            or "3,5" in texto
        )

        if not tem_over:
            continue

        if not tem_gols:
            continue

        if not tem_linha_25:
            continue

        if linha_incorreta:
            continue

        if not (
            ODD_OVER_MINIMA
            <= preco
            <= ODD_OVER_MAXIMA
        ):
            continue

        mercado = primeiro_valor(
            odd.get("market"),
            odd.get("name"),
            odd.get("selection"),
            odd.get("label"),
            "Over 2.5 Gols"
        )

        return preco, str(mercado)

    return None


# =========================================================
# RESULTADOS
# =========================================================

def finalizar_live(fid, jogo):
    if fid not in pendentes_live:
        return

    info = pendentes_live.pop(fid)

    total_cantos = extrair_cantos(jogo)
    casa_gols, fora_gols = extrair_placar(jogo)
    total_gols = casa_gols + fora_gols

    if total_cantos >= 10:
        resultado = "GREEN"
        emoji = "🟢"
        stats["live_wins"] += 1

    elif total_cantos == 9:
        resultado = "VOID"
        emoji = "⚪"
        stats["live_voids"] += 1

    else:
        resultado = "RED"
        emoji = "🔴"
        stats["live_losses"] += 1

    salvar_dados()

    percentual = taxa(
        stats["live_wins"],
        stats["live_losses"]
    )

    tg_msg(
        f"{emoji} <b>{resultado} - LIVE OVER 9.0</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"🚩 Escanteios finais: {total_cantos}\n"
        f"⚽ Gols finais: {total_gols}\n"
        f"💰 Entrada: Over 9.0 FT\n\n"
        f"📊 Live: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V "
        f"({percentual}%)"
    )

    registrar_historico(
        "LIVE_OVER_9",
        resultado,
        jogo,
        {
            "escanteios_finais": total_cantos,
            "gols_finais": total_gols,
            "odd_favorito": info.get("odd")
        }
    )


def finalizar_pre(fid, jogo):
    if fid not in pendentes_pre:
        return

    info = pendentes_pre.pop(fid)

    casa_gols, fora_gols = extrair_placar(jogo)
    total_gols = casa_gols + fora_gols

    if total_gols >= 3:
        resultado = "GREEN"
        emoji = "🟢"
        stats["pre_wins"] += 1

    else:
        resultado = "RED"
        emoji = "🔴"
        stats["pre_losses"] += 1

    salvar_dados()

    percentual = taxa(
        stats["pre_wins"],
        stats["pre_losses"]
    )

    tg_msg(
        f"{emoji} <b>{resultado} - PRE OVER 2.5</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"⚽ Gols finais: {total_gols}\n"
        f"💰 Entrada: Over 2.5 @ {info['odd']}\n\n"
        f"📊 Pré: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L "
        f"({percentual}%)"
    )

    registrar_historico(
        "PRE_OVER_2.5",
        resultado,
        jogo,
        {
            "gols_finais": total_gols,
            "odd": info.get("odd"),
            "mercado": info.get("mercado")
        }
    )


# =========================================================
# ANÁLISE LIVE
# =========================================================

def analisar_live(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return

    if fid in entradas_live:
        return

    minuto = extrair_minuto(jogo)

    if not (
        MINUTO_INICIAL_LIVE
        <= minuto
        <= MINUTO_FINAL_LIVE
    ):
        return

    cantos = extrair_cantos(jogo)

    if cantos < MIN_CANTOS_LIVE:
        return

    casa, fora = extrair_times(jogo)
    gols_casa, gols_fora = extrair_placar(jogo)

    favorito, odd_favorito, home_odd, away_odd = (
        get_favorito_info(jogo)
    )

    if not favorito:
        return

    condicao = False
    descricao = ""

    if favorito == "HOME":
        diferenca = gols_fora - gols_casa

        if diferenca == 0:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito da casa empatando"
            )

        elif 1 <= diferenca <= 2:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito da casa perdendo por "
                f"{diferenca}"
            )

    elif favorito == "AWAY":
        diferenca = gols_casa - gols_fora

        if diferenca == 0:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito visitante empatando"
            )

        elif 1 <= diferenca <= 2:
            condicao = True
            descricao = (
                f"{gols_casa}x{gols_fora} - "
                f"favorito visitante perdendo por "
                f"{diferenca}"
            )

    if not condicao:
        return

    entradas_live[fid] = True

    pendentes_live[fid] = {
        "home": casa,
        "away": fora,
        "odd": odd_favorito,
        "favorito": favorito,
        "minuto": minuto,
        "cantos_inicio": cantos,
        "hora": datetime.now().strftime("%H:%M:%S")
    }

    stats["total_sinais"] += 1
    salvar_dados()

    percentual = taxa(
        stats["live_wins"],
        stats["live_losses"]
    )

    tg_msg(
        f"🔥 <b>LIVE - OVER 9.0 ESCANTEIOS</b>\n\n"
        f"🏟️ {casa} x {fora}\n"
        f"⏰ Minuto: {minuto}'\n"
        f"📊 Placar: {descricao}\n"
        f"⭐ Favorito: {favorito} @ {odd_favorito}\n"
        f"🚩 Cantos atuais: {cantos}\n\n"
        f"👉 <b>ENTRADA: Over 9.0 FT</b>\n"
        f"📊 Live atual: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L "
        f"({percentual}%)"
    )

    print(
        f"SINAL LIVE ENVIADO: {casa} x {fora} | "
        f"{minuto}' | cantos {cantos}",
        flush=True
    )


# =========================================================
# ANÁLISE PRÉ-JOGO
# =========================================================

def analisar_pre(jogo):
    fid = extrair_id(jogo)

    if not fid:
        return False

    if fid in entradas_pre:
        return False

    resultado = filtra_over25(jogo)

    if not resultado:
        return False

    odd_valor, mercado = resultado

    casa, fora = extrair_times(jogo)

    entradas_pre[fid] = True

    pendentes_pre[fid] = {
        "home": casa,
        "away": fora,
        "odd": odd_valor,
        "mercado": mercado,
        "hora": datetime.now().strftime("%H:%M:%S")
    }

    stats["total_sinais"] += 1
    salvar_dados()

    percentual = taxa(
        stats["pre_wins"],
        stats["pre_losses"]
    )

    tg_msg(
        f"⚽ <b>PRÉ-JOGO - OVER 2.5 GOLS</b>\n\n"
        f"🏟️ {casa} x {fora}\n"
        f"💰 Mercado: {mercado}\n"
        f"📈 Odd: {odd_valor}\n\n"
        f"👉 <b>ENTRADA: Mais de 2.5 gols</b>\n"
        f"📊 Pré atual: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L "
        f"({percentual}%)"
    )

    print(
        f"SINAL PRÉ ENVIADO: {casa} x {fora} | "
        f"Over 2.5 @ {odd_valor}",
        flush=True
    )

    return True


# =========================================================
# LOOP PRINCIPAL
# =========================================================

def iniciar_bot():
    tg_msg(
        f"🤖 <b>ROBÔ DE ESCANTEIOS ONLINE</b>\n\n"
        f"🔥 Live: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V\n"
        f"⚽ Pré: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L\n"
        f"📊 Total de sinais: "
        f"{stats['total_sinais']}"
    )

    print(
        "ROBÔ INICIADO - Aguardando jogos...",
        flush=True
    )

    ultimo_pre = 0
    ultimo_resultados = 0

    while True:
        try:
            # -----------------------------
            # LIVE
            # -----------------------------
            jogos_live = buscar_live()

            for jogo in jogos_live:
                try:
                    analisar_live(jogo)

                except Exception as erro:
                    print(
                        f"ERRO AO ANALISAR LIVE: {erro}",
                        flush=True
                    )

            # -----------------------------
            # PRÉ-JOGO
            # -----------------------------
            if time.time() - ultimo_pre >= INTERVALO_PRE:
                ultimo_pre = time.time()

                jogos_pre = buscar_pre()

                for jogo in jogos_pre:
                    try:
                        enviado = analisar_pre(jogo)

                        # Envia no máximo um pré por ciclo
                        if enviado:
                            break

                    except Exception as erro:
                        print(
                            f"ERRO AO ANALISAR PRÉ: {erro}",
                            flush=True
                        )

            # -----------------------------
            # RESULTADOS
            # -----------------------------
            if (
                time.time() - ultimo_resultados
                >= INTERVALO_RESULTADOS
            ):
                ultimo_resultados = time.time()

                if pendentes_live or pendentes_pre:
                    finalizados = buscar_finalizados()

                    for jogo in finalizados:
                        try:
                            fid = extrair_id(jogo)

                            if not fid:
                                continue

                            if not eh_finalizado(jogo):
                                continue

                            if fid in pendentes_live:
                                finalizar_live(fid, jogo)

                            if fid in pendentes_pre:
                                finalizar_pre(fid, jogo)

                        except Exception as erro:
                            print(
                                f"ERRO AO FINALIZAR JOGO: {erro}",
                                flush=True
                            )

            time.sleep(INTERVALO_LIVE)

        except Exception as erro:
            print(
                f"ERRO GERAL DO LOOP: {erro}",
                flush=True
            )

            time.sleep(30)


# =========================================================
# ROTAS FLASK
# =========================================================

@app.route("/")
def home():
    live_percentual = taxa(
        stats["live_wins"],
        stats["live_losses"]
    )

    pre_percentual = taxa(
        stats["pre_wins"],
        stats["pre_losses"]
    )

    geral_percentual = taxa(
        stats["live_wins"] + stats["pre_wins"],
        stats["live_losses"] + stats["pre_losses"]
    )

    return (
        "ROBÔ ONLINE | "
        f"Live: {live_percentual}% "
        f"({stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V) | "
        f"Pré: {pre_percentual}% "
        f"({stats['pre_wins']}W-"
        f"{stats['pre_losses']}L) | "
        f"Geral: {geral_percentual}% | "
        f"Pendentes Live: {len(pendentes_live)} | "
        f"Pendentes Pré: {len(pendentes_pre)}"
    )


@app.route("/stats")
def rota_stats():
    with lock:
        resultado = stats.copy()

    resultado["live_taxa"] = taxa(
        stats["live_wins"],
        stats["live_losses"]
    )

    resultado["pre_taxa"] = taxa(
        stats["pre_wins"],
        stats["pre_losses"]
    )

    resultado["geral_taxa"] = taxa(
        stats["live_wins"] + stats["pre_wins"],
        stats["live_losses"] + stats["pre_losses"]
    )

    resultado["pendentes_live"] = len(pendentes_live)
    resultado["pendentes_pre"] = len(pendentes_pre)

    return jsonify(resultado)


@app.route("/historico")
def rota_historico():
    with lock:
        return jsonify(historico[-200:])


@app.route("/health")
def health():
    return jsonify({
        "online": True,
        "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pendentes_live": len(pendentes_live),
        "pendentes_pre": len(pendentes_pre),
        "total_historico": len(historico)
    })


# =========================================================
# INICIALIZAÇÃO
# =========================================================

def iniciar_thread():
    thread = threading.Thread(
        target=iniciar_bot,
        daemon=True
    )

    thread.start()


iniciar_thread()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT
    )
