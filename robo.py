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
FIVE_DOLLAR_API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")

ARQUIVO_STATS = "stats.json"
ARQUIVO_FIXTURES = "fixtures_usadas.json"

MERCADO = "Under 4.5 escanteios no 1º tempo"

ODD_MAXIMA = 2.50
MINIMO_HISTORICO = 5
INTERVALO_ANALISE = 60
MINUTO_MAXIMO = 45

TIMEOUT_API = 10
MAX_TENTATIVAS_API = 4

lock = threading.Lock()


# ============================================================
# ESTATÍSTICAS INICIAIS
# ============================================================

stats = {
    "wins": 0,
    "losses": 0,
    "sinais_total": 0,
    "pendentes": 0,
    "historico_analisado": 0,
    "fixtures_usadas": 0,
    "erros_api": 0,
    "ultima_analise": None,
    "ultimo_sinal": None,
    "sinais_pendentes": [],
    "historico_sinais": []
}


# ============================================================
# UTILITÁRIOS
# ============================================================

def agora_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(mensagem):
    print(f"[{agora_utc()}] {mensagem}", flush=True)


def carregar_json(arquivo, padrao):
    if not os.path.exists(arquivo):
        return padrao

    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            dados = json.load(f)

        if isinstance(dados, type(padrao)):
            return dados

        return padrao

    except Exception as erro:
        log(f"Erro carregando {arquivo}: {erro}")
        return padrao


def salvar_json(arquivo, dados):
    try:
        arquivo_temporario = f"{arquivo}.tmp"

        with open(arquivo_temporario, "w", encoding="utf-8") as f:
            json.dump(
                dados,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(arquivo_temporario, arquivo)

    except Exception as erro:
        log(f"Erro salvando {arquivo}: {erro}")


def carregar_dados():
    global stats

    dados = carregar_json(ARQUIVO_STATS, {})

    if isinstance(dados, dict):
        for chave, valor in dados.items():
            if chave in stats:
                stats[chave] = valor

    if not isinstance(stats.get("sinais_pendentes"), list):
        stats["sinais_pendentes"] = []

    if not isinstance(stats.get("historico_sinais"), list):
        stats["historico_sinais"] = []

    log(
        f"Estatísticas carregadas: "
        f"WINS={stats['wins']} | "
        f"LOSSES={stats['losses']} | "
        f"SINAIS={stats['sinais_total']}"
    )


def salvar_stats():
    with lock:
        salvar_json(ARQUIVO_STATS, stats)


def normalizar_numero(valor, padrao=0):
    try:
        if valor is None:
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


def primeiro_valor(dados, chaves, padrao=None):
    if not isinstance(dados, dict):
        return padrao

    for chave in chaves:
        if chave in dados and dados[chave] is not None:
            return dados[chave]

    return padrao


# ============================================================
# API
# ============================================================

def headers_api():
    headers = {
        "Accept": "application/json",
        "User-Agent": "RoboEscanteios/1.0"
    }

    if FIVE_DOLLAR_API_KEY:
        headers["x-api-key"] = FIVE_DOLLAR_API_KEY
        headers["X-API-Key"] = FIVE_DOLLAR_API_KEY
        headers["Authorization"] = f"Bearer {FIVE_DOLLAR_API_KEY}"

    return headers


def requisicao_api(endpoint, params=None):
    url = f"{BASE_API.rstrip('/')}/{endpoint.lstrip('/')}"

    try:
        log(f"Consultando API: {url} | params={params}")

        resposta = requests.get(
            url,
            headers=headers_api(),
            params=params or {},
            timeout=TIMEOUT_API
        )

        log(
            f"Resposta API: {resposta.status_code} "
            f"| endpoint={endpoint}"
        )

        if resposta.status_code != 200:
            log(
                f"API retornou status {resposta.status_code}: "
                f"{resposta.text[:300]}"
            )
            return None

        try:
            return resposta.json()

        except Exception:
            log("Resposta da API não é JSON válido.")
            return None

    except requests.exceptions.Timeout:
        log(f"Timeout na API: {endpoint}")
        return None

    except requests.exceptions.ConnectionError:
        log(f"Erro de conexão com a API: {endpoint}")
        return None

    except Exception as erro:
        log(f"Erro inesperado na API: {erro}")
        return None


def extrair_lista(resposta):
    if resposta is None:
        return []

    if isinstance(resposta, list):
        return resposta

    if isinstance(resposta, dict):
        possiveis_chaves = [
            "data",
            "results",
            "fixtures",
            "matches",
            "games",
            "events",
            "response",
            "items"
        ]

        for chave in possiveis_chaves:
            valor = resposta.get(chave)

            if isinstance(valor, list):
                return valor

            if isinstance(valor, dict):
                lista_interna = extrair_lista(valor)

                if lista_interna:
                    return lista_interna

    return []


def buscar_jogos_ao_vivo():
    """
    Tenta localizar jogos ao vivo.

    A API pode utilizar endpoints diferentes.
    Por isso, são testados alguns formatos comuns.
    """

    endpoints = [
        ("fixtures", {"live": "all"}),
        ("matches", {"live": "all"}),
        ("games", {"live": "all"}),
        ("events", {"live": "all"})
    ]

    tentativas = 0

    for endpoint, params in endpoints:
        if tentativas >= MAX_TENTATIVAS_API:
            break

        tentativas += 1

        resposta = requisicao_api(endpoint, params)
        jogos = extrair_lista(resposta)

        if jogos:
            log(
                f"Jogos encontrados: {len(jogos)} "
                f"| endpoint={endpoint}"
            )
            return jogos

    log("Nenhum jogo ao vivo encontrado pela API.")

    with lock:
        stats["erros_api"] += 1

    salvar_stats()

    return []


# ============================================================
# EXTRAÇÃO DOS DADOS DOS JOGOS
# ============================================================

def extrair_id_jogo(jogo):
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
            "game_id",
            "gameId",
            "event_id",
            "eventId"
        ]
    )

    if isinstance(valor, dict):
        return primeiro_valor(valor, ["id"])

    return valor


def extrair_times(jogo):
    if not isinstance(jogo, dict):
        return "Time A", "Time B"

    casa = primeiro_valor(
        jogo,
        [
            "home_team",
            "homeTeam",
            "home",
            "team_home",
            "teamHome"
        ]
    )

    fora = primeiro_valor(
        jogo,
        [
            "away_team",
            "awayTeam",
            "away",
            "team_away",
            "teamAway"
        ]
    )

    if isinstance(casa, dict):
        casa = primeiro_valor(
            casa,
            ["name", "short_name", "shortName", "title"],
            "Time A"
        )

    if isinstance(fora, dict):
        fora = primeiro_valor(
            fora,
            ["name", "short_name", "shortName", "title"],
            "Time B"
        )

    if not casa:
        casa = "Time A"

    if not fora:
        fora = "Time B"

    return str(casa), str(fora)


def extrair_minuto(jogo):
    if not isinstance(jogo, dict):
        return 0

    periodo = primeiro_valor(
        jogo,
        [
            "minute",
            "min",
            "elapsed",
            "elapsed_minute",
            "match_minute"
        ],
        0
    )

    if isinstance(periodo, dict):
        periodo = primeiro_valor(
            periodo,
            ["elapsed", "minute", "value"],
            0
        )

    return inteiro(periodo, 0)


def extrair_status(jogo):
    if not isinstance(jogo, dict):
        return ""

    status = primeiro_valor(
        jogo,
        [
            "status",
            "state",
            "match_status",
            "game_status"
        ],
        ""
    )

    if isinstance(status, dict):
        status = primeiro_valor(
            status,
            ["short", "long", "name", "value"],
            ""
        )

    return str(status).lower()


def jogo_esta_ao_vivo(jogo):
    status = extrair_status(jogo)

    palavras_ao_vivo = [
        "live",
        "inplay",
        "in_play",
        "playing",
        "1h",
        "2h",
        "ht",
        "halftime",
        "first_half",
        "second_half"
    ]

    if any(palavra in status for palavra in palavras_ao_vivo):
        return True

    minuto = extrair_minuto(jogo)

    return 1 <= minuto <= 45


def extrair_escanteios(jogo):
    """
    Procura escanteios em diferentes formatos possíveis.
    """

    if not isinstance(jogo, dict):
        return 0

    chaves_diretas = [
        "corners",
        "corner",
        "escanteios",
        "total_corners",
        "totalCorners",
        "corners_total",
        "cornersTotal"
    ]

    valor = primeiro_valor(jogo, chaves_diretas)

    if isinstance(valor, (int, float, str)):
        return inteiro(valor, 0)

    if isinstance(valor, dict):
        total = primeiro_valor(
            valor,
            [
                "total",
                "value",
                "current",
                "count"
            ]
        )

        if total is not None:
            return inteiro(total, 0)

    if isinstance(valor, list):
        total = 0

        for item in valor:
            if isinstance(item, dict):
                total += inteiro(
                    primeiro_valor(
                        item,
                        ["value", "count", "total"],
                        0
                    ),
                    0
                )
            else:
                total += inteiro(item, 0)

        return total

    estatisticas = primeiro_valor(
        jogo,
        [
            "statistics",
            "stats",
            "match_statistics"
        ],
        []
    )

    if isinstance(estatisticas, list):
        total = 0

        for item in estatisticas:
            if not isinstance(item, dict):
                continue

            nome = str(
                primeiro_valor(
                    item,
                    ["type", "name", "key", "label"],
                    ""
                )
            ).lower()

            if "corner" in nome or "escante" in nome:
                valor_item = primeiro_valor(
                    item,
                    ["value", "count", "total"],
                    0
                )

                total += inteiro(valor_item, 0)

        return total

    if isinstance(estatisticas, dict):
        for chave, valor_estatistica in estatisticas.items():
            nome = str(chave).lower()

            if "corner" in nome or "escante" in nome:
                if isinstance(valor_estatistica, dict):
                    valor_estatistica = primeiro_valor(
                        valor_estatistica,
                        ["value", "count", "total"],
                        0
                    )

                return inteiro(valor_estatistica, 0)

    return 0


def extrair_escanteios_primeiro_tempo(jogo):
    """
    Tenta identificar os escanteios somente do primeiro tempo.

    Se a API não informar o valor específico do primeiro tempo,
    utiliza o total atual como fallback.
    """

    if not isinstance(jogo, dict):
        return 0

    chaves_ht = [
        "corners_ht",
        "corners_1h",
        "corners_first_half",
        "first_half_corners",
        "escanteios_ht",
        "escanteios_1_tempo",
        "cornersFirstHalf",
        "corners1H"
    ]

    valor = primeiro_valor(jogo, chaves_ht)

    if valor is not None:
        if isinstance(valor, dict):
            valor = primeiro_valor(
                valor,
                ["total", "value", "count"],
                0
            )

        return inteiro(valor, 0)

    primeiro_tempo = primeiro_valor(
        jogo,
        [
            "first_half",
            "firstHalf",
            "period1",
            "period_1",
            "ht"
        ]
    )

    if isinstance(primeiro_tempo, dict):
        valor = primeiro_valor(
            primeiro_tempo,
            [
                "corners",
                "corner",
                "escanteios",
                "total_corners",
                "totalCorners"
            ],
            None
        )

        if isinstance(valor, dict):
            valor = primeiro_valor(
                valor,
                ["total", "value", "count"],
                0
            )

        if valor is not None:
            return inteiro(valor, 0)

    return extrair_escanteios(jogo)


def extrair_gols(jogo):
    if not isinstance(jogo, dict):
        return 0

    valor = primeiro_valor(
        jogo,
        [
            "goals",
            "total_goals",
            "totalGoals",
            "score",
            "scores"
        ],
        0
    )

    if isinstance(valor, dict):
        casa = primeiro_valor(
            valor,
            ["home", "home_score", "homeScore"],
            0
        )

        fora = primeiro_valor(
            valor,
            ["away", "away_score", "awayScore"],
            0
        )

        return inteiro(casa, 0) + inteiro(fora, 0)

    return inteiro(valor, 0)


def extrair_odd(jogo):
    if not isinstance(jogo, dict):
        return None

    chaves_odd = [
        "odd",
        "odds",
        "price",
        "value",
        "decimal",
        "odd_under_4_5",
        "under_4_5_odd"
    ]

    valor = primeiro_valor(jogo, chaves_odd)

    if isinstance(valor, dict):
        valor = primeiro_valor(
            valor,
            [
                "decimal",
                "value",
                "price",
                "odd"
            ],
            None
        )

    if isinstance(valor, list):
        for item in valor:
            if isinstance(item, dict):
                nome = str(
                    primeiro_valor(
                        item,
                        ["name", "market", "label", "selection"],
                        ""
                    )
                ).lower()

                if (
                    "under" in nome
                    or "4.5" in nome
                    or "escante" in nome
                    or "corner" in nome
                ):
                    valor = primeiro_valor(
                        item,
                        ["odd", "price", "value", "decimal"],
                        None
                    )
                    break

    if valor is None:
        return None

    odd = normalizar_numero(valor, 0)

    if odd <= 1:
        return None

    return odd


# ============================================================
# HISTÓRICO
# ============================================================

def carregar_historico_fixtures():
    dados = carregar_json(ARQUIVO_FIXTURES, [])

    if not isinstance(dados, list):
        return []

    return dados


def salvar_historico_fixture(jogo):
    jogo_id = extrair_id_jogo(jogo)

    if jogo_id is None:
        return

    historico = carregar_historico_fixtures()

    ids_existentes = {
        str(item.get("id"))
        for item in historico
        if isinstance(item, dict)
    }

    if str(jogo_id) in ids_existentes:
        return

    casa, fora = extrair_times(jogo)

    registro = {
        "id": jogo_id,
        "home": casa,
        "away": fora,
        "corners_ht": extrair_escanteios_primeiro_tempo(jogo),
        "goals": extrair_gols(jogo),
        "minute": extrair_minuto(jogo),
        "status": extrair_status(jogo),
        "data": agora_utc()
    }

    historico.append(registro)

    if len(historico) > 5000:
        historico = historico[-5000:]

    salvar_json(ARQUIVO_FIXTURES, historico)


def contar_historico_relevante():
    historico = carregar_historico_fixtures()

    if not historico:
        return 0

    quantidade = 0

    for jogo in historico:
        if not isinstance(jogo, dict):
            continue

        escanteios = inteiro(
            jogo.get("corners_ht"),
            999
        )

        if escanteios < 5:
            quantidade += 1

    return quantidade


# ============================================================
# FILTROS
# ============================================================

def filtro_minuto(jogo):
    minuto = extrair_minuto(jogo)

    if minuto <= 0:
        return False

    if minuto > MINUTO_MAXIMO:
        return False

    return True


def filtro_escanteios(jogo):
    escanteios_ht = extrair_escanteios_primeiro_tempo(jogo)

    return escanteios_ht < 5


def filtro_odd(jogo):
    odd = extrair_odd(jogo)

    if odd is None:
        """
        Caso a API não envie odds no retorno principal,
        o sinal não é criado.
        """
        return False

    return odd <= ODD_MAXIMA


def jogo_qualificado(jogo):
    if not jogo_esta_ao_vivo(jogo):
        return False

    if not filtro_minuto(jogo):
        return False

    if not filtro_escanteios(jogo):
        return False

    if not filtro_odd(jogo):
        return False

    return True


# ============================================================
# TELEGRAM
# ============================================================

def telegram_configurado():
    return bool(TELEGRAM_TOKEN and CHAT_ID)


def enviar_telegram(mensagem):
    if not telegram_configurado():
        log("Telegram não configurado. Verifique TELEGRAM_TOKEN e CHAT_ID.")
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
            data=dados,
            timeout=15
        )

        if resposta.status_code == 200:
            log("Mensagem enviada ao Telegram.")
            return True

        log(
            f"Erro Telegram {resposta.status_code}: "
            f"{resposta.text[:500]}"
        )

        return False

    except Exception as erro:
        log(f"Erro enviando Telegram: {erro}")
        return False


# ============================================================
# SINAIS
# ============================================================

def sinal_ja_enviado(jogo_id):
    with lock:
        for sinal in stats["sinais_pendentes"]:
            if str(sinal.get("jogo_id")) == str(jogo_id):
                return True

        for sinal in stats["historico_sinais"]:
            if str(sinal.get("jogo_id")) == str(jogo_id):
                return True

    return False


def criar_sinal(jogo):
    jogo_id = extrair_id_jogo(jogo)

    if jogo_id is None:
        return None

    if sinal_ja_enviado(jogo_id):
        return None

    casa, fora = extrair_times(jogo)
    minuto = extrair_minuto(jogo)
    escanteios_ht = extrair_escanteios_primeiro_tempo(jogo)
    gols = extrair_gols(jogo)
    odd = extrair_odd(jogo)

    if odd is None:
        log(
            f"Jogo {casa} x {fora} ignorado: "
            f"odd não encontrada."
        )
        return None

    sinal = {
        "jogo_id": jogo_id,
        "home": casa,
        "away": fora,
        "mercado": MERCADO,
        "minuto_entrada": minuto,
        "escanteios_ht_entrada": escanteios_ht,
        "gols_entrada": gols,
        "odd": odd,
        "status": "PENDENTE",
        "criado_em": agora_utc(),
        "resolvido_em": None,
        "resultado": None
    }

    mensagem = (
        "⚽ <b>SINAL DE ESCANTEIOS</b>\n\n"
        f"🏟️ <b>{html.escape(casa)} x "
        f"{html.escape(fora)}</b>\n\n"
        f"🎯 <b>Mercado:</b> {MERCADO}\n"
        f"⏱️ <b>Minuto:</b> {minuto}'\n"
        f"🚩 <b>Escanteios HT:</b> {escanteios_ht}\n"
        f"⚽ <b>Gols:</b> {gols}\n"
        f"📈 <b>Odd:</b> {odd:.2f}\n"
        f"📊 <b>Histórico:</b> "
        f"{contar_historico_relevante()} jogos\n\n"
        "🟢 <b>ENTRADA SUGERIDA</b>\n"
        "⚠️ Gestão de banca obrigatória."
    )

    enviado = enviar_telegram(mensagem)

    if not enviado:
        log(
            f"Sinal não registrado porque Telegram falhou: "
            f"{casa} x {fora}"
        )
        return None

    with lock:
        stats["sinais_pendentes"].append(sinal)
        stats["sinais_total"] += 1
        stats["pendentes"] = len(stats["sinais_pendentes"])
        stats["ultimo_sinal"] = agora_utc()

    salvar_stats()

    log(
        f"SINAL ENVIADO: {casa} x {fora} "
        f"| odd={odd} | minuto={minuto}"
    )

    return sinal


# ============================================================
# RESOLUÇÃO DOS SINAIS
# ============================================================

def buscar_jogo_por_id(jogo_id, jogos):
    for jogo in jogos:
        if str(extrair_id_jogo(jogo)) == str(jogo_id):
            return jogo

    return None


def resolver_sinais(jogos):
    """
    Resolve o sinal quando:
    - O jogo termina;
    - O primeiro tempo termina;
    - O mercado fica definido.

    Para Under 4.5 HT:
    - WIN se terminar o primeiro tempo com até 4 escanteios;
    - LOSS se terminar com 5 ou mais.
    """

    resolvidos = []

    with lock:
        pendentes = list(stats["sinais_pendentes"])

    for sinal in pendentes:
        jogo_id = sinal.get("jogo_id")
        jogo = buscar_jogo_por_id(jogo_id, jogos)

        if not jogo:
            continue

        minuto = extrair_minuto(jogo)
        status = extrair_status(jogo)
        escanteios_ht = extrair_escanteios_primeiro_tempo(jogo)

        terminou_ht = (
            minuto >= 45
            or "ht" in status
            or "halftime" in status
            or "half_time" in status
        )

        terminou_jogo = any(
            palavra in status
            for palavra in [
                "finished",
                "ended",
                "complete",
                "ft",
                "final"
            ]
        )

        if not terminou_ht and not terminou_jogo:
            continue

        if escanteios_ht < 5:
            resultado = "WIN"
        else:
            resultado = "LOSS"

        sinal_atualizado = dict(sinal)
        sinal_atualizado["resultado"] = resultado
        sinal_atualizado["status"] = resultado
        sinal_atualizado["resolvido_em"] = agora_utc()
        sinal_atualizado["escanteios_ht_final"] = escanteios_ht

        resolvidos.append(sinal_atualizado)

    if not resolvidos:
        return

    for sinal_resolvido in resolvidos:
        jogo_nome = (
            f"{sinal_resolvido.get('home')} x "
            f"{sinal_resolvido.get('away')}"
        )

        resultado = sinal_resolvido["resultado"]

        if resultado == "WIN":
            emoji = "✅"
        else:
            emoji = "❌"

        mensagem = (
            f"{emoji} <b>RESULTADO DO SINAL</b>\n\n"
            f"🏟️ <b>{html.escape(jogo_nome)}</b>\n"
            f"🎯 <b>Mercado:</b> {MERCADO}\n"
            f"🚩 <b>Escanteios HT:</b> "
            f"{sinal_resolvido.get('escanteios_ht_final')}\n"
            f"📊 <b>Resultado:</b> {resultado}"
        )

        enviar_telegram(mensagem)

        with lock:
            stats["sinais_pendentes"] = [
                item
                for item in stats["sinais_pendentes"]
                if str(item.get("jogo_id"))
                != str(sinal_resolvido.get("jogo_id"))
            ]

            stats["historico_sinais"].append(sinal_resolvido)

            if resultado == "WIN":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            stats["pendentes"] = len(stats["sinais_pendentes"])

        log(
            f"SINAL RESOLVIDO: {jogo_nome} "
            f"-> {resultado}"
        )

    with lock:
        if len(stats["historico_sinais"]) > 5000:
            stats["historico_sinais"] = (
                stats["historico_sinais"][-5000:]
            )

    salvar_stats()


# ============================================================
# ASSERTIVIDADE
# ============================================================

def calcular_assertividade():
    with lock:
        wins = inteiro(stats.get("wins"), 0)
        losses = inteiro(stats.get("losses"), 0)

    total = wins + losses

    if total == 0:
        return 0.0

    return round((wins / total) * 100, 2)


# ============================================================
# ANÁLIÇÃO PRINCIPAL
# ============================================================

def analisar_jogos():
    log("Iniciando ciclo de análise.")

    with lock:
        stats["ultima_analise"] = agora_utc()

    salvar_stats()

    jogos = buscar_jogos_ao_vivo()

    if not jogos:
        log("Ciclo finalizado: nenhum jogo encontrado.")
        return

    with lock:
        stats["historico_analisado"] += len(jogos)
        stats["fixtures_usadas"] = len(jogos)

    salvar_stats()

    qualificados = 0
    sinais_criados = 0

    for jogo in jogos:
        try:
            salvar_historico_fixture(jogo)

            if not jogo_qualificado(jogo):
                continue

            qualificados += 1

            sinal = criar_sinal(jogo)

            if sinal:
                sinais_criados += 1

        except Exception as erro:
            log(f"Erro analisando jogo: {erro}")

    resolver_sinais(jogos)

    log(
        f"Ciclo finalizado | "
        f"jogos={len(jogos)} | "
        f"qualificados={qualificados} | "
        f"novos_sinais={sinais_criados} | "
        f"assertividade={calcular_assertividade()}%"
    )


# ============================================================
# LOOP DO ROBÔ
# ============================================================

def loop_robo():
    log("Loop do robô iniciado.")

    while True:
        inicio = time.time()

        try:
            analisar_jogos()

        except Exception as erro:
            log(f"Erro geral no ciclo do robô: {erro}")

            with lock:
                stats["erros_api"] += 1
                stats["ultima_analise"] = agora_utc()

            salvar_stats()

        tempo_gasto = time.time() - inicio
        espera = max(5, INTERVALO_ANALISE - tempo_gasto)

        log(
            f"Aguardando {espera:.1f} segundos "
            f"para a próxima análise."
        )

        time.sleep(espera)


def iniciar_robo():
    log("Preparando inicialização do robô.")

    carregar_dados()

    thread = threading.Thread(
        target=loop_robo,
        daemon=True,
        name="robo-escanteios"
    )

    thread.start()

    log("Thread do robô iniciada.")


# ============================================================
# ROTAS FLASK
# ============================================================

@app.route("/")
def inicio():
    with lock:
        dados = {
            "robo": "Robô de escanteios HT",
            "status": "online",
            "mercado": MERCADO,
            "odd_maxima": ODD_MAXIMA,
            "minimo_historico": MINIMO_HISTORICO,
            "historico_jogos": stats["historico_analisado"],
            "horario": agora_utc()
        }

    return jsonify(dados)


@app.route("/status")
def status():
    with lock:
        dados = {
            "status": "online",
            "wins": stats["wins"],
            "losses": stats["losses"],
            "assertividade": calcular_assertividade(),
            "sinais_total": stats["sinais_total"],
            "pendentes": len(stats["sinais_pendentes"]),
            "sinais_pendentes": stats["sinais_pendentes"],
            "historico_analisado": stats["historico_analisado"],
            "fixtures_usadas": stats["fixtures_usadas"],
            "erros_api": stats["erros_api"],
            "ultima_analise": stats["ultima_analise"],
            "ultimo_sinal": stats["ultimo_sinal"],
            "horario_servidor": agora_utc()
        }

    return jsonify(dados)


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "robo": "escanteios",
        "horario": agora_utc()
    })


# ============================================================
# INICIALIZAÇÃO
# ============================================================

iniciar_robo()


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=porta,
        debug=False,
        use_reloader=False
        )
