import os
import json
import time
import html
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÕES
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("FIVE_DOLLAR_API_KEY", "").strip()

ARQUIVO_STATS = "stats.json"

# Limite oficial informado anteriormente: aproximadamente 10/minuto.
# Usamos 8 para manter margem de segurança.
LIMITE_REQUISICOES_MINUTO = int(
    os.getenv("LIMITE_REQUISICOES_MINUTO", "8")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "600")
)

INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "300")
)

MAX_JOGOS_ANALISAR = int(
    os.getenv("MAX_JOGOS_ANALISAR", "5")
)

MAX_SINAIS_POR_CICLO = int(
    os.getenv("MAX_SINAIS_POR_CICLO", "2")
)

# Percentual mínimo para enviar sinal
ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "72")
)

# Mercados
LINHA_GOLS = 2.5
LINHA_ESCANTEIOS = 8.5


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("ROBO_COMBINADO")


# ============================================================
# CONTROLE DE REQUISIÇÕES
# ============================================================

request_times = []
request_lock = threading.Lock()


def pode_fazer_request():
    """
    Controla o limite de requisições por minuto.

    Quando o limite é atingido, o robô aguarda automaticamente
    em vez de continuar gerando erros.
    """

    while True:
        agora = time.time()

        with request_lock:
            limite_inicio = agora - 60

            while request_times and request_times[0] < limite_inicio:
                request_times.pop(0)

            if len(request_times) < LIMITE_REQUISICOES_MINUTO:
                request_times.append(agora)
                return True

            tempo_espera = 60 - (agora - request_times[0])
            tempo_espera = max(1, int(tempo_espera) + 1)

        logger.warning(
            "Limite interno atingido. Aguardando %s segundos.",
            tempo_espera
        )

        time.sleep(tempo_espera)


# ============================================================
# ARQUIVO DE ESTATÍSTICAS
# ============================================================

stats_lock = threading.Lock()

stats_padrao = {
    "sinais_enviados": 0,
    "sinais_resolvidos": 0,
    "wins": 0,
    "losses": 0,
    "pendentes": 0,
    "assertividade": 0,
    "ultima_atualizacao": None,
    "historico": []
}


def carregar_stats():
    if not os.path.exists(ARQUIVO_STATS):
        return stats_padrao.copy()

    try:
        with open(ARQUIVO_STATS, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)

        for chave, valor in stats_padrao.items():
            if chave not in dados:
                dados[chave] = valor

        return dados

    except Exception as erro:
        logger.error("Erro ao carregar stats: %s", erro)
        return stats_padrao.copy()


stats = carregar_stats()


def salvar_stats():
    with stats_lock:
        try:
            with open(
                ARQUIVO_STATS,
                "w",
                encoding="utf-8"
            ) as arquivo:
                json.dump(
                    stats,
                    arquivo,
                    ensure_ascii=False,
                    indent=2
                )

        except Exception as erro:
            logger.error("Erro ao salvar stats: %s", erro)


def atualizar_assertividade():
    resolvidos = stats["wins"] + stats["losses"]

    if resolvidos > 0:
        stats["assertividade"] = round(
            stats["wins"] / resolvidos * 100,
            2
        )
    else:
        stats["assertividade"] = 0


# ============================================================
# CACHE DE HISTÓRICO
# ============================================================

cache_historico = {}
cache_lock = threading.Lock()

CACHE_MINUTOS = 60


def obter_cache(chave):
    with cache_lock:
        item = cache_historico.get(chave)

        if not item:
            return None

        horario, dados = item

        if time.time() - horario > CACHE_MINUTOS * 60:
            del cache_historico[chave]
            return None

        return dados


def salvar_cache(chave, dados):
    with cache_lock:
        cache_historico[chave] = (
            time.time(),
            dados
        )


# ============================================================
# REQUISIÇÃO À API
# ============================================================

def api_get(endpoint, params=None):
    """
    Faz requisição segura à API.

    O status da API deve ser:
    all, scheduled, live, finished ou unknown.
    """

    pode_fazer_request()

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    headers = {
        "Accept": "application/json"
    }

    if API_KEY:
        headers["X-API-Key"] = API_KEY
        headers["Authorization"] = f"Bearer {API_KEY}"

    try:
        resposta = requests.get(
            url,
            headers=headers,
            params=params or {},
            timeout=30
        )

        if resposta.status_code == 429:
            logger.warning(
                "API retornou 429. Aguardando 60 segundos."
            )
            time.sleep(60)
            return None

        if resposta.status_code >= 400:
            logger.error(
                "API HTTP %s: %s",
                resposta.status_code,
                resposta.text[:500]
            )
            return None

        try:
            dados = resposta.json()
        except Exception:
            logger.error("Resposta não é JSON válido.")
            return None

        if isinstance(dados, dict):
            if dados.get("success") == 0:
                erro = dados.get("error", {})
                logger.error(
                    "Erro da API: %s",
                    erro
                )
                return None

        return dados

    except requests.RequestException as erro:
        logger.error("Erro de conexão com API: %s", erro)
        return None

    except Exception as erro:
        logger.error("Erro inesperado na API: %s", erro)
        return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning(
            "TELEGRAM_TOKEN ou CHAT_ID não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    dados = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        resposta = requests.post(
            url,
            json=dados,
            timeout=30
        )

        if resposta.status_code != 200:
            logger.error(
                "Erro Telegram HTTP %s: %s",
                resposta.status_code,
                resposta.text[:500]
            )
            return False

        retorno = resposta.json()

        if not retorno.get("ok"):
            logger.error(
                "Telegram rejeitou mensagem: %s",
                retorno
            )
            return False

        logger.info("Mensagem enviada ao Telegram.")
        return True

    except requests.RequestException as erro:
        logger.error("Erro ao enviar Telegram: %s", erro)
        return False


# ============================================================
# BUSCAR JOGOS
# ============================================================

def buscar_jogos():
    """
    Busca jogos agendados e ao vivo.

    Não utiliza status inválido como 'upcoming'.
    """

    jogos = []

    for status in ["live", "scheduled"]:
        dados = api_get(
            "fixtures",
            params={
                "status": status
            }
        )

        if not dados:
            continue

        if isinstance(dados, dict):
            lista = (
                dados.get("data")
                or dados.get("fixtures")
                or dados.get("results")
                or []
            )
        elif isinstance(dados, list):
            lista = dados
        else:
            lista = []

        if isinstance(lista, list):
            jogos.extend(lista)

    # Remove duplicados
    resultado = []
    ids_vistos = set()

    for jogo in jogos:
        fixture_id = extrair_id_jogo(jogo)

        if fixture_id is None:
            continue

        if fixture_id in ids_vistos:
            continue

        ids_vistos.add(fixture_id)
        resultado.append(jogo)

    return resultado


def extrair_id_jogo(jogo):
    if not isinstance(jogo, dict):
        return None

    return (
        jogo.get("id")
        or jogo.get("fixture_id")
        or jogo.get("fixtureId")
        or jogo.get("match_id")
    )


def extrair_nome_time(jogo, casa=True):
    if not isinstance(jogo, dict):
        return "Time desconhecido"

    home = (
        jogo.get("home_team")
        or jogo.get("homeTeam")
        or jogo.get("home")
        or {}
    )

    away = (
        jogo.get("away_team")
        or jogo.get("awayTeam")
        or jogo.get("away")
        or {}
    )

    time = home if casa else away

    if isinstance(time, dict):
        return (
            time.get("name")
            or time.get("short_name")
            or time.get("shortName")
            or "Time desconhecido"
        )

    if isinstance(time, str):
        return time

    return "Time desconhecido"


# ============================================================
# HISTÓRICO DO JOGO
# ============================================================

def buscar_historico(fixture_id):
    chave = f"historico_{fixture_id}"

    cache = obter_cache(chave)

    if cache is not None:
        return cache

    endpoints = [
        f"fixtures/{fixture_id}/statistics",
        f"fixtures/{fixture_id}/events"
    ]

    resultado = {}

    for endpoint in endpoints:
        dados = api_get(endpoint)

        if dados:
            resultado[endpoint] = dados

    salvar_cache(chave, resultado)

    return resultado


# ============================================================
# EXTRAÇÃO DE ESTATÍSTICAS
# ============================================================

def numero(dados, chaves, padrao=0):
    if not isinstance(dados, dict):
        return padrao

    for chave in chaves:
        valor = dados.get(chave)

        if isinstance(valor, (int, float)):
            return valor

        if isinstance(valor, str):
            try:
                return float(valor.replace(",", "."))
            except ValueError:
                pass

    return padrao


def extrair_estatisticas(jogo, historico):
    """
    Tenta extrair os números mais comuns retornados pelas APIs.
    """

    gols_casa = numero(
        jogo,
        [
            "home_goals",
            "homeGoals",
            "goals_home",
            "score_home"
        ]
    )

    gols_fora = numero(
        jogo,
        [
            "away_goals",
            "awayGoals",
            "goals_away",
            "score_away"
        ]
    )

    escanteios_casa = numero(
        jogo,
        [
            "home_corners",
            "homeCorners",
            "corners_home"
        ]
    )

    escanteios_fora = numero(
        jogo,
        [
            "away_corners",
            "awayCorners",
            "corners_away"
        ]
    )

    finalizados = {
        "gols_casa": gols_casa,
        "gols_fora": gols_fora,
        "escanteios_casa": escanteios_casa,
        "escanteios_fora": escanteios_fora
    }

    # Algumas APIs retornam estatísticas dentro do histórico
    if isinstance(historico, dict):
        texto = json.dumps(
            historico,
            ensure_ascii=False
        ).lower()

        # Apenas sinaliza que há dados históricos.
        # Não inventa números que não foram retornados.
        finalizados["tem_historico"] = len(texto) > 10
    else:
        finalizados["tem_historico"] = False

    return finalizados


# ============================================================
# ANÁLISE DO SINAL
# ============================================================

def analisar_jogo(jogo):
    fixture_id = extrair_id_jogo(jogo)

    if fixture_id is None:
        return None

    nome_casa = extrair_nome_time(jogo, True)
    nome_fora = extrair_nome_time(jogo, False)

    historico = buscar_historico(fixture_id)

    estatisticas = extrair_estatisticas(
        jogo,
        historico
    )

    gols_casa = estatisticas["gols_casa"]
    gols_fora = estatisticas["gols_fora"]

    escanteios_casa = estatisticas["escanteios_casa"]
    escanteios_fora = estatisticas["escanteios_fora"]

    gols_total = gols_casa + gols_fora
    escanteios_total = (
        escanteios_casa + escanteios_fora
    )

    # Indicadores
    prob_gols = 0
    prob_escanteios = 0

    # Análise conservadora:
    # sem estatística suficiente, não força sinal.
    if gols_total >= 1:
        prob_gols += 25

    if gols_total >= 2:
        prob_gols += 20

    if escanteios_total >= 4:
        prob_escanteios += 20

    if escanteios_total >= 6:
        prob_escanteios += 20

    if estatisticas["tem_historico"]:
        prob_gols += 10
        prob_escanteios += 10

    assertividade = (
        prob_gols * 0.5
        + prob_escanteios * 0.5
    )

    assertividade = min(95, round(assertividade, 2))

    mercado = (
        f"Over {LINHA_GOLS} gols "
        f"+ Over {LINHA_ESCANTEIOS} escanteios"
    )

    # Filtros mínimos
    if not estatisticas["tem_historico"]:
        return None

    if assertividade < ASSERTIVIDADE_MINIMA:
        return None

    return {
        "fixture_id": fixture_id,
        "casa": nome_casa,
        "fora": nome_fora,
        "mercado": mercado,
        "assertividade": assertividade,
        "gols_total": gols_total,
        "escanteios_total": escanteios_total,
        "horario": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": "pendente"
    }


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def formatar_sinal(sinal):
    return (
        "🚨 <b>SINAL COMBINADO</b>\n\n"
        f"⚽ <b>{html.escape(sinal['casa'])}</b> "
        f"x "
        f"<b>{html.escape(sinal['fora'])}</b>\n\n"
        f"🎯 Mercado: <b>{sinal['mercado']}</b>\n"
        f"📊 Assertividade estimada: "
        f"<b>{sinal['assertividade']}%</b>\n"
        f"⚽ Gols atuais: {sinal['gols_total']}\n"
        f"🚩 Escanteios atuais: "
        f"{sinal['escanteios_total']}\n\n"
        "⚠️ Sinal estatístico. "
        "Não existe garantia de green."
    )


# ============================================================
# CONTROLE DE DUPLICIDADE
# ============================================================

sinais_enviados = {}
sinais_lock = threading.Lock()


def sinal_ja_enviado(fixture_id):
    with sinais_lock:
        return fixture_id in sinais_enviados


def registrar_sinal_enviado(sinal):
    with sinais_lock:
        sinais_enviados[
            sinal["fixture_id"]
        ] = time.time()


# ============================================================
# CICLO DE ANÁLISE
# ============================================================

def ciclo_analise():
    logger.info("Iniciando ciclo de análise.")

    jogos = buscar_jogos()

    if not jogos:
        logger.info("Nenhum jogo encontrado.")
        return

    logger.info(
        "Jogos encontrados: %s | Jogos analisados: %s",
        len(jogos),
        min(len(jogos), MAX_JOGOS_ANALISAR)
    )

    sinais_ciclo = 0

    # Limita a quantidade de jogos para evitar excesso de API
    jogos_limitados = jogos[:MAX_JOGOS_ANALISAR]

    for jogo in jogos_limitados:
        if sinais_ciclo >= MAX_SINAIS_POR_CICLO:
            break

        try:
            sinal = analisar_jogo(jogo)

            if not sinal:
                continue

            fixture_id = sinal["fixture_id"]

            if sinal_ja_enviado(fixture_id):
                logger.info(
                    "Sinal já enviado para jogo %s.",
                    fixture_id
                )
                continue

            mensagem = formatar_sinal(sinal)

            enviado = enviar_telegram(mensagem)

            if enviado:
                registrar_sinal_enviado(sinal)

                with stats_lock:
                    stats["sinais_enviados"] += 1
                    stats["pendentes"] += 1
                    stats["ultima_atualizacao"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    stats["historico"].append(sinal)

                    # Mantém somente os últimos 200 registros
                    stats["historico"] = (
                        stats["historico"][-200:]
                    )

                salvar_stats()

                sinais_ciclo += 1

                logger.info(
                    "Sinal enviado: %s x %s | %s%%",
                    sinal["casa"],
                    sinal["fora"],
                    sinal["assertividade"]
                )

        except Exception as erro:
            logger.exception(
                "Erro analisando jogo: %s",
                erro
            )

    logger.info(
        "Ciclo concluído. Sinais enviados: %s",
        sinais_ciclo
    )


# ============================================================
# RESOLUÇÃO DE RESULTADOS
# ============================================================

def buscar_resultados():
    """
    Busca partidas finalizadas.

    Usa status=finished, que é aceito pela API.
    """

    dados = api_get(
        "fixtures",
        params={
            "status": "finished"
        }
    )

    if not dados:
        return []

    if isinstance(dados, dict):
        resultados = (
            dados.get("data")
            or dados.get("fixtures")
            or dados.get("results")
            or []
        )
    elif isinstance(dados, list):
        resultados = dados
    else:
        resultados = []

    return resultados


def extrair_resultado(jogo):
    gols_casa = numero(
        jogo,
        [
            "home_goals",
            "homeGoals",
            "goals_home",
            "score_home"
        ]
    )

    gols_fora = numero(
        jogo,
        [
            "away_goals",
            "awayGoals",
            "goals_away",
            "score_away"
        ]
    )

    escanteios_casa = numero(
        jogo,
        [
            "home_corners",
            "homeCorners",
            "corners_home"
        ]
    )

    escanteios_fora = numero(
        jogo,
        [
            "away_corners",
            "awayCorners",
            "corners_away"
        ]
    )

    return {
        "gols_total": gols_casa + gols_fora,
        "escanteios_total": (
            escanteios_casa + escanteios_fora
        )
    }


def ciclo_resultados():
    logger.info("Iniciando ciclo de resultados.")

    resultados = buscar_resultados()

    if not resultados:
        logger.info("Nenhum resultado encontrado.")
        return

    with stats_lock:
        pendentes = [
            item
            for item in stats["historico"]
            if item.get("status") == "pendente"
        ]

    if not pendentes:
        logger.info("Nenhum sinal pendente.")
        return

    for sinal in pendentes:
        fixture_id = sinal["fixture_id"]

        jogo_encontrado = None

        for jogo in resultados:
            if extrair_id_jogo(jogo) == fixture_id:
                jogo_encontrado = jogo
                break

        if not jogo_encontrado:
            continue

        resultado = extrair_resultado(
            jogo_encontrado
        )

        gols_ok = (
            resultado["gols_total"] > LINHA_GOLS
        )

        escanteios_ok = (
            resultado["escanteios_total"]
            > LINHA_ESCANTEIOS
        )

        green = gols_ok and escanteios_ok

        with stats_lock:
            for item in stats["historico"]:
                if item.get("fixture_id") == fixture_id:
                    item["status"] = (
                        "win" if green else "loss"
                    )
                    item["resultado_gols"] = (
                        resultado["gols_total"]
                    )
                    item["resultado_escanteios"] = (
                        resultado["escanteios_total"]
                    )

                    break

            stats["sinais_resolvidos"] += 1

            if green:
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            if stats["pendentes"] > 0:
                stats["pendentes"] -= 1

            atualizar_assertividade()

            stats["ultima_atualizacao"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

        salvar_stats()

        logger.info(
            "Sinal resolvido: %s | %s",
            fixture_id,
            "WIN" if green else "LOSS"
        )


# ============================================================
# THREAD DE ANÁLISE
# ============================================================

def worker_analise():
    while True:
        try:
            ciclo_analise()

        except Exception as erro:
            logger.exception(
                "Erro no worker de análise: %s",
                erro
            )

        logger.info(
            "Aguardando %s segundos para nova análise.",
            INTERVALO_ANALISE
        )

        time.sleep(INTERVALO_ANALISE)


# ============================================================
# THREAD DE RESULTADOS
# ============================================================

def worker_resultados():
    while True:
        try:
            ciclo_resultados()

        except Exception as erro:
            logger.exception(
                "Erro no worker de resultados: %s",
                erro
            )

        logger.info(
            "Aguardando %s segundos para resultados.",
            INTERVALO_RESULTADOS
        )

        time.sleep(INTERVALO_RESULTADOS)


# ============================================================
# ROTAS FLASK
# ============================================================

@app.route("/")
def inicio():
    return jsonify({
        "status": "online",
        "bot": "Robô Gols + Escanteios",
        "horario": datetime.now(
            timezone.utc
        ).isoformat()
    })


@app.route("/status")
def status():
    with stats_lock:
        atualizar_assertividade()

        return jsonify({
            "status": "online",
            "stats": stats,
            "limite_requisicoes_minuto": (
                LIMITE_REQUISICOES_MINUTO
            ),
            "intervalo_analise": INTERVALO_ANALISE,
            "intervalo_resultados": INTERVALO_RESULTADOS,
            "max_jogos_analisar": MAX_JOGOS_ANALISAR,
            "assertividade_minima": ASSERTIVIDADE_MINIMA
        })


@app.route("/stats")
def rota_stats():
    with stats_lock:
        atualizar_assertividade()
        return jsonify(stats)


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def iniciar_workers():
    thread_analise = threading.Thread(
        target=worker_analise,
        daemon=True
    )

    thread_resultados = threading.Thread(
        target=worker_resultados,
        daemon=True
    )

    thread_analise.start()
    thread_resultados.start()

    logger.info("Workers iniciados.")


if __name__ == "__main__":
    iniciar_workers()

    porta = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=porta
    )
