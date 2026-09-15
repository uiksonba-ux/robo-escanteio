import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÕES GERAIS
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)s | %(message)s"
)

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Banca e Gale
ENTRADA_INICIAL = float(os.getenv("ENTRADA_INICIAL", "10"))
MULTIPLICADOR_GALE = float(os.getenv("MULTIPLICADOR_GALE", "2"))
MAX_GALES = int(os.getenv("MAX_GALES", "2"))
TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))

# Janela de análise
JANELA_HORAS = int(os.getenv("JANELA_HORAS", "24"))
INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv("INTERVALO_ANALISE_SEGUNDOS", "900")
)

# Odd da combinada
ODD_MINIMA = float(os.getenv("ODD_MINIMA", "1.80"))
ODD_MAXIMA = float(os.getenv("ODD_MAXIMA", "2.50"))
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))

# Mercados
MERCADO_ESCANTEIOS = os.getenv(
    "MERCADO_ESCANTEIOS",
    "over_8.5"
)

MERCADO_GOLS = os.getenv(
    "MERCADO_GOLS",
    "under_4.5"
)

# Filtros de segurança
MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "5")
)

MIN_MEDIA_ESCANTEIOS = float(
    os.getenv("MIN_MEDIA_ESCANTEIOS", "8.5")
)

MAX_MEDIA_GOLS = float(
    os.getenv("MAX_MEDIA_GOLS", "4.0")
)

MIN_PROBABILIDADE_HISTORICA = float(
    os.getenv("MIN_PROBABILIDADE_HISTORICA", "0.62")
)

MAX_JOGOS_ODDS_POR_CICLO = int(
    os.getenv("MAX_JOGOS_ODDS_POR_CICLO", "25")
)

TIMEZONE_OFFSET = os.getenv(
    "TIMEZONE_OFFSET",
    "-03:00"
)

ARQUIVO_STATS = os.getenv(
    "ARQUIVO_STATS",
    "stats.json"
)

ARQUIVO_SINAIS = os.getenv(
    "ARQUIVO_SINAIS",
    "sinais.json"
)

ARQUIVO_CACHE = os.getenv(
    "ARQUIVO_CACHE",
    "cache.json"
)

LOCK = threading.Lock()


# ============================================================
# ESTRUTURAS PADRÃO
# ============================================================

stats = {
    "total_sinais": 0,
    "resolvidos": 0,
    "wins": 0,
    "losses": 0,
    "assertividade": 0.0,
    "lucro_teorico": 0.0,
    "por_banca": {},
    "por_gale": {},
    "por_mercado": {},
    "ultima_atualizacao": None
}

sinais = {}

bancas = {}

cache = {
    "fixtures": [],
    "ultima_consulta": None
}


# ============================================================
# UTILITÁRIOS DE ARQUIVOS
# ============================================================

def carregar_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except Exception as erro:
        logging.error("Erro ao carregar %s: %s", caminho, erro)
        return padrao


def salvar_json(caminho, dados):
    try:
        temporario = caminho + ".tmp"

        with open(
            temporario,
            "w",
            encoding="utf-8"
        ) as arquivo:
            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, caminho)

    except Exception as erro:
        logging.error("Erro ao salvar %s: %s", caminho, erro)


def carregar_dados():
    global stats, sinais, bancas, cache

    stats = carregar_json(ARQUIVO_STATS, stats)
    sinais = carregar_json(ARQUIVO_SINAIS, {})
    cache = carregar_json(ARQUIVO_CACHE, cache)

    for numero in range(1, TOTAL_BANCAS + 1):
        chave = str(numero)

        if chave not in stats.get("por_banca", {}):
            stats["por_banca"][chave] = {
                "sinais": 0,
                "resolvidos": 0,
                "wins": 0,
                "losses": 0,
                "assertividade": 0.0,
                "gale_atual": 0,
                "saldo_teorico": 0.0
            }

        if chave not in bancas:
            bancas[chave] = {
                "id": numero,
                "status": "livre",
                "gale_atual": 0,
                "sinal_id": None,
                "ultima_resolucao": None
            }


def salvar_dados():
    salvar_json(ARQUIVO_STATS, stats)
    salvar_json(ARQUIVO_SINAIS, sinais)
    salvar_json(ARQUIVO_CACHE, cache)


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning(
            "TELEGRAM_TOKEN ou CHAT_ID não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        resposta = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if resposta.status_code != 200:
            logging.error(
                "Erro Telegram %s: %s",
                resposta.status_code,
                resposta.text
            )
            return False

        return True

    except Exception as erro:
        logging.error("Falha Telegram: %s", erro)
        return False


# ============================================================
# API
# ============================================================

def headers_api():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "RoboCombinado/1.0"
    }


def api_get(endpoint, params=None):
    if not API_KEY:
        logging.error(
            "FIVE_DOLLAR_API_KEY não configurada."
        )
        return None

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    try:
        resposta = requests.get(
            url,
            headers=headers_api(),
            params=params or {},
            timeout=30
        )

        if resposta.status_code != 200:
            logging.error(
                "API retornou status %s: %s",
                resposta.status_code,
                resposta.text[:1000]
            )
            return None

        dados = resposta.json()

        if isinstance(dados, dict):
            if dados.get("success") == 0:
                logging.error(
                    "Erro da API: %s",
                    dados
                )
                return None

        return dados

    except requests.RequestException as erro:
        logging.error("Erro de conexão com API: %s", erro)
        return None

    except ValueError as erro:
        logging.error("Resposta não é JSON válido: %s", erro)
        return None


def extrair_lista(dados, chaves=None):
    if not dados:
        return []

    if isinstance(dados, list):
        return dados

    if not isinstance(dados, dict):
        return []

    chaves = chaves or [
        "data",
        "fixtures",
        "matches",
        "games",
        "events",
        "results"
    ]

    for chave in chaves:
        valor = dados.get(chave)

        if isinstance(valor, list):
            return valor

        if isinstance(valor, dict):
            lista = extrair_lista(valor, chaves)
            if lista:
                return lista

    return []


def obter_fixtures_proximas_24h():
    agora = datetime.now(timezone.utc)
    fim = agora + timedelta(hours=JANELA_HORAS)

    params = {
        "start_time": agora.isoformat(),
        "end_time": fim.isoformat(),
        "status": "scheduled",
        "per_page": 100,
        "include": "odds"
    }

    dados = api_get("fixtures", params)

    if dados is None:
        return []

    jogos = extrair_lista(dados)

    cache["fixtures"] = jogos
    cache["ultima_consulta"] = datetime.now(
        timezone.utc
    ).isoformat()

    salvar_json(ARQUIVO_CACHE, cache)

    logging.info(
        "Fixtures encontrados na janela: %s",
        len(jogos)
    )

    return jogos


def obter_odds_fixture(fixture_id):
    if not fixture_id:
        return {}

    dados = api_get(
        f"fixtures/{fixture_id}/odds",
        params={
            "market": "corner,goalline"
        }
    )

    if dados is None:
        return {}

    return dados


# ============================================================
# PARSER DE DADOS
# ============================================================

def primeiro_valor(objeto, caminhos):
    for caminho in caminhos:
        atual = objeto

        try:
            for parte in caminho.split("."):
                if isinstance(atual, dict):
                    atual = atual.get(parte)
                else:
                    atual = None
                    break

            if atual is not None:
                return atual

        except Exception:
            continue

    return None


def numero(valor):
    if valor is None:
        return None

    if isinstance(valor, bool):
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    if isinstance(valor, str):
        valor = valor.strip()
        valor = valor.replace(",", ".")

        try:
            return float(valor)
        except ValueError:
            return None

    return None


def nome_time(fixture, casa=True):
    if casa:
        caminhos = [
            "home.name",
            "home_team.name",
            "teams.home.name",
            "participants.home.name",
            "localteam.name"
        ]
    else:
        caminhos = [
            "away.name",
            "away_team.name",
            "teams.away.name",
            "participants.away.name",
            "visitorteam.name"
        ]

    valor = primeiro_valor(fixture, caminhos)

    return str(valor) if valor else "Time desconhecido"


def obter_fixture_id(fixture):
    valor = primeiro_valor(
        fixture,
        [
            "id",
            "fixture_id",
            "match_id",
            "game_id",
            "event_id"
        ]
    )

    return str(valor) if valor is not None else None


def extrair_odd(valor):
    if valor is None:
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    if isinstance(valor, str):
        return numero(valor)

    if isinstance(valor, dict):
        for chave in [
            "odd",
            "price",
            "value",
            "decimal",
            "odds"
        ]:
            if chave in valor:
                resultado = extrair_odd(valor[chave])

                if resultado is not None:
                    return resultado

    return None


def encontrar_odd_mercado(objeto, mercado):
    """
    Tenta localizar odds em vários formatos possíveis.
    Não presume um único formato da API.
    """

    if not objeto:
        return None

    mercado_lower = mercado.lower()

    if isinstance(objeto, dict):
        # Busca direta por nomes comuns
        for chave, valor in objeto.items():
            chave_lower = str(chave).lower()

            if mercado_lower in chave_lower:
                odd = extrair_odd(valor)

                if odd:
                    return odd

            # Mercado de escanteios
            if "corner" in mercado_lower:
                if (
                    "corner" in chave_lower
                    or "corners" in chave_lower
                ):
                    texto = json.dumps(
                        valor,
                        ensure_ascii=False
                    ).lower()

                    if mercado_lower in texto:
                        odd = extrair_odd(valor)

                        if odd:
                            return odd

            # Mercado de gols
            if "under" in mercado_lower or "over" in mercado_lower:
                if (
                    "goal" in chave_lower
                    or "goalline" in chave_lower
                    or "total" in chave_lower
                ):
                    texto = json.dumps(
                        valor,
                        ensure_ascii=False
                    ).lower()

                    if mercado_lower in texto:
                        odd = extrair_odd(valor)

                        if odd:
                            return odd

        for valor in objeto.values():
            resultado = encontrar_odd_mercado(
                valor,
                mercado
            )

            if resultado is not None:
                return resultado

    elif isinstance(objeto, list):
        for item in objeto:
            resultado = encontrar_odd_mercado(
                item,
                mercado
            )

            if resultado is not None:
                return resultado

    return None


def extrair_odds(fixture):
    """
    Primeiro procura odds já incluídas no fixture.
    Depois o ciclo principal consulta /odds.
    """

    odds = primeiro_valor(
        fixture,
        [
            "odds",
            "markets",
            "bookmakers",
            "betting"
        ]
    )

    if odds is None:
        odds = fixture

    odd_escanteios = encontrar_odd_mercado(
        odds,
        MERCADO_ESCANTEIOS
    )

    odd_gols = encontrar_odd_mercado(
        odds,
        MERCADO_GOLS
    )

    return {
        "odd_escanteios": odd_escanteios,
        "odd_gols": odd_gols
    }


def calcular_odd_combinada(odd_escanteios, odd_gols):
    if odd_escanteios is None or odd_gols is None:
        return None

    if odd_escanteios <= 1 or odd_gols <= 1:
        return None

    return round(
        odd_escanteios * odd_gols,
        2
    )


# ============================================================
# HISTÓRICO E FILTROS
# ============================================================

def extrair_numero_escanteios(fixture):
    valor = primeiro_valor(
        fixture,
        [
            "corners.total",
            "corners.total_count",
            "statistics.corners.total",
            "stats.corners.total",
            "corner_total"
        ]
    )

    if valor is not None:
        return numero(valor)

    casa = numero(
        primeiro_valor(
            fixture,
            [
                "corners.home",
                "corner.home",
                "stats.corners.home"
            ]
        )
    )

    fora = numero(
        primeiro_valor(
            fixture,
            [
                "corners.away",
                "corner.away",
                "stats.corners.away"
            ]
        )
    )

    if casa is not None and fora is not None:
        return casa + fora

    return None


def extrair_numero_gols(fixture):
    casa = numero(
        primeiro_valor(
            fixture,
            [
                "goals.home",
                "score.home",
                "scores.home",
                "home_score"
            ]
        )
    )

    fora = numero(
        primeiro_valor(
            fixture,
            [
                "goals.away",
                "score.away",
                "scores.away",
                "away_score"
            ]
        )
    )

    if casa is not None and fora is not None:
        return casa + fora

    total = numero(
        primeiro_valor(
            fixture,
            [
                "goals.total",
                "score.total",
                "total_goals"
            ]
        )
    )

    return total


def calcular_historico_simples(historico):
    jogos_validos = []

    for jogo in historico:
        escanteios = extrair_numero_escanteios(jogo)
        gols = extrair_numero_gols(jogo)

        if escanteios is None or gols is None:
            continue

        jogos_validos.append({
            "escanteios": escanteios,
            "gols": gols
        })

    if len(jogos_validos) < MIN_JOGOS_HISTORICO:
        return None

    over_cantos = sum(
        1
        for jogo in jogos_validos
        if jogo["escanteios"] > 8.5
    )

    under_gols = sum(
        1
        for jogo in jogos_validos
        if jogo["gols"] < 4.5
    )

    combinados = sum(
        1
        for jogo in jogos_validos
        if (
            jogo["escanteios"] > 8.5
            and jogo["gols"] < 4.5
        )
    )

    total = len(jogos_validos)

    media_escanteios = sum(
        jogo["escanteios"]
        for jogo in jogos_validos
    ) / total

    media_gols = sum(
        jogo["gols"]
        for jogo in jogos_validos
    ) / total

    probabilidade_combinada = combinados / total

    return {
        "jogos": total,
        "over_escanteios": over_cantos / total,
        "under_gols": under_gols / total,
        "combinada": probabilidade_combinada,
        "media_escanteios": round(
            media_escanteios,
            2
        ),
        "media_gols": round(
            media_gols,
            2
        )
    }


def obter_historico_fixture(fixture):
    """
    Usa histórico que eventualmente já venha no fixture.
    Caso a API entregue outro endpoint específico de histórico,
    ele poderá ser conectado aqui sem alterar o restante do robô.
    """

    historico = primeiro_valor(
        fixture,
        [
            "history",
            "historical",
            "recent_matches",
            "last_matches",
            "team_history"
        ]
    )

    if isinstance(historico, list):
        return historico

    return []


def fixture_elegivel(fixture, odds):
    odd_escanteios = odds.get("odd_escanteios")
    odd_gols = odds.get("odd_gols")

    odd_combinada = calcular_odd_combinada(
        odd_escanteios,
        odd_gols
    )

    if odd_combinada is None:
        return False, "Odds não encontradas", None

    if odd_combinada < ODD_MINIMA:
        return False, "Odd abaixo do mínimo", odd_combinada

    if odd_combinada > ODD_MAXIMA:
        return False, "Odd acima do máximo", odd_combinada

    historico = obter_historico_fixture(fixture)
    analise = calcular_historico_simples(historico)

    # Se não houver histórico suficiente, não inventa assertividade.
    if analise is None:
        return (
            False,
            "Histórico insuficiente",
            odd_combinada
        )

    if analise["media_escanteios"] < MIN_MEDIA_ESCANTEIOS:
        return (
            False,
            "Média de escanteios insuficiente",
            odd_combinada
        )

    if analise["media_gols"] > MAX_MEDIA_GOLS:
        return (
            False,
            "Média de gols acima do limite",
            odd_combinada
        )

    if (
        analise["combinada"]
        < MIN_PROBABILIDADE_HISTORICA
    ):
        return (
            False,
            "Assertividade histórica abaixo do filtro",
            odd_combinada
        )

    return True, "Elegível", odd_combinada


# ============================================================
# ESCOLHA DA MELHOR COMBINADA
# ============================================================

def pontuar_fixture(fixture, odd_combinada):
    distancia_odd = abs(
        odd_combinada - ODD_ALVO
    )

    historico = calcular_historico_simples(
        obter_historico_fixture(fixture)
    )

    probabilidade = 0

    if historico:
        probabilidade = historico["combinada"]

    return (
        probabilidade * 100
        - distancia_odd * 10
    )


def montar_sinal(fixture, odds, odd_combinada, banca):
    fixture_id = obter_fixture_id(fixture)

    casa = nome_time(fixture, True)
    fora = nome_time(fixture, False)

    agora = datetime.now(timezone.utc)

    sinal_id = (
        f"{fixture_id}_"
        f"{banca}_"
        f"{int(agora.timestamp())}"
    )

    gale_atual = bancas[str(banca)]["gale_atual"]

    valor_entrada = (
        ENTRADA_INICIAL
        * (MULTIPLICADOR_GALE ** gale_atual)
    )

    historico = calcular_historico_simples(
        obter_historico_fixture(fixture)
    )

    sinal = {
        "id": sinal_id,
        "fixture_id": fixture_id,
        "banca": banca,
        "gale": gale_atual,
        "entrada": round(valor_entrada, 2),
        "status": "pendente",
        "criado_em": agora.isoformat(),
        "resolvido_em": None,
        "resultado": None,
        "casa": casa,
        "fora": fora,
        "mercado_escanteios": MERCADO_ESCANTEIOS,
        "mercado_gols": MERCADO_GOLS,
        "odd_escanteios": odds.get("odd_escanteios"),
        "odd_gols": odds.get("odd_gols"),
        "odd_combinada": odd_combinada,
        "historico": historico
    }

    return sinal


def bancas_livres():
    livres = []

    for banca_id, banca in bancas.items():
        if banca["status"] == "livre":
            livres.append(int(banca_id))

    return livres


# ============================================================
# ENVIO DOS SINAIS
# ============================================================

def formatar_sinal(sinal):
    historico = sinal.get("historico") or {}

    probabilidade = historico.get(
        "combinada"
    )

    if probabilidade is None:
        prob_texto = "Não calculada"
    else:
        prob_texto = f"{probabilidade * 100:.2f}%"

    return (
        "🎯 <b>SINAL PRÉ-JOGO COMBINADO</b>\n\n"
        f"🏟️ <b>{sinal['casa']}</b> x "
        f"<b>{sinal['fora']}</b>\n\n"
        f"🏦 Banca: <b>{sinal['banca']}</b>\n"
        f"🔁 Gale: <b>{sinal['gale']}</b>\n"
        f"💰 Entrada: <b>R$ {sinal['entrada']:.2f}</b>\n\n"
        f"📌 Escanteios: <b>{sinal['mercado_escanteios']}</b>\n"
        f"📌 Gols: <b>{sinal['mercado_gols']}</b>\n\n"
        f"📈 Odd escanteios: <b>{sinal['odd_escanteios']}</b>\n"
        f"📉 Odd gols: <b>{sinal['odd_gols']}</b>\n"
        f"🔥 Odd combinada: <b>{sinal['odd_combinada']}</b>\n"
        f"📊 Histórico combinado: <b>{prob_texto}</b>\n\n"
        "⚠️ Entrada pré-jogo. "
        "A combinada só vence se os dois mercados vencerem."
    )


def enviar_sinal(sinal):
    mensagem = formatar_sinal(sinal)

    enviado = enviar_telegram(mensagem)

    if not enviado:
        return False

    sinais[sinal["id"]] = sinal

    banca_id = str(sinal["banca"])

    bancas[banca_id]["status"] = "ocupada"
    bancas[banca_id]["sinal_id"] = sinal["id"]

    stats["total_sinais"] += 1
    stats["ultima_atualizacao"] = datetime.now(
        timezone.utc
    ).isoformat()

    stats["por_banca"][banca_id]["sinais"] += 1
    stats["por_banca"][banca_id]["gale_atual"] = (
        sinal["gale"]
    )

    salvar_dados()

    logging.info(
        "Sinal enviado: %s | Banca %s",
        sinal["id"],
        banca_id
    )

    return True


# ============================================================
# RESOLUÇÃO DOS SINAIS
# ============================================================

def obter_resultado_fixture(fixture_id):
    dados = api_get(
        f"fixtures/{fixture_id}"
    )

    if not dados:
        return None

    if isinstance(dados, dict):
        fixture = dados.get("data", dados)
    else:
        fixture = dados

    gols = extrair_numero_gols(fixture)
    escanteios = extrair_numero_escanteios(fixture)

    if gols is None or escanteios is None:
        return None

    return {
        "gols": gols,
        "escanteios": escanteios
    }


def resolver_sinal(sinal):
    resultado = obter_resultado_fixture(
        sinal["fixture_id"]
    )

    if resultado is None:
        return False

    venceu_escanteios = (
        resultado["escanteios"] > 8.5
    )

    venceu_gols = (
        resultado["gols"] < 4.5
    )

    venceu = (
        venceu_escanteios
        and venceu_gols
    )

    sinal["resultado"] = (
        "WIN" if venceu else "LOSS"
    )

    sinal["status"] = "resolvido"

    sinal["resolvido_em"] = datetime.now(
        timezone.utc
    ).isoformat()

    sinal["resultado_real"] = resultado

    banca_id = str(sinal["banca"])
    gale = str(sinal["gale"])
    entrada = float(sinal["entrada"])
    odd = float(sinal["odd_combinada"])

    if banca_id not in stats["por_banca"]:
        stats["por_banca"][banca_id] = {
            "sinais": 0,
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "assertividade": 0.0,
            "gale_atual": 0,
            "saldo_teorico": 0.0
        }

    banca_stats = stats["por_banca"][banca_id]

    banca_stats["resolvidos"] += 1
    stats["resolvidos"] += 1

    if venceu:
        stats["wins"] += 1
        banca_stats["wins"] += 1

        lucro = (
            entrada * odd
        ) - entrada

        banca_stats["saldo_teorico"] += lucro
        stats["lucro_teorico"] += lucro

        # Após WIN, volta ao valor inicial
        bancas[banca_id]["gale_atual"] = 0

    else:
        stats["losses"] += 1
        banca_stats["losses"] += 1

        prejuizo = entrada
        banca_stats["saldo_teorico"] -= prejuizo
        stats["lucro_teorico"] -= prejuizo

        gale_atual = int(sinal["gale"])

        if gale_atual < MAX_GALES:
            bancas[banca_id]["gale_atual"] = (
                gale_atual + 1
            )
        else:
            # Perdeu o ciclo completo
            bancas[banca_id]["gale_atual"] = 0

    total_resolvidos = stats["resolvidos"]

    if total_resolvidos > 0:
        stats["assertividade"] = round(
            (
                stats["wins"]
                / total_resolvidos
            ) * 100,
            2
        )

    total_banca_resolvidos = banca_stats[
        "resolvidos"
    ]

    if total_banca_resolvidos > 0:
        banca_stats["assertividade"] = round(
            (
                banca_stats["wins"]
                / total_banca_resolvidos
            ) * 100,
            2
        )

    if gale not in stats["por_gale"]:
        stats["por_gale"][gale] = {
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "assertividade": 0.0
        }

    gale_stats = stats["por_gale"][gale]

    gale_stats["resolvidos"] += 1

    if venceu:
        gale_stats["wins"] += 1
    else:
        gale_stats["losses"] += 1

    gale_stats["assertividade"] = round(
        (
            gale_stats["wins"]
            / gale_stats["resolvidos"]
        ) * 100,
        2
    )

    bancas[banca_id]["status"] = "livre"
    bancas[banca_id]["sinal_id"] = None
    bancas[banca_id]["ultima_resolucao"] = (
        sinal["resolvido_em"]
    )

    mensagem = (
        "📊 <b>RESOLUÇÃO DO SINAL</b>\n\n"
        f"🏟️ {sinal['casa']} x {sinal['fora']}\n"
        f"🏦 Banca: {banca_id}\n"
        f"🔁 Gale: {sinal['gale']}\n"
        f"🎯 Resultado: <b>{sinal['resultado']}</b>\n\n"
        f"⚽ Gols totais: {resultado['gols']}\n"
        f"🚩 Escanteios totais: "
        f"{resultado['escanteios']}\n\n"
        f"📈 Assertividade geral: "
        f"<b>{stats['assertividade']:.2f}%</b>\n"
        f"💰 Saldo teórico: "
        f"<b>R$ {stats['lucro_teorico']:.2f}</b>"
    )

    enviar_telegram(mensagem)

    salvar_dados()

    logging.info(
        "Sinal resolvido: %s | %s",
        sinal["id"],
        sinal["resultado"]
    )

    return True


def resolver_sinais_pendentes():
    for sinal_id, sinal in list(
        sinais.items()
    ):
        if sinal.get("status") != "pendente":
            continue

        try:
            resolver_sinal(sinal)
        except Exception as erro:
            logging.error(
                "Erro resolvendo %s: %s",
                sinal_id,
                erro
            )


# ============================================================
# CICLO PRINCIPAL
# ============================================================

def analisar_e_enviar():
    logging.info(
        "Iniciando análise pré-jogo das próximas %s horas.",
        JANELA_HORAS
    )

    resolver_sinais_pendentes()

    vagas = bancas_livres()

    if not vagas:
        logging.info(
            "Todas as bancas estão ocupadas."
        )
        return

    fixtures = obter_fixtures_proximas_24h()

    if not fixtures:
        logging.info(
            "Nenhum fixture encontrado."
        )
        return

    candidatos = []

    consultas = 0

    for fixture in fixtures:
        if consultas >= MAX_JOGOS_ODDS_POR_CICLO:
            break

        fixture_id = obter_fixture_id(fixture)

        if not fixture_id:
            continue

        odds = extrair_odds(fixture)

        if (
            odds["odd_escanteios"] is None
            or odds["odd_gols"] is None
        ):
            odds_externas = obter_odds_fixture(
                fixture_id
            )

            odds_externas_extraidas = extrair_odds(
                odds_externas
            )

            if odds["odd_escanteios"] is None:
                odds["odd_escanteios"] = (
                    odds_externas_extraidas[
                        "odd_escanteios"
                    ]
                )

            if odds["odd_gols"] is None:
                odds["odd_gols"] = (
                    odds_externas_extraidas[
                        "odd_gols"
                    ]
                )

        consultas += 1

        elegivel, motivo, odd_combinada = (
            fixture_elegivel(
                fixture,
                odds
            )
        )

        if not elegivel:
            logging.info(
                "Fixture %s rejeitado: %s",
                fixture_id,
                motivo
            )
            continue

        pontuacao = pontuar_fixture(
            fixture,
            odd_combinada
        )

        candidatos.append({
            "fixture": fixture,
            "odds": odds,
            "odd_combinada": odd_combinada,
            "pontuacao": pontuacao
        })

    candidatos.sort(
        key=lambda item: item["pontuacao"],
        reverse=True
    )

    for candidato, banca_id in zip(
        candidatos,
        vagas
    ):
        sinal = montar_sinal(
            candidato["fixture"],
            candidato["odds"],
            candidato["odd_combinada"],
            banca_id
        )

        enviar_sinal(sinal)


def loop_principal():
    logging.info(
        "Robô iniciado."
    )

    while True:
        try:
            with LOCK:
                analisar_e_enviar()

        except Exception as erro:
            logging.exception(
                "Erro no ciclo principal: %s",
                erro
            )

        time.sleep(
            INTERVALO_ANALISE_SEGUNDOS
        )


# ============================================================
# ROTAS FLASK
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "robo": "combinado_escanteios_gols",
        "modo": "pre_jogo",
        "janela_horas": JANELA_HORAS,
        "bancas": TOTAL_BANCAS,
        "odd_minima": ODD_MINIMA,
        "odd_maxima": ODD_MAXIMA,
        "odd_alvo": ODD_ALVO,
        "mercado_escanteios": MERCADO_ESCANTEIOS,
        "mercado_gols": MERCADO_GOLS,
        "ultima_atualizacao": stats.get(
            "ultima_atualizacao"
        )
    })


@app.route("/status")
def status():
    return jsonify({
        "stats": stats,
        "bancas": bancas,
        "sinais_pendentes": [
            sinal
            for sinal in sinais.values()
            if sinal.get("status") == "pendente"
        ]
    })


@app.route("/stats")
def rota_stats():
    return jsonify(stats)


@app.route("/bancas")
def rota_bancas():
    return jsonify(bancas)


@app.route("/sinais")
def rota_sinais():
    return jsonify(sinais)


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    })


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_dados()

threading.Thread(
    target=loop_principal,
    daemon=True
).start()


if __name__ == "__main__":
    porta = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=porta
        )
