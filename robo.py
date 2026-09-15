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

INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "300"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "180"))

HISTORICO_TTL = int(os.getenv("HISTORICO_TTL", "1800"))
FUTUROS_TTL = int(os.getenv("FUTUROS_TTL", "300"))

MONITOR_ATIVO = os.getenv("MONITOR_ATIVO", "true").lower() in (
    "1",
    "true",
    "yes",
    "sim"
)

ARQUIVO_ESTADO = "estado.json"

MAX_REQUISICOES_MINUTO = 10

# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo")

# ============================================================
# ESTADO
# ============================================================

lock = threading.Lock()
api_lock = threading.Lock()

estado = {
    "sinais": [],
    "resultados": [],
    "ultima_execucao_pre": None,
    "ultima_execucao_resultados": None,
    "api_remaining": None,
    "api_limit": None,
    "api_reset": None
}

if os.path.exists(ARQUIVO_ESTADO):
    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            carregado = json.load(f)

        if isinstance(carregado, dict):
            estado.update(carregado)

    except Exception as e:
        logger.warning("Não foi possível carregar estado.json: %s", e)


def salvar_estado():
    with lock:
        tmp = ARQUIVO_ESTADO + ".tmp"

        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    estado,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(tmp, ARQUIVO_ESTADO)

        except Exception as e:
            logger.error("Erro salvando estado: %s", e)


# ============================================================
# CACHE
# ============================================================

cache_futuros = {
    "timestamp": 0,
    "dados": []
}

cache_historico = {}


# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

def agora_utc():
    return datetime.now(timezone.utc)


def timestamp_utc(dt):
    return int(dt.timestamp())


def numero(valor):
    try:
        if valor is None:
            return None

        if isinstance(valor, bool):
            return None

        return float(valor)

    except Exception:
        return None


def nome_time(time_obj):
    if not isinstance(time_obj, dict):
        return "?"

    return (
        time_obj.get("name")
        or time_obj.get("short_name")
        or time_obj.get("display_name")
        or "?"
    )


def fixture_id(jogo):
    return (
        jogo.get("id")
        or jogo.get("fixture_id")
        or jogo.get("fixtureId")
    )


def status_jogo(jogo):
    status = jogo.get("status")

    if isinstance(status, dict):
        return (
            status.get("type")
            or status.get("short")
            or status.get("name")
            or ""
        ).lower()

    return str(status or "").lower()


def gols_jogo(jogo):
    gols = jogo.get("goals")

    if isinstance(gols, dict):

        total = numero(gols.get("total"))

        if total is not None:
            return total

        home = numero(gols.get("home"))
        away = numero(gols.get("away"))

        if home is not None and away is not None:
            return home + away

    home = numero(jogo.get("home_goals"))
    away = numero(jogo.get("away_goals"))

    if home is not None and away is not None:
        return home + away

    return None


def cantos_jogo(jogo):
    corners = jogo.get("corners")

    if isinstance(corners, dict):

        total = numero(corners.get("total"))

        if total is not None:
            return total

        home = numero(corners.get("home"))
        away = numero(corners.get("away"))

        if home is not None and away is not None:
            return home + away

    total = numero(jogo.get("corner_total"))

    if total is not None:
        return total

    total = numero(jogo.get("corners_total"))

    if total is not None:
        return total

    return None


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None, tentativas=3):

    if not API_KEY:
        logger.error("FIVE_DOLLAR_KEY não configurada.")
        return None

    url = BASE_API.rstrip("/") + "/" + endpoint.lstrip("/")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }

    for tentativa in range(1, tentativas + 1):

        with api_lock:

            try:

                response = requests.get(
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=30
                )

                # ------------------------------------------------
                # RATE LIMIT
                # ------------------------------------------------

                remaining = response.headers.get("X-RateLimit-Remaining")
                limit = response.headers.get("X-RateLimit-Limit")
                reset = response.headers.get("X-RateLimit-Reset")

                with lock:
                    estado["api_remaining"] = remaining
                    estado["api_limit"] = limit
                    estado["api_reset"] = reset

                if response.status_code == 429:

                    retry_after = response.headers.get("Retry-After")

                    try:
                        espera = float(retry_after)
                    except Exception:
                        espera = 30

                    logger.warning(
                        "Rate limit atingido. Aguardando %.1fs...",
                        espera
                    )

                    time.sleep(espera + 1)
                    continue

                if response.status_code != 200:

                    logger.error(
                        "API HTTP %s: %s",
                        response.status_code,
                        response.text[:1000]
                    )

                    return None

                try:
                    dados = response.json()
                except Exception:
                    logger.error("Resposta não é JSON.")
                    return None

                if isinstance(dados, dict):

                    if dados.get("success") in (0, False):

                        logger.error(
                            "API retornou erro: %s",
                            dados
                        )

                        return None

                    if "data" in dados:
                        return dados["data"]

                    return dados

                return dados

            except requests.RequestException as e:

                logger.error(
                    "Erro API tentativa %s/%s: %s",
                    tentativa,
                    tentativas,
                    e
                )

        time.sleep(2)

    return None


# ============================================================
# NORMALIZAÇÃO DA RESPOSTA
# ============================================================

def extrair_lista(data):

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for chave in (
        "fixtures",
        "data",
        "results",
        "items",
        "matches"
    ):
        valor = data.get(chave)

        if isinstance(valor, list):
            return valor

    return []


# ============================================================
# JOGOS FUTUROS
# ============================================================

def buscar_futuros(force=False):

    global cache_futuros

    agora = time.time()

    if (
        not force
        and cache_futuros["dados"]
        and agora - cache_futuros["timestamp"] < FUTUROS_TTL
    ):
        return cache_futuros["dados"]

    inicio = agora_utc() + timedelta(minutes=HORAS_MIN * 60)
    fim = agora_utc() + timedelta(hours=HORAS_MAX)

    params = {
        "start_time": timestamp_utc(inicio),
        "end_time": timestamp_utc(fim),
        "status": "scheduled",
        "include": "odds",
        "per_page": 50
    }

    logger.info(
        "Buscando jogos futuros entre %s e %s",
        inicio.isoformat(),
        fim.isoformat()
    )

    data = api_get("fixtures", params)

    if data is None:
        return []

    jogos = extrair_lista(data)

    # Segurança adicional
    filtrados = []

    for jogo in jogos:

        status = status_jogo(jogo)

        if status in (
            "finished",
            "in_play",
            "live",
            "ft"
        ):
            continue

        filtrados.append(jogo)

    cache_futuros = {
        "timestamp": agora,
        "dados": filtrados
    }

    logger.info(
        "Jogos futuros encontrados: %s",
        len(filtrados)
    )

    return filtrados


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico_liga(league_id, force=False):

    if not league_id:
        return []

    agora = time.time()

    cache = cache_historico.get(str(league_id))

    if (
        not force
        and cache
        and agora - cache["timestamp"] < HISTORICO_TTL
    ):
        return cache["dados"]

    fim = agora_utc()
    inicio = fim - timedelta(days=7)

    params = {
        "start_time": timestamp_utc(inicio),
        "end_time": timestamp_utc(fim),
        "status": "finished",
        "include": "odds",
        "per_page": 50
    }

    logger.info(
        "Buscando histórico liga %s",
        league_id
    )

    data = api_get(
        f"leagues/{league_id}/fixtures",
        params
    )

    if data is None:
        return []

    jogos = extrair_lista(data)

    cache_historico[str(league_id)] = {
        "timestamp": agora,
        "dados": jogos
    }

    logger.info(
        "Histórico liga %s: %s jogos",
        league_id,
        len(jogos)
    )

    return jogos


# ============================================================
# ODDS / MERCADOS
# ============================================================

def percorrer_objeto(obj):

    if isinstance(obj, dict):

        yield obj

        for valor in obj.values():
            yield from percorrer_objeto(valor)

    elif isinstance(obj, list):

        for item in obj:
            yield from percorrer_objeto(item)


def encontrar_linha_odd(obj, tipo):

    """
    Procura estruturas de mercado de forma flexível.

    tipo:
        goal
        corner
    """

    if not isinstance(obj, dict):
        return []

    encontrados = []

    palavras_goal = [
        "goal",
        "goals",
        "total_goals",
        "goal_line",
        "over_under"
    ]

    palavras_corner = [
        "corner",
        "corners",
        "corner_line",
        "total_corners"
    ]

    palavras = palavras_goal if tipo == "goal" else palavras_corner

    for chave, valor in obj.items():

        chave_lower = str(chave).lower()

        if not any(p in chave_lower for p in palavras):
            continue

        if isinstance(valor, dict):

            encontrados.extend(
                analisar_mercado_dict(valor)
            )

        elif isinstance(valor, list):

            for item in valor:

                if isinstance(item, dict):
                    encontrados.extend(
                        analisar_mercado_dict(item)
                    )

    return encontrados


def analisar_mercado_dict(obj):

    resultados = []

    if not isinstance(obj, dict):
        return resultados

    linha = (
        numero(obj.get("line"))
        or numero(obj.get("total"))
        or numero(obj.get("value"))
        or numero(obj.get("points"))
    )

    over = (
        numero(obj.get("over"))
        or numero(obj.get("over_odds"))
        or numero(obj.get("over_price"))
    )

    odd = (
        numero(obj.get("price"))
        or numero(obj.get("odds"))
        or numero(obj.get("odd"))
    )

    if linha is not None:

        if over is not None:
            resultados.append({
                "linha": linha,
                "odd": over
            })

        elif odd is not None:
            resultados.append({
                "linha": linha,
                "odd": odd
            })

    # Formatos alternativos
    for chave, valor in obj.items():

        if isinstance(valor, dict):

            sublinha = (
                numero(valor.get("line"))
                or numero(valor.get("total"))
                or numero(valor.get("value"))
            )

            subodd = (
                numero(valor.get("price"))
                or numero(valor.get("odds"))
                or numero(valor.get("odd"))
            )

            nome = str(chave).lower()

            if (
                sublinha is not None
                and subodd is not None
                and (
                    "over" in nome
                    or "above" in nome
                )
            ):
                resultados.append({
                    "linha": sublinha,
                    "odd": subodd
                })

    return resultados


def obter_mercados(jogo):

    odds = jogo.get("odds")

    if not odds:
        return []

    mercados = []

    # --------------------------------------------------------
    # OVER GOLS
    # --------------------------------------------------------

    goals = encontrar_linha_odd(
        odds,
        "goal"
    )

    for mercado in goals:

        linha = mercado["linha"]
        odd = mercado["odd"]

        if (
            linha >= 1.5
            and odd >= ODD_MIN
            and odd <= ODD_MAX
        ):
            mercados.append({
                "tipo": "gols",
                "linha": linha,
                "odd": odd
            })

    # --------------------------------------------------------
    # OVER ESCANTEIOS
    # --------------------------------------------------------

    corners = encontrar_linha_odd(
        odds,
        "corner"
    )

    for mercado in corners:

        linha = mercado["linha"]
        odd = mercado["odd"]

        if (
            linha >= 6.5
            and odd >= ODD_MIN
            and odd <= ODD_MAX
        ):
            mercados.append({
                "tipo": "escanteios",
                "linha": linha,
                "odd": odd
            })

    return mercados


# ============================================================
# MELHOR MERCADO
# ============================================================

def selecionar_mercados(jogo):

    mercados = obter_mercados(jogo)

    gols = [
        m for m in mercados
        if m["tipo"] == "gols"
    ]

    corners = [
        m for m in mercados
        if m["tipo"] == "escanteios"
    ]

    if not gols or not corners:
        return None

    # Prioriza linhas comuns
    gols.sort(
        key=lambda x: (
            abs(x["linha"] - 2.5),
            x["odd"]
        )
    )

    corners.sort(
        key=lambda x: (
            abs(x["linha"] - 8.5),
            x["odd"]
        )
    )

    return {
        "gols": gols[0],
        "escanteios": corners[0]
    }


# ============================================================
# HISTÓRICO DO COMBINADO
# ============================================================

def avaliar_historico(
    historico,
    linha_gols,
    linha_cantos
):

    decisivos = []

    for jogo in historico:

        if status_jogo(jogo) not in (
            "finished",
            "ft",
            "final"
        ):
            continue

        gols = gols_jogo(jogo)
        cantos = cantos_jogo(jogo)

        if gols is None or cantos is None:
            continue

        bateu_gols = gols > linha_gols
        bateu_cantos = cantos > linha_cantos

        combinado = (
            bateu_gols
            and bateu_cantos
        )

        decisivos.append({
            "fixture_id": fixture_id(jogo),
            "gols": gols,
            "cantos": cantos,
            "bateu_gols": bateu_gols,
            "bateu_cantos": bateu_cantos,
            "combinado": combinado
        })

    total = len(decisivos)

    if total == 0:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "assertividade": 0
        }

    wins = sum(
        1
        for x in decisivos
        if x["combinado"]
    )

    losses = total - wins

    assertividade = (
        wins / total
    ) * 100

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "assertividade": round(
            assertividade,
            2
        )
    }


# ============================================================
# CRIAÇÃO DO SINAL
# ============================================================

def gerar_sinal(jogo, historico):

    mercados = selecionar_mercados(jogo)

    if not mercados:
        return None

    mercado_gols = mercados["gols"]
    mercado_cantos = mercados["escanteios"]

    linha_gols = mercado_gols["linha"]
    linha_cantos = mercado_cantos["linha"]

    analise = avaliar_historico(
        historico,
        linha_gols,
        linha_cantos
    )

    if analise["total"] < MINIMO_HISTORICO:
        return None

    if analise["assertividade"] < ASSERTIVIDADE_MINIMA:
        return None

    odd_gols = mercado_gols["odd"]
    odd_cantos = mercado_cantos["odd"]

    odd_combinada = odd_gols * odd_cantos

    if (
        odd_combinada < ODD_MIN
        or odd_combinada > ODD_MAX
    ):
        return None

    fid = fixture_id(jogo)

    casa = nome_time(
        jogo.get("home")
        or jogo.get("home_team")
        or {}
    )

    fora = nome_time(
        jogo.get("away")
        or jogo.get("away_team")
        or {}
    )

    data_jogo = (
        jogo.get("start_time")
        or jogo.get("date")
        or jogo.get("datetime")
    )

    sinal = {
        "fixture_id": fid,
        "league_id": (
            jogo.get("league_id")
            or (
                jogo.get("league", {}).get("id")
                if isinstance(jogo.get("league"), dict)
                else None
            )
        ),
        "liga": (
            jogo.get("league", {}).get("name")
            if isinstance(jogo.get("league"), dict)
            else jogo.get("league_name", "?")
        ),
        "casa": casa,
        "fora": fora,
        "data": data_jogo,

        "mercado": "OVER GOLS + OVER ESCANTEIOS",

        "linha_gols": linha_gols,
        "linha_cantos": linha_cantos,

        "odd_gols": odd_gols,
        "odd_cantos": odd_cantos,
        "odd_combinada": round(
            odd_combinada,
            2
        ),

        "historico_jogos": analise["total"],
        "historico_wins": analise["wins"],
        "historico_losses": analise["losses"],

        "assertividade": analise["assertividade"],

        "criado_em": agora_utc().isoformat(),

        "status": "PENDENTE",
        "resultado": None
    }

    return sinal


# ============================================================
# DUPLICIDADE
# ============================================================

def sinal_ja_registrado(fid):

    with lock:

        for sinal in estado["sinais"]:

            if str(
                sinal.get("fixture_id")
            ) == str(fid):

                return True

    return False


# ============================================================
# TELEGRAM
# ============================================================

def telegram_enviar(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML"
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if response.status_code != 200:

            logger.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text
            )

            return False

        return True

    except Exception as e:

        logger.error(
            "Erro Telegram: %s",
            e
        )

        return False


def mensagem_sinal(sinal):

    return (
        "🚨 <b>NOVO SINAL</b>\n\n"

        f"⚽ <b>{sinal['casa']}</b> x "
        f"<b>{sinal['fora']}</b>\n"

        f"🏆 {sinal['liga']}\n\n"

        "🎯 <b>COMBINADO</b>\n"
        f"⚽ Over {sinal['linha_gols']} gols\n"
        f"🚩 Over {sinal['linha_cantos']} escanteios\n\n"

        f"💰 Odd gols: {sinal['odd_gols']:.2f}\n"
        f"💰 Odd escanteios: {sinal['odd_cantos']:.2f}\n"
        f"🔥 <b>Odd combinada: {sinal['odd_combinada']:.2f}</b>\n\n"

        f"📊 Histórico: {sinal['historico_jogos']} jogos\n"
        f"✅ Acertos: {sinal['historico_wins']}\n"
        f"❌ Erros: {sinal['historico_losses']}\n"
        f"🎯 <b>Assertividade: {sinal['assertividade']:.2f}%</b>\n\n"

        "⏳ Aguardando resultado..."
    )


def mensagem_resultado(sinal, resultado):

    emoji = {
        "WIN": "🟢",
        "LOSS": "🔴",
        "PUSH": "🟡"
    }.get(resultado, "⚪")

    return (
        f"{emoji} <b>RESULTADO DO SINAL</b>\n\n"

        f"⚽ {sinal['casa']} x "
        f"{sinal['fora']}\n\n"

        f"🎯 Over {sinal['linha_gols']} gols\n"
        f"🚩 Over {sinal['linha_cantos']} escanteios\n\n"

        f"📊 Gols: {sinal.get('gols_resultado', '?')}\n"
        f"🚩 Escanteios: {sinal.get('cantos_resultado', '?')}\n\n"

        f"<b>{resultado}</b>"
    )


# ============================================================
# REGISTRAR SINAIS
# ============================================================

def registrar_sinais():

    jogos = buscar_futuros()

    if not jogos:
        logger.info(
            "Nenhum jogo futuro disponível."
        )
        return

    # --------------------------------------------------------
    # Organiza por liga
    # --------------------------------------------------------

    ligas = {}

    for jogo in jogos:

        league = jogo.get("league")

        if isinstance(league, dict):
            league_id = league.get("id")
        else:
            league_id = (
                jogo.get("league_id")
            )

        if not league_id:
            continue

        ligas.setdefault(
            str(league_id),
            []
        ).append(jogo)

    # --------------------------------------------------------
    # Histórico
    # --------------------------------------------------------

    historicos = {}

    for league_id in ligas:

        historicos[league_id] = (
            buscar_historico_liga(
                league_id
            )
        )

    novos = []

    # --------------------------------------------------------
    # Geração
    # --------------------------------------------------------

    for jogo in jogos:

        fid = fixture_id(jogo)

        if not fid:
            continue

        if sinal_ja_registrado(fid):
            continue

        league = jogo.get("league")

        if isinstance(league, dict):
            league_id = league.get("id")
        else:
            league_id = jogo.get("league_id")

        historico = historicos.get(
            str(league_id),
            []
        )

        sinal = gerar_sinal(
            jogo,
            historico
        )

        if sinal:

            novos.append(sinal)

    # --------------------------------------------------------
    # Ordenação por assertividade
    # --------------------------------------------------------

    novos.sort(
        key=lambda x: (
            x["assertividade"],
            x["historico_jogos"]
        ),
        reverse=True
    )

    novos = novos[:QTD_POR_RODADA]

    # --------------------------------------------------------
    # Salva e envia
    # --------------------------------------------------------

    for sinal in novos:

        with lock:
            estado["sinais"].append(
                sinal
            )

        salvar_estado()

        enviado = telegram_enviar(
            mensagem_sinal(sinal)
        )

        if enviado:
            logger.info(
                "Sinal enviado: %s x %s | %.2f%%",
                sinal["casa"],
                sinal["fora"],
                sinal["assertividade"]
            )

    logger.info(
        "Novos sinais nesta rodada: %s",
        len(novos)
    )


# ============================================================
# BUSCAR DETALHES DE UM FIXTURE
# ============================================================

def buscar_fixture(fid):

    if not fid:
        return None

    return api_get(
        f"fixtures/{fid}"
    )


# ============================================================
# RESOLUÇÃO
# ============================================================

def resolver_sinal(sinal):

    fid = sinal.get("fixture_id")

    jogo = buscar_fixture(fid)

    if not jogo:
        return None

    if isinstance(jogo, dict):

        # algumas respostas vêm dentro de data
        if isinstance(jogo.get("data"), dict):
            jogo = jogo["data"]

    status = status_jogo(jogo)

    if status not in (
        "finished",
        "ft",
        "final"
    ):
        return None

    gols = gols_jogo(jogo)
    cantos = cantos_jogo(jogo)

    if gols is None or cantos is None:
        return None

    linha_gols = numero(
        sinal.get("linha_gols")
    )

    linha_cantos = numero(
        sinal.get("linha_cantos")
    )

    if linha_gols is None or linha_cantos is None:
        return None

    gols_ok = gols > linha_gols
    cantos_ok = cantos > linha_cantos

    # --------------------------------------------------------
    # Resultado
    # --------------------------------------------------------

    if gols_ok and cantos_ok:
        resultado = "WIN"

    elif (
        gols == linha_gols
        or cantos == linha_cantos
    ):
        resultado = "PUSH"

    else:
        resultado = "LOSS"

    sinal["gols_resultado"] = gols
    sinal["cantos_resultado"] = cantos
    sinal["resultado"] = resultado
    sinal["status"] = "RESOLVIDO"
    sinal["resolvido_em"] = agora_utc().isoformat()

    return resultado


def resolver_pendentes():

    with lock:
        pendentes = [
            s for s in estado["sinais"]
            if s.get("status") == "PENDENTE"
        ]

    if not pendentes:
        return

    logger.info(
        "Verificando %s sinais pendentes...",
        len(pendentes)
    )

    for sinal in pendentes:

        try:

            resultado = resolver_sinal(
                sinal
            )

            if resultado:

                with lock:
                    estado["resultados"].append({
                        "fixture_id": sinal.get(
                            "fixture_id"
                        ),
                        "resultado": resultado,
                        "gols": sinal.get(
                            "gols_resultado"
                        ),
                        "cantos": sinal.get(
                            "cantos_resultado"
                        ),
                        "data": agora_utc().isoformat()
                    })

                salvar_estado()

                telegram_enviar(
                    mensagem_resultado(
                        sinal,
                        resultado
                    )
                )

                logger.info(
                    "Resultado: %s | %s x %s",
                    resultado,
                    sinal["casa"],
                    sinal["fora"]
                )

        except Exception as e:

            logger.exception(
                "Erro resolvendo sinal: %s",
                e
            )


# ============================================================
# ESTATÍSTICAS
# ============================================================

def estatisticas():

    with lock:

        resultados = [
            r.get("resultado")
            for r in estado["resultados"]
        ]

    wins = resultados.count("WIN")
    losses = resultados.count("LOSS")
    push = resultados.count("PUSH")

    resolvidos = wins + losses

    assertividade = (
        wins / resolvidos * 100
        if resolvidos
        else 0
    )

    return {
        "wins": wins,
        "losses": losses,
        "push": push,
        "resolvidos": resolvidos,
        "assertividade": round(
            assertividade,
            2
        )
    }


# ============================================================
# MONITOR
# ============================================================

def monitor():

    logger.info(
        "Monitor iniciado."
    )

    ultima_pre = 0
    ultima_resultados = 0

    while True:

        try:

            agora = time.time()

            # ------------------------------------------------
            # SINAIS
            # ------------------------------------------------

            if (
                agora - ultima_pre
                >= INTERVALO_PRE
            ):

                logger.info(
                    "Executando análise pré-jogo..."
                )

                registrar_sinais()

                ultima_pre = agora

                with lock:
                    estado[
                        "ultima_execucao_pre"
                    ] = agora_utc().isoformat()

                salvar_estado()

            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            if (
                agora - ultima_resultados
                >= INTERVALO_RESULTADOS
            ):

                resolver_pendentes()

                ultima_resultados = agora

                with lock:
                    estado[
                        "ultima_execucao_resultados"
                    ] = agora_utc().isoformat()

                salvar_estado()

        except Exception as e:

            logger.exception(
                "Erro no monitor: %s",
                e
            )

        time.sleep(5)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def index():

    stats = estatisticas()

    with lock:

        pendentes = sum(
            1
            for s in estado["sinais"]
            if s.get("status") == "PENDENTE"
        )

    return jsonify({
        "bot": "Robo Futebol Combinado v6",
        "status": "online",

        "estrategia": (
            "OVER GOLS + OVER ESCANTEIOS"
        ),

        "historico_dias": 7,

        "minimo_historico":
            MINIMO_HISTORICO,

        "assertividade_minima":
            ASSERTIVIDADE_MINIMA,

        "odd_min":
            ODD_MIN,

        "odd_max":
            ODD_MAX,

        "max_sinais":
            QTD_POR_RODADA,

        "janela_horas": [
            HORAS_MIN,
            HORAS_MAX
        ],

        "monitor":
            MONITOR_ATIVO,

        "pendentes":
            pendentes,

        "estatisticas":
            stats,

        "api_limit":
            estado.get("api_limit"),

        "api_remaining":
            estado.get("api_remaining")
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "timestamp": agora_utc().isoformat()
    })


@app.route("/status")
def status():

    with lock:

        return jsonify({
            "status": "online",
            "monitor": MONITOR_ATIVO,
            "sinais": len(
                estado["sinais"]
            ),
            "pendentes": sum(
                1
                for s in estado["sinais"]
                if s.get("status") == "PENDENTE"
            ),
            "resultados": len(
                estado["resultados"]
            ),
            "api_limit":
                estado.get("api_limit"),
            "api_remaining":
                estado.get("api_remaining"),
            "api_reset":
                estado.get("api_reset"),
            "estatisticas":
                estatisticas()
        })


@app.route("/pendentes")
def pendentes():

    with lock:

        dados = [
            s
            for s in estado["sinais"]
            if s.get("status") == "PENDENTE"
        ]

    return jsonify(dados)


@app.route("/debug/futuros")
def debug_futuros():

    jogos = buscar_futuros(
        force=True
    )

    resultado = []

    for jogo in jogos:

        mercados = obter_mercados(
            jogo
        )

        resultado.append({
            "id": fixture_id(jogo),

            "casa": nome_time(
                jogo.get("home")
                or {}
            ),

            "fora": nome_time(
                jogo.get("away")
                or {}
            ),

            "status":
                status_jogo(jogo),

            "data":
                jogo.get("start_time")
                or jogo.get("date"),

            "league":
                jogo.get("league"),

            "mercados":
                mercados
        })

    return jsonify(resultado)


@app.route("/debug/sinais")
def debug_sinais():

    jogos = buscar_futuros(
        force=True
    )

    resultado = []

    for jogo in jogos:

        fid = fixture_id(jogo)

        league = jogo.get("league")

        if isinstance(league, dict):
            league_id = league.get("id")
        else:
            league_id = jogo.get("league_id")

        historico = buscar_historico_liga(
            league_id
        )

        mercados = selecionar_mercados(
            jogo
        )

        analise = None

        if mercados:

            analise = avaliar_historico(
                historico,
                mercados["gols"]["linha"],
                mercados["escanteios"]["linha"]
            )

        resultado.append({
            "fixture_id": fid,
            "casa": nome_time(
                jogo.get("home") or {}
            ),
            "fora": nome_time(
                jogo.get("away") or {}
            ),
            "mercados": mercados,
            "historico": analise
        })

    return jsonify(resultado)


@app.route("/debug/historico")
def debug_historico():

    jogos = buscar_futuros()

    ligas = {}

    for jogo in jogos:

        league = jogo.get("league")

        if isinstance(league, dict):
            league_id = league.get("id")
        else:
            league_id = jogo.get("league_id")

        if league_id:
            ligas[str(league_id)] = True

    resultado = {}

    for league_id in ligas:

        historico = buscar_historico_liga(
            league_id
        )

        resultado[league_id] = {
            "jogos": len(historico)
        }

    return jsonify(resultado)


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    try:

        registrar_sinais()

        return jsonify({
            "ok": True,
            "mensagem":
                "Análise executada."
        })

    except Exception as e:

        logger.exception(
            "Erro debug:"
        )

        return jsonify({
            "ok": False,
            "erro": str(e)
        }), 500


@app.route("/debug/api")
def debug_api():

    data = api_get(
        "status"
    )

    return jsonify({
        "ok": data is not None,
        "data": data,
        "api_limit":
            estado.get("api_limit"),
        "api_remaining":
            estado.get("api_remaining"),
        "api_reset":
            estado.get("api_reset")
    })


# ============================================================
# INICIALIZAÇÃO
# ============================================================

monitor_thread = None

if MONITOR_ATIVO:

    monitor_thread = threading.Thread(
        target=monitor,
        daemon=True
    )

    monitor_thread.start()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
