
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
from flask import Flask, jsonify, request


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

log = logging.getLogger("robo")


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Filtros de odd
ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))

# Quantidade máxima de sinais por rodada
QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

# Janela pré-jogo
HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

# Intervalos
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "60"))
INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "120")
)

# Histórico
MINIMO_HISTORICO = int(
    os.getenv("MINIMO_HISTORICO", "5")
)

ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "60")
)

ARQUIVO_ESTADO = os.getenv(
    "ARQUIVO_ESTADO",
    "bot_state.json"
)

# Tempo máximo para manter uma entrada pendente
HORAS_EXPIRACAO = float(
    os.getenv("HORAS_EXPIRACAO", "6")
)


# ============================================================
# SESSÃO HTTP
# ============================================================

def criar_session():

    s = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[
            429, 500, 502, 503, 504
        ],
        allowed_methods=["GET", "POST"]
    )

    adapter = HTTPAdapter(
        max_retries=retry
    )

    s.mount("https://", adapter)
    s.mount("http://", adapter)

    return s


SESSION = criar_session()


# ============================================================
# ESTADO
# ============================================================

lock = threading.RLock()

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


_ultimo_run = {
    "pre": 0,
    "resultado": 0,
    "iniciado_em": time.time()
}


# ============================================================
# ESTADO / ARQUIVO
# ============================================================

def salvar_estado():

    try:

        with lock:

            with open(
                ARQUIVO_ESTADO,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    estado,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

    except Exception as e:

        log.error(
            f"Erro ao salvar estado: {e}"
        )


def carregar_estado():

    global estado

    if not os.path.exists(ARQUIVO_ESTADO):
        return

    try:

        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as f:

            dados = json.load(f)

        if not isinstance(dados, dict):
            return

        if isinstance(dados.get("stats"), dict):

            estado["stats"].update(
                dados["stats"]
            )

        if isinstance(dados.get("pendentes"), dict):

            estado["pendentes"] = (
                dados["pendentes"]
            )

        if isinstance(dados.get("historico"), list):

            estado["historico"] = (
                dados["historico"]
            )

        log.info(
            "Estado carregado com sucesso."
        )

    except Exception as e:

        log.error(
            f"Erro ao carregar estado: {e}"
        )


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        log.warning(
            "Telegram não configurado."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        r = SESSION.post(
            url,
            json=payload,
            timeout=20
        )

        if r.ok:

            log.info(
                "Mensagem enviada ao Telegram."
            )

            return True

        log.error(
            f"Erro Telegram: "
            f"{r.status_code} "
            f"{r.text[:500]}"
        )

    except Exception as e:

        log.error(
            f"Erro ao enviar Telegram: {e}"
        )

    return False


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        log.error(
            "FIVE_DOLLAR_KEY não configurada."
        )

        return None

    url = f"{BASE_API}{endpoint}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }

    try:

        r = SESSION.get(
            url,
            headers=headers,
            params=params,
            timeout=25
        )

        if r.status_code != 200:

            log.error(
                f"API {endpoint} -> "
                f"{r.status_code}: "
                f"{r.text[:500]}"
            )

            return None

        return r.json()

    except Exception as e:

        log.error(
            f"Erro API {endpoint}: {e}"
        )

        return None


# ============================================================
# EXTRAÇÃO DE DADOS
# ============================================================

def extrair_lista(data):

    if not data:
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for chave in (
            "data",
            "fixtures",
            "results",
            "matches",
            "response"
        ):

            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def extrair_id(jogo):

    if not isinstance(jogo, dict):
        return None

    for chave in (
        "id",
        "fixture_id",
        "fixtureId",
        "match_id",
        "event_id"
    ):

        v = jogo.get(chave)

        if v is not None:
            return str(v)

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for chave in (
            "id",
            "fixture_id"
        ):

            v = fixture.get(chave)

            if v is not None:
                return str(v)

    return None


def extrair_times(jogo):

    home = "Casa"
    away = "Fora"

    if not isinstance(jogo, dict):
        return home, away

    # Formato padrão
    teams = jogo.get("teams")

    if isinstance(teams, dict):

        h = teams.get("home")
        a = teams.get("away")

        if isinstance(h, dict):

            home = (
                h.get("name")
                or h.get("team_name")
                or home
            )

        if isinstance(a, dict):

            away = (
                a.get("name")
                or a.get("team_name")
                or away
            )

    # Formato português da API
    times = jogo.get("times")

    if isinstance(times, dict):

        casa = times.get("casa")
        fora = times.get("fora")

        if isinstance(casa, dict):

            home = (
                casa.get("name")
                or casa.get("team_name")
                or home
            )

        if isinstance(fora, dict):

            away = (
                fora.get("name")
                or fora.get("team_name")
                or away
            )

    return str(home), str(away)


def extrair_placar(jogo):

    if not isinstance(jogo, dict):
        return 0, 0

    goals = jogo.get("goals")

    if isinstance(goals, dict):

        try:

            home = goals.get(
                "home",
                goals.get("casa", 0)
            )

            away = goals.get(
                "away",
                goals.get("fora", 0)
            )

            return (
                int(home or 0),
                int(away or 0)
            )

        except Exception:
            pass

    return 0, 0


def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return 0

    corners = jogo.get("corners")

    if isinstance(corners, dict):

        try:

            home = corners.get(
                "home",
                corners.get("casa", 0)
            )

            away = corners.get(
                "away",
                corners.get("fora", 0)
            )

            return (
                int(home or 0)
                + int(away or 0)
            )

        except Exception:
            pass

    # Alguns retornos podem usar total
    try:

        if jogo.get("corner_total") is not None:

            return int(
                jogo["corner_total"]
            )

    except Exception:
        pass

    return 0


def extrair_status(jogo):

    if not isinstance(jogo, dict):
        return ""

    status = jogo.get("status")

    if isinstance(status, dict):

        return str(
            status.get("short")
            or status.get("long")
            or status.get("status")
            or ""
        ).upper()

    return str(status or "").upper()


def eh_finalizado(jogo):

    status = extrair_status(jogo).lower()

    return any(
        f in status
        for f in (
            "ft",
            "aet",
            "pen",
            "finished",
            "finalizado",
            "ended",
            "match finished"
        )
    )


def eh_em_andamento(jogo):

    status = extrair_status(jogo).lower()

    return any(
        f in status
        for f in (
            "in_play",
            "live",
            "1h",
            "2h",
            "ht",
            "half",
            "playing",
            "inprogress"
        )
    )


def extrair_inicio_timestamp(jogo):

    if not isinstance(jogo, dict):
        return None

    valores = [
        jogo.get("kickoff_ts"),
        jogo.get("kickoff_utc"),
        jogo.get("start_time"),
        jogo.get("date"),
        jogo.get("datetime")
    ]

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        valores.extend([
            fixture.get("date"),
            fixture.get("start_time")
        ])

    for valor in valores:

        if not valor:
            continue

        if isinstance(valor, (int, float)):

            v = float(valor)

            if v > 1e12:
                v /= 1000

            return v

        texto = str(valor).strip()

        try:

            if texto.isdigit():

                v = float(texto)

                if v > 1e12:
                    v /= 1000

                return v

        except Exception:
            pass

        try:

            t = texto.replace(
                "Z",
                "+00:00"
            )

            dt = datetime.fromisoformat(t)

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.timestamp()

        except Exception:
            pass

    return None


def minutos_ate_jogo(jogo):

    ts = extrair_inicio_timestamp(jogo)

    if ts is None:
        return None

    return (
        ts
        - datetime.now(
            timezone.utc
        ).timestamp()
    ) / 60


def dentro_da_janela(jogo):

    minutos = minutos_ate_jogo(jogo)

    if minutos is None:
        return False

    return (
        HORAS_MIN * 60
        <= minutos
        <= HORAS_MAX * 60
    )


# ============================================================
# ODDS — CORREÇÃO PRINCIPAL
# ============================================================

def extrair_mercado_bookmakers(data, mercado):

    resultado = []

    if not isinstance(data, dict):
        return resultado

    # A API pode retornar:
    #
    # {"data": {"bookmakers": [...]}}
    #
    # ou diretamente:
    #
    # {"bookmakers": [...]}
    #
    # ou o debug pode trazer:
    #
    # {"corner": {"data": {...}}}

    raiz = data

    if mercado in data and isinstance(
        data.get(mercado),
        dict
    ):

        raiz = data[mercado]

    if isinstance(raiz, dict):

        raiz = raiz.get(
            "data",
            raiz
        )

    if not isinstance(raiz, dict):
        return resultado

    bookmakers = raiz.get(
        "bookmakers",
        []
    )

    if not isinstance(bookmakers, list):
        return resultado

    for bookmaker in bookmakers:

        if not isinstance(bookmaker, dict):
            continue

        nome_bookmaker = (
            bookmaker.get("name")
            or bookmaker.get("bookmaker")
            or "Bookmaker"
        )

        odds = bookmaker.get(
            "odds",
            {}
        )

        if not isinstance(odds, dict):
            continue

        if mercado == "gols":

            nomes = (
                "goal_line",
                "goalline",
                "goals"
            )

        else:

            nomes = (
                "corner_line",
                "corners",
                "corner"
            )

        for nome in nomes:

            bloco = odds.get(nome)

            if isinstance(bloco, dict):

                resultado.append({
                    "bookmaker": nome_bookmaker,
                    "bloco": bloco
                })

    return resultado


def extrair_preco_do_bloco(bloco):

    if not isinstance(bloco, dict):
        return None, None

    # Preferência:
    # closing -> inplay -> opening -> current

    fontes = [
        bloco.get("closing"),
        bloco.get("inplay"),
        bloco.get("opening"),
        bloco.get("current"),
        bloco
    ]

    for fonte in fontes:

        if not isinstance(fonte, dict):
            continue

        linha = fonte.get("line")

        over = (
            fonte.get("over")
            or fonte.get("price")
            or fonte.get("value")
        )

        try:

            if linha is not None:
                linha = float(linha)

            if over is not None:
                over = float(over)

            if (
                linha is not None
                and over is not None
            ):

                return linha, over

        except (
            TypeError,
            ValueError
        ):

            pass

    return None, None


def obter_melhor_odd(fixture_id, mercado):

    endpoint_mercado = (
        "goalline"
        if mercado == "gols"
        else "corner"
    )

    data = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "market": endpoint_mercado,
            "lang": "pt"
        }
    )

    if not data:
        return None

    blocos = extrair_mercado_bookmakers(
        data,
        mercado
    )

    candidatos = []

    for item in blocos:

        linha, odd = extrair_preco_do_bloco(
            item["bloco"]
        )

        if linha is None or odd is None:
            continue

        if not (
            ODD_MIN
            <= odd
            <= ODD_MAX
        ):

            continue

        candidatos.append({
            "bookmaker": item["bookmaker"],
            "linha": linha,
            "odd": odd
        })

    if not candidatos:
        return None

    # Melhor odd dentro dos limites
    return max(
        candidatos,
        key=lambda x: x["odd"]
    )


# ============================================================
# LINHAS VÁLIDAS
# ============================================================

def linha_gols_valida(linha):

    return linha in (
        0.5,
        1.0,
        1.5,
        2.0,
        2.5,
        3.0,
        3.5,
        4.0
    )


def linha_cantos_valida(linha):

    return linha in (
        6.0,
        6.5,
        7.0,
        7.5,
        8.0,
        8.5,
        9.0,
        9.5,
        10.0,
        10.5,
        11.0,
        11.5,
        12.0
    )


def filtrar_gols(odd_info):

    return (
        bool(odd_info)
        and linha_gols_valida(
            odd_info["linha"]
        )
    )


def filtrar_cantos(odd_info):

    return (
        bool(odd_info)
        and linha_cantos_valida(
            odd_info["linha"]
        )
    )


# ============================================================
# RESOLUÇÃO
# ============================================================

def resolver_linha(total, linha):

    total = float(total)
    linha = float(linha)

    # Linha fracionada
    if linha % 1 != 0:

        return (
            "WIN"
            if total > linha
            else "LOSS"
        )

    # Linha inteira
    if total > linha:
        return "WIN"

    if total == linha:
        return "PUSH"

    return "LOSS"


def resolver_combinado(rg, rc):

    if "LOSS" in {
        rg,
        rc
    }:

        return "LOSS"

    if (
        rg == "WIN"
        and rc == "WIN"
    ):

        return "WIN"

    return "PUSH"


# ============================================================
# ESTATÍSTICAS
# ============================================================

def calcular_assertividade(wins, losses):

    total = wins + losses

    if total <= 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def estatisticas_combinadas():

    s = estado["stats"]

    return {
        "wins": s["combinados_wins"],
        "losses": s["combinados_losses"],
        "push": s["combinados_push"],

        "assertividade": calcular_assertividade(
            s["combinados_wins"],
            s["combinados_losses"]
        )
    }


def obter_historico_7_dias():

    limite = (
        time.time()
        - 7 * 24 * 60 * 60
    )

    return [
        x
        for x in estado.get(
            "historico",
            []
        )
        if float(
            x.get("timestamp", 0)
        ) >= limite
    ]


def assertividade_7_dias():

    h = obter_historico_7_dias()

    wins = sum(
        1
        for x in h
        if x.get("resultado") == "WIN"
    )

    losses = sum(
        1
        for x in h
        if x.get("resultado") == "LOSS"
    )

    pushes = sum(
        1
        for x in h
        if x.get("resultado") == "PUSH"
    )

    return {
        "sinais": len(h),
        "wins": wins,
        "losses": losses,
        "push": pushes,

        "assertividade": calcular_assertividade(
            wins,
            losses
        )
    }


# ============================================================
# BUSCAR JOGOS COM PAGINAÇÃO
# ============================================================

def buscar_todas_paginas(params_base):

    todos = []

    page = 1

    max_paginas = 20

    while page <= max_paginas:

        params = dict(params_base)

        params["page"] = page

        data = api_get(
            "/fixtures",
            params=params
        )

        jogos = extrair_lista(data)

        if not jogos:
            break

        todos.extend(jogos)

        pag = {}

        if isinstance(data, dict):

            pag = data.get(
                "pagination",
                {}
            )

        has_more = False

        if isinstance(pag, dict):

            has_more = bool(
                pag.get("has_more", False)
            )

        log.info(
            f"Página {page}: "
            f"{len(jogos)} jogos"
        )

        if not has_more:
            break

        page += 1

    return todos


def buscar_pre():

    hoje = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    amanha = (
        datetime.now(timezone.utc)
        + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    jogos = []

    # Hoje
    jogos.extend(
        buscar_todas_paginas({
            "date": hoje,
            "per_page": 100,
            "lang": "pt"
        })
    )

    # Amanhã
    jogos.extend(
        buscar_todas_paginas({
            "date": amanha,
            "per_page": 100,
            "lang": "pt"
        })
    )

    # Remover duplicados
    unicos = {}

    for jogo in jogos:

        fid = extrair_id(jogo)

        if fid:
            unicos[fid] = jogo

    jogos = list(
        unicos.values()
    )

    log.info(
        f"buscar_pre: "
        f"{len(jogos)} jogos encontrados"
    )

    candidatos = []

    total_futuros = 0
    total_in_play = 0
    total_finalizados = 0

    for jogo in jogos:

        fid = extrair_id(jogo)

        minutos = minutos_ate_jogo(
            jogo
        )

        status = extrair_status(
            jogo
        )

        if fid is None or minutos is None:
            continue

        if eh_em_andamento(jogo):

            total_in_play += 1

            continue

        if eh_finalizado(jogo):

            total_finalizados += 1

            continue

        if minutos < 0:
            continue

        total_futuros += 1

        if (
            HORAS_MIN * 60
            <= minutos
            <= HORAS_MAX * 60
        ):

            candidatos.append(jogo)

            log.info(
                f"[{fid}] "
                f"{minutos:.0f}min | "
                f"status={status}"
            )

    log.info(
        f"buscar_pre: "
        f"{total_futuros} futuros, "
        f"{total_in_play} em andamento, "
        f"{total_finalizados} finalizados | "
        f"{len(candidatos)} candidatos"
    )

    return candidatos


# ============================================================
# CRIAR SINAL COMBINADO
# ============================================================

def criar_sinal_combinado(jogo):

    fid = extrair_id(jogo)

    if not fid:
        return None

    home, away = extrair_times(
        jogo
    )

    gols = obter_melhor_odd(
        fid,
        "gols"
    )

    cantos = obter_melhor_odd(
        fid,
        "cantos"
    )

    if not gols:

        log.info(
            f"[{fid}] "
            f"sem odds de gols"
        )

        return None

    if not cantos:

        log.info(
            f"[{fid}] "
            f"sem odds de cantos"
        )

        return None

    if not filtrar_gols(gols):

        log.info(
            f"[{fid}] "
            f"linha gols inválida: "
            f"{gols['linha']}"
        )

        return None

    if not filtrar_cantos(cantos):

        log.info(
            f"[{fid}] "
            f"linha cantos inválida: "
            f"{cantos['linha']}"
        )

        return None

    # Exige mesma bookmaker
    if (
        gols["bookmaker"].lower()
        != cantos["bookmaker"].lower()
    ):

        log.info(
            f"[{fid}] "
            f"bookmakers diferentes"
        )

        return None

    odd_combinada = (
        gols["odd"]
        * cantos["odd"]
    )

    if not (
        ODD_MIN
        <= odd_combinada
        <= ODD_MAX
    ):

        log.info(
            f"[{fid}] "
            f"odd combinada fora: "
            f"{odd_combinada:.2f}"
        )

        return None

    historico = assertividade_7_dias()

    # Filtro histórico global
    if (
        historico["sinais"]
        >= MINIMO_HISTORICO
        and historico["assertividade"]
        < ASSERTIVIDADE_MINIMA
    ):

        log.info(
            f"[{fid}] "
            f"filtrado por assertividade"
        )

        return None

    return {

        "id": fid,

        "home": home,
        "away": away,

        "bookmaker": gols["bookmaker"],

        "gols": {
            "linha": gols["linha"],
            "odd": gols["odd"]
        },

        "cantos": {
            "linha": cantos["linha"],
            "odd": cantos["odd"]
        },

        "odd_combinada": round(
            odd_combinada,
            2
        ),

        "assertividade_7d": (
            historico["assertividade"]
        ),

        "historico_7d": (
            historico["sinais"]
        ),

        "timestamp": time.time(),

        "resultado": None
    }


# ============================================================
# MENSAGENS
# ============================================================

def mensagem_sinal(s):

    return (

        "🔥 <b>SINAL COMBINADO</b>\n\n"

        f"⚽ <b>{html.escape(s['home'])}</b> x "
        f"<b>{html.escape(s['away'])}</b>\n\n"

        "🎯 <b>ENTRADA</b>\n"

        f"⚽ Over {s['gols']['linha']} "
        f"Gols @ {s['gols']['odd']:.2f}\n"

        f"🚩 Over {s['cantos']['linha']} "
        f"Escanteios @ {s['cantos']['odd']:.2f}\n\n"

        f"💰 <b>Odd combinada:</b> "
        f"{s['odd_combinada']:.2f}\n"

        f"🏦 <b>Casa:</b> "
        f"{html.escape(s['bookmaker'])}\n\n"

        f"📊 <b>Histórico 7 dias:</b> "
        f"{s['historico_7d']} sinais\n"

        f"📈 <b>Assertividade:</b> "
        f"{s['assertividade_7d']:.2f}%\n\n"

        "⚠️ Assertividade baseada no "
        "histórico do robô.\n"

        "⚠️ Sinal estatístico, sem garantia "
        "de resultado."
    )


# ============================================================
# ANALISAR PRÉ-JOGO
# ============================================================

def analisar_pre(jogo):

    fid = extrair_id(jogo)

    if not fid:
        return False

    chave = f"{fid}_COMBINADO"

    with lock:

        if chave in estado["pendentes"]:
            return False

        if any(
            h.get("id") == fid
            for h in estado.get(
                "historico",
                []
            )[-2000:]
        ):

            return False

    if not dentro_da_janela(jogo):
        return False

    if (
        eh_em_andamento(jogo)
        or eh_finalizado(jogo)
    ):

        return False

    sinal = criar_sinal_combinado(
        jogo
    )

    if not sinal:
        return False

    # Primeiro envia, depois registra
    if not enviar_telegram(
        mensagem_sinal(sinal)
    ):

        log.error(
            f"[{fid}] "
            f"Telegram falhou"
        )

        return False

    with lock:

        estado["pendentes"][chave] = sinal

        estado["stats"]["total_sinais"] += 1

    salvar_estado()

    log.info(
        f"[SINAL] "
        f"{sinal['home']} x "
        f"{sinal['away']} @ "
        f"{sinal['odd_combinada']}"
    )

    return True


# ============================================================
# BUSCAR JOGO POR ID
# ============================================================

def buscar_jogo_por_id(fid):

    data = api_get(
        f"/fixtures/{fid}",
        params={
            "lang": "pt"
        }
    )

    if not data:
        return None

    if isinstance(data, dict):

        for chave in (
            "data",
            "fixture"
        ):

            v = data.get(chave)

            if isinstance(v, dict):
                return v

            if (
                isinstance(v, list)
                and v
            ):

                return v[0]

    return data


# ============================================================
# FINALIZAR SINAL
# ============================================================

def finalizar(chave, sinal, jogo):

    gc, gf = extrair_placar(
        jogo
    )

    tg = gc + gf

    tc = extrair_cantos(
        jogo
    )

    rg = resolver_linha(
        tg,
        sinal["gols"]["linha"]
    )

    rc = resolver_linha(
        tc,
        sinal["cantos"]["linha"]
    )

    resultado = resolver_combinado(
        rg,
        rc
    )

    sinal.update({

        "resultado": resultado,

        "resultado_gols": rg,

        "resultado_cantos": rc,

        "placar_final": f"{gc} x {gf}",

        "total_gols": tg,

        "total_cantos": tc

    })

    with lock:

        s = estado["stats"]

        s[
            {
                "WIN": "combinados_wins",
                "LOSS": "combinados_losses"
            }.get(
                resultado,
                "combinados_push"
            )
        ] += 1

        s[
            {
                "WIN": "gols_wins",
                "LOSS": "gols_losses"
            }.get(
                rg,
                "gols_push"
            )
        ] += 1

        s[
            {
                "WIN": "cantos_wins",
                "LOSS": "cantos_losses"
            }.get(
                rc,
                "cantos_push"
            )
        ] += 1

        hist = dict(sinal)

        hist["timestamp_resultado"] = (
            time.time()
        )

        estado["historico"].append(
            hist
        )

        # Mantém histórico de 30 dias
        limite = (
            time.time()
            - 30 * 24 * 60 * 60
        )

        estado["historico"] = [
            x
            for x in estado["historico"]
            if float(
                x.get("timestamp", 0)
            ) >= limite
        ]

        estado["pendentes"].pop(
            chave,
            None
        )

    salvar_estado()

    ass = estatisticas_combinadas()

    mensagem = (

        "🏁 <b>RESULTADO DO SINAL</b>\n\n"

        f"⚽ <b>{html.escape(sinal['home'])}</b> x "
        f"<b>{html.escape(sinal['away'])}</b>\n\n"

        f"📊 Resultado: <b>{resultado}</b>\n"

        f"⚽ Gols: {rg}\n"

        f"🚩 Escanteios: {rc}\n\n"

        f"🔢 Placar: {gc} x {gf}\n"

        f"🚩 Total escanteios: {tc}\n\n"

        f"📈 Assertividade geral: "
        f"{ass['assertividade']:.2f}%"
    )

    enviar_telegram(
        mensagem
    )

    log.info(
        f"[FIM] "
        f"{sinal['home']} x "
        f"{sinal['away']} -> "
        f"{resultado}"
    )


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    with lock:

        pendentes = list(
            estado["pendentes"].items()
        )

    agora = time.time()

    expirados = []

    for chave, sinal in pendentes:

        if (
            agora
            - sinal.get(
                "timestamp",
                agora
            )
            > HORAS_EXPIRACAO * 3600
        ):

            expirados.append(chave)

            log.warning(
                f"[EXPIRADO] "
                f"{sinal['home']} x "
                f"{sinal['away']}"
            )

            continue

        try:

            jogo = buscar_jogo_por_id(
                sinal["id"]
            )

            if (
                jogo
                and eh_finalizado(jogo)
            ):

                finalizar(
                    chave,
                    sinal,
                    jogo
                )

        except Exception as e:

            log.exception(
                f"Erro finalizando "
                f"{chave}: {e}"
            )

    if expirados:

        with lock:

            for c in expirados:

                estado["pendentes"].pop(
                    c,
                    None
                )

        salvar_estado()


# ============================================================
# STATS
# ============================================================

def gerar_stats():

    with lock:

        return {

            "status": "online",

            "bot": "Gols + Escanteios",

            "combinados": (
                estatisticas_combinadas()
            ),

            "ultimos_7_dias": (
                assertividade_7_dias()
            ),

            "pendentes": len(
                estado["pendentes"]
            ),

            "total_sinais": (
                estado["stats"]["total_sinais"]
            ),

            "ultimo_run_pre": (
                _ultimo_run["pre"]
            ),

            "ultimo_run_resultado": (
                _ultimo_run["resultado"]
            ),

            "uptime_segundos": int(
                time.time()
                - _ultimo_run["iniciado_em"]
            )
        }


# ============================================================
# RODAR
# ============================================================

def rodar_se_preciso(forcar=False):

    agora = time.time()

    # Pré-jogos
    if (
        forcar
        or (
            agora
            - _ultimo_run["pre"]
            >= INTERVALO_PRE
        )
    ):

        _ultimo_run["pre"] = agora

        try:

            log.info(
                "🔎 Rodando checagem "
                "pré-jogos..."
            )

            jogos = buscar_pre()

            enviados = 0

            for jogo in jogos:

                if enviados >= QTD_POR_RODADA:
                    break

                if analisar_pre(jogo):

                    enviados += 1

            log.info(
                f"Sinais enviados "
                f"nesta rodada: "
                f"{enviados}"
            )

        except Exception:

            log.exception(
                "Erro análise pré"
            )

    # Resultados
    if (
        forcar
        or (
            agora
            - _ultimo_run["resultado"]
            >= INTERVALO_RESULTADOS
        )
    ):

        _ultimo_run["resultado"] = agora

        try:

            log.info(
                f"🏁 Verificando "
                f"resultados "
                f"({len(estado['pendentes'])} "
                f"pendentes)..."
            )

            verificar_resultados()

        except Exception:

            log.exception(
                "Erro resultados"
            )


# ============================================================
# ROTAS
# ============================================================

@app.before_request
def _antes_do_request():

    try:

        rodar_se_preciso()

    except Exception:

        log.exception(
            "Erro no before_request"
        )


@app.route("/")
def home():

    return jsonify({

        "status": "online",

        "bot": "Gols + Escanteios",

        "pendentes": len(
            estado["pendentes"]
        )

    })


@app.route("/health")
def health():

    return jsonify({

        "status": "ok",

        "telegram": bool(
            TELEGRAM_TOKEN
            and CHAT_ID
        ),

        "api": bool(
            API_KEY
        ),

        "pendentes": len(
            estado["pendentes"]
        )

    })


@app.route("/stats")
def stats():

    return jsonify(
        gerar_stats()
    )


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    resultado = {

        "api_key_ok": bool(
            API_KEY
        ),

        "telegram_ok": bool(
            TELEGRAM_TOKEN
            and CHAT_ID
        ),

        "jogos_encontrados": 0,

        "sinais_enviados": 0,

        "erros": []

    }

    try:

        jogos = buscar_pre()

        resultado[
            "jogos_encontrados"
        ] = len(jogos)

        enviados = 0

        for jogo in jogos:

            if enviados >= QTD_POR_RODADA:
                break

            if analisar_pre(jogo):

                enviados += 1

        resultado[
            "sinais_enviados"
        ] = enviados

        # Atualiza a última execução
        _ultimo_run["pre"] = time.time()

    except Exception as e:

        log.exception(
            "Erro no debug"
        )

        resultado[
            "erros"
        ].append(str(e))

    return jsonify(
        resultado
    )


@app.route("/debug/api-crua")
def debug_api_crua():

    per_page = request.args.get(
        "per_page",
        "5"
    )

    data = api_get(
        "/fixtures",
        params={
            "per_page": per_page,
            "lang": "pt"
        }
    )

    return jsonify({

        "fixtures": data

    })


@app.route("/debug/odds-crua/<fid>")
def debug_odds_crua(fid):

    resultados = {}

    for mercado in (
        "goalline",
        "corner",
        "goals",
        "corners",
        "over_under"
    ):

        resultados[mercado] = api_get(

            f"/fixtures/{fid}/odds",

            params={
                "market": mercado,
                "lang": "pt"
            }

        )

    return jsonify(
        resultados
    )


@app.route("/debug/procurar-odds/<fid>")
def debug_procurar_odds(fid):

    urls_teste = [

        f"/fixtures/{fid}/odds",

        f"/odds/{fid}",

        f"/fixtures/{fid}/markets",

        f"/fixtures/{fid}/bookmakers",

        f"/odds?fixture_id={fid}",

        f"/fixtures/{fid}/predictions",

        f"/fixtures/{fid}"

    ]

    resultados = {}

    for url in urls_teste:

        try:

            data = api_get(
                url,
                params={
                    "lang": "pt"
                }
            )

            if data is None:

                resultados[url] = {

                    "ok": False,

                    "info": "null/erro"

                }

            else:

                amostra = json.dumps(
                    data,
                    ensure_ascii=False
                )[:1000]

                resultados[url] = {

                    "ok": True,

                    "tipo": type(
                        data
                    ).__name__,

                    "amostra": amostra

                }

        except Exception as e:

            resultados[url] = {

                "ok": False,

                "erro": str(e)

            }

    return jsonify(
        resultados
    )


@app.route("/debug/fixture-completo/<fid>")
def debug_fixture_completo(fid):

    data = api_get(

        "/fixtures",

        params={
            "per_page": 500,
            "lang": "pt"
        }

    )

    jogos = extrair_lista(
        data
    )

    for jogo in jogos:

        if str(
            extrair_id(jogo)
        ) == str(fid):

            return jsonify({

                "encontrado": True,

                "fixture": jogo

            })

    return jsonify({

        "encontrado": False,

        "total_jogos": len(
            jogos
        ),

        "ids_disponiveis": [

            extrair_id(j)

            for j in jogos[:20]

        ]

    })


@app.route("/debug/proximos")
def debug_proximos():

    data = api_get(

        "/fixtures",

        params={
            "per_page": 200,
            "lang": "pt"
        }

    )

    jogos = extrair_lista(
        data
    )

    lista = []

    for jogo in jogos:

        fid = extrair_id(
            jogo
        )

        minutos = minutos_ate_jogo(
            jogo
        )

        status = extrair_status(
            jogo
        )

        if (
            fid is None
            or minutos is None
        ):

            continue

        if minutos < -30:
            continue

        home, away = extrair_times(
            jogo
        )

        lista.append({

            "id": fid,

            "home": home,

            "away": away,

            "minutos_ate": round(
                minutos,
                1
            ),

            "status": status

        })

    lista.sort(
        key=lambda x: x["minutos_ate"]
    )

    return jsonify(
        lista[:50]
    )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_estado()

log.info(
    "================================"
)

log.info(
    "ROBÔ GOLS + ESCANTEIOS"
)

log.info(
    "API_KEY? "
    f"{bool(API_KEY)}"
    " | Telegram? "
    f"{bool(TELEGRAM_TOKEN and CHAT_ID)}"
)

log.info(
    f"Janela: "
    f"{HORAS_MIN}h a {HORAS_MAX}h"
    f" | ODD: "
    f"{ODD_MIN}-{ODD_MAX}"
)

log.info(
    "================================"
)


if __name__ == "__main__":

    try:

        # Executa uma vez ao iniciar
        rodar_se_preciso(
            forcar=True
        )

    except Exception:

        log.exception(
            "Erro no startup"
        )

    app.run(

        host="0.0.0.0",

        port=PORT

    )
