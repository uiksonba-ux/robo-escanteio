import os
import sys
import json
import time
import logging
import threading
import html
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from flask import Flask, jsonify


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)
log = logging.getLogger("robo")


app = Flask(__name__)

BASE_API = os.getenv("BASE_API", "https://api.5dollarfootballapi.com/v1")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))
QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))
HORAS_MIN = float(os.getenv("HORAS_MIN", "1"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "6"))
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "600"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "600"))
MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))
ASSERTIVIDADE_MINIMA = float(os.getenv("ASSERTIVIDADE_MINIMA", "60"))
ARQUIVO_ESTADO = "bot_state.json"


def _criar_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _criar_session()

lock = threading.Lock()

estado = {
    "stats": {
        "combinados_wins": 0,
        "combinados_losses": 0,
        "combinados_push": 0,
        "gols_wins": 0,
        "gols_losses": 0,
        "gols_push": 0,
        "cantos_wins": 0,
        "cantos_losses": 0,
        "cantos_push": 0,
        "total_sinais": 0
    },
    "pendentes": {},
    "historico": []
}


def salvar_estado():
    try:
        with lock:
            with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
                json.dump(estado, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Erro ao salvar estado: {e}")


def carregar_estado():
    global estado
    if not os.path.exists(ARQUIVO_ESTADO):
        log.info("Nenhum estado anterior encontrado.")
        return
    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if isinstance(dados, dict):
            if "stats" in dados:
                estado["stats"].update(dados["stats"])
            if "pendentes" in dados:
                estado["pendentes"] = dados["pendentes"]
            if "historico" in dados:
                estado["historico"] = dados["historico"]
        log.info(
            f"Estado carregado: {len(estado['pendentes'])} pendentes, "
            f"{len(estado['historico'])} no histórico"
        )
    except Exception as e:
        log.error(f"Erro ao carregar estado: {e}")


def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning("Telegram não configurado")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = SESSION.post(url, json=payload, timeout=20)
        if r.ok:
            return True
        log.error(f"Erro Telegram: {r.status_code} {r.text[:300]}")
    except Exception as e:
        log.error(f"Erro ao enviar Telegram: {e}")

    return False


def api_get(endpoint, params=None):
    if not API_KEY:
        log.error("FIVE_DOLLAR_KEY não configurada.")
        return None

    url = f"{BASE_API}{endpoint}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }

    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=25)
        if r.status_code != 200:
            log.error(f"API {endpoint} -> {r.status_code}: {r.text[:400]}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"Erro API {endpoint}: {e}")
        return None


def extrair_lista(data):
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for chave in ("data", "fixtures", "results", "matches", "response"):
            valor = data.get(chave)
            if isinstance(valor, list):
                return valor
    return []


def extrair_id(jogo):
    if not isinstance(jogo, dict):
        return None
    for chave in ("id", "fixture_id", "fixtureId", "match_id", "event_id"):
        v = jogo.get(chave)
        if v is not None:
            return str(v)
    fixture = jogo.get("fixture")
    if isinstance(fixture, dict):
        for chave in ("id", "fixture_id"):
            v = fixture.get(chave)
            if v is not None:
                return str(v)
    return None


def extrair_times(jogo):
    home = "Casa"
    away = "Fora"
    if not isinstance(jogo, dict):
        return home, away
    teams = jogo.get("teams")
    if isinstance(teams, dict):
        h = teams.get("home")
        a = teams.get("away")
        if isinstance(h, dict):
            home = h.get("name") or h.get("team_name") or home
        if isinstance(a, dict):
            away = a.get("name") or a.get("team_name") or away
    return str(home), str(away)


def extrair_placar(jogo):
    if not isinstance(jogo, dict):
        return 0, 0
    goals = jogo.get("goals")
    if isinstance(goals, dict):
        try:
            return int(goals.get("home", 0) or 0), int(goals.get("away", 0) or 0)
        except Exception:
            pass
    score = jogo.get("score")
    if isinstance(score, dict):
        home = score.get("home")
        away = score.get("away")
        try:
            gh = home.get("goals") if isinstance(home, dict) else home
            ga = away.get("goals") if isinstance(away, dict) else away
            return int(gh or 0), int(ga or 0)
        except Exception:
            pass
    return 0, 0


def extrair_cantos(jogo):
    if not isinstance(jogo, dict):
        return 0
    corners = jogo.get("corners")
    if isinstance(corners, dict):
        try:
            h = int(corners.get("home", 0) or 0)
            a = int(corners.get("away", 0) or 0)
            return h + a
        except Exception:
            pass
    return 0


def extrair_status(jogo):
    if not isinstance(jogo, dict):
        return ""
    status = jogo.get("status")
    if isinstance(status, dict):
        return str(
            status.get("short") or status.get("long") or status.get("status") or ""
        ).upper()
    return str(status or "").upper()


def eh_finalizado(jogo):
    status = extrair_status(jogo).lower()
    return any(f in status for f in (
        "ft", "aet", "pen", "finished", "finalizado", "ended", "match finished"
    ))


def extrair_inicio_timestamp(jogo):
    if not isinstance(jogo, dict):
        return None
    valores = [
        jogo.get("kickoff_ts"),
        jogo.get("kickoff_utc"),
        jogo.get("start_time"),
        jogo.get("date"),
        jogo.get("datetime"),
    ]
    fixture = jogo.get("fixture")
    if isinstance(fixture, dict):
        valores.extend([fixture.get("date"), fixture.get("start_time")])

    for valor in valores:
        if not valor:
            continue
        if isinstance(valor, (int, float)):
            v = float(valor)
            if v > 1e12:
                v = v / 1000
            return v
        texto = str(valor).strip()
        try:
            if texto.isdigit():
                v = float(texto)
                if v > 1e12:
                    v = v / 1000
                return v
        except Exception:
            pass
        try:
            t = texto.replace("Z", "+00:00")
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
    return None


def minutos_ate_jogo(jogo):
    ts = extrair_inicio_timestamp(jogo)
    if ts is None:
        return None
    return (ts - datetime.now(timezone.utc).timestamp()) / 60


def dentro_da_janela(jogo):
    minutos = minutos_ate_jogo(jogo)
    if minutos is None:
        return False
    return (HORAS_MIN * 60) <= minutos <= (HORAS_MAX * 60)


def extrair_mercado_bookmakers(data, mercado):
    resultado = []
    if not isinstance(data, dict):
        return resultado

    bookmakers = data.get("bookmakers")
    if not isinstance(bookmakers, list):
        bookmakers = data.get("data", [])
    if not isinstance(bookmakers, list):
        return resultado

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue
        nome_bookmaker = (
            bookmaker.get("name") or bookmaker.get("bookmaker") or "Bookmaker"
        )
        mercados = (
            bookmaker.get("bets") or bookmaker.get("markets")
            or bookmaker.get("odds") or []
        )
        if isinstance(mercados, dict):
            mercados = list(mercados.values())
        if not isinstance(mercados, list):
            continue

        for bloco in mercados:
            if not isinstance(bloco, dict):
                continue
            nome = str(
                bloco.get("name") or bloco.get("market") or bloco.get("key") or ""
            ).lower()

            if mercado == "gols":
                aceito = ("goal" in nome or "goalline" in nome
                          or "total goals" in nome or "gols" in nome)
            else:
                aceito = ("corner" in nome or "corners" in nome
                          or "escanteio" in nome or "cantos" in nome)

            if aceito:
                resultado.append({"bookmaker": nome_bookmaker, "bloco": bloco})

    return resultado


def extrair_preco_do_bloco(bloco):
    if not isinstance(bloco, dict):
        return None, None
    fontes = [bloco.get("closing"), bloco.get("opening"),
              bloco.get("current"), bloco]
    for fonte in fontes:
        if not isinstance(fonte, dict):
            continue
        linha = (fonte.get("line") or fonte.get("goal_line")
                 or fonte.get("corner_line") or fonte.get("handicap"))
        over = (fonte.get("over") or fonte.get("odds")
                or fonte.get("price") or fonte.get("value"))
        try:
            if isinstance(over, dict):
                over = over.get("price") or over.get("odd") or over.get("value")
            if linha is not None:
                linha = float(linha)
            if over is not None:
                over = float(over)
            if linha is not None and over is not None:
                return linha, over
        except Exception:
            pass
    return None, None


def obter_melhor_odd(fixture_id, mercado):
    endpoint_mercado = "goalline" if mercado == "gols" else "corner"
    data = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={"market": endpoint_mercado, "lang": "pt"}
    )
    if not data:
        return None

    blocos = extrair_mercado_bookmakers(data, mercado)
    candidatos = []
    for item in blocos:
        linha, odd = extrair_preco_do_bloco(item["bloco"])
        if linha is None or odd is None:
            continue
        if not (ODD_MIN <= odd <= ODD_MAX):
            continue
        candidatos.append({
            "bookmaker": item["bookmaker"],
            "linha": linha,
            "odd": odd
        })

    if not candidatos:
        return None
    return max(candidatos, key=lambda x: x["odd"])


def linha_gols_valida(linha):
    return linha in (0.5, 1.5, 2.5)


def linha_cantos_valida(linha):
    return linha in (6.5, 7.5, 8.5, 9.5, 10.5)


def filtrar_gols(odd_info):
    return bool(odd_info) and linha_gols_valida(odd_info["linha"])


def filtrar_cantos(odd_info):
    return bool(odd_info) and linha_cantos_valida(odd_info["linha"])


def resolver_linha(total, linha):
    total = float(total)
    linha = float(linha)
    if linha % 1 != 0:
        return "WIN" if total > linha else "LOSS"
    if total > linha:
        return "WIN"
    if total == linha:
        return "PUSH"
    return "LOSS"


def resolver_combinado(rg, rc):
    if "LOSS" in {rg, rc}:
        return "LOSS"
    if rg == "WIN" and rc == "WIN":
        return "WIN"
    return "PUSH"


def calcular_assertividade(wins, losses):
    total = wins + losses
    if total <= 0:
        return 0.0
    return round(wins / total * 100, 2)


def estatisticas_combinadas():
    s = estado["stats"]
    return {
        "wins": s["combinados_wins"],
        "losses": s["combinados_losses"],
        "push": s["combinados_push"],
        "assertividade": calcular_assertividade(
            s["combinados_wins"], s["combinados_losses"]
        )
    }


def obter_historico_7_dias():
    limite = time.time() - (7 * 24 * 60 * 60)
    return [x for x in estado.get("historico", [])
            if float(x.get("timestamp", 0)) >= limite]


def assertividade_7_dias():
    h = obter_historico_7_dias()
    wins = sum(1 for x in h if x.get("resultado") == "WIN")
    losses = sum(1 for x in h if x.get("resultado") == "LOSS")
    pushes = sum(1 for x in h if x.get("resultado") == "PUSH")
    return {
        "sinais": len(h),
        "wins": wins,
        "losses": losses,
        "push": pushes,
        "assertividade": calcular_assertividade(wins, losses)
    }


def buscar_pre():
    # A API não aceita start_time/end_time em ISO. Buscamos sem filtro
    # e filtramos localmente pela janela HORAS_MIN..HORAS_MAX.
    data = api_get("/fixtures", params={"per_page": 200, "lang": "pt"})
    jogos = extrair_lista(data)
    log.info(f"buscar_pre: API retornou {len(jogos)} jogos")

    candidatos = []
    for jogo in jogos:
        fid = extrair_id(jogo)
        minutos = minutos_ate_jogo(jogo)

        if fid is None or minutos is None:
            continue

        # Só jogos ainda não começados
        if minutos < 0:
            continue

        if dentro_da_janela(jogo):
            candidatos.append(jogo)
            log.info(f"[{fid}] {minutos:.0f}min → candidato")

    log.info(f"buscar_pre: {len(candidatos)} candidatos")
    return candidatos


def criar_sinal_combinado(jogo):
    fid = extrair_id(jogo)
    if not fid:
        return None

    home, away = extrair_times(jogo)
    gols = obter_melhor_odd(fid, "gols")
    cantos = obter_melhor_odd(fid, "cantos")

    if not gols:
        log.info(f"[{fid}] sem odds de gols")
        return None
    if not cantos:
        log.info(f"[{fid}] sem odds de cantos")
        return None
    if not filtrar_gols(gols):
        log.info(f"[{fid}] linha gols inválida: {gols['linha']}")
        return None
    if not filtrar_cantos(cantos):
        log.info(f"[{fid}] linha cantos inválida: {cantos['linha']}")
        return None
    if gols["bookmaker"].lower() != cantos["bookmaker"].lower():
        log.info(f"[{fid}] bookmakers diferentes")
        return None

    odd_combinada = gols["odd"] * cantos["odd"]
    if not (ODD_MIN <= odd_combinada <= ODD_MAX):
        log.info(f"[{fid}] odd combinada fora: {odd_combinada:.2f}")
        return None

    historico = assertividade_7_dias()
    if (historico["sinais"] >= MINIMO_HISTORICO
            and historico["assertividade"] < ASSERTIVIDADE_MINIMA):
        log.info(f"[{fid}] filtrado por assertividade")
        return None

    return {
        "id": fid,
        "home": home,
        "away": away,
        "bookmaker": gols["bookmaker"],
        "gols": {"linha": gols["linha"], "odd": gols["odd"]},
        "cantos": {"linha": cantos["linha"], "odd": cantos["odd"]},
        "odd_combinada": round(odd_combinada, 2),
        "assertividade_7d": historico["assertividade"],
        "historico_7d": historico["sinais"],
        "timestamp": time.time(),
        "resultado": None
    }


def mensagem_sinal(s):
    return (
        "🔥 <b>SINAL COMBINADO</b>\n\n"
        f"⚽ <b>{html.escape(s['home'])}</b> x "
        f"<b>{html.escape(s['away'])}</b>\n\n"
        "🎯 <b>ENTRADA</b>\n"
        f"⚽ Over {s['gols']['linha']} Gols @ {s['gols']['odd']:.2f}\n"
        f"🚩 Over {s['cantos']['linha']} Escanteios @ {s['cantos']['odd']:.2f}\n\n"
        f"💰 <b>Odd combinada:</b> {s['odd_combinada']:.2f}\n"
        f"🏦 <b>Casa:</b> {html.escape(s['bookmaker'])}\n\n"
        f"📊 <b>Histórico 7 dias:</b> {s['historico_7d']} sinais\n"
        f"📈 <b>Assertividade:</b> {s['assertividade_7d']:.2f}%\n\n"
        "⚠️ Assertividade baseada no histórico do robô."
    )


def analisar_pre(jogo):
    fid = extrair_id(jogo)
    if not fid:
        return False

    chave = f"{fid}_COMBINADO"

    if chave in estado["pendentes"]:
        return False
    if any(h.get("id") == fid for h in estado.get("historico", [])[-2000:]):
        return False
    if not dentro_da_janela(jogo):
        return False

    sinal = criar_sinal_combinado(jogo)
    if not sinal:
        return False

    if not enviar_telegram(mensagem_sinal(sinal)):
        log.error(f"[{fid}] Telegram falhou")
        return False

    estado["pendentes"][chave] = sinal
    estado["stats"]["total_sinais"] += 1
    salvar_estado()
    log.info(f"[SINAL] {sinal['home']} x {sinal['away']} @ {sinal['odd_combinada']}")
    return True


def buscar_jogo_por_id(fid):
    data = api_get(f"/fixtures/{fid}", params={"lang": "pt"})
    if not data:
        return None
    if isinstance(data, dict):
        for chave in ("data", "fixture"):
            v = data.get(chave)
            if isinstance(v, dict):
                return v
            if isinstance(v, list) and v:
                return v[0]
    return data


def finalizar(chave, sinal, jogo):
    gc, gf = extrair_placar(jogo)
    tg = gc + gf
    tc = extrair_cantos(jogo)

    rg = resolver_linha(tg, sinal["gols"]["linha"])
    rc = resolver_linha(tc, sinal["cantos"]["linha"])
    resultado = resolver_combinado(rg, rc)

    sinal.update({
        "resultado": resultado,
        "resultado_gols": rg,
        "resultado_cantos": rc,
        "placar_final": f"{gc} x {gf}",
        "total_gols": tg,
        "total_cantos": tc
    })

    s = estado["stats"]
    s[{"WIN": "combinados_wins", "LOSS": "combinados_losses"}
      .get(resultado, "combinados_push")] += 1
    s[{"WIN": "gols_wins", "LOSS": "gols_losses"}
      .get(rg, "gols_push")] += 1
    s[{"WIN": "cantos_wins", "LOSS": "cantos_losses"}
      .get(rc, "cantos_push")] += 1

    hist = dict(sinal)
    hist["timestamp_resultado"] = time.time()
    estado["historico"].append(hist)
    estado["historico"] = [
        x for x in estado["historico"]
        if time.time() - float(x.get("timestamp", time.time())) <= 30 * 24 * 60 * 60
    ]

    estado["pendentes"].pop(chave, None)
    salvar_estado()

    ass = estatisticas_combinadas()
    enviar_telegram(
        "🏁 <b>RESULTADO DO SINAL</b>\n\n"
        f"⚽ <b>{html.escape(sinal['home'])}</b> x "
        f"<b>{html.escape(sinal['away'])}</b>\n\n"
        f"📊 Resultado: <b>{resultado}</b>\n"
        f"⚽ Gols: {rg}\n🚩 Escanteios: {rc}\n\n"
        f"🔢 Placar: {gc} x {gf}\n"
        f"🚩 Total escanteios: {tc}\n\n"
        f"📈 Assertividade geral: {ass['assertividade']:.2f}%"
    )
    log.info(f"[FIM] {sinal['home']} x {sinal['away']} -> {resultado}")


def verificar_resultados():
    pendentes = list(estado["pendentes"].items())
    agora = time.time()
    expirados = []

    for chave, sinal in pendentes:
        if agora - sinal.get("timestamp", agora) > 6 * 3600:
            expirados.append(chave)
            continue
        try:
            jogo = buscar_jogo_por_id(sinal["id"])
            if jogo and eh_finalizado(jogo):
                finalizar(chave, sinal, jogo)
        except Exception as e:
            log.exception(f"Erro finalizando {chave}: {e}")

    for c in expirados:
        estado["pendentes"].pop(c, None)
    if expirados:
        salvar_estado()


def gerar_stats():
    with lock:
        return {
            "combinados": estatisticas_combinadas(),
            "ultimos_7_dias": assertividade_7_dias(),
            "pendentes": len(estado["pendentes"]),
            "total_sinais": estado["stats"]["total_sinais"]
        }


def loop_bot():
    log.info("================================")
    log.info("ROBÔ GOLS + ESCANTEIOS")
    log.info(f"API_KEY? {bool(API_KEY)} | Telegram? {bool(TELEGRAM_TOKEN and CHAT_ID)}")
    log.info(f"Janela: {HORAS_MIN}h a {HORAS_MAX}h | ODD: {ODD_MIN}-{ODD_MAX}")
    log.info("================================")

    if TELEGRAM_TOKEN and CHAT_ID:
        enviar_telegram("🟢 <b>ROBÔ ONLINE</b>\n\nSistema iniciado.")

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:
        agora = time.time()

        if agora - ultimo_pre >= INTERVALO_PRE:
            ultimo_pre = agora
            log.info("🔎 Verificando pré-jogos...")
            try:
                jogos = buscar_pre()
                enviados = 0
                for jogo in jogos:
                    if enviados >= QTD_POR_RODADA:
                        break
                    if analisar_pre(jogo):
                        enviados += 1
                log.info(f"Sinais enviados: {enviados}")
            except Exception:
                log.exception("Erro análise pré")

        if agora - ultimo_resultado >= INTERVALO_RESULTADOS:
            ultimo_resultado = agora
            log.info(f"🏁 Resultados... ({len(estado['pendentes'])} pendentes)")
            try:
                verificar_resultados()
            except Exception:
                log.exception("Erro resultados")

        time.sleep(30)


@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Gols + Escanteios",
        "pendentes": len(estado["pendentes"])
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "telegram": bool(TELEGRAM_TOKEN and CHAT_ID),
        "api": bool(API_KEY),
        "pendentes": len(estado["pendentes"])
    })


@app.route("/stats")
def stats():
    return jsonify(gerar_stats())


@app.route("/debug/thread")
def debug_thread():
    return jsonify({
        "thread_ativa": _bot_thread.is_alive() if "_bot_thread" in globals() else False,
        "thread_nome": _bot_thread.name if "_bot_thread" in globals() else None,
        "threads": [t.name for t in threading.enumerate()],
        "pendentes": len(estado["pendentes"]),
        "total_sinais": estado["stats"]["total_sinais"]
    })


@app.route("/debug/rodar-agora")
def debug_rodar_agora():
    resultado = {
        "api_key_ok": bool(API_KEY),
        "telegram_ok": bool(TELEGRAM_TOKEN and CHAT_ID),
        "jogos_encontrados": 0,
        "candidatos_janela": 0,
        "sinais_enviados": 0,
        "erros": []
    }
    try:
        jogos = buscar_pre()
        resultado["jogos_encontrados"] = len(jogos)
        resultado["candidatos_janela"] = len(jogos)

        enviados = 0
        for jogo in jogos:
            if enviados >= QTD_POR_RODADA:
                break
            if analisar_pre(jogo):
                enviados += 1
        resultado["sinais_enviados"] = enviados
    except Exception as e:
        resultado["erros"].append(str(e))
    return jsonify(resultado)


@app.route("/debug/api-crua")
def debug_api_crua():
    data = api_get("/fixtures", params={"per_page": 5, "lang": "pt"})
    return jsonify({"fixtures": data})


@app.route("/debug/odds-crua/<fid>")
def debug_odds_crua(fid):
    resultados = {}
    for mercado in ("goalline", "corner", "goals", "corners", "over_under"):
        resultados[mercado] = api_get(
            f"/fixtures/{fid}/odds",
            params={"market": mercado, "lang": "pt"}
        )
    return jsonify(resultados)


carregar_estado()

_bot_thread = threading.Thread(target=loop_bot, daemon=True, name="loop_bot")
_bot_thread.start()
log.info("Thread do bot iniciada")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
