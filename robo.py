import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify

# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

API_KEY = os.getenv("FIVE_DOLLAR_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "10.00"))

QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))
ASSERTIVIDADE_MINIMA = float(os.getenv("ASSERTIVIDADE_MINIMA", "60"))

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "60"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "120"))

INTERVALO_MONITOR = int(os.getenv("INTERVALO_MONITOR", "30"))

DIAS_HISTORICO = int(os.getenv("DIAS_HISTORICO", "7"))

MONITOR_ATIVO = os.getenv(
    "MONITOR_ATIVO", "true"
).lower() in ("1", "true", "yes", "on")

ARQUIVO_ESTADO = "estado.json"
ARQUIVO_HISTORICO = "historico_api.json"

# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("robo")

# ============================================================
# LOCKS
# ============================================================

estado_lock = threading.Lock()
execucao_lock = threading.Lock()

# ============================================================
# ESTADO
# ============================================================

estado = {
    "historico": [],
    "ultimo_sinal": 0,
    "ultimo_resultado": 0,
    "ultimo_historico": 0,
    "ultimo_run": None,
    "ultima_execucao": None,
    "ultimo_diagnostico": {},
    "ultimo_erro": None,
    "versao": "3.1"
}

if os.path.exists(ARQUIVO_ESTADO):
    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            carregado = json.load(f)
            if isinstance(carregado, dict):
                estado.update(carregado)
    except Exception as exc:
        log.warning("Não foi possível carregar estado: %s", exc)

# ============================================================
# CACHE HISTÓRICO
# ============================================================

historico_cache = {
    "timestamp": 0,
    "linhas": {},
    "jogos_analisados": 0,
    "jogos_com_odds": 0,
    "jogos_sem_odds": 0,
    "periodos": [],
    "erro": None
}

if os.path.exists(ARQUIVO_HISTORICO):
    try:
        with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
            dados = json.load(f)

            if isinstance(dados, dict):
                historico_cache.update(dados)

    except Exception as exc:
        log.warning("Cache histórico inválido: %s", exc)

# ============================================================
# SESSION HTTP
# ============================================================

session = requests.Session()

if API_KEY:
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    })

# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

def agora_ts():
    return int(time.time())


def salvar_estado():
    with estado_lock:
        try:
            with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
                json.dump(estado, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.error("Erro salvando estado: %s", exc)


def salvar_historico():
    try:
        with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
            json.dump(
                historico_cache,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception as exc:
        log.error("Erro salvando histórico: %s", exc)


def api_get(path, params=None, timeout=30):
    if not API_KEY:
        raise RuntimeError("FIVE_DOLLAR_KEY não configurada")

    url = BASE_API + path

    for tentativa in range(4):
        try:
            response = session.get(
                url,
                params=params or {},
                timeout=timeout
            )

            if response.status_code == 429:
                espera = 10 * (tentativa + 1)
                log.warning(
                    "Rate limit da API. Aguardando %ss...",
                    espera
                )
                time.sleep(espera)
                continue

            response.raise_for_status()

            return response.json()

        except requests.RequestException as exc:

            if tentativa >= 3:
                raise

            espera = 2 * (tentativa + 1)

            log.warning(
                "Erro API tentativa %s: %s. "
                "Nova tentativa em %ss.",
                tentativa + 1,
                exc,
                espera
            )

            time.sleep(espera)

    raise RuntimeError("Falha inesperada na API")


def extrair_data(payload):
    if not isinstance(payload, dict):
        return []

    if isinstance(payload.get("data"), list):
        return payload["data"]

    if isinstance(payload.get("fixtures"), dict):
        if isinstance(payload["fixtures"].get("data"), list):
            return payload["fixtures"]["data"]

    if isinstance(payload.get("fixtures"), list):
        return payload["fixtures"]

    if isinstance(payload.get("fixture"), dict):
        return [payload["fixture"]]

    return []


def extrair_pagination(payload):
    if not isinstance(payload, dict):
        return {}

    if isinstance(payload.get("pagination"), dict):
        return payload["pagination"]

    if isinstance(payload.get("fixtures"), dict):
        if isinstance(payload["fixtures"].get("pagination"), dict):
            return payload["fixtures"]["pagination"]

    return {}

# ============================================================
# FIXTURES
# ============================================================

def buscar_fixtures_janela(inicio_ts, fim_ts, status=None):
    """
    Busca uma janela de no máximo 24h.
    Pro permite include=odds.
    """

    params = {
        "start_time": int(inicio_ts),
        "end_time": int(fim_ts),
        "per_page": 50,
        "include": "odds"
    }

    if status:
        params["status"] = status

    resultados = []
    pagina = 1

    while True:

        params["page"] = pagina

        payload = api_get(
            "/fixtures",
            params=params,
            timeout=40
        )

        dados = extrair_data(payload)

        resultados.extend(dados)

        pag = extrair_pagination(payload)

        has_more = bool(pag.get("has_more"))

        if not has_more:
            break

        pagina += 1

        if pagina > 50:
            break

    return resultados


def buscar_proximos():
    agora = agora_ts()

    inicio = agora + int(HORAS_MIN * 3600)
    fim = agora + int(HORAS_MAX * 3600)

    # Como /fixtures aceita no máximo 24h,
    # uma única janela cobre 0,5–12h.
    jogos = buscar_fixtures_janela(
        inicio,
        fim,
        status="scheduled"
    )

    return jogos


def preparar_jogos(jogos):
    agora = agora_ts()

    resultado = []

    for jogo in jogos:

        try:
            fid = int(jogo.get("id"))

            kickoff = jogo.get("kickoff_ts")

            if kickoff is None:
                kickoff_utc = jogo.get("kickoff_utc")

                if kickoff_utc:
                    dt = datetime.fromisoformat(
                        kickoff_utc.replace("Z", "+00:00")
                    )
                    kickoff = dt.timestamp()

            if kickoff is None:
                continue

            horas = (float(kickoff) - agora) / 3600

            if HORAS_MIN <= horas <= HORAS_MAX:

                teams = jogo.get("teams") or {}

                home = (
                    teams.get("home", {}).get("name")
                    or "Casa"
                )

                away = (
                    teams.get("away", {}).get("name")
                    or "Fora"
                )

                resultado.append({
                    "fixture_id": fid,
                    "home": home,
                    "away": away,
                    "kickoff_ts": float(kickoff),
                    "horas_ate_inicio": round(horas, 2),
                    "status": jogo.get("status"),
                    "raw": jogo
                })

        except Exception:
            continue

    resultado.sort(
        key=lambda x: x["kickoff_ts"]
    )

    return resultado

# ============================================================
# ODDS
# ============================================================

def obter_odds_inline(jogo):
    """
    Tenta localizar odds que vieram com include=odds.
    """

    # Formato direto
    odds = jogo.get("odds")

    if isinstance(odds, dict):
        return odds

    # Alguns envelopes podem trazer:
    # corner / goalline diretamente
    if "corner" in jogo or "goalline" in jogo:
        return jogo

    return {}


def procurar_market(odds, nomes):
    if not isinstance(odds, dict):
        return None

    for nome in nomes:

        valor = odds.get(nome)

        if isinstance(valor, dict):
            return valor

    return None


def obter_bookmaker(market):
    if not isinstance(market, dict):
        return None

    data = market.get("data")

    if isinstance(data, dict):

        books = data.get("bookmakers")

        if isinstance(books, list) and books:
            return books[0]

    if isinstance(data, list) and data:
        return data[0]

    books = market.get("bookmakers")

    if isinstance(books, list) and books:
        return books[0]

    return None


def extrair_estagio(estrutura, chave):
    if not isinstance(estrutura, dict):
        return None

    valor = estrutura.get(chave)

    if not isinstance(valor, dict):
        return None

    # Para histórico usamos closing primeiro.
    # Depois inplay/opening como fallback.
    for etapa in ("closing", "inplay", "current", "opening"):

        bloco = valor.get(etapa)

        if isinstance(bloco, dict):

            linha = bloco.get("line")
            over = bloco.get("over")

            if linha is not None and over is not None:

                try:
                    return {
                        "line": float(linha),
                        "over": float(over),
                        "stage": etapa
                    }
                except Exception:
                    pass

    return None


def extrair_linhas_odds(jogo):
    """
    Retorna:
      linha de gols
      odd over gols
      linha de escanteios
      odd over escanteios
    """

    odds = obter_odds_inline(jogo)

    if not odds:
        return None

    goal_market = procurar_market(
        odds,
        ["goalline", "goal_line", "goal"]
    )

    corner_market = procurar_market(
        odds,
        ["corner", "corners", "corner_line"]
    )

    if goal_market is None:
        goal_market = odds.get("goalline")

    if corner_market is None:
        corner_market = odds.get("corner")

    goal_book = obter_bookmaker(goal_market)
    corner_book = obter_bookmaker(corner_market)

    if not goal_book or not corner_book:
        return None

    # Garantir mesmo bookmaker
    goal_slug = goal_book.get("slug")
    corner_slug = corner_book.get("slug")

    if goal_slug and corner_slug:
        if goal_slug != corner_slug:
            return None

    goal_odds = goal_book.get("odds", {})
    corner_odds = corner_book.get("odds", {})

    goal = extrair_estagio(
        goal_odds,
        "goal_line"
    )

    if goal is None:
        goal = extrair_estagio(
            goal_odds,
            "goalline"
        )

    corner = extrair_estagio(
        corner_odds,
        "corner_line"
    )

    if corner is None:
        corner = extrair_estagio(
            corner_odds,
            "corner"
        )

    if not goal or not corner:
        return None

    return {
        "goal_line": goal["line"],
        "goal_over": goal["over"],
        "goal_stage": goal["stage"],
        "corner_line": corner["line"],
        "corner_over": corner["over"],
        "corner_stage": corner["stage"],
        "bookmaker": goal_slug or "bet365"
    }

# ============================================================
# RESULTADOS
# ============================================================

def obter_total_gols(jogo):
    gols = jogo.get("goals")

    if not isinstance(gols, dict):
        return None

    home = gols.get("home")
    away = gols.get("away")

    if home is None or away is None:
        return None

    try:
        return int(home) + int(away)
    except Exception:
        return None


def obter_total_cantos(jogo):
    cantos = jogo.get("corners")

    if not isinstance(cantos, dict):
        return None

    home = cantos.get("home")
    away = cantos.get("away")

    if home is None or away is None:
        return None

    try:
        return int(home) + int(away)
    except Exception:
        return None

# ============================================================
# HISTÓRICO REAL
# ============================================================

def resultado_linha(total, linha):
    """
    Over é vencedor quando o total fica acima da linha.
    """

    if total is None or linha is None:
        return None

    if total > linha:
        return "WIN"

    if total < linha:
        return "LOSS"

    return "PUSH"


def chave_combinacao(goal_line, corner_line):
    return (
        f"Over {goal_line:g} Gols + "
        f"Over {corner_line:g} Escanteios"
    )


def analisar_historico_jogo(jogo):
    odds = extrair_linhas_odds(jogo)

    if not odds:
        return []

    total_gols = obter_total_gols(jogo)
    total_cantos = obter_total_cantos(jogo)

    if total_gols is None or total_cantos is None:
        return []

    goal_line = odds["goal_line"]
    corner_line = odds["corner_line"]

    # Linhas válidas para o nosso mercado
    if not 0.5 <= goal_line <= 6.5:
        return []

    if not 4.5 <= corner_line <= 15.5:
        return []

    resultado_gols = resultado_linha(
        total_gols,
        goal_line
    )

    resultado_cantos = resultado_linha(
        total_cantos,
        corner_line
    )

    if resultado_gols is None or resultado_cantos is None:
        return []

    # PUSH não entra como WIN
    if resultado_gols == "WIN" and resultado_cantos == "WIN":
        resultado = "WIN"

    elif (
        resultado_gols == "PUSH"
        or resultado_cantos == "PUSH"
    ):
        resultado = "PUSH"

    else:
        resultado = "LOSS"

    combinacao = chave_combinacao(
        goal_line,
        corner_line
    )

    odd_combinada = (
        odds["goal_over"] *
        odds["corner_over"]
    )

    return [{
        "fixture_id": jogo.get("id"),
        "data": jogo.get("kickoff_utc"),
        "home": (
            jogo.get("teams", {})
            .get("home", {})
            .get("name")
        ),
        "away": (
            jogo.get("teams", {})
            .get("away", {})
            .get("name")
        ),
        "goal_line": goal_line,
        "corner_line": corner_line,
        "goal_over": odds["goal_over"],
        "corner_over": odds["corner_over"],
        "odd_combinada": round(
            odd_combinada,
            3
        ),
        "gols": total_gols,
        "cantos": total_cantos,
        "resultado": resultado,
        "combinacao": combinacao
    }]

# ============================================================
# DOWNLOAD DOS 7 DIAS
# ============================================================

def buscar_historico_7_dias(forcar=False):

    agora = agora_ts()

    # Cache por 30 minutos
    if (
        not forcar
        and historico_cache.get("timestamp", 0)
        and agora - historico_cache["timestamp"] < 1800
    ):
        return historico_cache

    inicio_total = agora - DIAS_HISTORICO * 86400
    fim_total = agora

    todos = []

    # 7 janelas de 24h
    cursor = inicio_total

    periodos = []

    while cursor < fim_total:

        fim = min(
            cursor + 86400,
            fim_total
        )

        periodos.append({
            "inicio": cursor,
            "fim": fim
        })

        cursor = fim

    log.info(
        "Iniciando histórico real: %s períodos.",
        len(periodos)
    )

    erro = None

    for numero, periodo in enumerate(
        periodos,
        start=1
    ):

        log.info(
            "Histórico %s/%s",
            numero,
            len(periodos)
        )

        try:

            jogos = buscar_fixtures_janela(
                periodo["inicio"],
                periodo["fim"],
                status="finished"
            )

            todos.extend(jogos)

            log.info(
                "Período retornou %s jogos.",
                len(jogos)
            )

        except Exception as exc:

            erro = str(exc)

            log.error(
                "Erro período %s: %s",
                numero,
                exc
            )

        # Proteção contra limite de requisições
        if numero < len(periodos):
            time.sleep(7)

    # Remover duplicados
    unicos = {}

    for jogo in todos:

        try:
            fid = int(jogo["id"])
            unicos[fid] = jogo
        except Exception:
            continue

    linhas = {}

    jogos_analisados = 0
    jogos_com_odds = 0
    jogos_sem_odds = 0

    for jogo in unicos.values():

        jogos_analisados += 1

        odds = extrair_linhas_odds(jogo)

        if not odds:
            jogos_sem_odds += 1
            continue

        jogos_com_odds += 1

        resultados = analisar_historico_jogo(
            jogo
        )

        for item in resultados:

            chave = item["combinacao"]

            if chave not in linhas:
                linhas[chave] = {
                    "jogos": 0,
                    "wins": 0,
                    "losses": 0,
                    "pushes": 0,
                    "assertividade": 0,
                    "odd_media": 0,
                    "amostra": []
                }

            registro = linhas[chave]

            registro["jogos"] += 1

            if item["resultado"] == "WIN":
                registro["wins"] += 1

            elif item["resultado"] == "LOSS":
                registro["losses"] += 1

            else:
                registro["pushes"] += 1

            registro["amostra"].append(item)

    # Calcular assertividade
    for chave, registro in linhas.items():

        resolvidos = (
            registro["wins"]
            + registro["losses"]
        )

        if resolvidos > 0:
            registro["assertividade"] = round(
                registro["wins"]
                / resolvidos
                * 100,
                2
            )

        odds_validas = [
            x["odd_combinada"]
            for x in registro["amostra"]
            if (
                x.get("odd_combinada")
                and ODD_MIN <= x["odd_combinada"] <= ODD_MAX
            )
        ]

        if odds_validas:
            registro["odd_media"] = round(
                sum(odds_validas)
                / len(odds_validas),
                3
            )

    historico_cache.clear()

    historico_cache.update({
        "timestamp": agora,
        "linhas": linhas,
        "jogos_analisados": jogos_analisados,
        "jogos_com_odds": jogos_com_odds,
        "jogos_sem_odds": jogos_sem_odds,
        "periodos": periodos,
        "erro": erro
    })

    salvar_historico()

    estado["ultimo_historico"] = agora
    salvar_estado()

    return historico_cache

# ============================================================
# COMBINAÇÕES APROVADAS
# ============================================================

def obter_combinacoes_aprovadas():

    historico = buscar_historico_7_dias()

    aprovadas = []

    for chave, dados in historico.get(
        "linhas",
        {}
    ).items():

        jogos = int(
            dados.get("jogos", 0)
        )

        assertividade = float(
            dados.get("assertividade", 0)
        )

        if jogos < MINIMO_HISTORICO:
            continue

        if assertividade < ASSERTIVIDADE_MINIMA:
            continue

        # Extrair linhas da chave
        try:

            parte_gols = chave.split(
                " Gols + "
            )[0]

            parte_cantos = chave.split(
                " + Over "
            )[1]

            goal_line = float(
                parte_gols.replace(
                    "Over ",
                    ""
                )
            )

            corner_line = float(
                parte_cantos.replace(
                    " Escanteios",
                    ""
                )
            )

        except Exception:
            continue

        aprovadas.append({
            "combinacao": chave,
            "goal_line": goal_line,
            "corner_line": corner_line,
            "jogos": jogos,
            "wins": dados.get("wins", 0),
            "losses": dados.get("losses", 0),
            "pushes": dados.get("pushes", 0),
            "assertividade": assertividade,
            "odd_media": dados.get(
                "odd_media",
                0
            )
        })

    aprovadas.sort(
        key=lambda x: (
            x["assertividade"],
            x["jogos"]
        ),
        reverse=True
    )

    return aprovadas

# ============================================================
# CRIAÇÃO DO SINAL
# ============================================================

def criar_sinal(jogo):

    odds = extrair_linhas_odds(
        jogo["raw"]
    )

    if not odds:
        return None, "sem_odds"

    goal_line = odds["goal_line"]
    corner_line = odds["corner_line"]

    if not 0.5 <= goal_line <= 6.5:
        return None, "linha_gols_invalida"

    if not 4.5 <= corner_line <= 15.5:
        return None, "linha_cantos_invalida"

    odd_gols = odds["goal_over"]
    odd_cantos = odds["corner_over"]

    if odd_gols is None or odd_cantos is None:
        return None, "odd_invalida"

    odd_combinada = odd_gols * odd_cantos

    if not (
        ODD_MIN
        <= odd_combinada
        <= ODD_MAX
    ):
        return None, "odd_fora_faixa"

    combinacao = chave_combinacao(
        goal_line,
        corner_line
    )

    historico = buscar_historico_7_dias()

    dados = historico.get(
        "linhas",
        {}
    ).get(combinacao)

    if not dados:
        return None, "sem_historico"

    jogos = int(
        dados.get("jogos", 0)
    )

    assertividade = float(
        dados.get("assertividade", 0)
    )

    if jogos < MINIMO_HISTORICO:
        return None, "historico_insuficiente"

    if assertividade < ASSERTIVIDADE_MINIMA:
        return None, "assertividade_baixa"

    sinal = {
        "fixture_id": jogo["fixture_id"],
        "home": jogo["home"],
        "away": jogo["away"],
        "kickoff_ts": jogo["kickoff_ts"],
        "goal_line": goal_line,
        "corner_line": corner_line,
        "odd_gols": odd_gols,
        "odd_cantos": odd_cantos,
        "odd_combinada": round(
            odd_combinada,
            3
        ),
        "combinacao": combinacao,
        "historico_jogos": jogos,
        "historico_wins": dados.get(
            "wins",
            0
        ),
        "historico_losses": dados.get(
            "losses",
            0
        ),
        "assertividade": assertividade,
        "status": "PENDENTE",
        "criado_em": agora_ts()
    }

    return sinal, "aprovado"

# ============================================================
# TELEGRAM
# ============================================================

def telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if not response.ok:

            log.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text
            )

            return False

        payload = response.json()

        if not payload.get("ok"):

            log.error(
                "Telegram rejeitou mensagem: %s",
                payload
            )

            return False

        return True

    except Exception as exc:

        log.error(
            "Erro Telegram: %s",
            exc
        )

        return False


def texto_sinal(sinal):

    horario = datetime.fromtimestamp(
        sinal["kickoff_ts"],
        tz=timezone.utc
    ).strftime("%d/%m %H:%M UTC")

    return (
        "⚽ <b>SINAL — GOLS + ESCANTEIOS</b>\n\n"
        f"🏠 <b>{sinal['home']}</b>\n"
        f"🆚 <b>{sinal['away']}</b>\n"
        f"🕐 {horario}\n\n"
        f"🎯 Over {sinal['goal_line']} Gols\n"
        f"🚩 Over {sinal['corner_line']} Escanteios\n\n"
        f"💰 Odd gols: {sinal['odd_gols']:.2f}\n"
        f"🚩 Odd cantos: {sinal['odd_cantos']:.2f}\n"
        f"🔥 <b>Odd combinada: {sinal['odd_combinada']:.2f}</b>\n\n"
        f"📊 Histórico: {sinal['historico_jogos']} jogos\n"
        f"✅ Wins: {sinal['historico_wins']}\n"
        f"❌ Losses: {sinal['historico_losses']}\n"
        f"📈 <b>Assertividade: {sinal['assertividade']:.2f}%</b>\n\n"
        "🤖 Robô Gols + Escanteios v3.1"
    )


def texto_resultado(sinal, resultado):

    emoji = (
        "✅"
        if resultado == "WIN"
        else "❌"
    )

    return (
        f"{emoji} <b>RESULTADO DO SINAL</b>\n\n"
        f"{sinal['home']} x {sinal['away']}\n"
        f"🎯 {sinal['combinacao']}\n"
        f"📊 Resultado: <b>{resultado}</b>"
    )

# ============================================================
# DUPLICIDADE
# ============================================================

def sinal_ja_enviado(fixture_id):

    for item in estado.get(
        "historico",
        []
    ):

        if item.get("fixture_id") == fixture_id:

            if item.get("status") in (
                "PENDENTE",
                "WIN",
                "LOSS",
                "PUSH"
            ):
                return True

    return False

# ============================================================
# RESOLUÇÃO
# ============================================================

def buscar_fixture(fid):

    try:

        payload = api_get(
            f"/fixtures/{fid}",
            timeout=30
        )

        dados = extrair_data(payload)

        if dados:
            return dados[0]

    except Exception as exc:

        log.error(
            "Erro buscando fixture %s: %s",
            fid,
            exc
        )

    return None


def resolver_sinal(sinal):

    fixture = buscar_fixture(
        sinal["fixture_id"]
    )

    if not fixture:
        return None

    status = fixture.get("status")

    if status not in (
        "finished",
        "FT",
        "AET",
        "PEN"
    ):
        return None

    gols = obter_total_gols(
        fixture
    )

    cantos = obter_total_cantos(
        fixture
    )

    if gols is None or cantos is None:
        return None

    resultado_gols = resultado_linha(
        gols,
        sinal["goal_line"]
    )

    resultado_cantos = resultado_linha(
        cantos,
        sinal["corner_line"]
    )

    if (
        resultado_gols == "WIN"
        and resultado_cantos == "WIN"
    ):
        return "WIN"

    if (
        resultado_gols == "PUSH"
        or resultado_cantos == "PUSH"
    ):
        return "PUSH"

    return "LOSS"

# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

def executar():

    inicio = time.time()

    diagnostico = {
        "jogos_api": 0,
        "na_janela": 0,
        "candidatos": 0,
        "odds_ok": 0,
        "sem_odds": 0,
        "sinais_aprovados": 0,
        "sinais_enviados": 0,
        "filtros": {},
        "erro": None
    }

    try:

        # Atualiza histórico primeiro
        historico = buscar_historico_7_dias()

        aprovadas = obter_combinacoes_aprovadas()

        diagnostico["combinacoes_aprovadas"] = len(
            aprovadas
        )

        jogos_raw = buscar_proximos()

        diagnostico["jogos_api"] = len(
            jogos_raw
        )

        jogos = preparar_jogos(
            jogos_raw
        )

        diagnostico["na_janela"] = len(
            jogos
        )

        sinais = []

        for jogo in jogos:

            diagnostico["candidatos"] += 1

            if sinal_ja_enviado(
                jogo["fixture_id"]
            ):
                diagnostico["filtros"][
                    str(jogo["fixture_id"])
                ] = "ja_enviado"
                continue

            sinal, motivo = criar_sinal(
                jogo
            )

            diagnostico["filtros"][
                str(jogo["fixture_id"])
            ] = motivo

            if motivo == "sem_odds":
                diagnostico["sem_odds"] += 1

            if sinal is None:
                continue

            diagnostico["odds_ok"] += 1

            diagnostico["sinais_aprovados"] += 1

            sinais.append(sinal)

        # Ordenar pela maior assertividade
        sinais.sort(
            key=lambda x: (
                x["assertividade"],
                x["historico_jogos"],
                x["odd_combinada"]
            ),
            reverse=True
        )

        sinais = sinais[
            :QTD_POR_RODADA
        ]

        for sinal in sinais:

            texto = texto_sinal(
                sinal
            )

            enviado = telegram(
                texto
            )

            if enviado:

                diagnostico[
                    "sinais_enviados"
                ] += 1

                estado.setdefault(
                    "historico",
                    []
                ).append(sinal)

                estado["ultimo_sinal"] = agora_ts()

                salvar_estado()

                log.info(
                    "Sinal enviado: %s x %s",
                    sinal["home"],
                    sinal["away"]
                )

        # Resolver sinais antigos
        resolver_resultados()

        estado["ultimo_run"] = agora_ts()

        estado[
            "ultima_execucao"
        ] = datetime.now(
            timezone.utc
        ).isoformat()

        estado[
            "ultimo_diagnostico"
        ] = diagnostico

        estado["ultimo_erro"] = None

        salvar_estado()

    except Exception as exc:

        diagnostico["erro"] = str(exc)

        estado["ultimo_erro"] = str(exc)

        log.exception(
            "Erro na execução."
        )

        salvar_estado()

    diagnostico[
        "duracao_segundos"
    ] = round(
        time.time() - inicio,
        2
    )

    return diagnostico

# ============================================================
# RESULTADOS
# ============================================================

def resolver_resultados():

    alterou = False

    historico = estado.get(
        "historico",
        []
    )

    for sinal in historico:

        if sinal.get("status") != "PENDENTE":
            continue

        # Só verificar periodicamente
        try:

            resultado = resolver_sinal(
                sinal
            )

        except Exception as exc:

            log.error(
                "Erro resolvendo %s: %s",
                sinal.get("fixture_id"),
                exc
            )

            continue

        if resultado is None:
            continue

        sinal["status"] = resultado

        sinal["resultado_em"] = agora_ts()

        telegram(
            texto_resultado(
                sinal,
                resultado
            )
        )

        alterou = True

        log.info(
            "Sinal %s resolvido: %s",
            sinal.get("fixture_id"),
            resultado
        )

    if alterou:

        estado[
            "ultimo_resultado"
        ] = agora_ts()

        salvar_estado()

# ============================================================
# BACKGROUND
# ============================================================

def executar_background():

    if not execucao_lock.acquire(
        blocking=False
    ):
        return False

    try:

        executar()

    finally:

        execucao_lock.release()

    return True


def monitor_loop():

    log.info(
        "Monitor iniciado."
    )

    while True:

        try:

            if MONITOR_ATIVO:

                executar_background()

        except Exception:

            log.exception(
                "Erro no monitor."
            )

        time.sleep(
            INTERVALO_MONITOR
        )

# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    historico = estado.get(
        "historico",
        []
    )

    wins = sum(
        1
        for x in historico
        if x.get("status") == "WIN"
    )

    losses = sum(
        1
        for x in historico
        if x.get("status") == "LOSS"
    )

    total_resolvidos = wins + losses

    assertividade = (
        round(
            wins
            / total_resolvidos
            * 100,
            2
        )
        if total_resolvidos
        else 0
    )

    return jsonify({
        "status": "online",
        "bot": "Gols + Escanteios",
        "versao": "3.1",
        "plano": "Pro",
        "historico_externo_dias": DIAS_HISTORICO,
        "historico_minimo": MINIMO_HISTORICO,
        "assertividade_minima": ASSERTIVIDADE_MINIMA,
        "odd_min": ODD_MIN,
        "odd_max": ODD_MAX,
        "janela_horas": [
            HORAS_MIN,
            HORAS_MAX
        ],
        "max_sinais": QTD_POR_RODADA,
        "monitor": MONITOR_ATIVO,
        "sinais_pendentes": sum(
            1
            for x in historico
            if x.get("status") == "PENDENTE"
        ),
        "wins": wins,
        "losses": losses,
        "assertividade_real": assertividade
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "api_configurada": bool(API_KEY),
        "telegram_configurado": bool(
            TELEGRAM_TOKEN and CHAT_ID
        ),
        "versao": "3.1"
    })


@app.route("/status")
def status():

    return jsonify({
        "estado": estado,
        "historico_cache": {
            "timestamp": historico_cache.get(
                "timestamp"
            ),
            "jogos_analisados":
                historico_cache.get(
                    "jogos_analisados",
                    0
                ),
            "jogos_com_odds":
                historico_cache.get(
                    "jogos_com_odds",
                    0
                ),
            "jogos_sem_odds":
                historico_cache.get(
                    "jogos_sem_odds",
                    0
                ),
            "linhas":
                len(
                    historico_cache.get(
                        "linhas",
                        {}
                    )
                ),
            "erro":
                historico_cache.get(
                    "erro"
                )
        }
    })


@app.route("/debug/proximos")
def debug_proximos():

    try:

        jogos_raw = buscar_proximos()

        jogos = preparar_jogos(
            jogos_raw
        )

        agora = agora_ts()

        ao_vivo = 0
        finalizados = 0
        fora_janela = 0
        sem_id = 0
        sem_horario = 0

        for jogo in jogos_raw:

            if not jogo.get("id"):
                sem_id += 1

            status_jogo = jogo.get("status")

            if status_jogo == "live":
                ao_vivo += 1

            if status_jogo == "finished":
                finalizados += 1

        # Diagnóstico dos retornados
        for jogo in jogos_raw:

            kickoff = jogo.get(
                "kickoff_ts"
            )

            if kickoff is None:
                sem_horario += 1
                continue

            horas = (
                float(kickoff) - agora
            ) / 3600

            if not (
                HORAS_MIN
                <= horas
                <= HORAS_MAX
            ):
                fora_janela += 1

        return jsonify({
            "diagnostico": {
                "jogos_api": len(jogos_raw),
                "na_janela": len(jogos),
                "ao_vivo": ao_vivo,
                "finalizados": finalizados,
                "fora_janela": fora_janela,
                "sem_horario": sem_horario,
                "sem_id": sem_id,
                "erro": None
            },
            "jogos": [
                {
                    "fixture_id":
                        x["fixture_id"],
                    "home":
                        x["home"],
                    "away":
                        x["away"],
                    "horas_ate_inicio":
                        x["horas_ate_inicio"],
                    "kickoff_ts":
                        x["kickoff_ts"],
                    "status":
                        x["status"]
                }
                for x in jogos
            ]
        })

    except Exception as exc:

        return jsonify({
            "diagnostico": {
                "erro": str(exc)
            }
        }), 500


@app.route("/debug/historico")
def debug_historico():

    forcar = (
        request_bool(
            "forcar"
        )
    )

    try:

        historico = buscar_historico_7_dias(
            forcar=forcar
        )

        aprovadas = (
            obter_combinacoes_aprovadas()
        )

        return jsonify({
            "dias": DIAS_HISTORICO,
            "jogos_analisados":
                historico.get(
                    "jogos_analisados",
                    0
                ),
            "jogos_com_odds":
                historico.get(
                    "jogos_com_odds",
                    0
                ),
            "jogos_sem_odds":
                historico.get(
                    "jogos_sem_odds",
                    0
                ),
            "total_linhas":
                len(
                    historico.get(
                        "linhas",
                        {}
                    )
                ),
            "linhas":
                historico.get(
                    "linhas",
                    {}
                ),
            "combinacoes_aprovadas":
                aprovadas,
            "erro":
                historico.get(
                    "erro"
                )
        })

    except Exception as exc:

        return jsonify({
            "erro": str(exc)
        }), 500


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    iniciou = executar_background()

    return jsonify({
        "status":
            "execucao_iniciada"
            if iniciou
            else "ja_em_execucao",
        "mensagem":
            "Acompanhe em /debug/ultimo-run"
    })


@app.route("/debug/ultimo-run")
def debug_ultimo_run():

    return jsonify({
        "ultimo_run":
            estado.get(
                "ultimo_run"
            ),
        "ultima_execucao":
            estado.get(
                "ultima_execucao"
            ),
        "diagnostico":
            estado.get(
                "ultimo_diagnostico",
                {}
            ),
        "erro":
            estado.get(
                "ultimo_erro"
            )
    })


@app.route("/debug/historico-detalhado")
def debug_historico_detalhado():

    return jsonify(
        historico_cache
    )


@app.route("/debug/teste-telegram")
def debug_telegram():

    ok = telegram(
        "🤖 <b>Teste Telegram OK</b>\n"
        "Robô Gols + Escanteios v3.1"
    )

    return jsonify({
        "telegram": ok
    })


@app.route("/debug/sinal/<int:fixture_id>")
def debug_sinal(fixture_id):

    try:

        jogos = buscar_fixtures_janela(
            agora_ts(),
            agora_ts() + 86400
        )

        encontrado = None

        for jogo in jogos:

            if int(
                jogo.get("id", 0)
            ) == fixture_id:

                encontrado = jogo
                break

        if not encontrado:

            return jsonify({
                "erro":
                    "Fixture não encontrado"
            }), 404

        preparado = preparar_jogos(
            [encontrado]
        )

        if not preparado:

            return jsonify({
                "erro":
                    "Fixture fora da janela"
            })

        sinal, motivo = criar_sinal(
            preparado[0]
        )

        return jsonify({
            "motivo": motivo,
            "sinal": sinal
        })

    except Exception as exc:

        return jsonify({
            "erro": str(exc)
        }), 500


@app.route("/debug/odds-crua/<int:fixture_id>")
def debug_odds_crua(fixture_id):

    try:

        payload = api_get(
            f"/fixtures/{fixture_id}/odds",
            params={
                "bookmakers": "bet365"
            },
            timeout=30
        )

        return jsonify(
            payload
        )

    except Exception as exc:

        return jsonify({
            "erro": str(exc)
        }), 500


@app.route("/debug/procurar-odds/<int:fixture_id>")
def debug_procurar_odds(fixture_id):

    try:

        payload = api_get(
            f"/fixtures/{fixture_id}/odds",
            params={
                "bookmakers": "bet365"
            },
            timeout=30
        )

        resultado = {
            "fixture_id": fixture_id,
            "gols": [],
            "cantos": []
        }

        data = payload.get("data")

        if not isinstance(data, dict):
            data = {}

        # Retornar as partes relevantes
        for chave in (
            "goalline",
            "goal_line",
            "corner",
            "corner_line"
        ):

            if chave in data:
                resultado[
                    "gols"
                    if "goal" in chave
                    else "cantos"
                ].append({
                    chave:
                        data[chave]
                })

        # Compatibilidade com envelope antigo
        for chave in (
            "goalline",
            "corner"
        ):

            if chave in payload:

                resultado[
                    "gols"
                    if chave == "goalline"
                    else "cantos"
                ].append(
                    payload[chave]
                )

        return jsonify(
            resultado
        )

    except Exception as exc:

        return jsonify({
            "erro": str(exc)
        }), 500


@app.route("/debug/combinacoes")
def debug_combinacoes():

    try:

        return jsonify({
            "combinacoes":
                obter_combinacoes_aprovadas()
        })

    except Exception as exc:

        return jsonify({
            "erro": str(exc)
        }), 500


# ============================================================
# HELPER QUERY
# ============================================================

def request_bool(nome):

    # Import local para evitar qualquer
    # conflito no carregamento inicial.
    from flask import request

    valor = request.args.get(
        nome,
        "false"
    )

    return valor.lower() in (
        "1",
        "true",
        "yes",
        "on"
    )


# ============================================================
# START
# ============================================================

def iniciar_monitor():

    if not MONITOR_ATIVO:
        return

    thread = threading.Thread(
        target=monitor_loop,
        daemon=True,
        name="monitor"
    )

    thread.start()


iniciar_monitor()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
        )
