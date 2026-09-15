import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FIVE_DOLLAR_KEY = os.getenv("FIVE_DOLLAR_KEY")

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

MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "20"))

# Quantos jogos antigos no máximo serão avaliados
MAX_FIXTURES_HISTORICOS = int(
    os.getenv("MAX_FIXTURES_HISTORICOS", "50")
)

# Quantos dias de histórico externo
DIAS_HISTORICO = int(
    os.getenv("DIAS_HISTORICO", "7")
)

ARQUIVO_ESTADO = "bot_state.json"

MONITOR_ATIVO = str(
    os.getenv("MONITOR_ATIVO", "true")
).lower() in ("1", "true", "yes", "sim")


# ============================================================
# ESTADO
# ============================================================

estado_lock = threading.RLock()
execucao_lock = threading.Lock()

estado = {
    "pendentes": {},
    "historico": [],
    "stats": {
        "wins": 0,
        "losses": 0,
        "push": 0
    }
}

ultimo_run = {
    "status": "nunca_executado",
    "inicio": None,
    "fim": None,
    "diagnostico": {},
    "sinais": [],
    "erro": None
}

run_counter = 0


# ============================================================
# CARREGAR ESTADO
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

            if isinstance(dados.get("pendentes"), dict):
                estado["pendentes"] = dados["pendentes"]

            if isinstance(dados.get("historico"), list):
                estado["historico"] = dados["historico"]

            if isinstance(dados.get("stats"), dict):
                estado["stats"].update(
                    dados["stats"]
                )

        logging.info(
            "Estado carregado: %s pendentes / %s históricos",
            len(estado["pendentes"]),
            len(estado["historico"])
        )

    except Exception:
        logging.exception(
            "Erro ao carregar estado"
        )


def salvar_estado():

    try:

        with estado_lock:

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

    except Exception:
        logging.exception(
            "Erro ao salvar estado"
        )


carregar_estado()


# ============================================================
# SESSION HTTP
# ============================================================

session = requests.Session()

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.5,
    status_forcelist=[
        429,
        500,
        502,
        503,
        504
    ],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(
    max_retries=retry,
    pool_connections=10,
    pool_maxsize=10
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

def api_get(
    endpoint,
    params=None,
    timeout=12
):

    if not FIVE_DOLLAR_KEY:
        raise RuntimeError(
            "FIVE_DOLLAR_KEY não configurada"
        )

    url = BASE_API + endpoint

    headers = {
        "Authorization": f"Bearer {FIVE_DOLLAR_KEY}",
        "Accept": "application/json"
    }

    resposta = session.get(
        url,
        headers=headers,
        params=params or {},
        timeout=timeout
    )

    resposta.raise_for_status()

    return resposta.json()


# ============================================================
# UTILITÁRIOS
# ============================================================

def agora_ts():

    return datetime.now(
        timezone.utc
    ).timestamp()


def numero(valor):

    try:
        if valor is None:
            return None

        return float(valor)

    except Exception:
        return None


def inteiro(valor):

    try:
        return int(float(valor))

    except Exception:
        return None


def extrair_fixture_id(fixture):

    valor = fixture.get("id")

    if valor is None:
        valor = fixture.get("fixture_id")

    try:
        return int(valor)

    except Exception:
        return None


def extrair_timestamp(valor):

    if valor is None:
        return None

    try:

        valor = float(valor)

        # milissegundos
        if valor > 10_000_000_000:
            valor /= 1000

        return valor

    except Exception:
        pass

    if isinstance(valor, str):

        texto = valor.strip()

        try:
            dt = datetime.fromisoformat(
                texto.replace("Z", "+00:00")
            )

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.timestamp()

        except Exception:
            return None

    return None


def extrair_kickoff(fixture):

    campos = [
        "kickoff_ts",
        "kickoff",
        "timestamp",
        "start_ts",
        "start_time",
        "kickoff_utc"
    ]

    for campo in campos:

        if campo in fixture:

            ts = extrair_timestamp(
                fixture.get(campo)
            )

            if ts:
                return ts

    return None


def nome_times(fixture):

    teams = fixture.get("teams") or {}

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    home_name = (
        home.get("name")
        or home.get("team_name")
        or fixture.get("home_name")
        or "Casa"
    )

    away_name = (
        away.get("name")
        or away.get("team_name")
        or fixture.get("away_name")
        or "Fora"
    )

    return home_name, away_name


def extrair_lista(data):

    if not isinstance(data, dict):
        return []

    fixtures = data.get("fixtures")

    if isinstance(fixtures, dict):

        lista = fixtures.get("data")

        if isinstance(lista, list):
            return lista

    lista = data.get("data")

    if isinstance(lista, list):
        return lista

    return []


def extrair_paginacao(data):

    if not isinstance(data, dict):
        return {}

    fixtures = data.get("fixtures")

    if isinstance(fixtures, dict):

        pag = fixtures.get("pagination")

        if isinstance(pag, dict):
            return pag

    pag = data.get("pagination")

    if isinstance(pag, dict):
        return pag

    return {}


# ============================================================
# FIXTURES / PAGINAÇÃO
# ============================================================

def buscar_todas_paginas():

    todos = []
    ids = set()

    for pagina in range(
        1,
        MAX_PAGINAS + 1
    ):

        try:

            dados = api_get(
                "/fixtures",
                params={
                    "page": pagina
                },
                timeout=12
            )

        except Exception as e:

            logging.warning(
                "Erro página %s: %s",
                pagina,
                e
            )

            break

        lista = extrair_lista(dados)

        if not lista:
            break

        novos = 0

        for fixture in lista:

            fid = extrair_fixture_id(
                fixture
            )

            if fid is None:
                continue

            if fid not in ids:

                ids.add(fid)
                todos.append(fixture)
                novos += 1

        pag = extrair_paginacao(
            dados
        )

        has_more = pag.get(
            "has_more"
        )

        per_page = inteiro(
            pag.get("per_page")
        )

        if has_more is False:
            break

        if (
            per_page
            and len(lista) < per_page
        ):
            break

        if novos == 0:
            break

    return todos


# ============================================================
# PRÓXIMOS JOGOS
# ============================================================

def buscar_pre():

    fixtures = buscar_todas_paginas()

    agora = agora_ts()

    candidatos = []

    diagnostico = {
        "jogos_api": len(fixtures),
        "sem_id": 0,
        "sem_horario": 0,
        "finalizados": 0,
        "ao_vivo": 0,
        "fora_janela": 0,
        "na_janela": 0,
        "erro": None
    }

    for fixture in fixtures:

        fid = extrair_fixture_id(
            fixture
        )

        if fid is None:

            diagnostico["sem_id"] += 1
            continue

        status = str(
            fixture.get("status", "")
        ).lower()

        if status in (
            "finished",
            "final",
            "completed",
            "cancelled",
            "canceled",
            "postponed"
        ):

            diagnostico["finalizados"] += 1
            continue

        if status in (
            "in_play",
            "live",
            "inplay"
        ):

            diagnostico["ao_vivo"] += 1
            continue

        kickoff = extrair_kickoff(
            fixture
        )

        if kickoff is None:

            diagnostico["sem_horario"] += 1
            continue

        horas = (
            kickoff - agora
        ) / 3600

        if (
            horas < HORAS_MIN
            or horas > HORAS_MAX
        ):

            diagnostico["fora_janela"] += 1
            continue

        diagnostico["na_janela"] += 1

        home, away = nome_times(
            fixture
        )

        candidatos.append({
            "fixture_id": fid,
            "home": home,
            "away": away,
            "kickoff_ts": kickoff,
            "status": status,
            "horas_ate_inicio": round(
                horas,
                2
            )
        })

    candidatos.sort(
        key=lambda x: x["kickoff_ts"]
    )

    return candidatos, diagnostico


# ============================================================
# ODDS
# ============================================================

def extrair_odds_mercado(
    dados,
    mercado
):

    resultado = []

    bloco = dados.get(
        mercado
    )

    if not isinstance(bloco, dict):
        return resultado

    data = bloco.get(
        "data"
    )

    if not isinstance(data, dict):
        return resultado

    bookmakers = data.get(
        "bookmakers"
    )

    if not isinstance(bookmakers, list):
        return resultado

    campo = (
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

        odds = bookmaker.get(
            "odds"
        )

        if not isinstance(odds, dict):
            continue

        linha = odds.get(
            campo
        )

        if not isinstance(linha, dict):
            continue

        bloco_preco = None

        # prioridade
        for nome in (
            "closing",
            "inplay",
            "current",
            "opening"
        ):

            candidato = linha.get(
                nome
            )

            if isinstance(
                candidato,
                dict
            ):

                over = numero(
                    candidato.get("over")
                )

                line = numero(
                    candidato.get("line")
                )

                if (
                    over is not None
                    and line is not None
                ):

                    bloco_preco = {
                        "line": line,
                        "over": over,
                        "under": numero(
                            candidato.get("under")
                        ),
                        "origem": nome
                    }

                    break

        if not bloco_preco:
            continue

        nome = (
            bookmaker.get("name")
            or bookmaker.get("bookmaker")
            or "Desconhecida"
        )

        slug = (
            bookmaker.get("slug")
            or nome.lower()
        )

        resultado.append({
            "bookmaker": nome,
            "slug": slug,
            **bloco_preco
        })

    return resultado


def buscar_odds(
    fixture_id
):

    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "market": "goalline|corner"
        },
        timeout=10
    )

    gols = extrair_odds_mercado(
        dados,
        "goalline"
    )

    cantos = extrair_odds_mercado(
        dados,
        "corner"
    )

    return gols, cantos


# ============================================================
# VALIDAÇÃO DE LINHAS
# ============================================================

def linha_gols_valida(
    linha
):

    return (
        linha is not None
        and 0.5 <= linha <= 6.5
    )


def linha_cantos_valida(
    linha
):

    return (
        linha is not None
        and 4.5 <= linha <= 15.5
    )


# ============================================================
# HISTÓRICO INTERNO
# ============================================================

def historico_interno(
    linha_gols,
    linha_cantos
):

    resultados = []

    with estado_lock:

        for item in estado["historico"]:

            if (
                abs(
                    float(
                        item.get(
                            "linha_gols",
                            -999
                        )
                    )
                    - linha_gols
                ) < 0.001
                and
                abs(
                    float(
                        item.get(
                            "linha_cantos",
                            -999
                        )
                    )
                    - linha_cantos
                ) < 0.001
            ):

                resultados.append(
                    item
                )

    wins = sum(
        1
        for x in resultados
        if x.get("resultado") == "WIN"
    )

    losses = sum(
        1
        for x in resultados
        if x.get("resultado") == "LOSS"
    )

    total = wins + losses

    assertividade = (
        wins / total * 100
        if total > 0
        else 0
    )

    return {
        "fonte": "bot",
        "total": total,
        "wins": wins,
        "losses": losses,
        "assertividade": round(
            assertividade,
            2
        )
    }


# ============================================================
# HISTÓRICO EXTERNO REAL
# ============================================================

historico_externo_cache = {
    "timestamp": 0,
    "dados": {}
}

historico_externo_lock = threading.Lock()


def fixture_finalizado(fixture):

    status = str(
        fixture.get(
            "status",
            ""
        )
    ).lower()

    return status in (
        "finished",
        "final",
        "completed"
    )


def extrair_placar(fixture):

    goals = fixture.get(
        "goals"
    )

    if not isinstance(
        goals,
        dict
    ):
        return None, None

    home = inteiro(
        goals.get("home")
    )

    away = inteiro(
        goals.get("away")
    )

    if (
        home is None
        or away is None
    ):
        return None, None

    return home, away


def extrair_cantos(fixture):

    corners = fixture.get(
        "corners"
    )

    if not isinstance(
        corners,
        dict
    ):
        return None, None

    home = inteiro(
        corners.get("home")
    )

    away = inteiro(
        corners.get("away")
    )

    if (
        home is None
        or away is None
    ):
        return None, None

    return home, away


def resultado_over(
    total,
    linha
):

    if total is None:
        return "UNKNOWN"

    # Linha inteira permite PUSH
    if abs(
        linha - round(linha)
    ) < 0.001:

        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    return (
        "WIN"
        if total > linha
        else "LOSS"
    )


def buscar_historico_externo():

    agora = time.time()

    with historico_externo_lock:

        if (
            agora
            - historico_externo_cache["timestamp"]
            < 900
        ):

            return (
                historico_externo_cache["dados"]
            )

    inicio = agora_ts() - (
        DIAS_HISTORICO * 86400
    )

    fim = agora_ts()

    try:

        fixtures = buscar_todas_paginas()

    except Exception as e:

        logging.warning(
            "Histórico externo indisponível: %s",
            e
        )

        return {}

    historico = {}

    for fixture in fixtures:

        kickoff = extrair_kickoff(
            fixture
        )

        if kickoff is None:
            continue

        if kickoff < inicio:
            continue

        if kickoff > fim:
            continue

        if not fixture_finalizado(
            fixture
        ):
            continue

        fid = extrair_fixture_id(
            fixture
        )

        if fid is None:
            continue

        gols_home, gols_away = (
            extrair_placar(fixture)
        )

        cantos_home, cantos_away = (
            extrair_cantos(fixture)
        )

        if (
            gols_home is None
            or gols_away is None
            or cantos_home is None
            or cantos_away is None
        ):
            continue

        try:

            odds_gols, odds_cantos = (
                buscar_odds(fid)
            )

        except Exception:
            continue

        home, away = nome_times(
            fixture
        )

        for og in odds_gols:

            if not linha_gols_valida(
                og["line"]
            ):
                continue

            for oc in odds_cantos:

                if not linha_cantos_valida(
                    oc["line"]
                ):
                    continue

                if og["slug"] != oc["slug"]:
                    continue

                linha_g = og["line"]
                linha_c = oc["line"]

                chave = (
                    round(linha_g, 2),
                    round(linha_c, 2)
                )

                if chave not in historico:
                    historico[chave] = []

                rg = resultado_over(
                    gols_home + gols_away,
                    linha_g
                )

                rc = resultado_over(
                    cantos_home + cantos_away,
                    linha_c
                )

                if (
                    rg == "WIN"
                    and rc == "WIN"
                ):
                    combinado = "WIN"

                elif (
                    rg == "LOSS"
                    or rc == "LOSS"
                ):
                    combinado = "LOSS"

                else:
                    combinado = "PUSH"

                historico[chave].append({
                    "fixture_id": fid,
                    "home": home,
                    "away": away,
                    "resultado_gols": rg,
                    "resultado_cantos": rc,
                    "resultado": combinado,
                    "data": kickoff
                })

    with historico_externo_lock:

        historico_externo_cache[
            "timestamp"
        ] = time.time()

        historico_externo_cache[
            "dados"
        ] = historico

    return historico


def calcular_historico(
    linha_gols,
    linha_cantos
):

    externo = buscar_historico_externo()

    chave = (
        round(linha_gols, 2),
        round(linha_cantos, 2)
    )

    resultados = externo.get(
        chave,
        []
    )

    wins = sum(
        1
        for x in resultados
        if x.get("resultado") == "WIN"
    )

    losses = sum(
        1
        for x in resultados
        if x.get("resultado") == "LOSS"
    )

    total = wins + losses

    assertividade = (
        wins / total * 100
        if total
        else 0
    )

    return {
        "fonte": "externa_7_dias",
        "total": total,
        "wins": wins,
        "losses": losses,
        "push": sum(
            1
            for x in resultados
            if x.get("resultado") == "PUSH"
        ),
        "assertividade": round(
            assertividade,
            2
        )
    }


# ============================================================
# PENDENTES
# ============================================================

def sinal_pendente(
    fixture_id
):

    with estado_lock:

        return str(
            fixture_id
        ) in estado["pendentes"]


# ============================================================
# CRIAÇÃO DO SINAL
# ============================================================

def criar_sinal_combinado(
    candidato
):

    fixture_id = candidato[
        "fixture_id"
    ]

    try:

        gols, cantos = buscar_odds(
            fixture_id
        )

    except Exception as e:

        return None, {
            "motivo": "erro_odds",
            "erro": str(e)
        }

    if not gols:

        return None, {
            "motivo": "odds_sem_gols"
        }

    if not cantos:

        return None, {
            "motivo": "odds_sem_cantos"
        }

    combinacoes = []

    mapa_cantos = {}

    for oc in cantos:

        mapa_cantos.setdefault(
            oc["slug"],
            []
        ).append(oc)

    for og in gols:

        if not linha_gols_valida(
            og.get("line")
        ):
            continue

        candidatos_cantos = mapa_cantos.get(
            og["slug"],
            []
        )

        for oc in candidatos_cantos:

            if not linha_cantos_valida(
                oc.get("line")
            ):
                continue

            odd_g = numero(
                og.get("over")
            )

            odd_c = numero(
                oc.get("over")
            )

            if (
                odd_g is None
                or odd_c is None
            ):
                continue

            odd_combinada = (
                odd_g * odd_c
            )

            if (
                odd_combinada < ODD_MIN
                or odd_combinada > ODD_MAX
            ):
                continue

            historico = calcular_historico(
                og["line"],
                oc["line"]
            )

            if (
                historico["total"]
                < MINIMO_HISTORICO
            ):

                continue

            if (
                historico["assertividade"]
                < ASSERTIVIDADE_MINIMA
            ):

                continue

            combinacoes.append({
                "linha_gols": og["line"],
                "odd_gols": odd_g,
                "linha_cantos": oc["line"],
                "odd_cantos": odd_c,
                "odd_combinada": round(
                    odd_combinada,
                    2
                ),
                "bookmaker": og[
                    "bookmaker"
                ],
                "slug": og["slug"],
                "origem_gols": og.get(
                    "origem"
                ),
                "origem_cantos": oc.get(
                    "origem"
                ),
                "historico": historico
            })

    if not combinacoes:

        return None, {
            "motivo": "sem_combinacao_aprovada",
            "quantidade_gols": len(gols),
            "quantidade_cantos": len(cantos)
        }

    combinacoes.sort(
        key=lambda x: (
            x["historico"]["assertividade"],
            x["historico"]["total"],
            x["odd_combinada"]
        ),
        reverse=True
    )

    melhor = combinacoes[0]

    sinal = {
        "fixture_id": fixture_id,
        "home": candidato["home"],
        "away": candidato["away"],
        "kickoff_ts": candidato[
            "kickoff_ts"
        ],
        "linha_gols": melhor[
            "linha_gols"
        ],
        "odd_gols": melhor[
            "odd_gols"
        ],
        "linha_cantos": melhor[
            "linha_cantos"
        ],
        "odd_cantos": melhor[
            "odd_cantos"
        ],
        "odd_combinada": melhor[
            "odd_combinada"
        ],
        "bookmaker": melhor[
            "bookmaker"
        ],
        "slug": melhor["slug"],
        "historico": melhor[
            "historico"
        ],
        "criado_em": agora_ts(),
        "status": "PENDENTE"
    }

    return sinal, {
        "motivo": "aprovado"
    }


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(
    texto
):

    if not TELEGRAM_TOKEN:
        return False, "TELEGRAM_TOKEN ausente"

    if not CHAT_ID:
        return False, "CHAT_ID ausente"

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        resposta = requests.post(
            url,
            json=payload,
            timeout=10
        )

        resposta.raise_for_status()

        return True, None

    except Exception as e:

        logging.exception(
            "Erro Telegram"
        )

        return False, str(e)


def formatar_sinal(
    sinal
):

    kickoff = datetime.fromtimestamp(
        sinal["kickoff_ts"],
        timezone.utc
    ).strftime(
        "%d/%m %H:%M UTC"
    )

    hist = sinal["historico"]

    return (
        "⚽ <b>SINAL — GOLS + ESCANTEIOS</b>\n\n"

        f"🏠 <b>{sinal['home']}</b>\n"
        f"🆚 <b>{sinal['away']}</b>\n"
        f"🕐 {kickoff}\n\n"

        f"⚽ Gols: <b>Over "
        f"{sinal['linha_gols']}</b>\n"
        f"📈 Odd gols: "
        f"<b>{sinal['odd_gols']:.2f}</b>\n\n"

        f"🚩 Escanteios: <b>Over "
        f"{sinal['linha_cantos']}</b>\n"
        f"📈 Odd escanteios: "
        f"<b>{sinal['odd_cantos']:.2f}</b>\n\n"

        f"🎯 <b>Odd combinada: "
        f"{sinal['odd_combinada']:.2f}</b>\n"

        f"🏦 {sinal['bookmaker']}\n\n"

        f"📊 Histórico real: "
        f"<b>{hist['total']}</b>\n"

        f"✅ Wins: {hist['wins']}\n"
        f"❌ Losses: {hist['losses']}\n"
        f"➖ Push: {hist.get('push', 0)}\n"

        f"🔥 Assertividade: "
        f"<b>{hist['assertividade']:.2f}%</b>\n\n"

        "⚠️ Sinal informativo. "
        "Não há garantia de resultado."
    )


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

def executar_pre():

    diagnostico = {
        "recebidos": 0,
        "ja_pendentes": 0,
        "odds_sem_gols": 0,
        "odds_sem_cantos": 0,
        "erro_odds": 0,
        "sem_combinacao_aprovada": 0,
        "sinais_validos": 0,
        "telegram_falhou": 0,
        "enviados": 0
    }

    sinais_enviados = []

    candidatos, diag_api = buscar_pre()

    diagnostico.update(
        diag_api
    )

    diagnostico["recebidos"] = len(
        candidatos
    )

    for candidato in candidatos:

        if (
            len(sinais_enviados)
            >= QTD_POR_RODADA
        ):
            break

        fixture_id = candidato[
            "fixture_id"
        ]

        if sinal_pendente(
            fixture_id
        ):

            diagnostico[
                "ja_pendentes"
            ] += 1

            continue

        try:

            sinal, resultado = (
                criar_sinal_combinado(
                    candidato
                )
            )

        except Exception as e:

            logging.exception(
                "Erro analisando fixture %s",
                fixture_id
            )

            diagnostico[
                "erro_odds"
            ] += 1

            continue

        motivo = resultado.get(
            "motivo"
        )

        if motivo in diagnostico:

            diagnostico[motivo] += 1

        elif motivo == "aprovado":

            pass

        else:

            diagnostico[
                "sem_combinacao_aprovada"
            ] += 1

        if sinal is None:
            continue

        diagnostico[
            "sinais_validos"
        ] += 1

        texto = formatar_sinal(
            sinal
        )

        sucesso, erro = enviar_telegram(
            texto
        )

        if not sucesso:

            diagnostico[
                "telegram_falhou"
            ] += 1

            logging.error(
                "Telegram falhou: %s",
                erro
            )

            continue

        with estado_lock:

            estado[
                "pendentes"
            ][str(fixture_id)] = sinal

        salvar_estado()

        diagnostico[
            "enviados"
        ] += 1

        sinais_enviados.append(
            sinal
        )

    return {
        "diagnostico": diagnostico,
        "sinais": sinais_enviados
    }


# ============================================================
# RESULTADOS
# ============================================================

def finalizar_sinal(
    sinal,
    fixture
):

    gols_home, gols_away = (
        extrair_placar(fixture)
    )

    cantos_home, cantos_away = (
        extrair_cantos(fixture)
    )

    if (
        gols_home is None
        or gols_away is None
        or cantos_home is None
        or cantos_away is None
    ):

        return None

    total_gols = (
        gols_home + gols_away
    )

    total_cantos = (
        cantos_home + cantos_away
    )

    resultado_gols = resultado_over(
        total_gols,
        float(
            sinal["linha_gols"]
        )
    )

    resultado_cantos = resultado_over(
        total_cantos,
        float(
            sinal["linha_cantos"]
        )
    )

    if (
        resultado_gols == "WIN"
        and resultado_cantos == "WIN"
    ):

        combinado = "WIN"

    elif (
        resultado_gols == "LOSS"
        or resultado_cantos == "LOSS"
    ):

        combinado = "LOSS"

    else:

        combinado = "PUSH"

    resultado = {
        "fixture_id": sinal[
            "fixture_id"
        ],
        "home": sinal["home"],
        "away": sinal["away"],
        "linha_gols": sinal[
            "linha_gols"
        ],
        "linha_cantos": sinal[
            "linha_cantos"
        ],
        "odd_combinada": sinal[
            "odd_combinada"
        ],
        "gols": total_gols,
        "cantos": total_cantos,
        "resultado_gols": resultado_gols,
        "resultado_cantos": resultado_cantos,
        "resultado": combinado,
        "data": agora_ts()
    }

    return resultado


def verificar_resultados():

    pendentes = []

    with estado_lock:

        pendentes = list(
            estado["pendentes"].values()
        )

    if not pendentes:
        return {
            "verificados": 0,
            "finalizados": 0
        }

    finalizados = 0

    for sinal in pendentes:

        fid = sinal[
            "fixture_id"
        ]

        try:

            dados = api_get(
                f"/fixtures/{fid}",
                timeout=10
            )

        except Exception:

            continue

        fixtures = extrair_lista(
            dados
        )

        fixture = None

        if fixtures:

            fixture = fixtures[0]

        elif isinstance(
            dados,
            dict
        ):

            if isinstance(
                dados.get("fixture"),
                dict
            ):
                fixture = dados[
                    "fixture"
                ]

        if not fixture:
            continue

        if not fixture_finalizado(
            fixture
        ):
            continue

        resultado = finalizar_sinal(
            sinal,
            fixture
        )

        if not resultado:
            continue

        with estado_lock:

            estado["historico"].append(
                resultado
            )

            if resultado[
                "resultado"
            ] == "WIN":

                estado["stats"][
                    "wins"
                ] += 1

            elif resultado[
                "resultado"
            ] == "LOSS":

                estado["stats"][
                    "losses"
                ] += 1

            else:

                estado["stats"][
                    "push"
                ] += 1

            estado[
                "pendentes"
            ].pop(
                str(fid),
                None
            )

        finalizados += 1

        salvar_estado()

        logging.info(
            "Resultado %s: %s x %s | gols=%s cantos=%s",
            fid,
            sinal["home"],
            sinal["away"],
            resultado["gols"],
            resultado["cantos"]
        )

    return {
        "verificados": len(
            pendentes
        ),
        "finalizados": finalizados
    }


# ============================================================
# CICLO
# ============================================================

ultima_execucao_pre = 0
ultima_execucao_resultados = 0


def rodar_se_preciso(
    forcar=False
):

    global ultima_execucao_pre
    global ultima_execucao_resultados

    agora = time.time()

    resultado_pre = None
    resultado_resultados = None

    if (
        forcar
        or agora - ultima_execucao_pre
        >= INTERVALO_PRE
    ):

        resultado_pre = executar_pre()

        ultima_execucao_pre = time.time()

    if (
        forcar
        or agora - ultima_execucao_resultados
        >= INTERVALO_RESULTADOS
    ):

        resultado_resultados = (
            verificar_resultados()
        )

        ultima_execucao_resultados = (
            time.time()
        )

    return {
        "pre": resultado_pre,
        "resultados": resultado_resultados
    }


# ============================================================
# EXECUÇÃO EM BACKGROUND
# ============================================================

def executar_background(
    run_id
):

    global ultimo_run

    if not execucao_lock.acquire(
        blocking=False
    ):

        with estado_lock:

            ultimo_run.update({
                "status": "ja_em_execucao",
                "erro": "Outra análise já está em andamento"
            })

        return

    inicio = datetime.now(
        timezone.utc
    ).isoformat()

    with estado_lock:

        ultimo_run.update({
            "status": "executando",
            "inicio": inicio,
            "fim": None,
            "diagnostico": {},
            "sinais": [],
            "erro": None,
            "run_id": run_id
        })

    try:

        resultado = rodar_se_preciso(
            forcar=True
        )

        pre = resultado.get(
            "pre"
        ) or {}

        with estado_lock:

            ultimo_run.update({
                "status": "concluido",
                "fim": datetime.now(
                    timezone.utc
                ).isoformat(),
                "diagnostico": pre.get(
                    "diagnostico",
                    {}
                ),
                "sinais": pre.get(
                    "sinais",
                    []
                ),
                "resultado_completo": resultado
            })

    except Exception as e:

        logging.exception(
            "Erro na execução background"
        )

        with estado_lock:

            ultimo_run.update({
                "status": "erro",
                "fim": datetime.now(
                    timezone.utc
                ).isoformat(),
                "erro": str(e)
            })

    finally:

        execucao_lock.release()


# ============================================================
# MONITOR
# ============================================================

def monitor_loop():

    logging.info(
        "Monitor iniciado"
    )

    while True:

        try:

            if not execucao_lock.locked():

                executar_background(
                    "monitor"
                )

        except Exception:

            logging.exception(
                "Erro no monitor"
            )

        time.sleep(5)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def index():

    return jsonify({
        "bot": "Gols + Escanteios",
        "status": "online",
        "versao": "3.0",
        "janela_horas": [
            HORAS_MIN,
            HORAS_MAX
        ],
        "odd_min": ODD_MIN,
        "odd_max": ODD_MAX,
        "max_sinais": QTD_POR_RODADA,
        "historico_minimo": MINIMO_HISTORICO,
        "assertividade_minima":
            ASSERTIVIDADE_MINIMA,
        "historico_externo_dias":
            DIAS_HISTORICO,
        "monitor": MONITOR_ATIVO
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok"
    })


@app.route("/status")
def status():

    with estado_lock:

        stats = dict(
            estado["stats"]
        )

        pendentes = len(
            estado["pendentes"]
        )

        historico = len(
            estado["historico"]
        )

        run = dict(
            ultimo_run
        )

    total_resolvidos = (
        stats["wins"]
        + stats["losses"]
    )

    assertividade = (
        stats["wins"]
        / total_resolvidos
        * 100
        if total_resolvidos
        else 0
    )

    return jsonify({
        "bot": "Gols + Escanteios",
        "stats": stats,
        "assertividade": round(
            assertividade,
            2
        ),
        "pendentes": pendentes,
        "historico_interno": historico,
        "ultima_execucao": run
    })


@app.route("/debug/proximos")
def debug_proximos():

    try:

        jogos, diagnostico = buscar_pre()

        return jsonify({
            "diagnostico": diagnostico,
            "jogos": jogos
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    global run_counter

    run_counter += 1

    run_id = (
        f"manual-{int(time.time())}-"
        f"{run_counter}"
    )

    if execucao_lock.locked():

        return jsonify({
            "status": "ja_em_execucao",
            "mensagem": (
                "Já existe uma análise em andamento."
            ),
            "run_id": run_id
        }), 202

    thread = threading.Thread(
        target=executar_background,
        args=(run_id,),
        daemon=True
    )

    thread.start()

    return jsonify({
        "status": "iniciado",
        "mensagem": (
            "Análise iniciada em segundo plano."
        ),
        "run_id": run_id,
        "consultar": "/debug/ultimo-run"
    }), 202


@app.route("/debug/ultimo-run")
def debug_ultimo_run():

    with estado_lock:

        return jsonify(
            ultimo_run
        )


@app.route("/debug/teste-telegram")
def debug_teste_telegram():

    sucesso, erro = enviar_telegram(
        "🤖 <b>Teste do robô Gols + Escanteios</b>\n\n"
        "Telegram funcionando corretamente."
    )

    if sucesso:

        return jsonify({
            "ok": True,
            "mensagem": "Telegram funcionando"
        })

    return jsonify({
        "ok": False,
        "erro": erro
    }), 500


@app.route("/debug/sinal/<int:fixture_id>")
def debug_sinal(
    fixture_id
):

    try:

        fixtures = buscar_todas_paginas()

        fixture = None

        for item in fixtures:

            if (
                extrair_fixture_id(item)
                == fixture_id
            ):

                fixture = item
                break

        if not fixture:

            return jsonify({
                "erro": "Fixture não encontrada"
            }), 404

        kickoff = extrair_kickoff(
            fixture
        )

        home, away = nome_times(
            fixture
        )

        candidato = {
            "fixture_id": fixture_id,
            "home": home,
            "away": away,
            "kickoff_ts": kickoff,
            "status": fixture.get(
                "status"
            )
        }

        sinal, diagnostico = (
            criar_sinal_combinado(
                candidato
            )
        )

        return jsonify({
            "fixture": candidato,
            "diagnostico": diagnostico,
            "sinal": sinal
        })

    except Exception as e:

        logging.exception(
            "Erro debug sinal"
        )

        return jsonify({
            "erro": str(e)
        }), 500


@app.route("/debug/odds-crua/<int:fixture_id>")
def debug_odds_crua(
    fixture_id
):

    try:

        dados = api_get(
            f"/fixtures/{fixture_id}/odds",
            params={
                "market": "goalline|corner"
            },
            timeout=10
        )

        return jsonify(dados)

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


@app.route("/debug/procurar-odds/<int:fixture_id>")
def debug_procurar_odds(
    fixture_id
):

    try:

        gols, cantos = buscar_odds(
            fixture_id
        )

        return jsonify({
            "fixture_id": fixture_id,
            "gols": gols,
            "cantos": cantos
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


@app.route("/debug/historico")
def debug_historico():

    try:

        dados = buscar_historico_externo()

        resumo = {}

        for chave, resultados in dados.items():

            wins = sum(
                1
                for x in resultados
                if x["resultado"] == "WIN"
            )

            losses = sum(
                1
                for x in resultados
                if x["resultado"] == "LOSS"
            )

            pushes = sum(
                1
                for x in resultados
                if x["resultado"] == "PUSH"
            )

            total = wins + losses

            assertividade = (
                wins / total * 100
                if total
                else 0
            )

            resumo[
                f"{chave[0]}+{chave[1]}"
            ] = {
                "total": len(
                    resultados
                ),
                "wins": wins,
                "losses": losses,
                "push": pushes,
                "assertividade": round(
                    assertividade,
                    2
                )
            }

        return jsonify({
            "dias": DIAS_HISTORICO,
            "linhas": resumo,
            "total_linhas": len(resumo),
            "observacao": (
                "Somente dados reais retornados "
                "pela API são utilizados."
            )
        })

    except Exception as e:

        return jsonify({
            "erro": str(e)
        }), 500


# ============================================================
# INICIAR MONITOR
# ============================================================

if MONITOR_ATIVO:

    thread_monitor = threading.Thread(
        target=monitor_loop,
        daemon=True
    )

    thread_monitor.start()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
                )
