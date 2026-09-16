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

API_KEY = os.getenv("FIVE_DOLLAR_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))

# Estratégia
TOTAL_BANCAS = int(os.getenv("TOTAL_BANCAS", "3"))
ENTRADA_INICIAL = float(os.getenv("ENTRADA_INICIAL", "10"))
MULTIPLICADOR_GALE = float(os.getenv("MULTIPLICADOR_GALE", "2"))
MAX_GALES = int(os.getenv("MAX_GALES", "2"))

# Odds
ODD_MINIMA = float(os.getenv("ODD_MINIMA", "1.80"))
ODD_MAXIMA = float(os.getenv("ODD_MAXIMA", "2.50"))
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))

# Janela
JANELA_HORAS = int(os.getenv("JANELA_HORAS", "24"))

# A cada 15 minutos
INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "900")
)

# Limita chamadas extras de odds por ciclo
MAX_CONSULTAS_ODDS = int(
    os.getenv("MAX_CONSULTAS_ODDS", "25")
)

# Arquivos
ARQUIVO_ESTADO = "estado.json"

lock = threading.Lock()


# ============================================================
# ESTADO
# ============================================================

def estado_inicial():
    return {
        "stats": {
            "enviados": 0,
            "resolvidos": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "half_wins": 0,
            "half_losses": 0,
            "assertividade": 0.0,
            "saldo_teorico": 0.0
        },
        "bancas": {
            str(i): {
                "status": "livre",
                "gale": 0,
                "sinal_id": None
            }
            for i in range(1, TOTAL_BANCAS + 1)
        },
        "sinais": {}
    }


def carregar_estado():
    if not os.path.exists(ARQUIVO_ESTADO):
        return estado_inicial()

    try:
        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as f:
            dados = json.load(f)

        # Garante bancas novas caso TOTAL_BANCAS mude
        for i in range(1, TOTAL_BANCAS + 1):
            chave = str(i)

            if chave not in dados.get("bancas", {}):
                dados.setdefault("bancas", {})[chave] = {
                    "status": "livre",
                    "gale": 0,
                    "sinal_id": None
                }

        return dados

    except Exception as e:
        logging.error(
            "Erro carregando estado: %s",
            e
        )
        return estado_inicial()


estado = carregar_estado()


def salvar_estado():
    try:
        temp = ARQUIVO_ESTADO + ".tmp"

        with open(
            temp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                estado,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp, ARQUIVO_ESTADO)

    except Exception as e:
        logging.error(
            "Erro salvando estado: %s",
            e
        )


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        logging.error(
            "FIVE_DOLLAR_API_KEY não configurada."
        )
        return None

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-combinado/2.0"
    }

    try:
        logging.info(
            "GET %s | params=%s",
            url,
            params
        )

        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        if r.status_code != 200:
            logging.error(
                "API %s | %s",
                r.status_code,
                r.text[:1000]
            )
            return None

        return r.json()

    except Exception as e:
        logging.error(
            "Erro API: %s",
            e
        )
        return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(texto):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if r.status_code != 200:
            logging.error(
                "Telegram %s: %s",
                r.status_code,
                r.text
            )
            return False

        return True

    except Exception as e:
        logging.error(
            "Erro Telegram: %s",
            e
        )
        return False


# ============================================================
# FIXTURES
# ============================================================

def extrair_lista_fixtures(resposta):
    if not resposta:
        return []

    data = resposta.get("data", [])

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for chave in (
            "fixtures",
            "data",
            "results"
        ):
            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def buscar_fixtures():
    agora = datetime.now(timezone.utc)
    fim = agora + timedelta(hours=JANELA_HORAS)

    params = {
        "start_time": agora.isoformat(),
        "end_time": fim.isoformat(),
        "status": "scheduled",
        "per_page": 100
    }

    resposta = api_get(
        "fixtures",
        params=params
    )

    fixtures = extrair_lista_fixtures(
        resposta
    )

    logging.info(
        "%d fixtures encontrados.",
        len(fixtures)
    )

    return fixtures


def fixture_id(fixture):
    return fixture.get("id") or fixture.get("fixture_id")


def nome_times(fixture):
    home = "Casa"
    away = "Fora"

    if isinstance(fixture.get("home"), dict):
        home = fixture["home"].get(
            "name",
            home
        )

    if isinstance(fixture.get("away"), dict):
        away = fixture["away"].get(
            "name",
            away
        )

    if isinstance(fixture.get("home_team"), dict):
        home = fixture["home_team"].get(
            "name",
            home
        )

    if isinstance(fixture.get("away_team"), dict):
        away = fixture["away_team"].get(
            "name",
            away
        )

    teams = fixture.get("teams")

    if isinstance(teams, dict):
        if isinstance(teams.get("home"), dict):
            home = teams["home"].get(
                "name",
                home
            )

        if isinstance(teams.get("away"), dict):
            away = teams["away"].get(
                "name",
                away
            )

    return home, away


# ============================================================
# ODDS — FORMATO REAL DA API
# ============================================================

def buscar_odds(fid):
    resposta = api_get(
        f"fixtures/{fid}/odds"
    )

    if resposta:
        logging.info(
            "Estrutura odds fixture %s: %s",
            fid,
            json.dumps(
                resposta,
                ensure_ascii=False
            )[:5000]
        )

    return resposta


def selecionar_periodo(mercado):
    """
    Pré-jogo:
    prefere closing.
    Se closing não estiver disponível, usa opening.
    """

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
    """
    Formato confirmado:

    data
      bookmakers[]
        name
        odds
          goal_line
            opening / closing
          corner_line
            opening / closing
    """

    resultados = []

    if not resposta:
        return resultados

    data = resposta.get("data", {})

    if not isinstance(data, dict):
        return resultados

    bookmakers = data.get(
        "bookmakers",
        []
    )

    if not isinstance(bookmakers, list):
        return resultados

    for bookmaker in bookmakers:

        if not isinstance(bookmaker, dict):
            continue

        nome_bookmaker = bookmaker.get(
            "name",
            "Desconhecida"
        )

        odds = bookmaker.get(
            "odds",
            {}
        )

        if not isinstance(odds, dict):
            continue

        goal_line = odds.get(
            "goal_line"
        )

        corner_line = odds.get(
            "corner_line"
        )

        gols, periodo_gols = selecionar_periodo(
            goal_line
        )

        cantos, periodo_cantos = selecionar_periodo(
            corner_line
        )

        if not gols or not cantos:
            logging.info(
                "%s sem goal_line/corner_line utilizável.",
                nome_bookmaker
            )
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

        except (ValueError, TypeError):
            continue

        if odd_under <= 1 or odd_over <= 1:
            continue

        combinada = round(
            odd_under * odd_over,
            3
        )

        resultados.append({
            "bookmaker": nome_bookmaker,

            "linha_gols": linha_gols,
            "odd_gols": odd_under,
            "periodo_gols": periodo_gols,

            "linha_cantos": linha_cantos,
            "odd_cantos": odd_over,
            "periodo_cantos": periodo_cantos,

            "odd_combinada": combinada
        })

    return resultados


def escolher_combinacao(resposta):
    combinacoes = extrair_combinacoes(
        resposta
    )

    validas = []

    for c in combinacoes:

        logging.info(
            "%s | Over %.2f cantos @%.3f | "
            "Under %.2f gols @%.3f | "
            "Combinada %.3f",
            c["bookmaker"],
            c["linha_cantos"],
            c["odd_cantos"],
            c["linha_gols"],
            c["odd_gols"],
            c["odd_combinada"]
        )

        if (
            ODD_MINIMA
            <= c["odd_combinada"]
            <= ODD_MAXIMA
        ):
            validas.append(c)

    if not validas:
        return None

    validas.sort(
        key=lambda x: abs(
            x["odd_combinada"]
            - ODD_ALVO
        )
    )

    return validas[0]


# ============================================================
# LINHAS ASIÁTICAS
# ============================================================

def avaliar_linha(total, linha, lado):
    """
    Liquidação matemática de linhas asiáticas.

    Retorna:
    WIN
    HALF_WIN
    PUSH
    HALF_LOSS
    LOSS
    """

    total = float(total)
    linha = float(linha)

    fracao = round(
        linha - int(linha),
        2
    )

    # Linha inteira
    if fracao == 0.0:

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

    # Linha .5
    if fracao == 0.5:

        if lado == "over":
            return (
                "WIN"
                if total > linha
                else "LOSS"
            )

        return (
            "WIN"
            if total < linha
            else "LOSS"
        )

    # Linha .25:
    # divide entre X.0 e X.5
    if fracao == 0.25:
        base = int(linha)

        r1 = avaliar_linha(
            total,
            float(base),
            lado
        )

        r2 = avaliar_linha(
            total,
            float(base) + 0.5,
            lado
        )

        return combinar_metades(
            r1,
            r2
        )

    # Linha .75:
    # divide entre X.5 e X+1.0
    if fracao == 0.75:
        base = int(linha)

        r1 = avaliar_linha(
            total,
            float(base) + 0.5,
            lado
        )

        r2 = avaliar_linha(
            total,
            float(base) + 1.0,
            lado
        )

        return combinar_metades(
            r1,
            r2
        )

    # Segurança
    if lado == "over":
        return (
            "WIN"
            if total > linha
            else "LOSS"
        )

    return (
        "WIN"
        if total < linha
        else "LOSS"
    )


def combinar_metades(r1, r2):

    resultados = {r1, r2}

    if resultados == {"WIN"}:
        return "WIN"

    if resultados == {"LOSS"}:
        return "LOSS"

    if resultados == {"PUSH"}:
        return "PUSH"

    if resultados == {
        "WIN",
        "PUSH"
    }:
        return "HALF_WIN"

    if resultados == {
        "LOSS",
        "PUSH"
    }:
        return "HALF_LOSS"

    # Situação incomum em mercados normais
    if resultados == {
        "WIN",
        "LOSS"
    }:
        return "PUSH"

    return "PUSH"


# ============================================================
# RESULTADO DO JOGO
# ============================================================

def buscar_fixture(fid):
    return api_get(
        f"fixtures/{fid}"
    )


def extrair_fixture(resposta):
    if not resposta:
        return None

    data = resposta.get(
        "data",
        resposta
    )

    if isinstance(data, dict):
        return data

    return None


def extrair_status(fixture):

    for chave in (
        "status",
        "state"
    ):
        valor = fixture.get(chave)

        if isinstance(valor, str):
            return valor.lower()

        if isinstance(valor, dict):
            nome = (
                valor.get("name")
                or valor.get("short")
                or valor.get("status")
            )

            if nome:
                return str(nome).lower()

    return ""


def extrair_totais(fixture):

    goals = fixture.get(
        "goals",
        {}
    )

    corners = fixture.get(
        "corners",
        {}
    )

    if not isinstance(goals, dict):
        goals = {}

    if not isinstance(corners, dict):
        corners = {}

    gh = goals.get("home")
    ga = goals.get("away")

    ch = corners.get("home")
    ca = corners.get("away")

    try:
        if None in (gh, ga, ch, ca):
            return None

        return {
            "gols": float(gh) + float(ga),
            "cantos": float(ch) + float(ca)
        }

    except (ValueError, TypeError):
        return None


# ============================================================
# LIQUIDAÇÃO DA COMBINADA
# ============================================================

def retorno_perna(
    stake,
    odd,
    resultado
):
    """
    Retorno bruto de uma perna asiática.
    """

    if resultado == "WIN":
        return stake * odd

    if resultado == "HALF_WIN":
        metade = stake / 2

        return (
            metade * odd
            + metade
        )

    if resultado == "PUSH":
        return stake

    if resultado == "HALF_LOSS":
        return stake / 2

    return 0.0


def resolver_combinada(
    stake,
    odd_cantos,
    resultado_cantos,
    odd_gols,
    resultado_gols
):
    """
    Para fins de acompanhamento do robô,
    liquida as duas pernas sequencialmente.

    Observação:
    a liquidação exata de uma múltipla asiática
    pode depender das regras da casa.
    """

    retorno_cantos = retorno_perna(
        stake,
        odd_cantos,
        resultado_cantos
    )

    if retorno_cantos <= 0:
        return 0.0

    retorno_final = retorno_perna(
        retorno_cantos,
        odd_gols,
        resultado_gols
    )

    return round(
        retorno_final,
        2
    )


def classificar_retorno(
    stake,
    retorno
):

    if retorno <= 0:
        return "LOSS"

    if retorno < stake:
        return "HALF_LOSS"

    if abs(retorno - stake) < 0.01:
        return "PUSH"

    lucro = retorno - stake

    if lucro > 0:
        return "WIN"

    return "PUSH"


# ============================================================
# BANCAS
# ============================================================

def bancas_livres():

    return [
        int(numero)
        for numero, banca
        in estado["bancas"].items()
        if banca["status"] == "livre"
    ]


def fixture_ja_usado(fid):

    for sinal in estado["sinais"].values():

        if str(
            sinal.get("fixture_id")
        ) == str(fid):

            return True

    return False


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    fixture,
    combinacao,
    banca_numero
):

    fid = fixture_id(
        fixture
    )

    home, away = nome_times(
        fixture
    )

    banca = estado["bancas"][
        str(banca_numero)
    ]

    gale = int(
        banca["gale"]
    )

    entrada = round(
        ENTRADA_INICIAL
        * (
            MULTIPLICADOR_GALE
            ** gale
        ),
        2
    )

    sinal_id = (
        f"{fid}-"
        f"B{banca_numero}-"
        f"{int(time.time())}"
    )

    return {
        "id": sinal_id,
        "fixture_id": fid,

        "home": home,
        "away": away,

        "banca": banca_numero,
        "gale": gale,
        "entrada": entrada,

        "bookmaker": combinacao[
            "bookmaker"
        ],

        "linha_cantos": combinacao[
            "linha_cantos"
        ],

        "odd_cantos": combinacao[
            "odd_cantos"
        ],

        "linha_gols": combinacao[
            "linha_gols"
        ],

        "odd_gols": combinacao[
            "odd_gols"
        ],

        "odd_combinada": combinacao[
            "odd_combinada"
        ],

        "status": "PENDENTE",

        "criado_em": datetime.now(
            timezone.utc
        ).isoformat()
    }


def mensagem_sinal(s):

    return (
        "🎯 <b>NOVO SINAL PRÉ-JOGO</b>\n\n"

        f"⚽ <b>{s['home']} x "
        f"{s['away']}</b>\n\n"

        f"🏦 Banca: <b>{s['banca']}</b>\n"
        f"🔁 Gale: <b>{s['gale']}</b>\n"
        f"💰 Entrada: "
        f"<b>R$ {s['entrada']:.2f}</b>\n\n"

        f"🚩 Over "
        f"<b>{s['linha_cantos']}</b> "
        f"escanteios "
        f"@{s['odd_cantos']}\n"

        f"🥅 Under "
        f"<b>{s['linha_gols']}</b> "
        f"gols "
        f"@{s['odd_gols']}\n\n"

        f"🔥 Odd teórica combinada: "
        f"<b>{s['odd_combinada']}</b>\n"

        f"🏢 Fonte: "
        f"<b>{s['bookmaker']}</b>"
    )


def registrar_sinal(s):

    if not telegram(
        mensagem_sinal(s)
    ):
        return False

    estado["sinais"][
        s["id"]
    ] = s

    banca = estado["bancas"][
        str(s["banca"])
    ]

    banca["status"] = "ocupada"
    banca["sinal_id"] = s["id"]

    estado["stats"]["enviados"] += 1

    salvar_estado()

    logging.info(
        "SINAL ENVIADO | %s x %s | "
        "Banca %s | Gale %s | Odd %.3f",
        s["home"],
        s["away"],
        s["banca"],
        s["gale"],
        s["odd_combinada"]
    )

    return True


# ============================================================
# RESOLUÇÃO
# ============================================================

def atualizar_assertividade():

    stats = estado["stats"]

    decisoes = (
        stats["wins"]
        + stats["losses"]
    )

    if decisoes > 0:
        stats["assertividade"] = round(
            stats["wins"]
            / decisoes
            * 100,
            2
        )
    else:
        stats["assertividade"] = 0.0


def resolver_sinal(s):

    resposta = buscar_fixture(
        s["fixture_id"]
    )

    fixture = extrair_fixture(
        resposta
    )

    if not fixture:
        return

    status = extrair_status(
        fixture
    )

    logging.info(
        "Status fixture %s: %s",
        s["fixture_id"],
        status
    )

    # Só tenta liquidar quando os totais existirem.
    totais = extrair_totais(
        fixture
    )

    if not totais:
        return

    resultado_cantos = avaliar_linha(
        totais["cantos"],
        s["linha_cantos"],
        "over"
    )

    resultado_gols = avaliar_linha(
        totais["gols"],
        s["linha_gols"],
        "under"
    )

    retorno = resolver_combinada(
        s["entrada"],
        s["odd_cantos"],
        resultado_cantos,
        s["odd_gols"],
        resultado_gols
    )

    classificacao = classificar_retorno(
        s["entrada"],
        retorno
    )

    lucro = round(
        retorno - s["entrada"],
        2
    )

    s["status"] = "RESOLVIDO"
    s["resultado"] = classificacao

    s["resultado_cantos"] = (
        resultado_cantos
    )

    s["resultado_gols"] = (
        resultado_gols
    )

    s["gols_totais"] = (
        totais["gols"]
    )

    s["cantos_totais"] = (
        totais["cantos"]
    )

    s["retorno_teorico"] = retorno
    s["lucro_teorico"] = lucro

    s["resolvido_em"] = datetime.now(
        timezone.utc
    ).isoformat()

    stats = estado["stats"]

    stats["resolvidos"] += 1
    stats["saldo_teorico"] = round(
        stats["saldo_teorico"]
        + lucro,
        2
    )

    if classificacao == "WIN":
        stats["wins"] += 1

    elif classificacao == "LOSS":
        stats["losses"] += 1

    elif classificacao == "PUSH":
        stats["pushes"] += 1

    elif classificacao == "HALF_WIN":
        stats["half_wins"] += 1

    elif classificacao == "HALF_LOSS":
        stats["half_losses"] += 1

    banca = estado["bancas"][
        str(s["banca"])
    ]

    # Gestão do Gale:
    # perda financeira -> avança
    # resultado não negativo -> reinicia
    if lucro < 0:

        if banca["gale"] < MAX_GALES:
            banca["gale"] += 1

        else:
            banca["gale"] = 0

    else:
        banca["gale"] = 0

    banca["status"] = "livre"
    banca["sinal_id"] = None

    atualizar_assertividade()
    salvar_estado()

    telegram(
        "📊 <b>SINAL RESOLVIDO</b>\n\n"

        f"⚽ {s['home']} x "
        f"{s['away']}\n"

        f"🏦 Banca: {s['banca']}\n"
        f"🔁 Gale utilizado: {s['gale']}\n\n"

        f"🚩 Cantos: {int(totais['cantos'])} "
        f"→ <b>{resultado_cantos}</b>\n"

        f"🥅 Gols: {int(totais['gols'])} "
        f"→ <b>{resultado_gols}</b>\n\n"

        f"🎯 Resultado: "
        f"<b>{classificacao}</b>\n"

        f"💵 Entrada: "
        f"R$ {s['entrada']:.2f}\n"

        f"💰 Retorno teórico: "
        f"R$ {retorno:.2f}\n"

        f"📈 Resultado financeiro: "
        f"R$ {lucro:+.2f}\n\n"

        f"📊 Assertividade: "
        f"<b>{stats['assertividade']:.2f}%</b>\n"

        f"💼 Saldo teórico: "
        f"<b>R$ "
        f"{stats['saldo_teorico']:.2f}</b>"
    )


def resolver_pendentes():

    pendentes = [
        s
        for s in estado["sinais"].values()
        if s.get("status") == "PENDENTE"
    ]

    logging.info(
        "Sinais pendentes: %d",
        len(pendentes)
    )

    for s in pendentes:

        try:
            resolver_sinal(s)

        except Exception:
            logging.exception(
                "Erro resolvendo sinal %s",
                s.get("id")
            )


# ============================================================
# ANÁLISE
# ============================================================

def analisar():

    logging.info(
        "========== NOVO CICLO =========="
    )

    # Primeiro libera bancas resolvidas
    resolver_pendentes()

    livres = bancas_livres()

    logging.info(
        "Bancas livres: %s",
        livres
    )

    if not livres:
        logging.info(
            "Nenhuma banca livre."
        )
        return

    fixtures = buscar_fixtures()

    if not fixtures:
        logging.info(
            "Nenhum jogo nas próximas %sh.",
            JANELA_HORAS
        )
        return

    candidatos = []

    consultas = 0

    for fixture in fixtures:

        if consultas >= MAX_CONSULTAS_ODDS:
            logging.info(
                "Limite de consultas de odds "
                "atingido neste ciclo."
            )
            break

        fid = fixture_id(
            fixture
        )

        if not fid:
            continue

        if fixture_ja_usado(
            fid
        ):
            continue

        home, away = nome_times(
            fixture
        )

        logging.info(
            "Analisando %s | %s x %s",
            fid,
            home,
            away
        )

        resposta_odds = buscar_odds(
            fid
        )

        consultas += 1

        combinacao = escolher_combinacao(
            resposta_odds
        )

        if not combinacao:

            todas = extrair_combinacoes(
                resposta_odds
            )

            if todas:
                melhor = min(
                    todas,
                    key=lambda x: abs(
                        x["odd_combinada"]
                        - ODD_ALVO
                    )
                )

                logging.info(
                    "REJEITADO %s | "
                    "melhor odd disponível %.3f | "
                    "faixa %.2f-%.2f",
                    fid,
                    melhor["odd_combinada"],
                    ODD_MINIMA,
                    ODD_MAXIMA
                )

            else:
                logging.warning(
                    "REJEITADO %s | "
                    "goal_line/corner_line "
                    "não disponíveis.",
                    fid
                )

            continue

        distancia = abs(
            combinacao["odd_combinada"]
            - ODD_ALVO
        )

        candidatos.append({
            "fixture": fixture,
            "combinacao": combinacao,
            "distancia": distancia
        })

        logging.info(
            "CANDIDATO %s | "
            "Over %.2f cantos @%.3f + "
            "Under %.2f gols @%.3f | "
            "Odd %.3f",
            fid,
            combinacao["linha_cantos"],
            combinacao["odd_cantos"],
            combinacao["linha_gols"],
            combinacao["odd_gols"],
            combinacao["odd_combinada"]
        )

    # Por enquanto a ordenação usa somente
    # proximidade da odd alvo.
    # Não chamamos isso de assertividade.
    candidatos.sort(
        key=lambda x: x["distancia"]
    )

    logging.info(
        "Candidatos: %d",
        len(candidatos)
    )

    quantidade = min(
        len(livres),
        len(candidatos)
    )

    for i in range(quantidade):

        banca_numero = livres[i]
        candidato = candidatos[i]

        sinal = criar_sinal(
            candidato["fixture"],
            candidato["combinacao"],
            banca_numero
        )

        registrar_sinal(
            sinal
        )


# ============================================================
# LOOP
# ============================================================

def loop_robo():

    logging.info(
        "ROBÔ V2 INICIADO"
    )

    logging.info(
        "3 bancas | Entrada %.2f | "
        "Gales %d | Odd %.2f-%.2f",
        ENTRADA_INICIAL,
        MAX_GALES,
        ODD_MINIMA,
        ODD_MAXIMA
    )

    while True:

        try:
            with lock:
                analisar()

        except Exception:
            logging.exception(
                "Erro geral no ciclo."
            )

        time.sleep(
            INTERVALO_ANALISE
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "versao": "2.0",
        "estrategia": (
            "Over escanteios + Under gols"
        ),
        "bancas": estado["bancas"],
        "stats": estado["stats"]
    })


@app.route("/status")
def status():

    return jsonify(
        estado
    )


@app.route("/stats")
def stats():

    return jsonify(
        estado["stats"]
    )


@app.route("/sinais")
def sinais():

    return jsonify(
        estado["sinais"]
    )


@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "hora": datetime.now(
            timezone.utc
        ).isoformat()
    })


# ============================================================
# START
# ============================================================

threading.Thread(
    target=loop_robo,
    daemon=True
).start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
        )
