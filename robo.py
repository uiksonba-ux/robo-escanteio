import os
import json
import time
import threading
import tempfile
from datetime import datetime

import requests
from flask import Flask, jsonify

# =========================================================
# CONFIGURAÇÃO
# =========================================================

app = Flask(__name__)

BASE_URL = "https://api.5dollarfootballapi.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("FIVE_DOLLAR_KEY", "").strip()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

ARQUIVO_STATS = "stats.json"
ARQUIVO_HISTORICO = "historico.json"

INTERVALO_LIVE = 60
INTERVALO_PRE = 1800
INTERVALO_RESULTADOS = 300

MINUTO_INICIAL_LIVE = 20
MINUTO_FINAL_LIVE = 45
MIN_CANTOS_LIVE = 4

ODD_FAVORITO_MAXIMA = 1.70
ODD_OVER_MINIMA = 1.70
ODD_OVER_MAXIMA = 2.50

# =========================================================
# DADOS
# =========================================================

lock = threading.RLock()

stats_padrao = {
    "live_wins": 0,
    "live_losses": 0,
    "live_voids": 0,
    "pre_wins": 0,
    "pre_losses": 0,
    "total_sinais": 0,
    "ultima_atualizacao": None,
}

stats = stats_padrao.copy()
historico = []

entradas_live = {}
entradas_pre = {}

pendentes_live = {}
pendentes_pre = {}


# =========================================================
# FUNÇÕES DE ARQUIVO
# =========================================================

def carregar_json(caminho, padrao):
    try:
        if not os.path.exists(caminho):
            return padrao

        with open(caminho, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)

        return dados

    except Exception as erro:
        print(f"ERRO AO CARREGAR {caminho}: {erro}", flush=True)
        return padrao


def salvar_json(caminho, dados):
    """
    Salva usando arquivo temporário para evitar corromper
    o JSON caso o processo seja interrompido durante a gravação.
    """
    try:
        pasta = os.path.dirname(os.path.abspath(caminho)) or "."
        nome_temporario = None

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=pasta,
            delete=False
        ) as arquivo:

            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

            arquivo.flush()
            os.fsync(arquivo.fileno())
            nome_temporario = arquivo.name

        os.replace(nome_temporario, caminho)

    except Exception as erro:
        print(f"ERRO AO SALVAR {caminho}: {erro}", flush=True)


def salvar_dados():
    with lock:
        stats["ultima_atualizacao"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        salvar_json(ARQUIVO_STATS, stats)
        salvar_json(ARQUIVO_HISTORICO, historico)


stats_carregadas = carregar_json(ARQUIVO_STATS, {})
if isinstance(stats_carregadas, dict):
    stats.update(stats_carregadas)

historico_carregado = carregar_json(ARQUIVO_HISTORICO, [])
if isinstance(historico_carregado, list):
    historico = historico_carregado


# =========================================================
# UTILITÁRIOS
# =========================================================

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


def extrair_id(jogo):
    fixture = jogo.get("fixture", {})

    if not isinstance(fixture, dict):
        fixture = {}

    valor = primeiro_valor(
        jogo.get("id"),
        fixture.get("id"),
        jogo.get("fixture_id")
    )

    if valor is None:
        return None

    return str(valor)


def nome_time(time_obj, padrao):
    if isinstance(time_obj, str):
        return time_obj

    if isinstance(time_obj, dict):
        return primeiro_valor(
            time_obj.get("name"),
            time_obj.get("short_name"),
            time_obj.get("title"),
            padrao
        )

    return padrao


def extrair_times(jogo):
    teams = jogo.get("teams", {})

    if not isinstance(teams, dict):
        teams = {}

    home = primeiro_valor(
        jogo.get("home_team"),
        jogo.get("home"),
        teams.get("home")
    )

    away = primeiro_valor(
        jogo.get("away_team"),
        jogo.get("away"),
        teams.get("away")
    )

    return (
        nome_time(home, "Casa"),
        nome_time(away, "Fora")
    )


def extrair_placar(jogo):
    goals = jogo.get("goals", {})

    if not isinstance(goals, dict):
        goals = {}

    score = jogo.get("score", {})

    if not isinstance(score, dict):
        score = {}

    home = inteiro(
        primeiro_valor(
            jogo.get("home_score"),
            score.get("home"),
            goals.get("home")
        ),
        0
    )

    away = inteiro(
        primeiro_valor(
            jogo.get("away_score"),
            score.get("away"),
            goals.get("away")
        ),
        0
    )

    return home, away


def extrair_minuto(jogo):
    fixture = jogo.get("fixture", {})

    if not isinstance(fixture, dict):
        fixture = {}

    status = fixture.get("status", {})

    if not isinstance(status, dict):
        status = {}

    return inteiro(
        primeiro_valor(
            jogo.get("elapsed"),
            status.get("elapsed"),
            jogo.get("minute")
        ),
        0
    )


def extrair_status(jogo):
    fixture = jogo.get("fixture", {})

    if not isinstance(fixture, dict):
        fixture = {}

    status = fixture.get("status", {})

    if isinstance(status, dict):
        valor = primeiro_valor(
            status.get("short"),
            status.get("long"),
            status.get("status")
        )
    else:
        valor = status

    valor = primeiro_valor(
        valor,
        jogo.get("status"),
        jogo.get("state")
    )

    return str(valor or "").lower().strip()


def eh_finalizado(jogo):
    status = extrair_status(jogo)

    status_finais = [
        "ft",
        "finished",
        "complete",
        "completed",
        "aet",
        "pen"
    ]

    return any(item in status for item in status_finais)


def extrair_cantos(jogo):
    """
    Tenta identificar escanteios em vários formatos possíveis.
    """

    corners = primeiro_valor(
        jogo.get("corners"),
        jogo.get("corner"),
        jogo.get("corner_kicks"),
        jogo.get("statistics", {}).get("corners")
        if isinstance(jogo.get("statistics"), dict)
        else None
    )

    if corners is None:
        return 0

    if isinstance(corners, (int, float, str)):
        return inteiro(corners, 0)

    if isinstance(corners, dict):
        total = primeiro_valor(
            corners.get("total"),
            corners.get("value"),
            corners.get("count")
        )

        if total is not None:
            return inteiro(total, 0)

        home = primeiro_valor(
            corners.get("home"),
            corners.get("local"),
            corners.get("home_total")
        )

        away = primeiro_valor(
            corners.get("away"),
            corners.get("visitor"),
            corners.get("away_total")
        )

        if home is not None or away is not None:
            return inteiro(home, 0) + inteiro(away, 0)

    return 0


def extrair_odds(jogo):
    odds = primeiro_valor(
        jogo.get("odds"),
        jogo.get("pre_odds"),
        jogo.get("bookmakers")
    )

    if odds is None:
        return []

    if isinstance(odds, list):
        return odds

    if isinstance(odds, dict):
        resultado = []

        # Caso venha como {"markets": [...]}
        if isinstance(odds.get("markets"), list):
            resultado.extend(odds["markets"])

        # Caso venha como {"data": [...]}
        if isinstance(odds.get("data"), list):
            resultado.extend(odds["data"])

        # Caso venha como {"bookmakers": [...]}
        if isinstance(odds.get("bookmakers"), list):
            resultado.extend(odds["bookmakers"])

        # Caso seja um único mercado
        if not resultado:
            resultado.append(odds)

        return resultado

    return []


def texto_odds(odd):
    partes = []

    if isinstance(odd, dict):
        for chave in [
            "market",
            "name",
            "selection",
            "label",
            "team",
            "side",
            "outcome",
            "value"
        ]:
            valor = odd.get(chave)

            if valor is not None:
                partes.append(str(valor))

    return " ".join(partes).lower()


def valor_odd(odd):
    if not isinstance(odd, dict):
        return 0.0

    return numero(
        primeiro_valor(
            odd.get("odd"),
            odd.get("price"),
            odd.get("value"),
            odd.get("decimal"),
            odd.get("odds")
        ),
        0.0
    )


# =========================================================
# API
# =========================================================

def api_get(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"

    try:
        resposta = requests.get(
            url,
            headers=HEADERS,
            params=params or {},
            timeout=25
        )

        print(
            f"API {endpoint} | HTTP {resposta.status_code}",
            flush=True
        )

        try:
            dados = resposta.json()
        except Exception:
            print(
                f"RESPOSTA NÃO JSON: {resposta.text[:500]}",
                flush=True
            )
            return []

        if resposta.status_code != 200:
            print(
                f"RESPOSTA API: {json.dumps(dados, ensure_ascii=False)[:1000]}",
                flush=True
            )
            return []

        if isinstance(dados, list):
            return dados

        if not isinstance(dados, dict):
            return []

        resultado = primeiro_valor(
            dados.get("data"),
            dados.get("response"),
            dados.get("fixtures"),
            dados.get("results")
        )

        if isinstance(resultado, list):
            return resultado

        if isinstance(resultado, dict):
            for chave in ["data", "response", "fixtures", "results"]:
                if isinstance(resultado.get(chave), list):
                    return resultado[chave]

        return []

    except requests.RequestException as erro:
        print(f"ERRO DE CONEXÃO API {endpoint}: {erro}", flush=True)
        return []

    except Exception as erro:
        print(f"ERRO API {endpoint}: {erro}", flush=True)
        return []


def buscar_live():
    jogos = api_get(
        "/fixtures",
        params={
            "status": "live",
            "include": "odds,events,stats",
            "per_page": 500
        }
    )

    print(
        f"LIVE: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


def buscar_pre():
    jogos = api_get(
        "/fixtures",
        params={
            "status": "scheduled",
            "include": "odds",
            "per_page": 100
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
            "status": "finished",
            "per_page": 500
        }
    )

    print(
        f"FINALIZADOS: {len(jogos)} jogos encontrados",
        flush=True
    )

    return jogos


# =========================================================
# TELEGRAM
# =========================================================

def enviar_telegram(mensagem):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_TOKEN ou CHAT_ID não configurado.", flush=True)
        return False

    try:
        resposta = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if resposta.status_code != 200:
            print(
                f"ERRO TELEGRAM {resposta.status_code}: {resposta.text}",
                flush=True
            )
            return False

        return True

    except Exception as erro:
        print(f"ERRO TELEGRAM: {erro}", flush=True)
        return False


# =========================================================
# ANÁLISE DE FAVORITO
# =========================================================

def get_favorito_info(jogo):
    odds = extrair_odds(jogo)

    home_odd = 99.0
    away_odd = 99.0

    for odd in odds:
        if not isinstance(odd, dict):
            continue

        texto = texto_odds(odd)
        preco = valor_odd(odd)

        if preco < 1.01:
            continue

        eh_vencedor = any(
            termo in texto
            for termo in [
                "match winner",
                "1x2",
                "full time result",
                "winner",
                "vencedor",
                "resultado final",
                "home",
                "away"
            ]
        )

        if not eh_vencedor:
            continue

        eh_home = (
            "home" in texto
            or "casa" in texto
            or "selection 1" in texto
            or texto.strip() == "1"
        )

        eh_away = (
            "away" in texto
            or "fora" in texto
            or "selection 2" in texto
            or texto.strip() == "2"
        )

        if eh_home and preco < home_odd:
            home_odd = preco

        if eh_away and preco < away_odd:
            away_odd = preco

    if home_odd <= away_odd and home_odd <= ODD_FAVORITO_MAXIMA:
        return "HOME", home_odd, home_odd, away_odd

    if away_odd < home_odd and away_odd <= ODD_FAVORITO_MAXIMA:
        return "AWAY", away_odd, home_odd, away_odd

    return None, 0.0, home_odd, away_odd


# =========================================================
# FILTRO OVER 2.5
# =========================================================

def filtra_over25(jogo):
    odds = extrair_odds(jogo)

    for odd in odds:
        if not isinstance(odd, dict):
            continue

        texto = texto_odds(odd)
        preco = valor_odd(odd)

        if preco <= 0:
            continue

        tem_over = "over" in texto or "mais de" in texto
        tem_gols = (
            "goal" in texto
            or "goals" in texto
            or "gol" in texto
            or "gols" in texto
        )

        # Aceita somente linha 2.5
        linha_25 = (
            "2.5" in texto
            or "2,5" in texto
            or "over 2 5" in texto
        )

        # Evita aceitar Over 2.0 ou mercados diferentes
        linha_20 = (
            "2.0" in texto
            or "2,0" in texto
            or "over 2 " in texto
        )

        if tem_over and tem_gols and linha_25 and not linha_20:
            if ODD_OVER_MINIMA <= preco <= ODD_OVER_MAXIMA:
                mercado = primeiro_valor(
                    odd.get("market"),
                    odd.get("name"),
                    odd.get("selection"),
                    "Over 2.5 Gols"
                )

                return preco, str(mercado)

    return None


# =========================================================
# HISTÓRICO
# =========================================================

def registrar_historico(tipo, resultado, info, dados_finais=None):
    registro = {
        "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tipo": tipo,
        "resultado": resultado,
        "home": info.get("home"),
        "away": info.get("away"),
        "odd": info.get("odd"),
        "favorito": info.get("fav"),
        "minuto_entrada": info.get("minuto"),
        "cantos_entrada": info.get("cantos"),
        "placar_entrada": info.get("placar"),
    }

    if dados_finais:
        registro.update(dados_finais)

    with lock:
        historico.append(registro)

        # Mantém no máximo 5.000 registros
        if len(historico) > 5000:
            del historico[:-5000]


# =========================================================
# FECHAMENTO DOS SINAIS
# =========================================================

def fechar_live(jogo, info):
    total_cantos = extrair_cantos(jogo)
    gols_home, gols_away = extrair_placar(jogo)
    total_gols = gols_home + gols_away

    if total_cantos > 9:
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

    registrar_historico(
        "LIVE OVER 9.0",
        resultado,
        info,
        {
            "cantos_finais": total_cantos,
            "gols_finais": total_gols
        }
    )

    salvar_dados()

    enviar_telegram(
        f"{emoji} <b>{resultado} - LIVE OVER 9.0</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"🚩 Escanteios finais: {total_cantos}\n"
        f"⚽ Gols finais: {total_gols}\n"
        f"📌 Entrada: Over 9.0 FT\n\n"
        f"📊 Live: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats['live_voids']}V\n"
        f"📈 Taxa Live: {taxa_live()}%"
    )


def fechar_pre(jogo, info):
    gols_home, gols_away = extrair_placar(jogo)
    total_gols = gols_home + gols_away

    if total_gols >= 3:
        resultado = "GREEN"
        emoji = "🟢"

        stats["pre_wins"] += 1

    else:
        resultado = "RED"
        emoji = "🔴"

        stats["pre_losses"] += 1

    registrar_historico(
        "PRÉ OVER 2.5",
        resultado,
        info,
        {
            "gols_finais": total_gols
        }
    )

    salvar_dados()

    enviar_telegram(
        f"{emoji} <b>{resultado} - PRÉ OVER 2.5</b>\n\n"
        f"🏟️ {info['home']} x {info['away']}\n"
        f"⚽ Gols finais: {total_gols}\n"
        f"💰 Odd: {info['odd']}\n"
        f"📌 Entrada: Over 2.5 Gols\n\n"
        f"📊 Pré: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L\n"
        f"📈 Taxa Pré: {taxa_pre()}%"
    )


# =========================================================
# PROCESSAMENTO LIVE
# =========================================================

def processar_live():
    jogos = buscar_live()

    for jogo in jogos:
        try:
            fid = extrair_id(jogo)

            if not fid:
                continue

            if fid in entradas_live:
                continue

            minuto = extrair_minuto(jogo)

            if not (
                MINUTO_INICIAL_LIVE
                <= minuto
                <= MINUTO_FINAL_LIVE
            ):
                continue

            home, away = extrair_times(jogo)
            gols_home, gols_away = extrair_placar(jogo)
            cantos = extrair_cantos(jogo)

            if cantos < MIN_CANTOS_LIVE:
                continue

            favorito, odd_favorito, home_odd, away_odd = (
                get_favorito_info(jogo)
            )

            if not favorito:
                continue

            condicao = False
            descricao = ""

            if favorito == "HOME":
                diferenca = gols_away - gols_home

                if diferenca == 0:
                    condicao = True
                    descricao = (
                        f"{gols_home}x{gols_away} - "
                        "favorito da casa empatando"
                    )

                elif 1 <= diferenca <= 2:
                    condicao = True
                    descricao = (
                        f"{gols_home}x{gols_away} - "
                        f"favorito da casa perdendo por {diferenca}"
                    )

            elif favorito == "AWAY":
                diferenca = gols_home - gols_away

                if diferenca == 0:
                    condicao = True
                    descricao = (
                        f"{gols_home}x{gols_away} - "
                        "favorito visitante empatando"
                    )

                elif 1 <= diferenca <= 2:
                    condicao = True
                    descricao = (
                        f"{gols_home}x{gols_away} - "
                        f"favorito visitante perdendo por {diferenca}"
                    )

            if not condicao:
                continue

            info = {
                "home": home,
                "away": away,
                "odd": odd_favorito,
                "fav": favorito,
                "minuto": minuto,
                "cantos": cantos,
                "placar": f"{gols_home}x{gols_away}",
                "entrada": datetime.now().strftime("%H:%M:%S")
            }

            entradas_live[fid] = True
            pendentes_live[fid] = info

            stats["total_sinais"] += 1
            salvar_dados()

            enviar_telegram(
                f"🔥 <b>LIVE - OVER 9.0 ESCANTEIOS</b>\n\n"
                f"🏟️ {home} x {away}\n"
                f"⏰ Minuto: {minuto}'\n"
                f"📊 Placar: {descricao}\n"
                f"⭐ Favorito: {favorito} @ {odd_favorito}\n"
                f"🚩 Cantos atuais: {cantos}\n\n"
                f"👉 <b>ENTRADA: Over 9.0 FT</b>\n"
                f"📈 Taxa Live: {taxa_live()}%"
            )

            print(
                f"SINAL LIVE: {home} x {away} | "
                f"{minuto}' | cantos {cantos}",
                flush=True
            )

        except Exception as erro:
            print(f"ERRO AO PROCESSAR LIVE: {erro}", flush=True)


# =========================================================
# PROCESSAMENTO PRÉ-JOGO
# =========================================================

def processar_pre():
    jogos = buscar_pre()

    for jogo in jogos:
        try:
            fid = extrair_id(jogo)

            if not fid:
                continue

            if fid in entradas_pre:
                continue

            resultado_odd = filtra_over25(jogo)

            if not resultado_odd:
                continue

            odd_valor, mercado = resultado_odd

            home, away = extrair_times(jogo)

            info = {
                "home": home,
                "away": away,
                "odd": odd_valor,
                "fav": None,
                "minuto": None,
                "cantos": None,
                "placar": None,
                "mercado": mercado,
                "entrada": datetime.now().strftime("%H:%M:%S")
            }

            entradas_pre[fid] = True
            pendentes_pre[fid] = info

            stats["total_sinais"] += 1
            salvar_dados()

            enviar_telegram(
                f"⚽ <b>PRÉ-JOGO - OVER 2.5 GOLS</b>\n\n"
                f"🏟️ {home} x {away}\n"
                f"💰 Mercado: {mercado}\n"
                f"📌 Odd: {odd_valor}\n\n"
                f"👉 <b>ENTRADA: Mais de 2.5 gols</b>\n"
                f"📈 Taxa Pré: {taxa_pre()}%"
            )

            print(
                f"SINAL PRÉ: {home} x {away} | odd {odd_valor}",
                flush=True
            )

            # Mantém apenas um sinal pré por ciclo
            break

        except Exception as erro:
            print(f"ERRO AO PROCESSAR PRÉ: {erro}", flush=True)


# =========================================================
# CONFERIR RESULTADOS
# =========================================================

def conferir_resultados():
    if not pendentes_live and not pendentes_pre:
        return

    jogos = buscar_finalizados()

    for jogo in jogos:
        try:
            fid = extrair_id(jogo)

            if not fid:
                continue

            if not eh_finalizado(jogo):
                continue

            if fid in pendentes_live:
                info = pendentes_live.pop(fid)
                fechar_live(jogo, info)

            if fid in pendentes_pre:
                info = pendentes_pre.pop(fid)
                fechar_pre(jogo, info)

        except Exception as erro:
            print(
                f"ERRO AO CONFERIR RESULTADO: {erro}",
                flush=True
            )


# =========================================================
# LOOP PRINCIPAL
# =========================================================

def iniciar_bot():
    print("========================================", flush=True)
    print("BOT DE ESCANTEIOS INICIADO", flush=True)
    print(f"BASE API: {BASE_URL}", flush=True)
    print(f"LIVE: {MINUTO_INICIAL_LIVE}-{MINUTO_FINAL_LIVE} minutos", flush=True)
    print(f"MÍNIMO DE CANTOS: {MIN_CANTOS_LIVE}", flush=True)
    print("========================================", flush=True)

    enviar_telegram(
        f"🤖 <b>BOT DE ESCANTEIOS ONLINE</b>\n\n"
        f"📊 Live: "
        f"{stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats.get('live_voids', 0)}V\n"
        f"📊 Pré: "
        f"{stats['pre_wins']}W-"
        f"{stats['pre_losses']}L\n"
        f"📈 Taxa Live: {taxa_live()}%\n"
        f"📈 Taxa Pré: {taxa_pre()}%\n\n"
        f"✅ API configurada: {'SIM' if API_KEY else 'NÃO'}"
    )

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:
        try:
            processar_live()

            agora = time.time()

            if agora - ultimo_pre >= INTERVALO_PRE:
                ultimo_pre = agora
                processar_pre()

            if agora - ultimo_resultado >= INTERVALO_RESULTADOS:
                ultimo_resultado = agora
                conferir_resultados()

        except Exception as erro:
            print(
                f"ERRO NO LOOP PRINCIPAL: {erro}",
                flush=True
            )

        time.sleep(INTERVALO_LIVE)


# =========================================================
# ROTAS FLASK
# =========================================================

@app.route("/")
def home():
    return (
        "BOT ONLINE | "
        f"Live: {stats['live_wins']}W-"
        f"{stats['live_losses']}L-"
        f"{stats.get('live_voids', 0)}V "
        f"({taxa_live()}%) | "
        f"Pré: {stats['pre_wins']}W-"
        f"{stats['pre_losses']}L "
        f"({taxa_pre()}%) | "
        f"Geral: {taxa_geral()}% | "
        f"Sinais: {stats.get('total_sinais', 0)}"
    )


@app.route("/stats")
def rota_stats():
    with lock:
        return jsonify({
            "stats": stats,
            "taxa_live": taxa_live(),
            "taxa_pre": taxa_pre(),
            "taxa_geral": taxa_geral(),
            "pendentes_live": len(pendentes_live),
            "pendentes_pre": len(pendentes_pre)
        })


@app.route("/historico")
def rota_historico():
    with lock:
        return jsonify(historico[-100:])


@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "api_configurada": bool(API_KEY),
        "telegram_configurado": bool(TOKEN and CHAT_ID),
        "pendentes_live": len(pendentes_live),
        "pendentes_pre": len(pendentes_pre),
        "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


# =========================================================
# INICIALIZAÇÃO
# =========================================================

thread_bot = threading.Thread(
    target=iniciar_bot,
    daemon=True
)

thread_bot.start()


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=porta,
        debug=False,
        use_reloader=False
    )
