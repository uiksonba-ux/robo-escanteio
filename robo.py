import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

# Aceita os dois nomes para evitar problema com Environment antigo
API_KEY = (
    os.getenv("FIVE_DOLLAR_API_KEY")
    or os.getenv("FIVE_DOLLAR_KEY")
    or ""
).strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))
ENTRADA_INICIAL = float(os.getenv("ENTRADA_INICIAL", "10"))
MULTIPLICADOR_GALE = float(os.getenv("MULTIPLICADOR_GALE", "2"))
MAX_GALES = int(os.getenv("MAX_GALES", "2"))

ODD_MINIMA = float(os.getenv("ODD_MINIMA", "1.80"))
ODD_MAXIMA = float(os.getenv("ODD_MAXIMA", "2.50"))
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))

JANELA_HORAS = int(os.getenv("JANELA_HORAS", "24"))
INTERVALO_ANALISE = int(os.getenv("INTERVALO_ANALISE", "900"))
MAX_CONSULTAS_ODDS = int(os.getenv("MAX_CONSULTAS_ODDS", "25"))

ARQUIVO_ESTADO = "estado.json"

lock = threading.Lock()


# ============================================================
# ESTADO
# ============================================================

def criar_estado_inicial():
    bancas = {}

    for numero in range(1, TOTAL_BANCAS + 1):
        bancas[str(numero)] = {
            "status": "livre",
            "gale": 0,
            "sinal_id": None
        }

    return {
        "stats": {
            "enviados": 0,
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "assertividade": 0.0,
            "saldo_teorico": 0.0
        },
        "bancas": bancas,
        "sinais": {}
    }


def carregar_estado():
    inicial = criar_estado_inicial()

    if not os.path.exists(ARQUIVO_ESTADO):
        return inicial

    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)

        if not isinstance(dados, dict):
            return inicial

        dados.setdefault("stats", inicial["stats"])
        dados.setdefault("bancas", {})
        dados.setdefault("sinais", {})

        # Garante campos de stats mesmo se o estado for de versão antiga
        for chave, valor in inicial["stats"].items():
            dados["stats"].setdefault(chave, valor)

        # Garante as 3 bancas
        for numero in range(1, TOTAL_BANCAS + 1):
            chave = str(numero)

            if chave not in dados["bancas"]:
                dados["bancas"][chave] = {
                    "status": "livre",
                    "gale": 0,
                    "sinal_id": None
                }

        return dados

    except Exception as erro:
        logging.error("Erro carregando estado: %s", erro)
        return inicial


estado = carregar_estado()


def salvar_estado():
    try:
        temporario = ARQUIVO_ESTADO + ".tmp"

        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(
                estado,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temporario, ARQUIVO_ESTADO)

    except Exception as erro:
        logging.error("Erro salvando estado: %s", erro)


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        logging.error(
            "Chave da API não configurada. "
            "Use FIVE_DOLLAR_API_KEY ou FIVE_DOLLAR_KEY."
        )
        return None

    url = BASE_API + "/" + endpoint.lstrip("/")

    headers = {
        "Authorization": "Bearer " + API_KEY,
        "Accept": "application/json",
        "User-Agent": "robo-combinado/3.0"
    }

    try:
        logging.info("GET %s | params=%s", url, params)

        resposta = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        if resposta.status_code != 200:
            logging.error(
                "API %s | %s",
                resposta.status_code,
                resposta.text[:2000]
            )
            return None

        try:
            dados = resposta.json()
        except ValueError:
            logging.error("API retornou resposta que não é JSON.")
            return None

        if isinstance(dados, dict) and dados.get("success") == 0:
            logging.error(
                "Erro retornado pela API: %s",
                json.dumps(dados, ensure_ascii=False)
            )
            return None

        return dados

    except requests.RequestException as erro:
        logging.error("Erro de conexão com API: %s", erro)
        return None

    except Exception as erro:
        logging.exception("Erro inesperado na API: %s", erro)
        return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning(
            "TELEGRAM_TOKEN ou CHAT_ID não configurado."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
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
            timeout=20
        )

        if resposta.status_code != 200:
            logging.error(
                "Erro Telegram %s | %s",
                resposta.status_code,
                resposta.text[:1000]
            )
            return False

        return True

    except Exception as erro:
        logging.error("Erro enviando Telegram: %s", erro)
        return False


# ============================================================
# FIXTURES
# ============================================================

def extrair_lista_fixtures(resposta):
    if not resposta:
        return []

    if isinstance(resposta, list):
        return resposta

    if not isinstance(resposta, dict):
        return []

    data = resposta.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for chave in ("fixtures", "results", "data"):
            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def buscar_fixtures():
    agora = datetime.now(timezone.utc)
    fim = agora + timedelta(hours=JANELA_HORAS)

    # CORREÇÃO DO ERRO invalid_time:
    # a API exige Unix timestamp EM SEGUNDOS.
    start_timestamp = int(agora.timestamp())
    end_timestamp = int(fim.timestamp())

    params = {
        "start_time": start_timestamp,
        "end_time": end_timestamp,
        "status": "scheduled",
        "per_page": 100
    }

    logging.info(
        "Buscando fixtures | inicio=%s | fim=%s | janela=%sh",
        start_timestamp,
        end_timestamp,
        JANELA_HORAS
    )

    resposta = api_get("fixtures", params=params)
    fixtures = extrair_lista_fixtures(resposta)

    logging.info(
        "%d fixtures encontrados nas próximas %sh.",
        len(fixtures),
        JANELA_HORAS
    )

    return fixtures


def obter_fixture_id(fixture):
    if not isinstance(fixture, dict):
        return None

    return fixture.get("id") or fixture.get("fixture_id")


def obter_nome_time(valor, padrao):
    if isinstance(valor, dict):
        return (
            valor.get("name")
            or valor.get("team_name")
            or valor.get("short_name")
            or padrao
        )

    if isinstance(valor, str) and valor.strip():
        return valor.strip()

    return padrao


def obter_times(fixture):
    home = "Casa"
    away = "Fora"

    if not isinstance(fixture, dict):
        return home, away

    home = obter_nome_time(fixture.get("home"), home)
    away = obter_nome_time(fixture.get("away"), away)

    home = obter_nome_time(fixture.get("home_team"), home)
    away = obter_nome_time(fixture.get("away_team"), away)

    teams = fixture.get("teams")

    if isinstance(teams, dict):
        home = obter_nome_time(teams.get("home"), home)
        away = obter_nome_time(teams.get("away"), away)

    return home, away


# ============================================================
# ODDS
# ============================================================

def buscar_odds(fixture_id):
    resposta = api_get(
        "fixtures/" + str(fixture_id) + "/odds"
    )

    if resposta:
        logging.info(
            "Estrutura odds fixture %s: %s",
            fixture_id,
            json.dumps(
                resposta,
                ensure_ascii=False
            )[:5000]
        )

    return resposta


def selecionar_pre_jogo(mercado):
    if not isinstance(mercado, dict):
        return None, None

    closing = mercado.get("closing")

    if isinstance(closing, dict):
        return closing, "closing"

    opening = mercado.get("opening")

    if isinstance(opening, dict):
        return opening, "opening"

    return None, None


def extrair_combinacoes(resposta):
    combinacoes = []

    if not isinstance(resposta, dict):
        return combinacoes

    data = resposta.get("data")

    if not isinstance(data, dict):
        return combinacoes

    bookmakers = data.get("bookmakers")

    if not isinstance(bookmakers, list):
        return combinacoes

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue

        nome_bookmaker = bookmaker.get("name") or "Bookmaker"

        odds = bookmaker.get("odds")

        if not isinstance(odds, dict):
            continue

        # FORMATO REAL QUE APARECEU NO SEU LOG:
        # odds.goal_line
        # odds.corner_line

        goal_line = odds.get("goal_line")
        corner_line = odds.get("corner_line")

        gols, origem_gols = selecionar_pre_jogo(goal_line)
        cantos, origem_cantos = selecionar_pre_jogo(corner_line)

        if not gols or not cantos:
            continue

        linha_gols = gols.get("line")
        odd_under = gols.get("under")

        linha_cantos = cantos.get("line")
        odd_over = cantos.get("over")

        if (
            linha_gols is None
            or odd_under is None
            or linha_cantos is None
            or odd_over is None
        ):
            continue

        try:
            linha_gols = float(linha_gols)
            odd_under = float(odd_under)
            linha_cantos = float(linha_cantos)
            odd_over = float(odd_over)
        except (TypeError, ValueError):
            continue

        if odd_under <= 1 or odd_over <= 1:
            continue

        # IMPORTANTE:
        # isto é uma odd combinada matemática das duas pernas.
        # A casa pode oferecer preço diferente para Bet Builder.
        odd_combinada = round(odd_under * odd_over, 3)

        combinacoes.append({
            "bookmaker": nome_bookmaker,
            "linha_gols": linha_gols,
            "odd_gols": odd_under,
            "linha_cantos": linha_cantos,
            "odd_cantos": odd_over,
            "odd_combinada": odd_combinada,
            "origem_gols": origem_gols,
            "origem_cantos": origem_cantos
        })

    return combinacoes


def escolher_combinacao(resposta):
    combinacoes = extrair_combinacoes(resposta)
    validas = []

    for item in combinacoes:
        logging.info(
            "%s | Over %.2f cantos @%.3f | "
            "Under %.2f gols @%.3f | Combinada %.3f",
            item["bookmaker"],
            item["linha_cantos"],
            item["odd_cantos"],
            item["linha_gols"],
            item["odd_gols"],
            item["odd_combinada"]
        )

        odd = item["odd_combinada"]

        if ODD_MINIMA <= odd <= ODD_MAXIMA:
            validas.append(item)

    if not validas:
        return None

    validas.sort(
        key=lambda item: abs(item["odd_combinada"] - ODD_ALVO)
    )

    return validas[0]


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():
    livres = []

    for numero, banca in estado["bancas"].items():
        if banca.get("status") == "livre":
            livres.append(int(numero))

    return sorted(livres)


def fixture_ja_utilizado(fixture_id):
    for sinal in estado["sinais"].values():
        antigo_id = sinal.get("fixture_id")

        if str(antigo_id) == str(fixture_id):
            return True

    return False


# ============================================================
# SINAL
# ============================================================

def criar_sinal(fixture, combinacao, numero_banca):
    fid = obter_fixture_id(fixture)
    home, away = obter_times(fixture)

    banca = estado["bancas"][str(numero_banca)]
    gale = int(banca.get("gale", 0))

    entrada = ENTRADA_INICIAL * (MULTIPLICADOR_GALE ** gale)
    entrada = round(entrada, 2)

    sinal_id = (
        str(fid)
        + "-B"
        + str(numero_banca)
        + "-"
        + str(int(time.time()))
    )

    return {
        "id": sinal_id,
        "fixture_id": fid,
        "home": home,
        "away": away,
        "banca": numero_banca,
        "gale": gale,
        "entrada": entrada,
        "bookmaker": combinacao["bookmaker"],
        "linha_cantos": combinacao["linha_cantos"],
        "odd_cantos": combinacao["odd_cantos"],
        "linha_gols": combinacao["linha_gols"],
        "odd_gols": combinacao["odd_gols"],
        "odd_combinada": combinacao["odd_combinada"],
        "status": "PENDENTE",
        "criado_em": datetime.now(timezone.utc).isoformat()
    }


def montar_mensagem_sinal(sinal):
    linhas = [
        "🎯 <b>NOVO SINAL PRÉ-JOGO</b>",
        "",
        "⚽ <b>"
        + str(sinal["home"])
        + " x "
        + str(sinal["away"])
        + "</b>",
        "",
        "🏦 Banca: <b>"
        + str(sinal["banca"])
        + "</b>",
        "🔁 Gale: <b>"
        + str(sinal["gale"])
        + "</b>",
        "💰 Entrada: <b>R$ "
        + format(sinal["entrada"], ".2f")
        + "</b>",
        "",
        "🚩 Over <b>"
        + str(sinal["linha_cantos"])
        + "</b> escanteios @"
        + str(sinal["odd_cantos"]),
        "⚽ Under <b>"
        + str(sinal["linha_gols"])
        + "</b> gols @"
        + str(sinal["odd_gols"]),
        "",
        "🔥 Odd combinada teórica: <b>"
        + str(sinal["odd_combinada"])
        + "</b>",
        "🏢 Bookmaker: <b>"
        + str(sinal["bookmaker"])
        + "</b>"
    ]

    return "\n".join(linhas)


def registrar_sinal(sinal):
    mensagem = montar_mensagem_sinal(sinal)

    if not enviar_telegram(mensagem):
        logging.error(
            "Sinal %s não registrado porque o Telegram falhou.",
            sinal["id"]
        )
        return False

    estado["sinais"][sinal["id"]] = sinal

    banca = estado["bancas"][str(sinal["banca"])]
    banca["status"] = "ocupada"
    banca["sinal_id"] = sinal["id"]

    estado["stats"]["enviados"] += 1

    salvar_estado()

    logging.info(
        "SINAL ENVIADO | %s x %s | Banca %s | "
        "Gale %s | R$ %.2f | Odd %.3f",
        sinal["home"],
        sinal["away"],
        sinal["banca"],
        sinal["gale"],
        sinal["entrada"],
        sinal["odd_combinada"]
    )

    return True


# ============================================================
# RESULTADO DO JOGO
# ============================================================

def buscar_fixture_final(fixture_id):
    return api_get(
        "fixtures/" + str(fixture_id)
    )


def extrair_fixture_detalhado(resposta):
    if not isinstance(resposta, dict):
        return None

    data = resposta.get("data")

    if isinstance(data, dict):
        # Algumas APIs retornam {"data":{"fixture":{...}}}
        if isinstance(data.get("fixture"), dict):
            return data["fixture"]

        return data

    return None


def extrair_totais(fixture):
    if not isinstance(fixture, dict):
        return None

    goals = fixture.get("goals")
    corners = fixture.get("corners")

    if not isinstance(goals, dict):
        return None

    if not isinstance(corners, dict):
        return None

    gols_home = goals.get("home")
    gols_away = goals.get("away")

    cantos_home = corners.get("home")
    cantos_away = corners.get("away")

    valores = [
        gols_home,
        gols_away,
        cantos_home,
        cantos_away
    ]

    if any(valor is None for valor in valores):
        return None

    try:
        total_gols = float(gols_home) + float(gols_away)
        total_cantos = float(cantos_home) + float(cantos_away)
    except (TypeError, ValueError):
        return None

    return {
        "gols": total_gols,
        "cantos": total_cantos
    }


# ============================================================
# AVALIAÇÃO DE LINHAS
# ============================================================

def avaliar_linha_simples(total, linha, lado):
    total = float(total)
    linha = float(linha)

    if lado == "over":
        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    if total < linha:
        return "WIN"

    if total == linha:
        return "PUSH"

    return "LOSS"


def avaliar_linha(total, linha, lado):
    total = float(total)
    linha = float(linha)

    inteiro = int(linha)
    fracao = round(linha - inteiro, 2)

    # Linha inteira ou .5
    if fracao in (0.0, 0.5):
        return avaliar_linha_simples(total, linha, lado)

    # Linha .25:
    # 2.25 = metade em 2.0 e metade em 2.5
    if fracao == 0.25:
        linha_a = float(inteiro)
        linha_b = float(inteiro) + 0.5

    # Linha .75:
    # 2.75 = metade em 2.5 e metade em 3.0
    elif fracao == 0.75:
        linha_a = float(inteiro) + 0.5
        linha_b = float(inteiro) + 1.0

    else:
        return avaliar_linha_simples(total, linha, lado)

    resultado_a = avaliar_linha_simples(total, linha_a, lado)
    resultado_b = avaliar_linha_simples(total, linha_b, lado)

    dupla = {resultado_a, resultado_b}

    if dupla == {"WIN"}:
        return "WIN"

    if dupla == {"LOSS"}:
        return "LOSS"

    if dupla == {"PUSH"}:
        return "PUSH"

    if dupla == {"WIN", "PUSH"}:
        return "HALF_WIN"

    if dupla == {"LOSS", "PUSH"}:
        return "HALF_LOSS"

    # Uma metade ganha e outra perde:
    if dupla == {"WIN", "LOSS"}:
        return "PUSH"

    return "PUSH"


# ============================================================
# CÁLCULO DE RETORNO
# ============================================================

def retorno_perna(valor, odd, resultado):
    valor = float(valor)
    odd = float(odd)

    if resultado == "WIN":
        return valor * odd

    if resultado == "PUSH":
        return valor

    if resultado == "HALF_WIN":
        metade = valor / 2.0
        return (metade * odd) + metade

    if resultado == "HALF_LOSS":
        return valor / 2.0

    return 0.0


def calcular_retorno_combinada(sinal, resultado_cantos, resultado_gols):
    entrada = float(sinal["entrada"])

    retorno_cantos = retorno_perna(
        entrada,
        sinal["odd_cantos"],
        resultado_cantos
    )

    if retorno_cantos <= 0:
        return 0.0

    retorno_final = retorno_perna(
        retorno_cantos,
        sinal["odd_gols"],
        resultado_gols
    )

    return round(retorno_final, 2)


def classificar_retorno(entrada, retorno):
    entrada = float(entrada)
    retorno = float(retorno)

    lucro = round(retorno - entrada, 2)

    if lucro > 0:
        return "WIN"

    if lucro < 0:
        return "LOSS"

    return "PUSH"


# ============================================================
# ASSERTIVIDADE
# ============================================================

def atualizar_assertividade():
    stats = estado["stats"]

    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))

    decisoes = wins + losses

    if decisoes <= 0:
        stats["assertividade"] = 0.0
        return

    taxa = (wins / decisoes) * 100.0
    stats["assertividade"] = round(taxa, 2)


# ============================================================
# RESOLUÇÃO
# ============================================================

def montar_mensagem_resultado(
    sinal,
    totais,
    resultado_cantos,
    resultado_gols,
    resultado_final,
    retorno,
    lucro
):
    assertividade = estado["stats"]["assertividade"]
    saldo = estado["stats"]["saldo_teorico"]

    linhas = [
        "📊 <b>SINAL RESOLVIDO</b>",
        "",
        "⚽ "
        + str(sinal["home"])
        + " x "
        + str(sinal["away"]),
        "",
        "🏦 Banca: "
        + str(sinal["banca"]),
        "🔁 Gale utilizado: "
        + str(sinal["gale"]),
        "",
        "🚩 Escanteios totais: "
        + str(int(totais["cantos"])),
        "Over "
        + str(sinal["linha_cantos"])
        + " → <b>"
        + resultado_cantos
        + "</b>",
        "",
        "⚽ Gols totais: "
        + str(int(totais["gols"])),
        "Under "
        + str(sinal["linha_gols"])
        + " → <b>"
        + resultado_gols
        + "</b>",
        "",
        "🎯 Resultado: <b>"
        + resultado_final
        + "</b>",
        "",
        "💵 Entrada: R$ "
        + format(sinal["entrada"], ".2f"),
        "💰 Retorno teórico: R$ "
        + format(retorno, ".2f"),
        "📈 Resultado financeiro: R$ "
        + format(lucro, "+.2f"),
        "",
        "📊 Assertividade: <b>"
        + format(assertividade, ".2f")
        + "%</b>",
        "💼 Saldo teórico: <b>R$ "
        + format(saldo, ".2f")
        + "</b>"
    ]

    return "\n".join(linhas)


def resolver_sinal(sinal):
    fixture_id = sinal["fixture_id"]

    resposta = buscar_fixture_final(fixture_id)
    fixture = extrair_fixture_detalhado(resposta)

    if not fixture:
        logging.info(
            "Fixture %s ainda sem dados detalhados.",
            fixture_id
        )
        return False

    totais = extrair_totais(fixture)

    if not totais:
        logging.info(
            "Fixture %s ainda sem gols/cantos completos.",
            fixture_id
        )
        return False

    resultado_cantos = avaliar_linha(
        totais["cantos"],
        sinal["linha_cantos"],
        "over"
    )

    resultado_gols = avaliar_linha(
        totais["gols"],
        sinal["linha_gols"],
        "under"
    )

    retorno = calcular_retorno_combinada(
        sinal,
        resultado_cantos,
        resultado_gols
    )

    resultado_final = classificar_retorno(
        sinal["entrada"],
        retorno
    )

    lucro = round(
        retorno - float(sinal["entrada"]),
        2
    )

    sinal["status"] = "RESOLVIDO"
    sinal["resultado"] = resultado_final
    sinal["resultado_cantos"] = resultado_cantos
    sinal["resultado_gols"] = resultado_gols
    sinal["gols_totais"] = totais["gols"]
    sinal["cantos_totais"] = totais["cantos"]
    sinal["retorno_teorico"] = retorno
    sinal["lucro_teorico"] = lucro
    sinal["resolvido_em"] = datetime.now(timezone.utc).isoformat()

    stats = estado["stats"]

    stats["resolvidos"] += 1
    stats["saldo_teorico"] = round(
        float(stats.get("saldo_teorico", 0)) + lucro,
        2
    )

    if resultado_final == "WIN":
        stats["wins"] += 1

    elif resultado_final == "LOSS":
        stats["losses"] += 1

    else:
        stats["pushes"] += 1

    # ========================================================
    # CONTROLE DA BANCA / GALE
    # ========================================================

    banca = estado["bancas"][str(sinal["banca"])]
    gale_atual = int(banca.get("gale", 0))

    if resultado_final == "LOSS":
        if gale_atual < MAX_GALES:
            banca["gale"] = gale_atual + 1
        else:
            # Perdeu no último Gale -> encerra ciclo
            banca["gale"] = 0
    else:
        # WIN ou PUSH -> reinicia
        banca["gale"] = 0

    banca["status"] = "livre"
    banca["sinal_id"] = None

    atualizar_assertividade()
    salvar_estado()

    mensagem = montar_mensagem_resultado(
        sinal,
        totais,
        resultado_cantos,
        resultado_gols,
        resultado_final,
        retorno,
        lucro
    )

    enviar_telegram(mensagem)

    logging.info(
        "RESOLVIDO | fixture=%s | resultado=%s | lucro=%+.2f",
        fixture_id,
        resultado_final,
        lucro
    )

    return True


def resolver_pendentes():
    pendentes = []

    for sinal in estado["sinais"].values():
        if sinal.get("status") == "PENDENTE":
            pendentes.append(sinal)

    logging.info("Sinais pendentes: %d", len(pendentes))

    for sinal in pendentes:
        try:
            resolver_sinal(sinal)

        except Exception as erro:
            logging.exception(
                "Erro resolvendo sinal %s: %s",
                sinal.get("id"),
                erro
            )


# ============================================================
# ANÁLISE
# ============================================================

def analisar():
    logging.info("=" * 60)
    logging.info("NOVO CICLO DE ANÁLISE")
    logging.info("=" * 60)

    # Primeiro tenta liberar bancas de jogos já resolvidos
    resolver_pendentes()

    livres = bancas_livres()

    logging.info("Bancas livres: %s", livres)

    if not livres:
        logging.info(
            "Todas as bancas estão ocupadas. "
            "Nenhum novo sinal será enviado."
        )
        return

    fixtures = buscar_fixtures()

    if not fixtures:
        logging.info("Nenhum fixture encontrado
