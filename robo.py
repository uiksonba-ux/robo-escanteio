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
# CONFIGURAÇÃO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

log = logging.getLogger("robo")

app = Flask(__name__)

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# ESTRATÉGIA
# ============================================================

ODD_MIN = float(
    os.getenv("ODD_MIN", "1.35")
)

# ALTERADO PARA 10.00
ODD_MAX = float(
    os.getenv("ODD_MAX", "10.00")
)

QTD_POR_RODADA = int(
    os.getenv("QTD_POR_RODADA", "8")
)

HORAS_MIN = float(
    os.getenv("HORAS_MIN", "0.5")
)

HORAS_MAX = float(
    os.getenv("HORAS_MAX", "12")
)

INTERVALO_PRE = int(
    os.getenv("INTERVALO_PRE", "60")
)

INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "120")
)

# ALTERADO PARA 10
MINIMO_HISTORICO = int(
    os.getenv("MINIMO_HISTORICO", "10")
)

ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "60")
)

MAX_PAGINAS = int(
    os.getenv("MAX_PAGINAS", "20")
)

# Monitor automático
MONITOR_ATIVO = os.getenv(
    "MONITOR_ATIVO",
    "1"
).lower() in (
    "1",
    "true",
    "yes",
    "sim"
)

ARQUIVO_ESTADO = os.getenv(
    "ARQUIVO_ESTADO",
    "bot_state.json"
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
            429,
            500,
            502,
            503,
            504
        ],
        allowed_methods=[
            "GET",
            "POST"
        ]
    )

    adapter = HTTPAdapter(
        max_retries=retry
    )

    s.mount(
        "https://",
        adapter
    )

    s.mount(
        "http://",
        adapter
    )

    return s


SESSION = criar_session()

lock = threading.RLock()

monitor_thread = None
monitor_lock = threading.Lock()


# ============================================================
# ESTADO
# ============================================================

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
# ESTADO — SALVAR
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


# ============================================================
# ESTADO — CARREGAR
# ============================================================

def carregar_estado():

    global estado

    if not os.path.exists(
        ARQUIVO_ESTADO
    ):
        return

    try:

        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as f:

            dados = json.load(f)

        if not isinstance(
            dados,
            dict
        ):
            return

        if isinstance(
            dados.get("stats"),
            dict
        ):

            estado["stats"].update(
                dados["stats"]
            )

        if isinstance(
            dados.get("pendentes"),
            dict
        ):

            estado["pendentes"] = (
                dados["pendentes"]
            )

        if isinstance(
            dados.get("historico"),
            list
        ):

            estado["historico"] = (
                dados["historico"]
            )

        log.info(
            "Estado anterior carregado."
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
        "https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
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

def api_get(
    endpoint,
    params=None
):

    if not API_KEY:

        log.error(
            "FIVE_DOLLAR_KEY não configurada."
        )

        return None

    url = f"{BASE_API}{endpoint}"

    headers = {

        "Authorization":
            f"Bearer {API_KEY}",

        "Accept":
            "application/json"

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
# EXTRAÇÃO DE LISTAS
# ============================================================

def extrair_lista(data):

    if not data:
        return []

    if isinstance(
        data,
        list
    ):
        return data

    if not isinstance(
        data,
        dict
    ):
        return []

    # ========================================================
    # FORMATO REAL:
    #
    # {
    #   "fixtures": {
    #       "data": [...]
    #   }
    # }
    # ========================================================

    fixtures = data.get(
        "fixtures"
    )

    if isinstance(
        fixtures,
        dict
    ):

        lista = fixtures.get(
            "data"
        )

        if isinstance(
            lista,
            list
        ):
            return lista

    # ========================================================
    # Outros formatos
    # ========================================================

    for chave in (
        "data",
        "results",
        "matches",
        "response"
    ):

        valor = data.get(
            chave
        )

        if isinstance(
            valor,
            list
        ):
            return valor

        if isinstance(
            valor,
            dict
        ):

            sublista = valor.get(
                "data"
            )

            if isinstance(
                sublista,
                list
            ):
                return sublista

    return []


# ============================================================
# PAGINAÇÃO
# ============================================================

def extrair_paginacao(data):

    if not isinstance(
        data,
        dict
    ):
        return {}

    # Formato raiz
    pagination = data.get(
        "pagination"
    )

    if isinstance(
        pagination,
        dict
    ):
        return pagination

    # Formato real:
    #
    # fixtures:
    #   data: [...]
    #   pagination: {...}

    fixtures = data.get(
        "fixtures"
    )

    if isinstance(
        fixtures,
        dict
    ):

        pagination = fixtures.get(
            "pagination"
        )

        if isinstance(
            pagination,
            dict
        ):
            return pagination

    return {}


# ============================================================
# ID
# ============================================================

def extrair_id(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return None

    for chave in (
        "id",
        "fixture_id",
        "fixtureId",
        "match_id",
        "event_id"
    ):

        valor = jogo.get(
            chave
        )

        if valor is not None:
            return str(valor)

    fixture = jogo.get(
        "fixture"
    )

    if isinstance(
        fixture,
        dict
    ):

        for chave in (
            "id",
            "fixture_id"
        ):

            valor = fixture.get(
                chave
            )

            if valor is not None:
                return str(valor)

    return None


# ============================================================
# TIMES
# ============================================================

def extrair_times(jogo):

    home = "Casa"
    away = "Fora"

    if not isinstance(
        jogo,
        dict
    ):
        return home, away

    # ========================================================
    # FORMATO:
    # teams -> home / away
    # ========================================================

    teams = jogo.get(
        "teams"
    )

    if isinstance(
        teams,
        dict
    ):

        h = teams.get(
            "home"
        )

        a = teams.get(
            "away"
        )

        if isinstance(
            h,
            dict
        ):

            home = (
                h.get("name")
                or h.get("team_name")
                or home
            )

        if isinstance(
            a,
            dict
        ):

            away = (
                a.get("name")
                or a.get("team_name")
                or away
            )

    # ========================================================
    # FORMATO REAL ALTERNATIVO:
    # times -> casa / fora
    # ========================================================

    times = jogo.get(
        "times"
    )

    if isinstance(
        times,
        dict
    ):

        casa = (
            times.get("casa")
            or times.get("home")
        )

        fora = (
            times.get("fora")
            or times.get("away")
        )

        if isinstance(
            casa,
            dict
        ):

            home = (
                casa.get("name")
                or casa.get("team_name")
                or home
            )

        elif isinstance(
            casa,
            str
        ):

            home = casa

        if isinstance(
            fora,
            dict
        ):

            away = (
                fora.get("name")
                or fora.get("team_name")
                or away
            )

        elif isinstance(
            fora,
            str
        ):

            away = fora

    # ========================================================
    # CASA / FORA DIRETO
    # ========================================================

    if home == "Casa":

        casa = jogo.get(
            "casa"
        )

        if isinstance(
            casa,
            dict
        ):

            home = (
                casa.get("name")
                or home
            )

        elif isinstance(
            casa,
            str
        ):

            home = casa

    if away == "Fora":

        fora = jogo.get(
            "fora"
        )

        if isinstance(
            fora,
            dict
        ):

            away = (
                fora.get("name")
                or away
            )

        elif isinstance(
            fora,
            str
        ):

            away = fora

    return (
        str(home),
        str(away)
    )


# ============================================================
# PLACAR
# ============================================================

def extrair_placar(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return 0, 0

    goals = jogo.get(
        "goals"
    )

    if isinstance(
        goals,
        dict
    ):

        try:

            home = int(
                goals.get(
                    "home",
                    0
                ) or 0
            )

            away = int(
                goals.get(
                    "away",
                    0
                ) or 0
            )

            return home, away

        except Exception:
            pass

    return 0, 0


# ============================================================
# ESCANTEIOS
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return 0

    corners = jogo.get(
        "corners"
    )

    if isinstance(
        corners,
        dict
    ):

        try:

            home = int(
                corners.get(
                    "home",
                    0
                ) or 0
            )

            away = int(
                corners.get(
                    "away",
                    0
                ) or 0
            )

            return home + away

        except Exception:
            pass

    for chave in (
        "total_corners",
        "corner_total",
        "corners_total"
    ):

        valor = jogo.get(
            chave
        )

        if valor is not None:

            try:
                return int(
                    float(valor)
                )

            except Exception:
                pass

    return 0


# ============================================================
# STATUS
# ============================================================

def extrair_status(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return ""

    status = jogo.get(
        "status"
    )

    if isinstance(
        status,
        dict
    ):

        return str(

            status.get("short")
            or status.get("long")
            or status.get("status")
            or ""

        ).upper()

    return str(
        status or ""
    ).upper()


# ============================================================
# FINALIZADO
# ============================================================

def eh_finalizado(jogo):

    status = extrair_status(
        jogo
    ).lower()

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


# ============================================================
# AO VIVO
# ============================================================

def eh_em_andamento(jogo):

    status = extrair_status(
        jogo
    ).lower()

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


# ============================================================
# TIMESTAMP
# ============================================================

def extrair_inicio_timestamp(jogo):

    if not isinstance(
        jogo,
        dict
    ):
        return None

    valores = [

        jogo.get("kickoff_ts"),
        jogo.get("kickoff_utc"),
        jogo.get("start_time"),
        jogo.get("date"),
        jogo.get("datetime")

    ]

    fixture = jogo.get(
        "fixture"
    )

    if isinstance(
        fixture,
        dict
    ):

        valores.extend([

            fixture.get("date"),
            fixture.get("start_time")

        ])

    for valor in valores:

        if not valor:
            continue

        if isinstance(
            valor,
            (int, float)
        ):

            v = float(
                valor
            )

            if v > 1e12:
                v /= 1000

            return v

        texto = str(
            valor
        ).strip()

        try:

            if texto.isdigit():

                v = float(
                    texto
                )

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

            dt = datetime.fromisoformat(
                t
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.timestamp()

        except Exception:
            pass

    return None


# ============================================================
# MINUTOS ATÉ O JOGO
# ============================================================

def minutos_ate_jogo(jogo):

    ts = extrair_inicio_timestamp(
        jogo
    )

    if ts is None:
        return None

    return (

        ts
        - datetime.now(
            timezone.utc
        ).timestamp()

    ) / 60


# ============================================================
# JANELA
# ============================================================

def dentro_da_janela(jogo):

    minutos = minutos_ate_jogo(
        jogo
    )

    if minutos is None:
        return False

    return (

        HORAS_MIN * 60
        <= minutos
        <= HORAS_MAX * 60

    )


# ============================================================
# PAGINAÇÃO REAL
# ============================================================

def buscar_todas_paginas(
    endpoint,
    params=None
):

    params = dict(
        params or {}
    )

    todos = []
    vistos = set()

    for pagina in range(
        1,
        MAX_PAGINAS + 1
    ):

        params["page"] = pagina

        data = api_get(
            endpoint,
            params=params
        )

        if not data:
            break

        jogos = extrair_lista(
            data
        )

        if not jogos:
            break

        novos = 0

        for jogo in jogos:

            fid = extrair_id(
                jogo
            )

            if fid:

                if fid not in vistos:

                    vistos.add(
                        fid
                    )

                    todos.append(
                        jogo
                    )

                    novos += 1

        paginacao = extrair_paginacao(
            data
        )

        has_more = bool(
            paginacao.get(
                "has_more",
                False
            )
        )

        log.info(

            f"Página {pagina}: "
            f"{len(jogos)} jogos | "
            f"{novos} novos | "
            f"has_more={has_more}"

        )

        if not has_more:
            break

        if novos == 0:
            break

    return todos


# ============================================================
# BOOKMAKER
# ============================================================

def normalizar_nome_bookmaker(nome):

    return (

        str(nome or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")

    )


# ============================================================
# EXTRAIR MERCADO
# ============================================================

def extrair_mercado_bookmakers(
    data,
    mercado
):

    resultado = []

    if not isinstance(
        data,
        dict
    ):
        return resultado

    raiz = data.get(
        "data",
        data
    )

    if not isinstance(
        raiz,
        dict
    ):
        return resultado

    bookmakers = raiz.get(
        "bookmakers",
        []
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return resultado

    chave_mercado = (

        "goal_line"
        if mercado == "gols"
        else "corner_line"

    )

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome = (

            bookmaker.get("name")
            or bookmaker.get("bookmaker")
            or "Bookmaker"

        )

        odds = bookmaker.get(
            "odds",
            {}
        )

        if not isinstance(
            odds,
            dict
        ):
            continue

        bloco = odds.get(
            chave_mercado
        )

        if isinstance(
            bloco,
            dict
        ):

            resultado.append({

                "bookmaker":
                    str(nome),

                "bloco":
                    bloco

            })

    return resultado


# ============================================================
# EXTRAIR LINHA E ODD
# ============================================================

def extrair_preco_do_bloco(
    bloco
):

    if not isinstance(
        bloco,
        dict
    ):
        return None, None

    fontes = [

        bloco.get("closing"),
        bloco.get("inplay"),
        bloco.get("current"),
        bloco.get("opening")

    ]

    for fonte in fontes:

        if not isinstance(
            fonte,
            dict
        ):
            continue

        linha = fonte.get(
            "line"
        )

        over = fonte.get(
            "over"
        )

        if (
            linha is None
            or over is None
        ):
            continue

        try:

            linha = float(
                linha
            )

            over = float(
                over
            )

            return (
                linha,
                over
            )

        except Exception:
            continue

    return None, None


# ============================================================
# MELHOR ODD
# ============================================================

def obter_melhor_odd(
    fixture_id,
    mercado
):

    endpoint_mercado = (

        "goalline"
        if mercado == "gols"
        else "corner"

    )

    data = api_get(

        f"/fixtures/{fixture_id}/odds",

        params={

            "market":
                endpoint_mercado,

            "lang":
                "pt"

        }

    )

    if not data:
        return None

    blocos = (
        extrair_mercado_bookmakers(
            data,
            mercado
        )
    )

    candidatos = []

    for item in blocos:

        linha, odd = (
            extrair_preco_do_bloco(
                item["bloco"]
            )
        )

        if (
            linha is None
            or odd is None
        ):
            continue

        if not (

            ODD_MIN
            <= odd
            <= ODD_MAX

        ):
            continue

        candidatos.append({

            "bookmaker":
                item["bookmaker"],

            "linha":
                linha,

            "odd":
                odd

        })

    if not candidatos:
        return None

    return max(

        candidatos,

        key=lambda x:
            x["odd"]

    )


# ============================================================
# FILTROS DE LINHAS
# ============================================================

def linha_gols_valida(
    linha
):

    return (
        0.5
        <= linha
        <= 6.5
    )


def linha_cantos_valida(
    linha
):

    return (
        4.5
        <= linha
        <= 15.5
    )


def filtrar_gols(
    odd_info
):

    return (

        bool(odd_info)
        and linha_gols_valida(
            odd_info["linha"]
        )

    )


def filtrar_cantos(
    odd_info
):

    return (

        bool(odd_info)
        and linha_cantos_valida(
            odd_info["linha"]
        )

    )


# ============================================================
# RESOLUÇÃO DE LINHA
# ============================================================

def resolver_linha(
    total,
    linha
):

    total = float(
        total
    )

    linha = float(
        linha
    )

    # Linha inteira:
    # Over 2.0:
    # 3+ = WIN
    # 2  = PUSH
    # 0-1 = LOSS

    if linha % 1 == 0:

        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    # Linha .5:
    # Over 2.5:
    # 3+ = WIN
    # 0-2 = LOSS

    return (

        "WIN"
        if total > linha
        else "LOSS"

    )


# ============================================================
# RESOLVER COMBINADO
# ============================================================

def resolver_combinado(
    rg,
    rc
):

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
# ASSERTIVIDADE
# ============================================================

def calcular_assertividade(
    wins,
    losses
):

    total = (
        wins
        + losses
    )

    if total <= 0:
        return 0.0

    return round(

        wins
        / total
        * 100,

        2

    )


# ============================================================
# ESTATÍSTICAS COMBINADAS
# ============================================================

def estatisticas_combinadas():

    s = estado[
        "stats"
    ]

    return {

        "wins":
            s["combinados_wins"],

        "losses":
            s["combinados_losses"],

        "push":
            s["combinados_push"],

        "assertividade":
            calcular_assertividade(

                s["combinados_wins"],

                s["combinados_losses"]

            )

    }


# ============================================================
# HISTÓRICO 7 DIAS
# ============================================================

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
            x.get(
                "timestamp",
                0
            )
        ) >= limite

    ]


def assertividade_7_dias():

    h = obter_historico_7_dias()

    wins = sum(

        1

        for x in h

        if x.get(
            "resultado"
        ) == "WIN"

    )

    losses = sum(

        1

        for x in h

        if x.get(
            "resultado"
        ) == "LOSS"

    )

    pushes = sum(

        1

        for x in h

        if x.get(
            "resultado"
        ) == "PUSH"

    )

    return {

        "sinais":
            len(h),

        "wins":
            wins,

        "losses":
            losses,

        "push":
            pushes,

        "assertividade":
            calcular_assertividade(
                wins,
                losses
            )

    }


# ============================================================
# BUSCAR PRÉ-JOGOS
# ============================================================

def buscar_pre():

    hoje = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d"
    )

    amanha = (

        datetime.now(
            timezone.utc
        )
        + timedelta(days=1)

    ).strftime(
        "%Y-%m-%d"
    )

    jogos = []

    # ========================================================
    # HOJE
    # ========================================================

    jogos.extend(

        buscar_todas_paginas(

            "/fixtures",

            {

                "date":
                    hoje,

                "per_page":
                    100,

                "lang":
                    "pt"

            }

        )

    )

    # ========================================================
    # AMANHÃ
    # ========================================================

    jogos.extend(

        buscar_todas_paginas(

            "/fixtures",

            {

                "date":
                    amanha,

                "per_page":
                    100,

                "lang":
                    "pt"

            }

        )

    )

    # ========================================================
    # REMOVER DUPLICADOS
    # ========================================================

    unicos = {}

    for jogo in jogos:

        fid = extrair_id(
            jogo
        )

        if fid:
            unicos[fid] = jogo

    jogos = list(
        unicos.values()
    )

    log.info(

        f"buscar_pre: "
        f"{len(jogos)} jogos encontrados."

    )

    candidatos = []

    total_futuros = 0
    total_in_play = 0
    total_finalizados = 0

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

        if eh_em_andamento(
            jogo
        ):

            total_in_play += 1
            continue

        if eh_finalizado(
            jogo
        ):

            total_finalizados += 1
            continue

        if minutos < 0:
            continue

        total_futuros += 1

        if dentro_da_janela(
            jogo
        ):

            candidatos.append(
                jogo
            )

            home, away = (
                extrair_times(
                    jogo
                )
            )

            log.info(

                f"[CANDIDATO] "
                f"{home} x {away} | "
                f"{minutos:.0f}min | "
                f"status={status}"

            )

    log.info(

        f"buscar_pre: "
        f"{total_futuros} futuros | "
        f"{total_in_play} ao vivo | "
        f"{total_finalizados} finalizados | "
        f"{len(candidatos)} candidatos"

    )

    return candidatos


# ============================================================
# CRIAR SINAL COMBINADO
# ============================================================

def criar_sinal_combinado(
    jogo
):

    fid = extrair_id(
        jogo
    )

    if not fid:
        return None

    home, away = (
        extrair_times(
            jogo
        )
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
            f"[{fid}] Sem odds de gols."
        )

        return None

    if not cantos:

        log.info(
            f"[{fid}] Sem odds de cantos."
        )

        return None

    if not filtrar_gols(
        gols
    ):

        log.info(

            f"[{fid}] "
            f"Linha gols inválida: "
            f"{gols['linha']}"

        )

        return None

    if not filtrar_cantos(
        cantos
    ):

        log.info(

            f"[{fid}] "
            f"Linha cantos inválida: "
            f"{cantos['linha']}"

        )

        return None

    bookmaker_gols = (
        normalizar_nome_bookmaker(
            gols["bookmaker"]
        )
    )

    bookmaker_cantos = (
        normalizar_nome_bookmaker(
            cantos["bookmaker"]
        )
    )

    if (
        bookmaker_gols
        != bookmaker_cantos
    ):

        log.info(

            f"[{fid}] "
            f"Bookmakers diferentes: "
            f"{gols['bookmaker']} / "
            f"{cantos['bookmaker']}"

        )

        return None

    odd_combinada = (

        gols["odd"]
        * cantos["odd"]

    )

    # ========================================================
    # ODD COMBINADA ATÉ 10.00
    # ========================================================

    if not (

        ODD_MIN
        <= odd_combinada
        <= ODD_MAX

    ):

        log.info(

            f"[{fid}] "
            f"Odd combinada fora: "
            f"{odd_combinada:.2f}"

        )

        return None

    historico = (
        assertividade_7_dias()
    )

    # ========================================================
    # FILTRO DE ASSERTIVIDADE
    #
    # Só ativa quando já existem pelo menos
    # MINIMO_HISTORICO sinais.
    # ========================================================

    if (

        historico["sinais"]
        >= MINIMO_HISTORICO

        and

        historico["assertividade"]
        < ASSERTIVIDADE_MINIMA

    ):

        log.info(

            f"[{fid}] "
            f"Filtrado por assertividade: "
            f"{historico['assertividade']:.2f}%"

        )

        return None

    return {

        "id":
            fid,

        "home":
            home,

        "away":
            away,

        "bookmaker":
            gols["bookmaker"],

        "gols": {

            "linha":
                gols["linha"],

            "odd":
                gols["odd"]

        },

        "cantos": {

            "linha":
                cantos["linha"],

            "odd":
                cantos["odd"]

        },

        "odd_combinada":
            round(
                odd_combinada,
                2
            ),

        "assertividade_7d":
            historico[
                "assertividade"
            ],

        "historico_7d":
            historico[
                "sinais"
            ],

        "timestamp":
            time.time(),

        "resultado":
            None

    }


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def mensagem_sinal(s):

    return (

        "🔥 <b>SINAL COMBINADO</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(s['home'])}"
        f"</b> x "
        f"<b>"
        f"{html.escape(s['away'])}"
        f"</b>\n\n"

        "🎯 <b>ENTRADA</b>\n"

        f"⚽ Over "
        f"{s['gols']['linha']} "
        f"Gols @ "
        f"{s['gols']['odd']:.2f}\n"

        f"🚩 Over "
        f"{s['cantos']['linha']} "
        f"Escanteios @ "
        f"{s['cantos']['odd']:.2f}\n\n"

        f"💰 <b>Odd combinada:</b> "
        f"{s['odd_combinada']:.2f}\n"

        f"🏦 <b>Casa:</b> "
        f"{html.escape(s['bookmaker'])}\n\n"

        f"📊 <b>Histórico 7 dias:</b> "
        f"{s['historico_7d']} sinais\n"

        f"📈 <b>Assertividade:</b> "
        f"{s['assertividade_7d']:.2f}%\n\n"

        "⚠️ Assertividade baseada "
        "no histórico do robô.\n"

        "⚠️ Sinal estatístico, "
        "sem garantia de lucro."

    )


# ============================================================
# ANALISAR PRÉ-JOGO
# ============================================================

def analisar_pre(
    jogo
):

    fid = extrair_id(
        jogo
    )

    if not fid:
        return False

    chave = (
        f"{fid}_COMBINADO"
    )

    if chave in (
        estado["pendentes"]
    ):
        return False

    if any(

        h.get("id") == fid

        for h in estado.get(
            "historico",
            []
        )[-2000:]

    ):

        return False

    if not dentro_da_janela(
        jogo
    ):
        return False

    if eh_em_andamento(
        jogo
    ):
        return False

    if eh_finalizado(
        jogo
    ):
        return False

    sinal = criar_sinal_combinado(
        jogo
    )

    if not sinal:
        return False

    if not enviar_telegram(
        mensagem_sinal(
            sinal
        )
    ):

        log.error(
            f"[{fid}] Telegram falhou."
        )

        return False

    with lock:

        estado[
            "pendentes"
        ][chave] = sinal

        estado[
            "stats"
        ][
            "total_sinais"
        ] += 1

    salvar_estado()

    log.info(

        f"[SINAL] "
        f"{sinal['home']} x "
        f"{sinal['away']} | "
        f"Odd "
        f"{sinal['odd_combinada']}"

    )

    return True


# ============================================================
# BUSCAR JOGO POR ID
# ============================================================

def buscar_jogo_por_id(
    fid
):

    data = api_get(

        f"/fixtures/{fid}",

        params={
            "lang": "pt"
        }

    )

    if not data:
        return None

    # Caso direto
    if isinstance(
        data,
        dict
    ):

        valor = data.get(
            "data"
        )

        if isinstance(
            valor,
            dict
        ):

            # data -> fixture
            return valor

        if isinstance(
            valor,
            list
        ) and valor:

            return valor[0]

        fixture = data.get(
            "fixture"
        )

        if isinstance(
            fixture,
            dict
        ):
            return fixture

        # Caso:
        # fixtures -> data -> [...]
        fixtures = data.get(
            "fixtures"
        )

        if isinstance(
            fixtures,
            dict
        ):

            lista = fixtures.get(
                "data"
            )

            if isinstance(
                lista,
                list
            ) and lista:

                return lista[0]

            if isinstance(
                lista,
                dict
            ):

                return lista

    return data


# ============================================================
# FINALIZAR SINAL
# ============================================================

def finalizar(
    chave,
    sinal,
    jogo
):

    gc, gf = (
        extrair_placar(
            jogo
        )
    )

    total_gols = (
        gc + gf
    )

    total_cantos = (
        extrair_cantos(
            jogo
        )
    )

    rg = resolver_linha(

        total_gols,

        sinal[
            "gols"
        ][
            "linha"
        ]

    )

    rc = resolver_linha(

        total_cantos,

        sinal[
            "cantos"
        ][
            "linha"
        ]

    )

    resultado = (
        resolver_combinado(
            rg,
            rc
        )
    )

    sinal.update({

        "resultado":
            resultado,

        "resultado_gols":
            rg,

        "resultado_cantos":
            rc,

        "placar_final":
            f"{gc} x {gf}",

        "total_gols":
            total_gols,

        "total_cantos":
            total_cantos

    })

    s = estado[
        "stats"
    ]

    # ========================================================
    # COMBINADO
    # ========================================================

    if resultado == "WIN":

        s[
            "combinados_wins"
        ] += 1

    elif resultado == "LOSS":

        s[
            "combinados_losses"
        ] += 1

    else:

        s[
            "combinados_push"
        ] += 1

    # ========================================================
    # GOLS
    # ========================================================

    if rg == "WIN":

        s[
            "gols_wins"
        ] += 1

    elif rg == "LOSS":

        s[
            "gols_losses"
        ] += 1

    else:

        s[
            "gols_push"
        ] += 1

    # ========================================================
    # CANTOS
    # ========================================================

    if rc == "WIN":

        s[
            "cantos_wins"
        ] += 1

    elif rc == "LOSS":

        s[
            "cantos_losses"
        ] += 1

    else:

        s[
            "cantos_push"
        ] += 1

    hist = dict(
        sinal
    )

    hist[
        "timestamp_resultado"
    ] = time.time()

    estado[
        "historico"
    ].append(
        hist
    )

    # ========================================================
    # MANTÉM HISTÓRICO DE 30 DIAS
    # ========================================================

    limite = (

        time.time()
        - 30 * 24 * 60 * 60

    )

    estado[
        "historico"
    ] = [

        x

        for x in estado[
            "historico"
        ]

        if float(
            x.get(
                "timestamp",
                time.time()
            )
        ) >= limite

    ]

    estado[
        "pendentes"
    ].pop(
        chave,
        None
    )

    salvar_estado()

    ass = (
        estatisticas_combinadas()
    )

    enviar_telegram(

        "🏁 <b>RESULTADO DO SINAL</b>\n\n"

        f"⚽ <b>"
        f"{html.escape(sinal['home'])}"
        f"</b> x "
        f"<b>"
        f"{html.escape(sinal['away'])}"
        f"</b>\n\n"

        f"📊 Resultado: "
        f"<b>{resultado}</b>\n"

        f"⚽ Gols: {rg}\n"

        f"🚩 Escanteios: {rc}\n\n"

        f"🔢 Placar: "
        f"{gc} x {gf}\n"

        f"🚩 Total escanteios: "
        f"{total_cantos}\n\n"

        f"📈 Assertividade geral: "
        f"{ass['assertividade']:.2f}%"

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

    pendentes = list(
        estado[
            "pendentes"
        ].items()
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
            > 6 * 3600

        ):

            expirados.append(
                chave
            )

            continue

        try:

            jogo = (
                buscar_jogo_por_id(
                    sinal["id"]
                )
            )

            if (
                jogo
                and eh_finalizado(
                    jogo
                )
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

    for chave in expirados:

        estado[
            "pendentes"
        ].pop(
            chave,
            None
        )

    if expirados:
        salvar_estado()


# ============================================================
# STATS
# ============================================================

def gerar_stats():

    with lock:

        return {

            "status":
                "online",

            "bot":
                "Gols + Escanteios",

            "configuracao": {

                "odd_min":
                    ODD_MIN,

                "odd_max":
                    ODD_MAX,

                "minimo_historico":
                    MINIMO_HISTORICO,

                "assertividade_minima":
                    ASSERTIVIDADE_MINIMA,

                "janela_min_horas":
                    HORAS_MIN,

                "janela_max_horas":
                    HORAS_MAX

            },

            "combinados":
                estatisticas_combinadas(),

            "ultimos_7_dias":
                assertividade_7_dias(),

            "pendentes":
                len(
                    estado[
                        "pendentes"
                    ]
                ),

            "total_sinais":
                estado[
                    "stats"
                ][
                    "total_sinais"
                ],

            "ultimo_run_pre":
                _ultimo_run[
                    "pre"
                ],

            "ultimo_run_resultado":
                _ultimo_run[
                    "resultado"
                ],

            "monitor_ativo":
                MONITOR_ATIVO,

            "uptime_segundos":
                int(

                    time.time()
                    - _ultimo_run[
                        "iniciado_em"
                    ]

                )

        }


# ============================================================
# EXECUÇÃO
# ============================================================

def rodar_se_preciso(
    forcar=False
):

    agora = time.time()

    # ========================================================
    # PRÉ-JOGOS
    # ========================================================

    if (

        forcar

        or

        agora
        - _ultimo_run[
            "pre"
        ]
        >= INTERVALO_PRE

    ):

        _ultimo_run[
            "pre"
        ] = agora

        try:

            log.info(
                "🔎 Rodando checagem pré-jogos..."
            )

            jogos = buscar_pre()

            enviados = 0

            for jogo in jogos:

                if (
                    enviados
                    >= QTD_POR_RODADA
                ):
                    break

                if analisar_pre(
                    jogo
                ):

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

    # ========================================================
    # RESULTADOS
    # ========================================================

    if (

        forcar

        or

        agora
        - _ultimo_run[
            "resultado"
        ]
        >= INTERVALO_RESULTADOS

    ):

        _ultimo_run[
            "resultado"
        ] = agora

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
# MONITOR AUTOMÁTICO
# ============================================================

def loop_monitor():

    log.info(
        "🤖 Monitor automático iniciado."
    )

    while True:

        try:

            rodar_se_preciso()

        except Exception:

            log.exception(
                "Erro no monitor automático."
            )

        # Pequena pausa para não consumir CPU
        time.sleep(10)


def iniciar_monitor():

    global monitor_thread

    if not MONITOR_ATIVO:

        log.info(
            "Monitor automático desativado."
        )

        return

    with monitor_lock:

        if (
            monitor_thread
            and monitor_thread.is_alive()
        ):
            return

        monitor_thread = threading.Thread(

            target=loop_monitor,

            name="robo-monitor",

            daemon=True

        )

        monitor_thread.start()


# ============================================================
# ROTAS
# ============================================================

@app.before_request
def antes_do_request():

    try:

        rodar_se_preciso()

    except Exception:

        log.exception(
            "Erro no before_request"
        )


@app.route("/")
def home():

    return jsonify({

        "status":
            "online",

        "bot":
            "Gols + Escanteios",

        "pendentes":
            len(
                estado[
                    "pendentes"
                ]
            ),

        "odd_max":
            ODD_MAX,

        "monitor":
            MONITOR_ATIVO

    })


@app.route("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "telegram":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),

        "api":
            bool(API_KEY),

        "pendentes":
            len(
                estado[
                    "pendentes"
                ]
            ),

        "monitor":
            bool(
                monitor_thread
                and monitor_thread.is_alive()
            )

    })


@app.route("/stats")
def stats():

    return jsonify(
        gerar_stats()
    )


# ============================================================
# DEBUG — RODAR AGORA
# ============================================================

@app.route(
    "/debug/rodar-agora"
)
def debug_rodar_agora():

    resultado = {

        "api_key_ok":
            bool(API_KEY),

        "telegram_ok":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),

        "jogos_encontrados":
            0,

        "sinais_enviados":
            0,

        "erros":
            []

    }

    try:

        jogos = buscar_pre()

        resultado[
            "jogos_encontrados"
        ] = len(jogos)

        enviados = 0

        for jogo in jogos:

            if (
                enviados
                >= QTD_POR_RODADA
            ):
                break

            if analisar_pre(
                jogo
            ):

                enviados += 1

        resultado[
            "sinais_enviados"
        ] = enviados

    except Exception as e:

        resultado[
            "erros"
        ].append(
            str(e)
        )

        log.exception(
            "Erro no debug."
        )

    return jsonify(
        resultado
    )


# ============================================================
# DEBUG — API CRUA
# ============================================================

@app.route(
    "/debug/api-crua"
)
def debug_api_crua():

    per_page = request.args.get(
        "per_page",
        "5"
    )

    data = api_get(

        "/fixtures",

        params={

            "per_page":
                per_page,

            "lang":
                "pt"

        }

    )

    jogos = extrair_lista(
        data
    )

    paginacao = (
        extrair_paginacao(
            data
        )
    )

    amostra = []

    for jogo in jogos[:20]:

        fid = extrair_id(
            jogo
        )

        home, away = (
            extrair_times(
                jogo
            )
        )

        amostra.append({

            "id":
                fid,

            "home":
                home,

            "away":
                away,

            "status":
                extrair_status(
                    jogo
                ),

            "minutos_ate":
                minutos_ate_jogo(
                    jogo
                )

        })

    return jsonify({

        "api_ok":
            data is not None,

        "quantidade_extraida":
            len(jogos),

        "pagination":
            paginacao,

        "jogos_extraidos":
            amostra,

        "fixtures":
            data

    })


# ============================================================
# DEBUG — ODDS
# ============================================================

@app.route(
    "/debug/odds-crua/<fid>"
)
def debug_odds_crua(
    fid
):

    resultados = {}

    for mercado in (

        "goalline",
        "corner",
        "goals",
        "corners",
        "over_under"

    ):

        resultados[
            mercado
        ] = api_get(

            f"/fixtures/{fid}/odds",

            params={

                "market":
                    mercado,

                "lang":
                    "pt"

            }

        )

    return jsonify(
        resultados
    )


# ============================================================
# DEBUG — PROCURAR ODDS
# ============================================================

@app.route(
    "/debug/procurar-odds/<fid>"
)
def debug_procurar_odds(
    fid
):

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

                resultados[
                    url
                ] = {

                    "ok":
                        False,

                    "info":
                        "null/erro"

                }

            else:

                amostra = json.dumps(

                    data,

                    ensure_ascii=False

                )[:600]

                resultados[
                    url
                ] = {

                    "ok":
                        True,

                    "tipo":
                        type(
                            data
                        ).__name__,

                    "amostra":
                        amostra

                }

        except Exception as e:

            resultados[
                url
            ] = {

                "ok":
                    False,

                "erro":
                    str(e)

            }

    return jsonify(
        resultados
    )


# ============================================================
# DEBUG — PRÓXIMOS
# ============================================================

@app.route(
    "/debug/proximos"
)
def debug_proximos():

    jogos = buscar_todas_paginas(

        "/fixtures",

        {

            "per_page":
                100,

            "lang":
                "pt"

        }

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

        home, away = (
            extrair_times(
                jogo
            )
        )

        lista.append({

            "id":
                fid,

            "home":
                home,

            "away":
                away,

            "minutos_ate":
                round(
                    minutos,
                    1
                ),

            "status":
                status

        })

    lista.sort(
        key=lambda x:
            x["minutos_ate"]
    )

    return jsonify(
        lista[:100]
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

    f"API_KEY? "
    f"{bool(API_KEY)} | "

    f"Telegram? "
    f"{bool(TELEGRAM_TOKEN and CHAT_ID)}"

)

log.info(

    f"Janela: "
    f"{HORAS_MIN}h a "
    f"{HORAS_MAX}h | "

    f"ODD: "
    f"{ODD_MIN} - "
    f"{ODD_MAX}"

)

log.info(

    f"Histórico mínimo: "
    f"{MINIMO_HISTORICO}"

)

log.info(

    f"Assertividade mínima: "
    f"{ASSERTIVIDADE_MINIMA}%"

)

log.info(
    f"Monitor automático: "
    f"{MONITOR_ATIVO}"
)

log.info(
    "================================"
)


# ============================================================
# INICIAR MONITOR
# ============================================================

iniciar_monitor()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    try:

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
