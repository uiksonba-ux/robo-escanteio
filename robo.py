import os
import json
import time
import threading
import html
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÕES
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")

ARQUIVO_STATS = "stats.json"

INTERVALO_ANALISE = 60
JOGOS_HISTORICO = 5
MINIMO_HISTORICO = 5

ODD_MINIMA = 1.01
ODD_MAXIMA = 2.50

# Linha principal do mercado
LINHA_ESCANTEIOS_HT = 4.5

# Caso queira exigir uma odd mínima mais específica,
# altere aqui para 1.70, 1.80, 1.85 etc.
ODD_MINIMA_SINAL = 1.01

# Tempo máximo para considerar um jogo ao vivo
MINUTO_MAXIMO_ANALISE = 45

# Arquivo de controle dos jogos já utilizados
ARQUIVO_FIXTURES = "fixtures_usadas.json"


# ============================================================
# ESTADO GLOBAL
# ============================================================

# RLock evita deadlock quando uma função chama outra
# que também utiliza o mesmo lock.
estado_lock = threading.RLock()

stats = {
    "sinais_total": 0,
    "wins": 0,
    "losses": 0,
    "pendentes": 0,
    "historico_analisado": 0,
    "ultimo_sinal": None,
    "ultima_analise": None,
    "erros_api": 0
}

sinais_pendentes = {}
fixtures_usadas = set()


# ============================================================
# CARREGAMENTO E SALVAMENTO
# ============================================================

def carregar_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao

    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except Exception:
        return padrao


def salvar_json(caminho, dados):
    try:
        with open(caminho, "w", encoding="utf-8") as arquivo:
            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
                indent=2
            )
    except Exception as erro:
        print(f"[ERRO] Falha ao salvar {caminho}: {erro}")


def carregar_estado():
    global stats
    global fixtures_usadas

    dados_stats = carregar_json(ARQUIVO_STATS, {})

    if isinstance(dados_stats, dict):
        stats.update(dados_stats)

    dados_fixtures = carregar_json(ARQUIVO_FIXTURES, [])

    if isinstance(dados_fixtures, list):
        fixtures_usadas = set(str(item) for item in dados_fixtures)


def salvar_stats():
    with estado_lock:
        salvar_json(ARQUIVO_STATS, stats)


def salvar_fixtures():
    with estado_lock:
        salvar_json(
            ARQUIVO_FIXTURES,
            list(fixtures_usadas)
        )


carregar_estado()


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def agora_utc():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def numero(valor, padrao=0.0):
    try:
        if valor is None:
            return padrao

        if isinstance(valor, bool):
            return padrao

        if isinstance(valor, str):
            valor = valor.replace(",", ".").strip()

        return float(valor)
    except Exception:
        return padrao


def inteiro(valor, padrao=0):
    try:
        return int(float(valor))
    except Exception:
        return padrao


def primeiro_valor(dicionario, chaves, padrao=None):
    if not isinstance(dicionario, dict):
        return padrao

    for chave in chaves:
        if chave in dicionario:
            valor = dicionario[chave]

            if valor is not None:
                return valor

    return padrao


def nome_time(time_data):
    if isinstance(time_data, str):
        return time_data

    if not isinstance(time_data, dict):
        return "Desconhecido"

    return (
        time_data.get("name")
        or time_data.get("short_name")
        or time_data.get("shortName")
        or time_data.get("title")
        or "Desconhecido"
    )


def extrair_fixture_id(jogo):
    if not isinstance(jogo, dict):
        return None

    valor = primeiro_valor(
        jogo,
        [
            "id",
            "fixture_id",
            "fixtureId",
            "match_id",
            "matchId",
            "event_id",
            "eventId"
        ]
    )

    if valor is None:
        return None

    return str(valor)


def extrair_minuto(jogo):
    status = jogo.get("status", {}) if isinstance(jogo, dict) else {}

    valor = primeiro_valor(
        jogo,
        [
            "minute",
            "elapsed",
            "current_minute",
            "currentMinute"
        ]
    )

    if valor is None and isinstance(status, dict):
        valor = primeiro_valor(
            status,
            [
                "minute",
                "elapsed",
                "current_minute",
                "currentMinute"
            ]
        )

    return inteiro(valor, 0)


def jogo_ao_vivo(jogo):
    status = jogo.get("status", {}) if isinstance(jogo, dict) else {}

    textos = []

    if isinstance(status, dict):
        textos.extend([
            str(status.get("short", "")),
            str(status.get("long", "")),
            str(status.get("type", ""))
        ])

    textos.extend([
        str(jogo.get("status", "")),
        str(jogo.get("state", ""))
    ])

    texto = " ".join(textos).lower()

    palavras_ao_vivo = [
        "live",
        "inplay",
        "in-play",
        "1h",
        "2h",
        "first half",
        "second half",
        "ao vivo"
    ]

    return any(palavra in texto for palavra in palavras_ao_vivo)


def extrair_times(jogo):
    home = (
        jogo.get("home")
        or jogo.get("home_team")
        or jogo.get("homeTeam")
        or jogo.get("teams", {}).get("home", {})
        if isinstance(jogo.get("teams", {}), dict)
        else jogo.get("home")
    )

    away = (
        jogo.get("away")
        or jogo.get("away_team")
        or jogo.get("awayTeam")
        or jogo.get("teams", {}).get("away", {})
        if isinstance(jogo.get("teams", {}), dict)
        else jogo.get("away")
    )

    return nome_time(home), nome_time(away)


def extrair_odd(jogo):
    possiveis = [
        "odd",
        "odds",
        "price",
        "value",
        "selection_odd",
        "selectionOdd",
        "market_odd",
        "marketOdd"
    ]

    valor = primeiro_valor(jogo, possiveis)

    if isinstance(valor, dict):
        valor = primeiro_valor(
            valor,
            [
                "value",
                "price",
                "odd",
                "decimal"
            ]
        )

    return numero(valor, 0.0)


# ============================================================
# API
# ============================================================

def headers_api():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    if API_KEY:
        headers["x-api-key"] = API_KEY
        headers["X-API-KEY"] = API_KEY
        headers["Authorization"] = f"Bearer {API_KEY}"

    return headers


def requisicao_api(endpoint, params=None):
    url = f"{BASE_API.rstrip('/')}/{endpoint.lstrip('/')}"

    try:
        resposta = requests.get(
            url,
            headers=headers_api(),
            params=params or {},
            timeout=25
        )

        if resposta.status_code != 200:
            print(
                f"[API] Status {resposta.status_code}: "
                f"{resposta.text[:300]}"
            )

            with estado_lock:
                stats["erros_api"] += 1

            salvar_stats()
            return None

        try:
            return resposta.json()
        except Exception:
            print("[API] Resposta não é JSON válido")
            return None

    except requests.RequestException as erro:
        print(f"[API] Erro de conexão: {erro}")

        with estado_lock:
            stats["erros_api"] += 1

        salvar_stats()
        return None


def extrair_lista_resposta(resposta):
    if resposta is None:
        return []

    if isinstance(resposta, list):
        return resposta

    if not isinstance(resposta, dict):
        return []

    for chave in [
        "data",
        "results",
        "fixtures",
        "matches",
        "events",
        "games"
    ]:
        valor = resposta.get(chave)

        if isinstance(valor, list):
            return valor

        if isinstance(valor, dict):
            for chave_interna in [
                "data",
                "results",
                "fixtures",
                "matches",
                "events",
                "games"
            ]:
                lista = valor.get(chave_interna)

                if isinstance(lista, list):
                    return lista

    return []


def buscar_jogos_ao_vivo():
    endpoints = [
        "fixtures",
        "matches",
        "events",
        "games"
    ]

    parametros = [
        {"live": "all"},
        {"status": "live"},
        {"inplay": "true"},
        {"is_live": "true"}
    ]

    for endpoint in endpoints:
        for params in parametros:
            resposta = requisicao_api(
                endpoint,
                params
            )

            lista = extrair_lista_resposta(resposta)

            if lista:
                jogos_validos = [
                    jogo for jogo in lista
                    if isinstance(jogo, dict)
                ]

                if jogos_validos:
                    return jogos_validos

    return []


def buscar_historico_time(time_id):
    endpoints = [
        "fixtures",
        "matches",
        "events",
        "games"
    ]

    parametros = [
        {
            "team_id": time_id,
            "last": JOGOS_HISTORICO
        },
        {
            "teamId": time_id,
            "limit": JOGOS_HISTORICO
        },
        {
            "team": time_id,
            "last": JOGOS_HISTORICO
        }
    ]

    for endpoint in endpoints:
        for params in parametros:
            resposta = requisicao_api(
                endpoint,
                params
            )

            lista = extrair_lista_resposta(resposta)

            if lista:
                return lista[:JOGOS_HISTORICO]

    return []


def buscar_detalhes_jogo(fixture_id):
    endpoints = [
        f"fixtures/{fixture_id}",
        f"matches/{fixture_id}",
        f"events/{fixture_id}",
        f"games/{fixture_id}"
    ]

    for endpoint in endpoints:
        resposta = requisicao_api(endpoint)

        if isinstance(resposta, dict):
            lista = extrair_lista_resposta(resposta)

            if lista:
                return lista[0]

            if resposta:
                return resposta

    return {}


# ============================================================
# EXTRAÇÃO DE ESCANTEIOS
# ============================================================

def extrair_valor_estatistica(objeto, chaves):
    if not isinstance(objeto, dict):
        return None

    valor = primeiro_valor(objeto, chaves)

    if valor is not None:
        return numero(valor, None)

    # Alguns fornecedores enviam:
    # {"statistics": [{"type": "corners", "value": 3}]}
    listas = [
        objeto.get("statistics"),
        objeto.get("stats"),
        objeto.get("statistics_home"),
        objeto.get("statisticsHome"),
        objeto.get("home_statistics"),
        objeto.get("away_statistics")
    ]

    for lista in listas:
        if not isinstance(lista, list):
            continue

        for item in lista:
            if not isinstance(item, dict):
                continue

            nome = str(
                item.get("type")
                or item.get("name")
                or item.get("label")
                or ""
            ).lower()

            if any(chave.lower() in nome for chave in chaves):
                valor = (
                    item.get("value")
                    or item.get("total")
                    or item.get("count")
                )

                if valor is not None:
                    return numero(valor, None)

    return None


def extrair_escanteios_primeiro_tempo(jogo):
    """
    Tenta localizar escanteios do primeiro tempo.

    Nomes possíveis:
    - corners_1h
    - first_half_corners
    - corners_first_half
    - corners_ht
    - corners_first
    - period_1_corners
    - 1h_corners
    """

    if not isinstance(jogo, dict):
        return None

    chaves_diretas = [
        "corners_1h",
        "corners_1H",
        "first_half_corners",
        "firstHalfCorners",
        "corners_first_half",
        "cornersFirstHalf",
        "corners_ht",
        "cornersHT",
        "corners_first",
        "cornersFirst",
        "period_1_corners",
        "period1Corners",
        "1h_corners",
        "ht_corners",
        "half_time_corners"
    ]

    valor = extrair_valor_estatistica(
        jogo,
        chaves_diretas
    )

    if valor is not None:
        return valor

    # Verifica objetos aninhados
    objetos = [
        jogo.get("stats"),
        jogo.get("statistics"),
        jogo.get("periods"),
        jogo.get("period"),
        jogo.get("score"),
        jogo.get("scores"),
        jogo.get("halftime"),
        jogo.get("half_time"),
        jogo.get("first_half"),
        jogo.get("firstHalf")
    ]

    for objeto in objetos:
        if isinstance(objeto, dict):
            valor = extrair_valor_estatistica(
                objeto,
                chaves_diretas
            )

            if valor is not None:
                return valor

            # Alguns formatos:
            # {"first_half": {"corners": 3}}
            valor = extrair_valor_estatistica(
                objeto,
                [
                    "corners",
                    "corner",
                    "total_corners",
                    "totalCorners"
                ]
            )

            if valor is not None:
                return valor

    return None


def extrair_escanteios_total(jogo):
    if not isinstance(jogo, dict):
        return None

    chaves = [
        "corners",
        "corner",
        "total_corners",
        "totalCorners",
        "corners_total",
        "cornersTotal"
    ]

    valor = extrair_valor_estatistica(
        jogo,
        chaves
    )

    if valor is not None:
        return valor

    return None


def extrair_gols_total(jogo):
    if not isinstance(jogo, dict):
        return 0

    gols = primeiro_valor(
        jogo,
        [
            "goals",
            "total_goals",
            "totalGoals",
            "goals_total",
            "goalsTotal"
        ],
        0
    )

    if isinstance(gols, dict):
        home = numero(
            primeiro_valor(
                gols,
                ["home", "home_goals", "homeGoals"],
                0
            )
        )

        away = numero(
            primeiro_valor(
                gols,
                ["away", "away_goals", "awayGoals"],
                0
            )
        )

        return home + away

    return numero(gols, 0)


# ============================================================
# HISTÓRICO
# ============================================================

def extrair_time_id(jogo, casa=True):
    if not isinstance(jogo, dict):
        return None

    teams = jogo.get("teams")

    if isinstance(teams, dict):
        lado = teams.get("home" if casa else "away")

        if isinstance(lado, dict):
            return (
                lado.get("id")
                or lado.get("team_id")
                or lado.get("teamId")
            )

    chave = "home" if casa else "away"

    lado = jogo.get(chave)

    if isinstance(lado, dict):
        return (
            lado.get("id")
            or lado.get("team_id")
            or lado.get("teamId")
        )

    return (
        jogo.get(f"{chave}_id")
        or jogo.get(f"{chave}Id")
    )


def montar_historico(jogo):
    historico = []

    home_id = extrair_time_id(jogo, True)
    away_id = extrair_time_id(jogo, False)

    jogos_consultados = []

    if home_id:
        jogos_consultados.extend(
            buscar_historico_time(home_id)
        )

    if away_id:
        jogos_consultados.extend(
            buscar_historico_time(away_id)
        )

    vistos = set()

    for partida in jogos_consultados:
        fixture_id = extrair_fixture_id(partida)

        if fixture_id and fixture_id in vistos:
            continue

        if fixture_id:
            vistos.add(fixture_id)

        detalhes = partida

        # Se a partida não possui escanteios do 1º tempo,
        # tenta consultar os detalhes.
        cantos_ht = extrair_escanteios_primeiro_tempo(
            detalhes
        )

        if cantos_ht is None and fixture_id:
            detalhes_api = buscar_detalhes_jogo(
                fixture_id
            )

            if detalhes_api:
                detalhes = detalhes_api
                cantos_ht = extrair_escanteios_primeiro_tempo(
                    detalhes
                )

        if cantos_ht is None:
            continue

        historico.append({
            "fixture_id": fixture_id,
            "cantos_ht": cantos_ht,
            "gols_total": extrair_gols_total(detalhes),
            "escanteios_total": extrair_escanteios_total(detalhes)
        })

        if len(historico) >= JOGOS_HISTORICO:
            break

    with estado_lock:
        stats["historico_analisado"] += len(historico)

    salvar_stats()

    return historico


def calcular_assertividade_under(historico, linha):
    if not historico:
        return 0.0

    acertos = 0

    for partida in historico:
        valor = numero(
            partida.get("cantos_ht"),
            999
        )

        if valor < linha:
            acertos += 1

    return round(
        (acertos / len(historico)) * 100,
        2
    )


def historico_aprovado(historico, linha):
    if len(historico) < MINIMO_HISTORICO:
        return False

    assertividade = calcular_assertividade_under(
        historico,
        linha
    )

    # Filtro principal de assertividade
    return assertividade >= 80.0


# ============================================================
# MERCADO E ODDS
# ============================================================

def extrair_mercados(jogo):
    mercados = []

    possiveis = [
        jogo.get("markets"),
        jogo.get("odds"),
        jogo.get("betting"),
        jogo.get("bookmakers"),
        jogo.get("selections")
    ]

    for item in possiveis:
        if isinstance(item, list):
            mercados.extend(item)

        elif isinstance(item, dict):
            mercados.append(item)

    return mercados


def localizar_odd_under_ht(jogo):
    """
    Procura uma odd de Under 4.5 escanteios no primeiro tempo.

    A API pode retornar o mercado em formatos diferentes.
    """

    mercados = extrair_mercados(jogo)

    # Também tenta o próprio jogo como objeto de mercado
    if isinstance(jogo, dict):
        mercados.append(jogo)

    for mercado in mercados:
        if not isinstance(mercado, dict):
            continue

        texto = json.dumps(
            mercado,
            ensure_ascii=False
        ).lower()

        eh_escanteio = (
            "corner" in texto
            or "corners" in texto
            or "escanteio" in texto
        )

        eh_primeiro_tempo = (
            "first half" in texto
            or "first_half" in texto
            or "firsthalf" in texto
            or "1h" in texto
            or "ht" in texto
            or "half time" in texto
        )

        eh_under = (
            "under" in texto
            or "menos de" in texto
            or "under 4.5" in texto
        )

        if not eh_escanteio:
            continue

        if not eh_primeiro_tempo:
            continue

        if not eh_under:
            continue

        linha = (
            primeiro_valor(
                mercado,
                [
                    "line",
                    "total",
                    "handicap",
                    "value",
                    "points"
                ]
            )
        )

        linha_numero = numero(linha, 0)

        if linha_numero not in [0, 4.5]:
            # Caso o texto contenha "4.5"
            if "4.5" not in texto:
                continue

        odd = extrair_odd(mercado)

        if ODD_MINIMA_SINAL <= odd <= ODD_MAXIMA:
            return {
                "odd": odd,
                "linha": LINHA_ESCANTEIOS_HT,
                "mercado": "Under 4.5 escanteios 1º tempo"
            }

    return None


# ============================================================
# SINAIS
# ============================================================

def fixture_ja_usado(fixture_id):
    with estado_lock:
        return str(fixture_id) in fixtures_usadas


def marcar_fixture_usado(fixture_id):
    with estado_lock:
        fixtures_usadas.add(str(fixture_id))

    salvar_fixtures()


def criar_sinal(jogo):
    fixture_id = extrair_fixture_id(jogo)

    if not fixture_id:
        return None

    if fixture_ja_usado(fixture_id):
        return None

    if not jogo_ao_vivo(jogo):
        return None

    minuto = extrair_minuto(jogo)

    if minuto <= 0:
        return None

    if minuto > MINUTO_MAXIMO_ANALISE:
        return None

    home, away = extrair_times(jogo)

    odd_data = localizar_odd_under_ht(jogo)

    if not odd_data:
        return None

    historico = montar_historico(jogo)

    if not historico_aprovado(
        historico,
        LINHA_ESCANTEIOS_HT
    ):
        return None

    assertividade = calcular_assertividade_under(
        historico,
        LINHA_ESCANTEIOS_HT
    )

    sinal = {
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "minuto": minuto,
        "mercado": odd_data["mercado"],
        "linha": odd_data["linha"],
        "odd": odd_data["odd"],
        "assertividade": assertividade,
        "historico_jogos": len(historico),
        "criado_em": agora_utc(),
        "status": "PENDENTE"
    }

    with estado_lock:
        sinais_pendentes[fixture_id] = sinal
        stats["sinais_total"] += 1
        stats["pendentes"] = len(sinais_pendentes)
        stats["ultimo_sinal"] = sinal

    marcar_fixture_usado(fixture_id)
    salvar_stats()

    return sinal


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[TELEGRAM] Token ou Chat ID não configurado")
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
            timeout=20
        )

        if resposta.status_code == 200:
            print("[TELEGRAM] Mensagem enviada")
            return True

        print(
            f"[TELEGRAM] Erro {resposta.status_code}: "
            f"{resposta.text[:300]}"
        )

        return False

    except requests.RequestException as erro:
        print(f"[TELEGRAM] Erro: {erro}")
        return False


def mensagem_entrada(sinal):
    home = html.escape(str(sinal["home"]))
    away = html.escape(str(sinal["away"]))
    mercado = html.escape(str(sinal["mercado"]))

    return (
        "⚽ <b>NOVO SINAL AO VIVO</b>\n\n"
        f"🏟️ <b>{home} x {away}</b>\n"
        f"⏱️ Minuto: <b>{sinal['minuto']}'</b>\n\n"
        f"🎯 Mercado: <b>{mercado}</b>\n"
        f"💰 Odd: <b>{sinal['odd']:.2f}</b>\n"
        f"📊 Assertividade histórica: "
        f"<b>{sinal['assertividade']:.2f}%</b>\n"
        f"📚 Jogos analisados: "
        f"<b>{sinal['historico_jogos']}</b>\n\n"
        "⚠️ <i>Entrada sugerida. "
        "Aposte com responsabilidade.</i>"
    )


# ============================================================
# RESOLUÇÃO DOS SINAIS
# ============================================================

def resultado_sinal(sinal, jogo_atual):
    """
    Resolve o sinal usando os escanteios do primeiro tempo.

    Para Under 4.5:
    - WIN se total de escanteios HT < 4.5
    - LOSS se total de escanteios HT >= 4.5
    """

    cantos_ht = extrair_escanteios_primeiro_tempo(
        jogo_atual
    )

    if cantos_ht is None:
        return None

    if cantos_ht < sinal["linha"]:
        return "WIN"

    return "LOSS"


def resolver_sinais():
    if not sinais_pendentes:
        return

    for fixture_id, sinal in list(
        sinais_pendentes.items()
    ):
        detalhes = buscar_detalhes_jogo(
            fixture_id
        )

        if not detalhes:
            continue

        resultado = resultado_sinal(
            sinal,
            detalhes
        )

        if resultado is None:
            continue

        with estado_lock:
            if resultado == "WIN":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            sinais_pendentes.pop(
                fixture_id,
                None
            )

            stats["pendentes"] = len(
                sinais_pendentes
            )

        salvar_stats()

        mensagem = (
            "📌 <b>RESOLUÇÃO DO SINAL</b>\n\n"
            f"🏟️ {html.escape(str(sinal['home']))} "
            f"x "
            f"{html.escape(str(sinal['away']))}\n"
            f"🎯 {html.escape(str(sinal['mercado']))}\n"
            f"💰 Odd: <b>{sinal['odd']:.2f}</b>\n"
            f"📊 Assertividade prevista: "
            f"<b>{sinal['assertividade']:.2f}%</b>\n\n"
            f"🏁 Resultado: <b>{resultado}</b>"
        )

        enviar_telegram(mensagem)


# ============================================================
# ASSERTIVIDADE DO ROBÔ
# ============================================================

def assertividade_robo():
    with estado_lock:
        wins = inteiro(stats.get("wins"), 0)
        losses = inteiro(stats.get("losses"), 0)

    total = wins + losses

    if total == 0:
        return 0.0

    return round(
        (wins / total) * 100,
        2
    )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def analisar_jogos():
    print("[ROBO] Iniciando análise de jogos ao vivo...")

    while True:
        try:
            jogos = buscar_jogos_ao_vivo()

            with estado_lock:
                stats["ultima_analise"] = agora_utc()

            salvar_stats()

            print(
                f"[ROBO] Jogos encontrados: {len(jogos)}"
            )

            for jogo in jogos:
                try:
                    sinal = criar_sinal(jogo)

                    if sinal:
                        print(
                            "[ROBO] Novo sinal: "
                            f"{sinal['home']} x "
                            f"{sinal['away']}"
                        )

                        enviar_telegram(
                            mensagem_entrada(sinal)
                        )

                except Exception as erro:
                    print(
                        f"[ROBO] Erro ao analisar jogo: "
                        f"{erro}"
                    )

            try:
                resolver_sinais()
            except Exception as erro:
                print(
                    f"[ROBO] Erro ao resolver sinais: "
                    f"{erro}"
                )

        except Exception as erro:
            print(
                f"[ROBO] Erro geral no ciclo: {erro}"
            )

        time.sleep(INTERVALO_ANALISE)


# ============================================================
# ENDPOINTS FLASK
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "robo": "Robô de escanteios HT",
        "mercado": "Under 4.5 escanteios no 1º tempo",
        "odd_maxima": ODD_MAXIMA,
        "historico_jogos": JOGOS_HISTORICO,
        "minimo_historico": MINIMO_HISTORICO,
        "horario": agora_utc()
    })


@app.route("/status")
def status():
    with estado_lock:
        dados = dict(stats)
        dados["assertividade"] = assertividade_robo()
        dados["sinais_pendentes"] = list(
            sinais_pendentes.values()
        )
        dados["fixtures_usadas"] = len(
            fixtures_usadas
        )

    return jsonify(dados)


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "horario": agora_utc()
    })


# ============================================================
# INICIAR THREAD DO ROBÔ
# ============================================================

_thread_iniciada = False
_thread_lock = threading.Lock()


def iniciar_robo():
    global _thread_iniciada

    with _thread_lock:
        if _thread_iniciada:
            return

        thread = threading.Thread(
            target=analisar_jogos,
            daemon=True
        )

        thread.start()
        _thread_iniciada = True

        print("[ROBO] Thread iniciada")


# No Render/Gunicorn, este trecho será executado
# quando o módulo app for importado.
iniciar_robo()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "5000")
        ),
        debug=False
    )
