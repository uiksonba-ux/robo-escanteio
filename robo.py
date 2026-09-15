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


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FIVE_DOLLAR_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# ============================================================
# FILTROS DA ESTRATÉGIA
# ============================================================

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "10.00"))

QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "60"))
INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "120")
)

MINIMO_HISTORICO = int(
    os.getenv("MINIMO_HISTORICO", "5")
)

ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "60")
)

MAX_PAGINAS = int(
    os.getenv("MAX_PAGINAS", "20")
)

ARQUIVO_ESTADO = "bot_state.json"

MONITOR_ATIVO = (
    os.getenv("MONITOR_ATIVO", "1").lower()
    not in ("0", "false", "no", "off")
)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout
)

log = logging.getLogger("robo")


# ============================================================
# ESTADO
# ============================================================

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

_ultimo_run = {
    "pre": 0,
    "resultado": 0,
    "iniciado_em": time.time()
}

_monitor_thread = None


# ============================================================
# CARREGAR ESTADO
# ============================================================

def carregar_estado():
    global estado

    if not os.path.exists(ARQUIVO_ESTADO):
        log.info("Nenhum estado anterior encontrado.")
        return

    try:
        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as f:
            dados = json.load(f)

        if isinstance(dados, dict):
            estado.update(dados)

        log.info("Estado carregado com sucesso.")

    except Exception as e:
        log.exception(
            "Erro carregando estado: %s",
            e
        )


# ============================================================
# SALVAR ESTADO
# ============================================================

def salvar_estado():

    try:

        with lock:

            arquivo_tmp = (
                ARQUIVO_ESTADO + ".tmp"
            )

            with open(
                arquivo_tmp,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    estado,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(
                arquivo_tmp,
                ARQUIVO_ESTADO
            )

    except Exception as e:

        log.exception(
            "Erro salvando estado: %s",
            e
        )


# ============================================================
# SESSION HTTP
# ============================================================

session = requests.Session()

retry = Retry(
    total=3,
    backoff_factor=0.5,
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

session.mount(
    "https://",
    adapter
)

session.mount(
    "http://",
    adapter
)


# ============================================================
# HEADERS API
# ============================================================

def api_headers():

    return {
        "Authorization": (
            f"Bearer {FIVE_DOLLAR_KEY}"
        ),
        "Accept": "application/json"
    }


# ============================================================
# GET API
# ============================================================

def api_get(
    path,
    params=None,
    timeout=30
):

    if not FIVE_DOLLAR_KEY:
        raise RuntimeError(
            "FIVE_DOLLAR_KEY não configurada."
        )

    url = (
        BASE_API.rstrip("/")
        + "/"
        + path.lstrip("/")
    )

    resposta = session.get(
        url,
        headers=api_headers(),
        params=params or {},
        timeout=timeout
    )

    resposta.raise_for_status()

    return resposta.json()


# ============================================================
# EXTRAIR LISTA DA API
# ============================================================

def extrair_lista(obj):

    if isinstance(obj, list):
        return obj

    if not isinstance(obj, dict):
        return []

    # Exemplo:
    #
    # {
    #   "data": [...]
    # }
    data = obj.get("data")

    if isinstance(data, list):
        return data

    # Exemplo real da API:
    #
    # {
    #   "fixtures": {
    #       "data": [...]
    #   }
    # }

    fixtures = obj.get("fixtures")

    if isinstance(fixtures, list):
        return fixtures

    if isinstance(fixtures, dict):

        data = fixtures.get("data")

        if isinstance(data, list):
            return data

    # Outra possibilidade

    fixture = obj.get("fixture")

    if isinstance(fixture, list):
        return fixture

    if isinstance(fixture, dict):

        data = fixture.get("data")

        if isinstance(data, list):
            return data

    return []


# ============================================================
# EXTRAIR PAGINAÇÃO
# ============================================================

def extrair_paginacao(obj):

    if not isinstance(obj, dict):
        return {}

    pagination = obj.get("pagination")

    if isinstance(pagination, dict):
        return pagination

    fixtures = obj.get("fixtures")

    if isinstance(fixtures, dict):

        pagination = fixtures.get(
            "pagination"
        )

        if isinstance(pagination, dict):
            return pagination

    data = obj.get("data")

    if isinstance(data, dict):

        pagination = data.get(
            "pagination"
        )

        if isinstance(pagination, dict):
            return pagination

    return {}


# ============================================================
# EXTRAIR ID
# ============================================================

def extrair_id(jogo):

    if not isinstance(jogo, dict):
        return None

    for chave in (
        "id",
        "fixture_id",
        "match_id"
    ):

        valor = jogo.get(chave)

        if valor is not None:
            return str(valor)

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for chave in (
            "id",
            "fixture_id"
        ):

            valor = fixture.get(chave)

            if valor is not None:
                return str(valor)

    return None


# ============================================================
# NOME DO TIME
# ============================================================

def nome_time(valor):

    if isinstance(valor, dict):

        return (
            valor.get("name")
            or valor.get("nome")
            or valor.get("team_name")
            or "?"
        )

    if valor:
        return str(valor)

    return "?"


# ============================================================
# EXTRAIR TIMES
# ============================================================

def extrair_times(jogo):

    if not isinstance(jogo, dict):
        return "?", "?"

    # Estrutura:
    #
    # teams:
    #   home:
    #   away:

    teams = jogo.get("teams")

    if isinstance(teams, dict):

        home = teams.get("home")
        away = teams.get("away")

        if home or away:

            return (
                nome_time(home),
                nome_time(away)
            )

    # Estrutura real encontrada:
    #
    # times:
    #   casa:
    #   fora:

    times = jogo.get("times")

    if isinstance(times, dict):

        casa = times.get("casa")
        fora = times.get("fora")

        if casa or fora:

            return (
                nome_time(casa),
                nome_time(fora)
            )

    # Estrutura direta

    home = (
        jogo.get("home")
        or jogo.get("casa")
        or jogo.get("home_team")
    )

    away = (
        jogo.get("away")
        or jogo.get("fora")
        or jogo.get("away_team")
    )

    return (
        nome_time(home),
        nome_time(away)
    )


# ============================================================
# STATUS
# ============================================================

def extrair_status(jogo):

    if not isinstance(jogo, dict):
        return ""

    status = jogo.get("status")

    if isinstance(status, dict):

        return str(
            status.get("short")
            or status.get("long")
            or status.get("name")
            or ""
        ).lower()

    return str(
        status or ""
    ).lower()


# ============================================================
# TIMESTAMP DO JOGO
# ============================================================

def extrair_inicio_timestamp(jogo):

    if not isinstance(jogo, dict):
        return None

    candidatos = [
        jogo.get("kickoff_ts"),
        jogo.get("start_ts"),
        jogo.get("timestamp"),
        jogo.get("start_time"),
        jogo.get("kickoff_utc"),
        jogo.get("date"),
        jogo.get("datetime")
    ]

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        candidatos.extend([
            fixture.get("kickoff_ts"),
            fixture.get("timestamp"),
            fixture.get("start_time"),
            fixture.get("date")
        ])

    for valor in candidatos:

        if valor is None:
            continue

        try:

            if isinstance(
                valor,
                (int, float)
            ):
                return float(valor)

            texto = str(valor).strip()

            if texto.isdigit():
                return float(texto)

            texto = texto.replace(
                "Z",
                "+00:00"
            )

            dt = datetime.fromisoformat(
                texto
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.timestamp()

        except Exception:
            continue

    return None


# ============================================================
# PLACAR
# ============================================================

def extrair_placar(jogo):

    if not isinstance(jogo, dict):
        return None, None

    goals = jogo.get("goals")

    if isinstance(goals, dict):

        home = goals.get("home")
        away = goals.get("away")

        if (
            home is not None
            or away is not None
        ):

            try:

                return (
                    int(home or 0),
                    int(away or 0)
                )

            except Exception:
                pass

    return None, None


# ============================================================
# ESCANTEIOS
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return None

    corners = jogo.get("corners")

    if isinstance(corners, dict):

        home = corners.get("home")
        away = corners.get("away")

        if (
            home is not None
            or away is not None
        ):

            try:

                return (
                    int(home or 0)
                    + int(away or 0)
                )

            except Exception:
                pass

        for chave in (
            "total",
            "totals"
        ):

            valor = corners.get(chave)

            if isinstance(
                valor,
                (int, float)
            ):
                return float(valor)

    for chave in (
        "corners_total",
        "total_corners",
        "corner_total"
    ):

        valor = jogo.get(chave)

        if isinstance(
            valor,
            (int, float)
        ):
            return float(valor)

    return None


# ============================================================
# VERIFICAR FINALIZADO
# ============================================================

def eh_finalizado(status):

    s = str(
        status or ""
    ).lower()

    palavras = (
        "ft",
        "aet",
        "pen",
        "finished",
        "finalizado",
        "ended",
        "match finished",
        "fim"
    )

    return any(
        palavra in s
        for palavra in palavras
    )


# ============================================================
# VERIFICAR AO VIVO
# ============================================================

def eh_em_andamento(status):

    s = str(
        status or ""
    ).lower()

    palavras = (
        "in_play",
        "inplay",
        "live",
        "1h",
        "2h",
        "ht",
        "half",
        "playing",
        "inprogress",
        "ao_vivo"
    )

    return any(
        palavra in s
        for palavra in palavras
    )


# ============================================================
# BUSCAR TODAS AS PÁGINAS
# ============================================================

def buscar_todas_paginas(
    params_base=None
):

    params_base = dict(
        params_base or {}
    )

    resultados = []

    for pagina in range(
        1,
        MAX_PAGINAS + 1
    ):

        params = dict(
            params_base
        )

        params["page"] = pagina

        try:

            resposta = api_get(
                "/fixtures",
                params=params
            )

        except Exception as e:

            log.exception(
                "Erro buscando página %s: %s",
                pagina,
                e
            )

            break

        lista = extrair_lista(
            resposta
        )

        resultados.extend(lista)

        pag = extrair_paginacao(
            resposta
        )

        has_more = pag.get(
            "has_more"
        )

        log.info(
            "Página %s | jogos=%s | "
            "has_more=%s",
            pagina,
            len(lista),
            has_more
        )

        if not lista:
            break

        if has_more is False:
            break

        # A API atual retorna 5 por página.
        if (
            has_more is None
            and len(lista) < 5
        ):
            break

    return resultados


# ============================================================
# BUSCAR JOGOS PRÉ
# ============================================================

def buscar_pre():

    agora = datetime.now(
        timezone.utc
    )

    hoje = agora.strftime(
        "%Y-%m-%d"
    )

    amanha = (
        agora
        + timedelta(days=1)
    ).strftime(
        "%Y-%m-%d"
    )

    jogos = []

    for data in (
        hoje,
        amanha
    ):

        try:

            pagina = buscar_todas_paginas({
                "date": data
            })

            jogos.extend(
                pagina
            )

        except Exception as e:

            log.exception(
                "Erro buscando jogos de %s: %s",
                data,
                e
            )

    # ========================================================
    # DEDUPLICAR
    # ========================================================

    unicos = {}

    for jogo in jogos:

        fid = extrair_id(jogo)

        if fid:
            unicos[fid] = jogo

    jogos = list(
        unicos.values()
    )

    # ========================================================
    # DIAGNÓSTICO
    # ========================================================

    diagnostico = {
        "jogos_api": len(jogos),
        "sem_id": 0,
        "sem_horario": 0,
        "finalizados": 0,
        "ao_vivo": 0,
        "fora_janela": 0,
        "na_janela": 0
    }

    candidatos = []

    agora_ts = time.time()

    for jogo in jogos:

        fid = extrair_id(
            jogo
        )

        if not fid:

            diagnostico[
                "sem_id"
            ] += 1

            continue

        status = extrair_status(
            jogo
        )

        if eh_finalizado(status):

            diagnostico[
                "finalizados"
            ] += 1

            continue

        if eh_em_andamento(status):

            diagnostico[
                "ao_vivo"
            ] += 1

            continue

        inicio = (
            extrair_inicio_timestamp(
                jogo
            )
        )

        if inicio is None:

            diagnostico[
                "sem_horario"
            ] += 1

            continue

        minutos = (
            inicio - agora_ts
        ) / 60

        horas = minutos / 60

        if (
            HORAS_MIN
            <= horas
            <= HORAS_MAX
        ):

            diagnostico[
                "na_janela"
            ] += 1

            candidatos.append(
                jogo
            )

        else:

            diagnostico[
                "fora_janela"
            ] += 1

    log.info(
        "DIAGNÓSTICO PRÉ: %s",
        diagnostico
    )

    return (
        candidatos,
        diagnostico
    )


# ============================================================
# EXTRAIR PREÇO
# ============================================================

def extrair_preco_do_bloco(
    bloco
):

    if not isinstance(
        bloco,
        dict
    ):
        return None

    for chave in (
        "closing",
        "inplay",
        "current",
        "opening"
    ):

        valor = bloco.get(
            chave
        )

        if not isinstance(
            valor,
            dict
        ):
            continue

        odd = (
            valor.get("over")
            or valor.get("price")
            or valor.get("odd")
        )

        line = (
            valor.get("line")
            or valor.get("total")
        )

        try:

            if odd is not None:
                odd = float(odd)

            if line is not None:
                line = float(line)

        except Exception:
            continue

        if odd is not None:

            return {
                "odd": odd,
                "line": line
            }

    return None


# ============================================================
# EXTRAIR ODDS
# ============================================================

def extrair_odds_market(
    resposta,
    tipo
):

    if not isinstance(
        resposta,
        dict
    ):
        return []

    resultados = []

    if tipo == "gols":

        chaves = [
            "goalline",
            "goal",
            "goals"
        ]

    else:

        chaves = [
            "corner",
            "corners"
        ]

    for chave in chaves:

        bloco = resposta.get(
            chave
        )

        if not isinstance(
            bloco,
            dict
        ):
            continue

        data = bloco.get(
            "data"
        )

        if not isinstance(
            data,
            dict
        ):
            continue

        bookmakers = data.get(
            "bookmakers",
            []
        )

        if not isinstance(
            bookmakers,
            list
        ):
            continue

        for bookmaker in bookmakers:

            if not isinstance(
                bookmaker,
                dict
            ):
                continue

            nome = (
                bookmaker.get("name")
                or bookmaker.get("title")
                or bookmaker.get("slug")
                or "?"
            )

            odds = bookmaker.get(
                "odds"
            )

            if not isinstance(
                odds,
                dict
            ):
                continue

            if tipo == "gols":

                mercado = odds.get(
                    "goal_line"
                )

            else:

                mercado = odds.get(
                    "corner_line"
                )

            if not isinstance(
                mercado,
                dict
            ):
                continue

            preco = (
                extrair_preco_do_bloco(
                    mercado
                )
            )

            if preco:

                preco["bookmaker"] = str(
                    nome
                )

                resultados.append(
                    preco
                )

    return resultados


# ============================================================
# BUSCAR ODDS
# ============================================================

def buscar_odds_combinadas(
    fixture_id
):

    resposta = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "market": "goalline|corner"
        }
    )

    gols = extrair_odds_market(
        resposta,
        "gols"
    )

    cantos = extrair_odds_market(
        resposta,
        "cantos"
    )

    return (
        gols,
        cantos
    )


# ============================================================
# VALIDAR LINHAS
# ============================================================

def linha_valida_gols(
    line
):

    if line is None:
        return False

    return (
        0.5
        <= float(line)
        <= 6.5
    )


def linha_valida_cantos(
    line
):

    if line is None:
        return False

    return (
        4.5
        <= float(line)
        <= 15.5
    )


# ============================================================
# HISTÓRICO 7 DIAS
# ============================================================

def calcular_historico_7_dias(
    linha_gols,
    linha_cantos
):

    limite = (
        datetime.now(timezone.utc)
        - timedelta(days=7)
    )

    wins = 0
    losses = 0
    sinais = 0

    with lock:

        registros = list(
            estado.get(
                "historico",
                []
            )
        )

    for item in registros:

        try:

            data = item.get(
                "data"
            )

            if not data:
                continue

            dt = datetime.fromisoformat(
                data.replace(
                    "Z",
                    "+00:00"
                )
            )

            if dt < limite:
                continue

        except Exception:
            continue

        if (
            item.get("tipo")
            != "combinado"
        ):
            continue

        if (
            float(
                item.get(
                    "linha_gols",
                    -1
                )
            )
            != float(linha_gols)
        ):
            continue

        if (
            float(
                item.get(
                    "linha_cantos",
                    -1
                )
            )
            != float(linha_cantos)
        ):
            continue

        resultado = item.get(
            "resultado"
        )

        sinais += 1

        if resultado == "WIN":
            wins += 1

        elif resultado == "LOSS":
            losses += 1

    resolvidos = (
        wins + losses
    )

    if resolvidos > 0:

        assertividade = (
            wins / resolvidos
        ) * 100

    else:

        assertividade = 0

    return {
        "sinais": sinais,
        "wins": wins,
        "losses": losses,
        "assertividade": round(
            assertividade,
            2
        )
    }


# ============================================================
# CRIAR SINAL COMBINADO
# ============================================================

def criar_sinal_combinado(
    jogo
):

    fixture_id = extrair_id(
        jogo
    )

    if not fixture_id:
        return None, "sem_id"

    try:

        gols, cantos = (
            buscar_odds_combinadas(
                fixture_id
            )
        )

    except Exception as e:

        log.warning(
            "Erro nas odds %s: %s",
            fixture_id,
            e
        )

        return None, "erro_odds"

    if not gols:

        return None, "sem_odds_gols"

    if not cantos:

        return None, "sem_odds_cantos"

    combinacoes = []

    for odd_gols in gols:

        if not linha_valida_gols(
            odd_gols.get("line")
        ):
            continue

        for odd_cantos in cantos:

            if not linha_valida_cantos(
                odd_cantos.get("line")
            ):
                continue

            bookmaker_gols = str(
                odd_gols.get(
                    "bookmaker",
                    ""
                )
            ).lower().strip()

            bookmaker_cantos = str(
                odd_cantos.get(
                    "bookmaker",
                    ""
                )
            ).lower().strip()

            # Mesmo bookmaker
            if (
                bookmaker_gols
                != bookmaker_cantos
            ):
                continue

            odd_g = float(
                odd_gols["odd"]
            )

            odd_c = float(
                odd_cantos["odd"]
            )

            odd_combinada = (
                odd_g * odd_c
            )

            if not (
                ODD_MIN
                <= odd_combinada
                <= ODD_MAX
            ):
                continue

            combinacoes.append({

                "bookmaker":
                    odd_gols.get(
                        "bookmaker"
                    ),

                "odd_gols":
                    odd_g,

                "linha_gols":
                    float(
                        odd_gols["line"]
                    ),

                "odd_cantos":
                    odd_c,

                "linha_cantos":
                    float(
                        odd_cantos["line"]
                    ),

                "odd_combinada":
                    round(
                        odd_combinada,
                        2
                    )
            })

    if not combinacoes:

        return (
            None,
            "nenhuma_combinacao_valida"
        )

    # Melhor combinação pela maior odd
    melhor = max(
        combinacoes,
        key=lambda x:
            x["odd_combinada"]
    )

    historico = (
        calcular_historico_7_dias(
            melhor["linha_gols"],
            melhor["linha_cantos"]
        )
    )

    # Só aplica o filtro se já houver
    # histórico suficiente.
    if (
        historico["sinais"]
        >= MINIMO_HISTORICO
        and
        historico["assertividade"]
        < ASSERTIVIDADE_MINIMA
    ):

        return (
            None,
            "historico_baixo"
        )

    casa, fora = (
        extrair_times(jogo)
    )

    inicio = (
        extrair_inicio_timestamp(
            jogo
        )
    )

    minutos = None

    if inicio:

        minutos = (
            inicio - time.time()
        ) / 60

    sinal = {

        "fixture_id":
            fixture_id,

        "home":
            casa,

        "away":
            fora,

        "inicio_ts":
            inicio,

        "minutos_ate":
            minutos,

        "status":
            extrair_status(jogo),

        "bookmaker":
            melhor["bookmaker"],

        "linha_gols":
            melhor["linha_gols"],

        "odd_gols":
            melhor["odd_gols"],

        "linha_cantos":
            melhor["linha_cantos"],

        "odd_cantos":
            melhor["odd_cantos"],

        "odd_combinada":
            melhor["odd_combinada"],

        "historico":
            historico,

        "criado_em":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    return (
        sinal,
        "ok"
    )


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(
    mensagem
):

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}"
        "/sendMessage"
    )

    payload = {

        "chat_id":
            CHAT_ID,

        "text":
            mensagem,

        "parse_mode":
            "HTML",

        "disable_web_page_preview":
            True
    }

    try:

        resposta = session.post(
            url,
            json=payload,
            timeout=20
        )

        if not resposta.ok:

            log.warning(
                "Telegram HTTP %s: %s",
                resposta.status_code,
                resposta.text[:500]
            )

            return False

        dados = resposta.json()

        return bool(
            dados.get("ok")
        )

    except Exception as e:

        log.exception(
            "Erro Telegram: %s",
            e
        )

        return False


# ============================================================
# FORMATAR SINAL
# ============================================================

def formatar_sinal(
    sinal
):

    home = html.escape(
        str(sinal["home"])
    )

    away = html.escape(
        str(sinal["away"])
    )

    bookmaker = html.escape(
        str(sinal["bookmaker"])
    )

    hist = sinal[
        "historico"
    ]

    return (

        "⚽ <b>SINAL "
        "GOLS + ESCANTEIOS</b>\n\n"

        f"🏠 <b>{home}</b>\n"

        f"✈️ <b>{away}</b>\n\n"

        f"⚽ Gols: <b>Over "
        f"{sinal['linha_gols']}</b> "
        f"@ {sinal['odd_gols']:.2f}\n"

        f"🚩 Cantos: <b>Over "
        f"{sinal['linha_cantos']}</b> "
        f"@ {sinal['odd_cantos']:.2f}\n\n"

        f"🎯 Odd combinada: "
        f"<b>{sinal['odd_combinada']:.2f}</b>\n"

        f"🏦 Bookmaker: "
        f"{bookmaker}\n\n"

        f"📊 Histórico 7 dias: "
        f"{hist['sinais']} sinais\n"

        f"✅ Assertividade: "
        f"<b>{hist['assertividade']:.1f}%</b>\n\n"

        f"🆔 {sinal['fixture_id']}"
    )


# ============================================================
# ANALISAR PRÉ-JOGO
# ============================================================

def analisar_pre(
    jogos,
    limite=None
):

    if limite is None:
        limite = QTD_POR_RODADA

    enviados = 0

    diagnostico = {

        "recebidos":
            len(jogos),

        "ja_pendentes":
            0,

        "odds_sem_gols":
            0,

        "odds_sem_cantos":
            0,

        "erro_odds":
            0,

        "combinacao_invalida":
            0,

        "historico_baixo":
            0,

        "sinais_validos":
            0,

        "telegram_falhou":
            0,

        "enviados":
            0
    }

    for jogo in jogos:

        if enviados >= limite:
            break

        fixture_id = extrair_id(
            jogo
        )

        if not fixture_id:
            continue

        with lock:

            if (
                fixture_id
                in estado["pendentes"]
            ):

                diagnostico[
                    "ja_pendentes"
                ] += 1

                continue

        sinal, motivo = (
            criar_sinal_combinado(
                jogo
            )
        )

        if sinal is None:

            if motivo == (
                "sem_odds_gols"
            ):

                diagnostico[
                    "odds_sem_gols"
                ] += 1

            elif motivo == (
                "sem_odds_cantos"
            ):

                diagnostico[
                    "odds_sem_cantos"
                ] += 1

            elif motivo == (
                "erro_odds"
            ):

                diagnostico[
                    "erro_odds"
                ] += 1

            elif motivo == (
                "nenhuma_combinacao_valida"
            ):

                diagnostico[
                    "combinacao_invalida"
                ] += 1

            elif motivo == (
                "historico_baixo"
            ):

                diagnostico[
                    "historico_baixo"
                ] += 1

            continue

        diagnostico[
            "sinais_validos"
        ] += 1

        mensagem = formatar_sinal(
            sinal
        )

        telegram_ok = (
            enviar_telegram(
                mensagem
            )
        )

        if not telegram_ok:

            diagnostico[
                "telegram_falhou"
            ] += 1

            continue

        with lock:

            estado[
                "pendentes"
            ][fixture_id] = sinal

            estado[
                "stats"
            ][
                "total_sinais"
            ] += 1

        enviados += 1

        diagnostico[
            "enviados"
        ] += 1

        salvar_estado()

        log.info(
            "SINAL ENVIADO | "
            "%s x %s | "
            "odd %.2f",
            sinal["home"],
            sinal["away"],
            sinal["odd_combinada"]
        )

    return (
        enviados,
        diagnostico
    )


# ============================================================
# BUSCAR JOGO POR ID
# ============================================================

def buscar_jogo_por_id(
    fixture_id
):

    try:

        resposta = api_get(
            f"/fixtures/{fixture_id}"
        )

        lista = extrair_lista(
            resposta
        )

        if lista:
            return lista[0]

        if isinstance(
            resposta,
            dict
        ):

            for chave in (
                "fixture",
                "data"
            ):

                valor = resposta.get(
                    chave
                )

                if isinstance(
                    valor,
                    dict
                ):

                    if (
                        extrair_id(
                            valor
                        )
                        == str(
                            fixture_id
                        )
                    ):

                        return valor

                    lista2 = valor.get(
                        "data"
                    )

                    if isinstance(
                        lista2,
                        list
                    ):

                        for item in lista2:

                            if (
                                extrair_id(
                                    item
                                )
                                == str(
                                    fixture_id
                                )
                            ):

                                return item

        return None

    except Exception as e:

        log.warning(
            "Erro buscando fixture %s: %s",
            fixture_id,
            e
        )

        return None


# ============================================================
# RESOLVER LINHA
# ============================================================

def resolver_linha(
    total,
    linha
):

    total = float(total)
    linha = float(linha)

    # Linha inteira permite PUSH

    if linha.is_integer():

        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    # Linha quebrada

    if total > linha:
        return "WIN"

    return "LOSS"


# ============================================================
# FINALIZAR SINAL
# ============================================================

def finalizar(
    fixture_id,
    jogo
):

    fixture_id = str(
        fixture_id
    )

    with lock:

        sinal = (
            estado[
                "pendentes"
            ].get(
                fixture_id
            )
        )

    if not sinal:
        return False

    gols_home, gols_away = (
        extrair_placar(jogo)
    )

    cantos = extrair_cantos(
        jogo
    )

    if (
        gols_home is None
        or gols_away is None
        or cantos is None
    ):

        return False

    total_gols = (
        int(gols_home)
        + int(gols_away)
    )

    resultado_gols = (
        resolver_linha(
            total_gols,
            sinal["linha_gols"]
        )
    )

    resultado_cantos = (
        resolver_linha(
            cantos,
            sinal["linha_cantos"]
        )
    )

    if (
        resultado_gols == "LOSS"
        or
        resultado_cantos == "LOSS"
    ):

        resultado_combinado = (
            "LOSS"
        )

    elif (
        resultado_gols == "WIN"
        and
        resultado_cantos == "WIN"
    ):

        resultado_combinado = (
            "WIN"
        )

    else:

        resultado_combinado = (
            "PUSH"
        )

    with lock:

        estado["stats"][
            f"gols_{resultado_gols.lower()}s"
        ] += 1

        estado["stats"][
            f"cantos_{resultado_cantos.lower()}s"
        ] += 1

        estado["stats"][
            f"combinados_"
            f"{resultado_combinado.lower()}s"
        ] += 1

        estado[
            "historico"
        ].append({

            "data":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "fixture_id":
                fixture_id,

            "tipo":
                "combinado",

            "linha_gols":
                sinal["linha_gols"],

            "linha_cantos":
                sinal["linha_cantos"],

            "resultado_gols":
                resultado_gols,

            "resultado_cantos":
                resultado_cantos,

            "resultado":
                resultado_combinado
        })

        # Limita histórico
        estado[
            "historico"
        ] = estado[
            "historico"
        ][-2000:]

        del estado[
            "pendentes"
        ][fixture_id]

    salvar_estado()

    casa = html.escape(
        str(sinal["home"])
    )

    fora = html.escape(
        str(sinal["away"])
    )

    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "↩️"
    }

    mensagem = (

        "🏁 <b>RESULTADO "
        "DO SINAL</b>\n\n"

        f"{casa} x {fora}\n\n"

        f"⚽ Gols: "
        f"{gols_home}-{gols_away} "
        f"→ "
        f"{emoji[resultado_gols]} "
        f"<b>{resultado_gols}</b>\n"

        f"🚩 Cantos: "
        f"{cantos} "
        f"→ "
        f"{emoji[resultado_cantos]} "
        f"<b>{resultado_cantos}</b>\n\n"

        f"🎯 COMBINADO: "
        f"{emoji[resultado_combinado]} "
        f"<b>{resultado_combinado}</b>"
    )

    enviar_telegram(
        mensagem
    )

    log.info(
        "RESULTADO %s | %s x %s | %s",
        resultado_combinado,
        casa,
        fora,
        fixture_id
    )

    return True


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    with lock:

        pendentes = dict(
            estado[
                "pendentes"
            ]
        )

    resolvidos = 0

    for fixture_id, sinal in (
        pendentes.items()
    ):

        jogo = (
            buscar_jogo_por_id(
                fixture_id
            )
        )

        if not jogo:
            continue

        status = extrair_status(
            jogo
        )

        if eh_finalizado(
            status
        ):

            if finalizar(
                fixture_id,
                jogo
            ):

                resolvidos += 1

            continue

        # Expirar após 6 horas

        try:

            criado = datetime.fromisoformat(
                sinal[
                    "criado_em"
                ].replace(
                    "Z",
                    "+00:00"
                )
            )

            idade = (
                datetime.now(
                    timezone.utc
                )
                - criado
            ).total_seconds()

            if idade > 21600:

                with lock:

                    if (
                        fixture_id
                        in estado[
                            "pendentes"
                        ]
                    ):

                        del estado[
                            "pendentes"
                        ][fixture_id]

                salvar_estado()

                log.warning(
                    "Sinal expirado: %s",
                    fixture_id
                )

        except Exception:
            pass

    return resolvidos


# ============================================================
# CICLO PRINCIPAL
# ============================================================

def rodar_se_preciso(
    forcar=False
):

    agora = time.time()

    resultado = {

        "pre_executado":
            False,

        "resultado_executado":
            False,

        "jogos_encontrados":
            0,

        "sinais_enviados":
            0,

        "diagnostico_pre":
            {},

        "diagnostico_sinais":
            {},

        "resolvidos":
            0,

        "erros":
            []
    }

    # ========================================================
    # PRÉ-JOGO
    # ========================================================

    if (
        forcar
        or
        (
            agora
            - _ultimo_run["pre"]
        )
        >= INTERVALO_PRE
    ):

        _ultimo_run["pre"] = (
            agora
        )

        try:

            jogos, diagnostico = (
                buscar_pre()
            )

            resultado[
                "pre_executado"
            ] = True

            resultado[
                "jogos_encontrados"
            ] = len(jogos)

            resultado[
                "diagnostico_pre"
            ] = diagnostico

            enviados, diag_sinais = (
                analisar_pre(
                    jogos
                )
            )

            resultado[
                "sinais_enviados"
            ] = enviados

            resultado[
                "diagnostico_sinais"
            ] = diag_sinais

        except Exception as e:

            log.exception(
                "Erro no pré-jogo: %s",
                e
            )

            resultado[
                "erros"
            ].append(
                f"PRE: {str(e)}"
            )

    # ========================================================
    # RESULTADOS
    # ========================================================

    if (
        forcar
        or
        (
            agora
            - _ultimo_run["resultado"]
        )
        >= INTERVALO_RESULTADOS
    ):

        _ultimo_run[
            "resultado"
        ] = agora

        try:

            resolvidos = (
                verificar_resultados()
            )

            resultado[
                "resultado_executado"
            ] = True

            resultado[
                "resolvidos"
            ] = resolvidos

        except Exception as e:

            log.exception(
                "Erro nos resultados: %s",
                e
            )

            resultado[
                "erros"
            ].append(
                f"RESULTADOS: {str(e)}"
            )

    return resultado


# ============================================================
# MONITOR AUTOMÁTICO
# ============================================================

def monitor_loop():

    log.info(
        "Monitor automático iniciado."
    )

    while True:

        try:

            rodar_se_preciso()

        except Exception as e:

            log.exception(
                "Erro no monitor: %s",
                e
            )

        time.sleep(5)


# ============================================================
# INICIAR MONITOR
# ============================================================

def iniciar_monitor():

    global _monitor_thread

    if not MONITOR_ATIVO:

        log.warning(
            "MONITOR_ATIVO está desativado."
        )

        return

    if (
        _monitor_thread
        and
        _monitor_thread.is_alive()
    ):

        return

    _monitor_thread = (
        threading.Thread(
            target=monitor_loop,
            daemon=True,
            name="monitor-robo"
        )
    )

    _monitor_thread.start()


# ============================================================
# ROTA PRINCIPAL
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "ok":
            True,

        "bot":
            "Gols + Escanteios",

        "status":
            "online",

        "odd_min":
            ODD_MIN,

        "odd_max":
            ODD_MAX,

        "janela_horas": [
            HORAS_MIN,
            HORAS_MAX
        ],

        "qtd_por_rodada":
            QTD_POR_RODADA,

        "monitor_ativo":
            MONITOR_ATIVO
    })


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    with lock:

        stats = dict(
            estado[
                "stats"
            ]
        )

        pendentes = len(
            estado[
                "pendentes"
            ]
        )

        historico = len(
            estado[
                "historico"
            ]
        )

    return jsonify({

        "online":
            True,

        "config": {

            "odd_min":
                ODD_MIN,

            "odd_max":
                ODD_MAX,

            "horas_min":
                HORAS_MIN,

            "horas_max":
                HORAS_MAX,

            "qtd_por_rodada":
                QTD_POR_RODADA,

            "minimo_historico":
                MINIMO_HISTORICO,

            "assertividade_minima":
                ASSERTIVIDADE_MINIMA
        },

        "stats":
            stats,

        "pendentes":
            pendentes,

        "historico":
            historico,

        "monitor":
            (
                _monitor_thread.is_alive()
                if _monitor_thread
                else False
            )
    })


# ============================================================
# DEBUG - PRÓXIMOS JOGOS
# ============================================================

@app.route(
    "/debug/proximos"
)
def debug_proximos():

    jogos, diagnostico = (
        buscar_pre()
    )

    lista = []

    agora = time.time()

    for jogo in jogos:

        inicio = (
            extrair_inicio_timestamp(
                jogo
            )
        )

        minutos = None

        if inicio:

            minutos = (
                inicio - agora
            ) / 60

        casa, fora = (
            extrair_times(jogo)
        )

        lista.append({

            "id":
                extrair_id(jogo),

            "home":
                casa,

            "away":
                fora,

            "status":
                extrair_status(
                    jogo
                ).upper(),

            "minutos_ate":
                (
                    round(
                        minutos,
                        1
                    )
                    if minutos is not None
                    else None
                )
        })

    return jsonify({

        "diagnostico":
            diagnostico,

        "jogos":
            lista
    })


# ============================================================
# DEBUG - API CRUA
# ============================================================

@app.route(
    "/debug/api-crua"
)
def debug_api_crua():

    try:

        resposta = api_get(
            "/fixtures",
            params={
                "page": 1
            }
        )

        jogos = extrair_lista(
            resposta
        )

        return jsonify({

            "api_key_ok":
                bool(
                    FIVE_DOLLAR_KEY
                ),

            "telegram_ok":
                bool(
                    TELEGRAM_TOKEN
                    and CHAT_ID
                ),

            "jogos_extraidos":
                len(jogos),

            "pagination":
                extrair_paginacao(
                    resposta
                ),

            "fixtures":
                resposta
        })

    except Exception as e:

        return jsonify({

            "api_key_ok":
                bool(
                    FIVE_DOLLAR_KEY
                ),

            "telegram_ok":
                bool(
                    TELEGRAM_TOKEN
                    and CHAT_ID
                ),

            "jogos_encontrados":
                0,

            "erros": [
                str(e)
            ]

        }), 500


# ============================================================
# DEBUG - ODDS CRUAS
# ============================================================

@app.route(
    "/debug/odds-crua/<fixture_id>"
)
def debug_odds_crua(
    fixture_id
):

    resultado = {}

    mercados = [
        "goalline",
        "corner",
        "goalline|corner"
    ]

    for mercado in mercados:

        try:

            resposta = api_get(
                f"/fixtures/{fixture_id}/odds",
                params={
                    "market":
                        mercado
                }
            )

            resultado[
                mercado
            ] = resposta

        except Exception as e:

            resultado[
                mercado
            ] = {
                "erro":
                    str(e)
            }

    return jsonify(
        resultado
    )


# ============================================================
# DEBUG - PROCURAR ODDS
# ============================================================

@app.route(
    "/debug/procurar-odds/<fixture_id>"
)
def debug_procurar_odds(
    fixture_id
):

    try:

        gols, cantos = (
            buscar_odds_combinadas(
                fixture_id
            )
        )

        return jsonify({

            "fixture_id":
                fixture_id,

            "gols":
                gols,

            "cantos":
                cantos,

            "odd_min":
                ODD_MIN,

            "odd_max":
                ODD_MAX
        })

    except Exception as e:

        return jsonify({
            "erro":
                str(e)
        }), 500


# ============================================================
# DEBUG - RODAR AGORA
# ============================================================

@app.route(
    "/debug/rodar-agora"
)
def debug_rodar_agora():

    resultado = (
        rodar_se_preciso(
            forcar=True
        )
    )

    return jsonify(
        resultado
    )


# ============================================================
# DEBUG - TESTAR TELEGRAM
# ============================================================

@app.route(
    "/debug/teste-telegram"
)
def debug_teste_telegram():

    ok = enviar_telegram(
        "🤖 <b>Teste do robô</b>\n\n"
        "Telegram funcionando corretamente."
    )

    return jsonify({
        "telegram_ok":
            ok
    })


# ============================================================
# BEFORE REQUEST
# ============================================================

@app.before_request
def executar_monitor():

    try:

        rodar_se_preciso()

    except Exception as e:

        log.exception(
            "Erro no before_request: %s",
            e
        )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_estado()

iniciar_monitor()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    log.info(
        "======================================"
    )

    log.info(
        "ROBÔ GOLS + ESCANTEIOS"
    )

    log.info(
        "ODD_MIN: %.2f",
        ODD_MIN
    )

    log.info(
        "ODD_MAX: %.2f",
        ODD_MAX
    )

    log.info(
        "JANELA: %.1fh até %.1fh",
        HORAS_MIN,
        HORAS_MAX
    )

    log.info(
        "SINAIS POR RODADA: %s",
        QTD_POR_RODADA
    )

    log.info(
        "======================================"
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
        )
