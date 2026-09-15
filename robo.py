import os
import sys
import json
import time
import html
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
# ESTRATÉGIA
# ============================================================

ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "10.00"))

QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))
ASSERTIVIDADE_MINIMA = float(os.getenv("ASSERTIVIDADE_MINIMA", "60"))

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "60"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "120"))

MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "20"))

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
    stream=sys.stdout,
)

log = logging.getLogger("robo")


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

        "total_sinais": 0,
    },

    "pendentes": {},

    "historico": [],
}


lock = threading.Lock()

_ultimo_run = {
    "pre": 0,
    "resultado": 0,
    "iniciado_em": time.time(),
}


# ============================================================
# PERSISTÊNCIA
# ============================================================

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

        if isinstance(dados, dict):

            estado.update(dados)

        log.info(
            "Estado carregado: %s pendentes | %s histórico",
            len(estado.get("pendentes", {})),
            len(estado.get("historico", []))
        )

    except Exception:

        log.exception("Erro carregando estado")


def salvar_estado():

    try:

        temporario = ARQUIVO_ESTADO + ".tmp"

        with open(
            temporario,
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
            temporario,
            ARQUIVO_ESTADO
        )

    except Exception:

        log.exception("Erro salvando estado")


carregar_estado()


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
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
# API
# ============================================================

def api_get(endpoint, params=None):

    if not FIVE_DOLLAR_KEY:
        raise RuntimeError(
            "FIVE_DOLLAR_KEY não configurada"
        )

    url = BASE_API.rstrip("/") + "/" + endpoint.lstrip("/")

    headers = {
        "Authorization": f"Bearer {FIVE_DOLLAR_KEY}",
        "Accept": "application/json",
        "User-Agent": "Robo-Gols-Cantos/1.0",
    }

    resposta = session.get(
        url,
        headers=headers,
        params=params or {},
        timeout=30,
    )

    resposta.raise_for_status()

    return resposta.json()


# ============================================================
# PARSERS DA API
# ============================================================

def extrair_lista(resposta):

    if not isinstance(resposta, dict):
        return []

    # /fixtures
    fixtures = resposta.get("fixtures")

    if isinstance(fixtures, dict):

        data = fixtures.get("data")

        if isinstance(data, list):
            return data

    # data diretamente
    data = resposta.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        lista = data.get("data")

        if isinstance(lista, list):
            return lista

        fixtures = data.get("fixtures")

        if isinstance(fixtures, list):
            return fixtures

    # alguma API pode devolver diretamente
    for chave in (
        "results",
        "matches",
        "games",
        "items",
    ):

        valor = resposta.get(chave)

        if isinstance(valor, list):
            return valor

    return []


def extrair_paginacao(resposta):

    if not isinstance(resposta, dict):
        return {}

    fixtures = resposta.get("fixtures")

    if isinstance(fixtures, dict):

        pag = fixtures.get("pagination")

        if isinstance(pag, dict):
            return pag

    pag = resposta.get("pagination")

    if isinstance(pag, dict):
        return pag

    data = resposta.get("data")

    if isinstance(data, dict):

        pag = data.get("pagination")

        if isinstance(pag, dict):
            return pag

    return {}


# ============================================================
# PAGINAÇÃO CORRIGIDA
# ============================================================

def buscar_todas_paginas(endpoint="/fixtures"):

    todos = []

    vistos = set()

    ultimo_erro = None

    for pagina in range(1, MAX_PAGINAS + 1):

        try:

            log.info(
                "Consultando API: %s | página=%s",
                endpoint,
                pagina
            )

            resposta = api_get(
                endpoint,
                params={
                    "page": pagina
                }
            )

            lista = extrair_lista(resposta)

            pag = extrair_paginacao(resposta)

            log.info(
                "Página %s retornou %s jogos | paginação=%s",
                pagina,
                len(lista),
                pag
            )

            if not lista:
                break

            for jogo in lista:

                fixture_id = extrair_id(jogo)

                if fixture_id:

                    chave = str(fixture_id)

                    if chave in vistos:
                        continue

                    vistos.add(chave)

                todos.append(jogo)

            has_more = pag.get("has_more")

            if has_more is False:
                break

            if has_more is True:
                continue

            # fallback caso a API não informe has_more
            per_page = pag.get("per_page")

            try:
                per_page = int(per_page)
            except Exception:
                per_page = 50

            if len(lista) < per_page:
                break

        except Exception as e:

            ultimo_erro = str(e)

            log.exception(
                "Erro na página %s",
                pagina
            )

            break

    if ultimo_erro:

        log.error(
            "Busca de fixtures terminou com erro: %s",
            ultimo_erro
        )

    log.info(
        "TOTAL DE FIXTURES EXTRAÍDOS: %s",
        len(todos)
    )

    return todos


# ============================================================
# ID
# ============================================================

def extrair_id(jogo):

    if not isinstance(jogo, dict):
        return None

    for chave in (
        "id",
        "fixture_id",
        "match_id",
    ):

        valor = jogo.get(chave)

        if valor is not None:
            return valor

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for chave in (
            "id",
            "fixture_id",
        ):

            valor = fixture.get(chave)

            if valor is not None:
                return valor

    return None


# ============================================================
# TIMES
# ============================================================

def extrair_times(jogo):

    home = "Casa"
    away = "Fora"

    if not isinstance(jogo, dict):
        return home, away

    teams = jogo.get("teams")

    if isinstance(teams, dict):

        casa = teams.get("home")
        fora = teams.get("away")

        if isinstance(casa, dict):
            home = (
                casa.get("name")
                or casa.get("team_name")
                or home
            )

        elif isinstance(casa, str):
            home = casa

        if isinstance(fora, dict):
            away = (
                fora.get("name")
                or fora.get("team_name")
                or away
            )

        elif isinstance(fora, str):
            away = fora

    if home == "Casa":

        home = (
            jogo.get("home_team")
            or jogo.get("home")
            or jogo.get("casa")
            or home
        )

    if away == "Fora":

        away = (
            jogo.get("away_team")
            or jogo.get("away")
            or jogo.get("fora")
            or away
        )

    return str(home), str(away)


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
            or status.get("type")
            or status.get("name")
            or ""
        ).lower()

    return str(status or "").lower()


def eh_em_andamento(jogo):

    status = extrair_status(jogo)

    indicadores = (
        "in_play",
        "inplay",
        "live",
        "1h",
        "2h",
        "ht",
        "half",
        "extra",
        "penalty",
    )

    return any(
        item in status
        for item in indicadores
    )


def eh_finalizado(jogo):

    status = extrair_status(jogo)

    indicadores = (
        "finished",
        "ft",
        "final",
        "ended",
        "completed",
        "after",
    )

    return any(
        item in status
        for item in indicadores
    )


# ============================================================
# HORÁRIO
# ============================================================

def extrair_inicio_timestamp(jogo):

    if not isinstance(jogo, dict):
        return None

    candidatos = [
        jogo.get("kickoff_ts"),
        jogo.get("kickoff_utc"),
        jogo.get("start_time"),
        jogo.get("datetime"),
        jogo.get("date"),
    ]

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        candidatos.extend([
            fixture.get("kickoff_ts"),
            fixture.get("kickoff_utc"),
            fixture.get("start_time"),
            fixture.get("datetime"),
            fixture.get("date"),
        ])

    for valor in candidatos:

        if valor is None:
            continue

        # timestamp numérico
        if isinstance(valor, (int, float)):

            # milissegundos
            if valor > 10_000_000_000:
                valor = valor / 1000

            return float(valor)

        texto = str(valor).strip()

        if not texto:
            continue

        # número em string
        try:

            numero = float(texto)

            if numero > 10_000_000_000:
                numero /= 1000

            return numero

        except Exception:
            pass

        # ISO
        try:

            texto_iso = texto.replace(
                "Z",
                "+00:00"
            )

            dt = datetime.fromisoformat(
                texto_iso
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
# PLACAR
# ============================================================

def extrair_placar(jogo):

    if not isinstance(jogo, dict):
        return None, None

    goals = jogo.get("goals")

    if isinstance(goals, dict):

        home = (
            goals.get("home")
            if goals.get("home") is not None
            else goals.get("casa")
        )

        away = (
            goals.get("away")
            if goals.get("away") is not None
            else goals.get("fora")
        )

        if home is not None and away is not None:

            try:
                return int(home), int(away)
            except Exception:
                pass

    home = (
        jogo.get("home_goals")
        or jogo.get("goals_home")
    )

    away = (
        jogo.get("away_goals")
        or jogo.get("goals_away")
    )

    try:

        if home is not None and away is not None:
            return int(home), int(away)

    except Exception:
        pass

    return None, None


# ============================================================
# CANTOS
# ============================================================

def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return None, None

    corners = jogo.get("corners")

    if isinstance(corners, dict):

        home = (
            corners.get("home")
            if corners.get("home") is not None
            else corners.get("casa")
        )

        away = (
            corners.get("away")
            if corners.get("away") is not None
            else corners.get("fora")
        )

        if home is not None and away is not None:

            try:
                return int(home), int(away)
            except Exception:
                pass

    home = jogo.get("home_corners")
    away = jogo.get("away_corners")

    try:

        if home is not None and away is not None:
            return int(home), int(away)

    except Exception:
        pass

    return None, None


# ============================================================
# BUSCAR JOGO POR ID
# ============================================================

def buscar_jogo_por_id(fixture_id):

    try:

        resposta = api_get(
            f"/fixtures/{fixture_id}"
        )

        lista = extrair_lista(resposta)

        if lista:
            return lista[0]

        if isinstance(
            resposta,
            dict
        ):

            fixture = resposta.get(
                "fixture"
            )

            if isinstance(
                fixture,
                dict
            ):

                return fixture

            data = resposta.get(
                "data"
            )

            if isinstance(
                data,
                dict
            ):

                return data

    except Exception:

        log.exception(
            "Erro buscando fixture %s",
            fixture_id
        )

    return None


# ============================================================
# BUSCAR PRÉ-JOGO
# ============================================================

def buscar_pre():

    diagnostico = {
        "jogos_api": 0,
        "sem_id": 0,
        "sem_horario": 0,
        "finalizados": 0,
        "ao_vivo": 0,
        "fora_janela": 0,
        "na_janela": 0,
        "erro": None,
    }

    candidatos = []

    try:

        # IMPORTANTE:
        # chamada direta para /fixtures
        # sem filtro de data que poderia eliminar jogos
        jogos = buscar_todas_paginas(
            "/fixtures"
        )

        diagnostico["jogos_api"] = len(jogos)

        agora_ts = time.time()

        log.info(
            "AGORA UNIX: %s",
            agora_ts
        )

        for jogo in jogos:

            fixture_id = extrair_id(jogo)

            if not fixture_id:

                diagnostico["sem_id"] += 1
                continue

            if eh_finalizado(jogo):

                diagnostico["finalizados"] += 1
                continue

            if eh_em_andamento(jogo):

                diagnostico["ao_vivo"] += 1
                continue

            kickoff_ts = extrair_inicio_timestamp(
                jogo
            )

            if kickoff_ts is None:

                diagnostico["sem_horario"] += 1
                continue

            try:
                kickoff_ts = float(
                    kickoff_ts
                )
            except Exception:

                diagnostico["sem_horario"] += 1
                continue

            horas = (
                kickoff_ts - agora_ts
            ) / 3600.0

            if (
                HORAS_MIN
                <= horas
                <= HORAS_MAX
            ):

                diagnostico["na_janela"] += 1

                candidatos.append({
                    "fixture_id": fixture_id,
                    "jogo": jogo,
                    "horas_ate_inicio": round(
                        horas,
                        2
                    ),
                    "kickoff_ts": kickoff_ts,
                })

            else:

                diagnostico["fora_janela"] += 1

        candidatos.sort(
            key=lambda x: x["kickoff_ts"]
        )

        log.info(
            "DIAGNÓSTICO PRÉ: %s",
            diagnostico
        )

        log.info(
            "CANDIDATOS PRÉ: %s",
            len(candidatos)
        )

        return candidatos, diagnostico

    except Exception as e:

        diagnostico["erro"] = str(e)

        log.exception(
            "Erro em buscar_pre"
        )

        return [], diagnostico


# ============================================================
# ODDS
# ============================================================

def extrair_preco_do_bloco(bloco):

    if not isinstance(bloco, dict):
        return None

    # prioridade
    for chave in (
        "closing",
        "inplay",
        "current",
        "opening",
    ):

        valor = bloco.get(chave)

        if isinstance(valor, dict):

            linha = valor.get("line")
            over = valor.get("over")
            under = valor.get("under")

            if (
                linha is not None
                and over is not None
                and under is not None
            ):

                try:

                    return {
                        "line": float(linha),
                        "over": float(over),
                        "under": float(under),
                    }

                except Exception:
                    pass

    # caso já seja o bloco
    try:

        if (
            bloco.get("line") is not None
            and bloco.get("over") is not None
            and bloco.get("under") is not None
        ):

            return {
                "line": float(bloco["line"]),
                "over": float(bloco["over"]),
                "under": float(bloco["under"]),
            }

    except Exception:
        pass

    return None


def extrair_odds_mercado(resposta, mercado):

    resultado = []

    if not isinstance(
        resposta,
        dict
    ):
        return resultado

    raiz = resposta.get(
        mercado
    )

    if not isinstance(
        raiz,
        dict
    ):
        return resultado

    data = raiz.get(
        "data"
    )

    if not isinstance(
        data,
        dict
    ):
        return resultado

    bookmakers = data.get(
        "bookmakers"
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return resultado

    bloco_nome = (
        "goal_line"
        if mercado == "goalline"
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
            or bookmaker.get("slug")
            or "Bookmaker"
        )

        odds = bookmaker.get(
            "odds"
        )

        if not isinstance(
            odds,
            dict
        ):
            continue

        bloco = odds.get(
            bloco_nome
        )

        preco = extrair_preco_do_bloco(
            bloco
        )

        if preco:

            preco["bookmaker"] = str(
                nome
            )

            preco["slug"] = str(
                bookmaker.get("slug")
                or ""
            )

            resultado.append(
                preco
            )

    return resultado


def buscar_odds(fixture_id):

    resposta = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "market": "goalline|corner"
        }
    )

    gols = extrair_odds_mercado(
        resposta,
        "goalline"
    )

    cantos = extrair_odds_mercado(
        resposta,
        "corner"
    )

    return gols, cantos, resposta


# ============================================================
# HISTÓRICO 7 DIAS
# ============================================================

def calcular_historico_7_dias(
    linha_gols,
    linha_cantos
):

    agora = datetime.now(
        timezone.utc
    )

    limite = agora - timedelta(
        days=7
    )

    registros = []

    for item in estado.get(
        "historico",
        []
    ):

        try:

            dt = datetime.fromisoformat(
                item["data"].replace(
                    "Z",
                    "+00:00"
                )
            )

            if dt < limite:
                continue

            if (
                float(item.get("linha_gols"))
                != float(linha_gols)
            ):
                continue

            if (
                float(item.get("linha_cantos"))
                != float(linha_cantos)
            ):
                continue

            registros.append(
                item
            )

        except Exception:
            continue

    total = len(registros)

    wins = sum(
        1
        for x in registros
        if x.get("resultado") == "WIN"
    )

    losses = sum(
        1
        for x in registros
        if x.get("resultado") == "LOSS"
    )

    push = sum(
        1
        for x in registros
        if x.get("resultado") == "PUSH"
    )

    decididos = wins + losses

    if decididos > 0:

        assertividade = (
            wins / decididos
        ) * 100

    else:

        assertividade = 0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "push": push,
        "assertividade": round(
            assertividade,
            2
        ),
    }


# ============================================================
# RESOLUÇÃO DE LINHAS
# ============================================================

def resolver_linha(total, linha):

    if total is None or linha is None:
        return None

    try:

        total = float(total)
        linha = float(linha)

    except Exception:

        return None

    # Linha inteira
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
# COMBINADO
# ============================================================

def resolver_combinado(
    resultado_gols,
    resultado_cantos
):

    if (
        resultado_gols == "LOSS"
        or resultado_cantos == "LOSS"
    ):
        return "LOSS"

    if (
        resultado_gols == "WIN"
        and resultado_cantos == "WIN"
    ):
        return "WIN"

    return "PUSH"


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal_combinado(
    fixture_id,
    jogo
):

    try:

        gols_odds, cantos_odds, _ = buscar_odds(
            fixture_id
        )

    except Exception as e:

        return None, "erro_odds", str(e)

    if not gols_odds:

        return None, "odds_sem_gols", None

    if not cantos_odds:

        return None, "odds_sem_cantos", None

    # ========================================================
    # COMPARAR APENAS BOOKMAKERS EM COMUM
    # ========================================================

    mapa_cantos = {}

    for odd in cantos_odds:

        chave = (
            odd.get("slug")
            or odd.get("bookmaker")
            or ""
        ).lower()

        mapa_cantos[chave] = odd

    combinacoes = []

    for odd_gols in gols_odds:

        chave = (
            odd_gols.get("slug")
            or odd_gols.get("bookmaker")
            or ""
        ).lower()

        odd_cantos = mapa_cantos.get(
            chave
        )

        if not odd_cantos:
            continue

        linha_gols = odd_gols.get(
            "line"
        )

        linha_cantos = odd_cantos.get(
            "line"
        )

        odd_over_gols = odd_gols.get(
            "over"
        )

        odd_over_cantos = odd_cantos.get(
            "over"
        )

        if (
            linha_gols is None
            or linha_cantos is None
            or odd_over_gols is None
            or odd_over_cantos is None
        ):
            continue

        # linhas aceitáveis
        if not (
            0.5
            <= float(linha_gols)
            <= 6.5
        ):
            continue

        if not (
            4.5
            <= float(linha_cantos)
            <= 15.5
        ):
            continue

        try:

            odd_combinada = (
                float(odd_over_gols)
                * float(odd_over_cantos)
            )

        except Exception:
            continue

        if not (
            ODD_MIN
            <= odd_combinada
            <= ODD_MAX
        ):
            continue

        historico = calcular_historico_7_dias(
            linha_gols,
            linha_cantos
        )

        # Histórico mínimo
        if (
            historico["total"]
            < MINIMO_HISTORICO
        ):

            continue

        # Assertividade mínima
        if (
            historico["assertividade"]
            < ASSERTIVIDADE_MINIMA
        ):

            continue

        combinacoes.append({
            "fixture_id": fixture_id,

            "bookmaker": odd_gols.get(
                "bookmaker"
            ),

            "linha_gols": float(
                linha_gols
            ),

            "linha_cantos": float(
                linha_cantos
            ),

            "odd_gols": float(
                odd_over_gols
            ),

            "odd_cantos": float(
                odd_over_cantos
            ),

            "odd_combinada": round(
                odd_combinada,
                2
            ),

            "historico": historico,
        })

    if not combinacoes:

        return None, "combinacao_invalida", None

    # Melhor combinação = maior assertividade
    # e depois maior odd
    combinacoes.sort(
        key=lambda x: (
            x["historico"]["assertividade"],
            x["odd_combinada"],
        ),
        reverse=True
    )

    sinal = combinacoes[0]

    home, away = extrair_times(
        jogo
    )

    kickoff_ts = extrair_inicio_timestamp(
        jogo
    )

    if kickoff_ts:

        kickoff = datetime.fromtimestamp(
            kickoff_ts,
            tz=timezone.utc
        )

        horario = kickoff.strftime(
            "%d/%m %H:%M UTC"
        )

    else:

        horario = "Horário indisponível"

    sinal.update({
        "home": home,
        "away": away,
        "horario": horario,
        "criado_em": datetime.now(
            timezone.utc
        ).isoformat(),
    })

    return sinal, None, None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN:
        log.error(
            "TELEGRAM_TOKEN não configurado"
        )
        return False

    if not CHAT_ID:
        log.error(
            "CHAT_ID não configurado"
        )
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
    }

    try:

        resposta = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        log.info(
            "Telegram status=%s",
            resposta.status_code
        )

        resposta.raise_for_status()

        dados = resposta.json()

        log.info(
            "Telegram resposta=%s",
            dados
        )

        return bool(
            dados.get("ok")
        )

    except Exception:

        log.exception(
            "Erro enviando Telegram"
        )

        return False


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def mensagem_sinal(sinal):

    historico = sinal["historico"]

    return (
        "🚨 <b>SINAL GOLS + ESCANTEIOS</b>\n"
        "\n"
        f"⚽ <b>{html.escape(sinal['home'])}</b> "
        f"x <b>{html.escape(sinal['away'])}</b>\n"
        f"🕐 {html.escape(sinal['horario'])}\n"
        "\n"
        f"⚽ Gols: <b>OVER {sinal['linha_gols']}</b>\n"
        f"📐 Odd gols: <b>{sinal['odd_gols']:.2f}</b>\n"
        "\n"
        f"🚩 Cantos: <b>OVER {sinal['linha_cantos']}</b>\n"
        f"📐 Odd cantos: <b>{sinal['odd_cantos']:.2f}</b>\n"
        "\n"
        f"💰 <b>ODD COMBINADA: {sinal['odd_combinada']:.2f}</b>\n"
        f"🏦 Casa: <b>{html.escape(str(sinal['bookmaker']))}</b>\n"
        "\n"
        f"📊 Histórico 7 dias: <b>{historico['total']}</b>\n"
        f"✅ Wins: {historico['wins']}\n"
        f"❌ Losses: {historico['losses']}\n"
        f"➖ Push: {historico['push']}\n"
        f"🎯 Assertividade: <b>{historico['assertividade']:.2f}%</b>\n"
    )


# ============================================================
# EXECUÇÃO PRÉ-JOGO
# ============================================================

def executar_pre():

    candidatos, diagnostico_pre = buscar_pre()

    diagnostico = {
        "recebidos": len(candidatos),
        "ja_pendentes": 0,
        "odds_sem_gols": 0,
        "odds_sem_cantos": 0,
        "erro_odds": 0,
        "combinacao_invalida": 0,
        "historico_baixo": 0,
        "sinais_validos": 0,
        "telegram_falhou": 0,
        "enviados": 0,
    }

    sinais = []

    for candidato in candidatos:

        fixture_id = str(
            candidato["fixture_id"]
        )

        with lock:

            if fixture_id in estado["pendentes"]:

                diagnostico[
                    "ja_pendentes"
                ] += 1

                continue

        sinal, motivo, erro = criar_sinal_combinado(
            candidato["fixture_id"],
            candidato["jogo"]
        )

        if sinal is None:

            if motivo == "odds_sem_gols":

                diagnostico[
                    "odds_sem_gols"
                ] += 1

            elif motivo == "odds_sem_cantos":

                diagnostico[
                    "odds_sem_cantos"
                ] += 1

            elif motivo == "erro_odds":

                diagnostico[
                    "erro_odds"
                ] += 1

            elif motivo == "historico_baixo":

                diagnostico[
                    "historico_baixo"
                ] += 1

            else:

                diagnostico[
                    "combinacao_invalida"
                ] += 1

            continue

        diagnostico[
            "sinais_validos"
        ] += 1

        sinais.append(
            sinal
        )

    # ========================================================
    # ORDENAR MELHORES
    # ========================================================

    sinais.sort(
        key=lambda x: (
            x["historico"]["assertividade"],
            x["odd_combinada"],
        ),
        reverse=True
    )

    sinais = sinais[
        :QTD_POR_RODADA
    ]

    # ========================================================
    # ENVIAR
    # ========================================================

    for sinal in sinais:

        fixture_id = str(
            sinal["fixture_id"]
        )

        texto = mensagem_sinal(
            sinal
        )

        enviado = enviar_telegram(
            texto
        )

        if not enviado:

            diagnostico[
                "telegram_falhou"
            ] += 1

            continue

        with lock:

            estado["pendentes"][
                fixture_id
            ] = {
                "fixture_id": sinal[
                    "fixture_id"
                ],

                "home": sinal[
                    "home"
                ],

                "away": sinal[
                    "away"
                ],

                "linha_gols": sinal[
                    "linha_gols"
                ],

                "linha_cantos": sinal[
                    "linha_cantos"
                ],

                "odd_gols": sinal[
                    "odd_gols"
                ],

                "odd_cantos": sinal[
                    "odd_cantos"
                ],

                "odd_combinada": sinal[
                    "odd_combinada"
                ],

                "bookmaker": sinal[
                    "bookmaker"
                ],

                "historico": sinal[
                    "historico"
                ],

                "criado_em": sinal[
                    "criado_em"
                ],
            }

            estado["stats"][
                "total_sinais"
            ] += 1

            salvar_estado()

        diagnostico[
            "enviados"
        ] += 1

    return {
        "diagnostico_pre": diagnostico_pre,
        "diagnostico_sinais": diagnostico,
        "sinais": sinais,
    }


# ============================================================
# FINALIZAÇÃO DOS SINAIS
# ============================================================

def finalizar_sinal(
    fixture_id,
    pendente,
    jogo
):

    home_goals, away_goals = extrair_placar(
        jogo
    )

    home_corners, away_corners = extrair_cantos(
        jogo
    )

    if (
        home_goals is None
        or away_goals is None
        or home_corners is None
        or away_corners is None
    ):

        return False

    total_gols = (
        home_goals
        + away_goals
    )

    total_cantos = (
        home_corners
        + away_corners
    )

    resultado_gols = resolver_linha(
        total_gols,
        pendente["linha_gols"]
    )

    resultado_cantos = resolver_linha(
        total_cantos,
        pendente["linha_cantos"]
    )

    resultado_combinado = resolver_combinado(
        resultado_gols,
        resultado_cantos
    )

    agora = datetime.now(
        timezone.utc
    ).isoformat()

    # ========================================================
    # STATS
    # ========================================================

    mapa_resultado = {
        "WIN": "wins",
        "LOSS": "losses",
        "PUSH": "push",
    }

    with lock:

        estado["stats"][
            f"gols_{mapa_resultado[resultado_gols]}"
        ] += 1

        estado["stats"][
            f"cantos_{mapa_resultado[resultado_cantos]}"
        ] += 1

        estado["stats"][
            f"combinados_{mapa_resultado[resultado_combinado]}"
        ] += 1

        estado["historico"].append({
            "data": agora,

            "fixture_id": fixture_id,

            "home": pendente[
                "home"
            ],

            "away": pendente[
                "away"
            ],

            "linha_gols": pendente[
                "linha_gols"
            ],

            "linha_cantos": pendente[
                "linha_cantos"
            ],

            "resultado": resultado_combinado,

            "resultado_gols": resultado_gols,

            "resultado_cantos": resultado_cantos,

            "total_gols": total_gols,

            "total_cantos": total_cantos,
        })

        # mantém apenas últimos 30 dias
        limite = datetime.now(
            timezone.utc
        ) - timedelta(
            days=30
        )

        novo_historico = []

        for item in estado[
            "historico"
        ]:

            try:

                dt = datetime.fromisoformat(
                    item["data"].replace(
                        "Z",
                        "+00:00"
                    )
                )

                if dt >= limite:
                    novo_historico.append(
                        item
                    )

            except Exception:
                pass

        estado["historico"] = (
            novo_historico
        )

        del estado[
            "pendentes"
        ][str(fixture_id)]

        salvar_estado()

    # ========================================================
    # TELEGRAM
    # ========================================================

    emoji = {
        "WIN": "✅",
        "LOSS": "❌",
        "PUSH": "➖",
    }

    texto = (
        "📊 <b>RESULTADO DO SINAL</b>\n"
        "\n"
        f"⚽ <b>{html.escape(pendente['home'])}</b> "
        f"x <b>{html.escape(pendente['away'])}</b>\n"
        "\n"
        f"⚽ Gols: {total_gols} "
        f"→ {emoji[resultado_gols]} "
        f"<b>{resultado_gols}</b>\n"
        f"🚩 Cantos: {total_cantos} "
        f"→ {emoji[resultado_cantos]} "
        f"<b>{resultado_cantos}</b>\n"
        "\n"
        f"🔥 <b>COMBINADO: "
        f"{resultado_combinado}</b>\n"
        "\n"
        f"Placar: {home_goals} x {away_goals}\n"
        f"Cantos: {home_corners} x {away_corners}\n"
        f"Odd: {pendente['odd_combinada']:.2f}"
    )

    enviar_telegram(
        texto
    )

    return True


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    resolvidos = 0

    pendentes = list(
        estado.get(
            "pendentes",
            {}
        ).items()
    )

    for fixture_id, pendente in pendentes:

        try:

            jogo = buscar_jogo_por_id(
                fixture_id
            )

            if not jogo:
                continue

            if not eh_finalizado(
                jogo
            ):
                continue

            ok = finalizar_sinal(
                fixture_id,
                pendente,
                jogo
            )

            if ok:
                resolvidos += 1

        except Exception:

            log.exception(
                "Erro resolvendo %s",
                fixture_id
            )

    return resolvidos


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

def rodar_se_preciso(
    forcar=False
):

    agora = time.time()

    resultado = {
        "pre_executado": False,
        "resultado_executado": False,
        "sinais_enviados": 0,
        "resolvidos": 0,
        "jogos_encontrados": 0,
        "diagnostico_pre": {},
        "diagnostico_sinais": {},
        "erros": [],
    }

    # ========================================================
    # PRÉ-JOGO
    # ========================================================

    if (
        forcar
        or agora - _ultimo_run["pre"]
        >= INTERVALO_PRE
    ):

        try:

            dados = executar_pre()

            _ultimo_run["pre"] = agora

            resultado[
                "pre_executado"
            ] = True

            resultado[
                "diagnostico_pre"
            ] = dados.get(
                "diagnostico_pre",
                {}
            )

            resultado[
                "diagnostico_sinais"
            ] = dados.get(
                "diagnostico_sinais",
                {}
            )

            resultado[
                "jogos_encontrados"
            ] = len(
                dados.get(
                    "sinais",
                    []
                )
            )

            resultado[
                "sinais_enviados"
            ] = dados.get(
                "diagnostico_sinais",
                {}
            ).get(
                "enviados",
                0
            )

        except Exception as e:

            resultado[
                "erros"
            ].append(
                f"pré: {str(e)}"
            )

            log.exception(
                "Erro na execução pré"
            )

    # ========================================================
    # RESULTADOS
    # ========================================================

    if (
        forcar
        or agora - _ultimo_run["resultado"]
        >= INTERVALO_RESULTADOS
    ):

        try:

            resolvidos = verificar_resultados()

            _ultimo_run[
                "resultado"
            ] = agora

            resultado[
                "resultado_executado"
            ] = True

            resultado[
                "resolvidos"
            ] = resolvidos

        except Exception as e:

            resultado[
                "erros"
            ].append(
                f"resultado: {str(e)}"
            )

            log.exception(
                "Erro verificando resultados"
            )

    return resultado


# ============================================================
# MONITOR BACKGROUND
# ============================================================

def monitor_loop():

    log.info(
        "Monitor iniciado"
    )

    while True:

        try:

            rodar_se_preciso()

        except Exception:

            log.exception(
                "Erro geral no monitor"
            )

        time.sleep(5)


if MONITOR_ATIVO:

    thread_monitor = threading.Thread(
        target=monitor_loop,
        daemon=True,
        name="monitor"
    )

    thread_monitor.start()


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def index():

    return jsonify({
        "status": "online",
        "bot": "Gols + Escanteios",
        "versao": "2.0",
        "odd_min": ODD_MIN,
        "odd_max": ODD_MAX,
        "janela_horas": [
            HORAS_MIN,
            HORAS_MAX
        ],
        "max_sinais": QTD_POR_RODADA,
        "monitor": MONITOR_ATIVO,
    })


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    return jsonify({
        "online": True,

        "config": {
            "odd_min": ODD_MIN,
            "odd_max": ODD_MAX,
            "qtd_por_rodada": QTD_POR_RODADA,
            "horas_min": HORAS_MIN,
            "horas_max": HORAS_MAX,
            "minimo_historico": MINIMO_HISTORICO,
            "assertividade_minima": ASSERTIVIDADE_MINIMA,
        },

        "stats": estado[
            "stats"
        ],

        "pendentes": len(
            estado.get(
                "pendentes",
                {}
            )
        ),

        "historico": len(
            estado.get(
                "historico",
                []
            )
        ),

        "monitor": MONITOR_ATIVO,

        "ultimo_pre": _ultimo_run[
            "pre"
        ],

        "ultimo_resultado": _ultimo_run[
            "resultado"
        ],
    })


# ============================================================
# DEBUG API CRUA
# ============================================================

@app.route("/debug/api-crua")
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

        pag = extrair_paginacao(
            resposta
        )

        return jsonify({
            "api_key_ok": bool(
                FIVE_DOLLAR_KEY
            ),

            "telegram_ok": bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),

            "jogos_extraidos": len(
                jogos
            ),

            "pagination": pag,

            "resposta": resposta,
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


# ============================================================
# DEBUG PRÓXIMOS
# ============================================================

@app.route("/debug/proximos")
def debug_proximos():

    candidatos, diagnostico = buscar_pre()

    jogos = []

    for item in candidatos:

        jogo = item["jogo"]

        home, away = extrair_times(
            jogo
        )

        jogos.append({
            "fixture_id": item[
                "fixture_id"
            ],

            "home": home,
            "away": away,

            "horas_ate_inicio": item[
                "horas_ate_inicio"
            ],

            "kickoff_ts": item[
                "kickoff_ts"
            ],

            "status": extrair_status(
                jogo
            ),
        })

    return jsonify({
        "diagnostico": diagnostico,
        "jogos": jogos,
    })


# ============================================================
# DEBUG RODAR AGORA
# ============================================================

@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    resultado = rodar_se_preciso(
        forcar=True
    )

    return jsonify(
        resultado
    )


# ============================================================
# DEBUG TESTE TELEGRAM
# ============================================================

@app.route("/debug/teste-telegram")
def debug_teste_telegram():

    texto = (
        "🤖 <b>TESTE DO ROBÔ</b>\n\n"
        "Telegram conectado corretamente.\n"
        "Sistema Gols + Escanteios ativo."
    )

    ok = enviar_telegram(
        texto
    )

    return jsonify({
        "telegram_ok": ok
    })


# ============================================================
# DEBUG ODDS
# ============================================================

@app.route("/debug/odds-crua/<fixture_id>")
def debug_odds_crua(fixture_id):

    try:

        resposta = api_get(
            f"/fixtures/{fixture_id}/odds",
            params={
                "market": "goalline|corner"
            }
        )

        return jsonify(
            resposta
        )

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


# ============================================================
# DEBUG PROCURAR ODDS
# ============================================================

@app.route("/debug/procurar-odds/<fixture_id>")
def debug_procurar_odds(fixture_id):

    try:

        gols, cantos, resposta = buscar_odds(
            fixture_id
        )

        return jsonify({
            "fixture_id": fixture_id,

            "gols": gols,

            "cantos": cantos,

            "combinacoes": [
                {
                    "bookmaker": g["bookmaker"],
                    "linha_gols": g["line"],
                    "odd_gols": g["over"],
                    "linha_cantos": c["line"],
                    "odd_cantos": c["over"],
                    "odd_combinada": round(
                        g["over"]
                        * c["over"],
                        2
                    ),
                }

                for g in gols

                for c in cantos

                if (
                    str(
                        g.get("slug", "")
                    ).lower()
                    ==
                    str(
                        c.get("slug", "")
                    ).lower()
                )
            ],
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


# ============================================================
# DEBUG SIGNAL
# ============================================================

@app.route("/debug/sinal/<fixture_id>")
def debug_sinal(fixture_id):

    try:

        jogo = buscar_jogo_por_id(
            fixture_id
        )

        if not jogo:

            return jsonify({
                "erro": "Fixture não encontrado"
            }), 404

        sinal, motivo, erro = criar_sinal_combinado(
            fixture_id,
            jogo
        )

        return jsonify({
            "sinal": sinal,
            "motivo": motivo,
            "erro": erro,
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    log.info(
        "Iniciando robô..."
    )

    log.info(
        "ODD_MIN=%s | ODD_MAX=%s",
        ODD_MIN,
        ODD_MAX
    )

    log.info(
        "Janela: %.2fh até %.2fh",
        HORAS_MIN,
        HORAS_MAX
    )

    log.info(
        "Máximo de sinais: %s",
        QTD_POR_RODADA
    )

    app.run(
        host="0.0.0.0",
        port=PORT
            )
