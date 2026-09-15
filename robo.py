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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("FIVE_DOLLAR_KEY", "").strip()

PORT = int(os.getenv("PORT", "10000"))

# Gestão
STAKE_BASE = float(os.getenv("STAKE_BASE", "10"))
GALES = [10, 20, 40, 80, 160]

# 3 bancas independentes
NUM_BANCAS = 3

# Odd desejada
ODD_ALVO = float(os.getenv("ODD_ALVO", "2.00"))
ODD_MIN = float(os.getenv("ODD_MIN", "1.85"))
ODD_MAX = float(os.getenv("ODD_MAX", "2.15"))

# Histórico
ULTIMOS_JOGOS = int(os.getenv("ULTIMOS_JOGOS", "10"))
ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "65")
)

# Cada mercado precisa atingir pelo menos isso
ASSERTIVIDADE_MERCADO_MIN = float(
    os.getenv("ASSERTIVIDADE_MERCADO_MIN", "60")
)

# Janela para procurar jogos futuros
HORAS_FUTUROS = float(os.getenv("HORAS_FUTUROS", "24"))

# Intervalos
INTERVALO_ANALISE = int(os.getenv("INTERVALO_ANALISE", "300"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "180"))

# Quantidade máxima de páginas
MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "5"))

# Arquivo persistente
ARQUIVO_ESTADO = "estado.json"

# Cache
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
CACHE_HISTORICO_TTL = int(
    os.getenv("CACHE_HISTORICO_TTL", "1800")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("robo")


# ============================================================
# ESTADO
# ============================================================

lock = threading.RLock()
api_lock = threading.Lock()

cache = {}

estado_padrao = {
    "bancas": {},
    "proximo_sinal": 1,
    "wins": 0,
    "losses": 0,
    "pushes": 0,
    "total_sinais": 0,
    "ciclos": 0,
    "ultimo_erro": None,
    "ultima_analise": None
}


def criar_bancas():
    bancas = {}

    for i in range(1, NUM_BANCAS + 1):
        bancas[str(i)] = {
            "id": i,
            "status": "livre",
            "fixture_id": None,
            "sinal_id": None,
            "gale": 0,
            "stake": GALES[0],
            "ciclo": 1,
            "fixture": None,
            "mercados": [],
            "odd": None,
            "assertividade": None,
            "historico": None,
            "enviado_em": None,
            "resultado": None,
            "resolvido_em": None,
            "lucro": 0.0
        }

    return bancas


def carregar_estado():
    if not os.path.exists(ARQUIVO_ESTADO):
        estado = estado_padrao.copy()
        estado["bancas"] = criar_bancas()
        return estado

    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            estado = json.load(f)

        if "bancas" not in estado:
            estado["bancas"] = criar_bancas()

        for i in range(1, NUM_BANCAS + 1):
            if str(i) not in estado["bancas"]:
                estado["bancas"][str(i)] = criar_bancas()[str(i)]

        return estado

    except Exception as e:
        logger.error(f"Erro carregando estado: {e}")

        estado = estado_padrao.copy()
        estado["bancas"] = criar_bancas()
        return estado


estado = carregar_estado()


def salvar_estado():
    try:
        with lock:
            tmp = ARQUIVO_ESTADO + ".tmp"

            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    estado,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(tmp, ARQUIVO_ESTADO)

    except Exception as e:
        logger.error(f"Erro salvando estado: {e}")


# ============================================================
# UTILITÁRIOS
# ============================================================

def agora():
    return datetime.now(timezone.utc)


def timestamp(dt):
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


def primeiro_numero(*valores):
    for valor in valores:
        n = numero(valor)

        if n is not None:
            return n

    return None


def percentual(vitorias, total):
    if total <= 0:
        return 0.0

    return round((vitorias / total) * 100, 2)


def formatar_odd(valor):
    if valor is None:
        return "-"

    return f"{float(valor):.2f}"


def formatar_pct(valor):
    return f"{float(valor):.1f}%"


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None, cache_ttl=CACHE_TTL):
    url = BASE_API + endpoint

    chave = url + "|" + json.dumps(
        params or {},
        sort_keys=True
    )

    agora_ts = time.time()

    # Cache
    if chave in cache:
        salvo, resposta = cache[chave]

        if agora_ts - salvo < cache_ttl:
            return resposta

    with api_lock:

        # Reconfere cache após esperar lock
        if chave in cache:
            salvo, resposta = cache[chave]

            if agora_ts - salvo < cache_ttl:
                return resposta

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json"
        }

        for tentativa in range(3):

            try:
                r = requests.get(
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=30
                )

                if r.status_code == 429:

                    retry = r.headers.get(
                        "Retry-After",
                        "30"
                    )

                    try:
                        espera = min(float(retry), 60)
                    except Exception:
                        espera = 30

                    logger.warning(
                        f"Rate limit. Aguardando {espera}s"
                    )

                    time.sleep(espera)
                    continue

                if r.status_code >= 400:

                    logger.error(
                        f"API HTTP {r.status_code}: {r.text[:500]}"
                    )

                    with lock:
                        estado["ultimo_erro"] = (
                            f"HTTP {r.status_code}: "
                            f"{r.text[:500]}"
                        )

                    salvar_estado()

                    return None

                dados = r.json()

                if not dados.get("success", 1):
                    logger.error(
                        f"API retornou erro: {dados}"
                    )
                    return None

                cache[chave] = (
                    time.time(),
                    dados
                )

                return dados

            except requests.RequestException as e:

                logger.error(
                    f"Erro de conexão API: {e}"
                )

                if tentativa < 2:
                    time.sleep(3)

        return None


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):

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
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if r.status_code != 200:
            logger.error(
                f"Telegram HTTP {r.status_code}: "
                f"{r.text[:500]}"
            )
            return False

        return True

    except Exception as e:
        logger.error(
            f"Erro Telegram: {e}"
        )
        return False


# ============================================================
# EXTRAÇÃO DE FIXTURES
# ============================================================

def extrair_lista(data):

    if not data:
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for chave in [
            "data",
            "fixtures",
            "results"
        ]:

            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def buscar_futuros():

    inicio = agora()
    fim = inicio + timedelta(
        hours=HORAS_FUTUROS
    )

    params = {
        "start_time": timestamp(inicio),
        "end_time": timestamp(fim),
        "status": "scheduled",
        "include": "odds",
        "per_page": 50
    }

    dados = api_get(
        "/fixtures",
        params,
        cache_ttl=60
    )

    if not dados:
        return []

    jogos = extrair_lista(dados)

    jogos.sort(
        key=lambda x: (
            x.get("kickoff_ts")
            or x.get("start_time")
            or 0
        )
    )

    return jogos


# ============================================================
# DADOS DO JOGO
# ============================================================

def equipes_fixture(fixture):

    teams = fixture.get("teams") or {}

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    return (
        home.get("id"),
        away.get("id"),
        home.get("name", "Casa"),
        away.get("name", "Fora")
    )


def nome_fixture(fixture):

    _, _, home, away = equipes_fixture(
        fixture
    )

    return f"{home} x {away}"


def liga_fixture(fixture):

    league = fixture.get("league") or {}

    if isinstance(league, dict):
        return league.get(
            "name",
            "Campeonato"
        )

    return str(league)


def kickoff_fixture(fixture):

    valor = (
        fixture.get("kickoff_utc")
        or fixture.get("kickoff")
        or fixture.get("date")
    )

    if not valor:
        return "Horário não informado"

    try:

        dt = datetime.fromisoformat(
            str(valor).replace(
                "Z",
                "+00:00"
            )
        )

        return dt.astimezone().strftime(
            "%H:%M"
        )

    except Exception:
        return str(valor)


# ============================================================
# HISTÓRICO DOS TIMES
# ============================================================

def buscar_historico_time(team_id):

    if not team_id:
        return []

    chave = f"hist_time_{team_id}"

    if chave in cache:

        salvo, valor = cache[chave]

        if time.time() - salvo < CACHE_HISTORICO_TTL:
            return valor

    fim = agora()
    inicio = fim - timedelta(days=180)

    params = {
        "status": "finished",
        "start_time": timestamp(inicio),
        "end_time": timestamp(fim),
        "include": "stats",
        "per_page": 50
    }

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        params,
        cache_ttl=CACHE_HISTORICO_TTL
    )

    jogos = extrair_lista(dados)

    jogos.sort(
        key=lambda x: (
            x.get("kickoff_ts")
            or 0
        ),
        reverse=True
    )

    jogos = jogos[:ULTIMOS_JOGOS]

    cache[chave] = (
        time.time(),
        jogos
    )

    return jogos


# ============================================================
# ESTATÍSTICAS HISTÓRICAS
# ============================================================

def gols_jogo(jogo):

    gols = jogo.get("goals") or {}

    h = primeiro_numero(
        gols.get("home")
    )

    a = primeiro_numero(
        gols.get("away")
    )

    if h is None or a is None:
        return None

    return h + a


def escanteios_jogo(jogo):

    corners = jogo.get(
        "corners"
    ) or {}

    total = primeiro_numero(
        corners.get("total")
    )

    if total is not None:
        return total

    h = primeiro_numero(
        corners.get("home")
    )

    a = primeiro_numero(
        corners.get("away")
    )

    if h is None or a is None:
        return None

    return h + a


def cartoes_jogo(jogo):

    cards = jogo.get(
        "cards"
    ) or {}

    if not isinstance(cards, dict):
        return None

    total = 0

    encontrou = False

    for lado in ["home", "away"]:

        dados = cards.get(lado) or {}

        amarelos = primeiro_numero(
            dados.get("yellow"),
            dados.get("yellow_cards")
        )

        vermelhos = primeiro_numero(
            dados.get("red"),
            dados.get("red_cards")
        )

        if amarelos is not None:
            total += amarelos
            encontrou = True

        if vermelhos is not None:
            total += vermelhos
            encontrou = True

    if not encontrou:
        return None

    return total


def historico_under_gols(jogos, linha):

    total = 0
    acertos = 0

    for jogo in jogos:

        gols = gols_jogo(jogo)

        if gols is None:
            continue

        total += 1

        if gols < linha:
            acertos += 1

    return acertos, total


def historico_over_cantos(jogos, linha):

    total = 0
    acertos = 0

    for jogo in jogos:

        cantos = escanteios_jogo(jogo)

        if cantos is None:
            continue

        total += 1

        if cantos > linha:
            acertos += 1

    return acertos, total


def historico_over_cartoes(jogos, linha):

    total = 0
    acertos = 0

    for jogo in jogos:

        cartoes = cartoes_jogo(jogo)

        if cartoes is None:
            continue

        total += 1

        if cartoes > linha:
            acertos += 1

    return acertos, total


# ============================================================
# ODDS
# ============================================================

def obter_odds_fixture(fixture):

    odds = fixture.get("odds")

    if odds:
        return odds

    fixture_id = fixture.get("id")

    if not fixture_id:
        return None

    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        cache_ttl=300
    )

    if not dados:
        return None

    return dados.get(
        "data",
        dados
    )


def percorrer_objeto(obj):

    if isinstance(obj, dict):

        yield obj

        for valor in obj.values():
            yield from percorrer_objeto(valor)

    elif isinstance(obj, list):

        for valor in obj:
            yield from percorrer_objeto(valor)


def eh_odd(valor):

    n = numero(valor)

    return (
        n is not None
        and 1.01 <= n <= 100
    )


def linha_numero(valor):

    if valor is None:
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()

    try:
        return float(
            texto.replace(",", ".")
        )
    except Exception:
        return None


# ============================================================
# EXTRATOR FLEXÍVEL DE MERCADOS
# ============================================================

def extrair_mercados(odds):

    """
    Tenta interpretar diferentes formatos
    possíveis retornados pela API.

    Retorna:

    {
        "goals_under": [
            {"line": 2.5, "odd": 1.40}
        ],
        "corners_over": [
            {"line": 8.5, "odd": 1.50}
        ],
        "cards_over": [
            {"line": 3.5, "odd": 1.50}
        ]
    }
    """

    resultado = {
        "goals_under": [],
        "corners_over": [],
        "cards_over": []
    }

    if not odds:
        return resultado

    # --------------------------------------------------------
    # Tentativa 1 — estruturas explícitas
    # --------------------------------------------------------

    def adicionar(tipo, linha, odd):

        linha = linha_numero(linha)
        odd = numero(odd)

        if linha is None or odd is None:
            return

        if not eh_odd(odd):
            return

        item = {
            "line": linha,
            "odd": odd
        }

        if item not in resultado[tipo]:
            resultado[tipo].append(item)

    # Percorre toda estrutura procurando nomes conhecidos
    for obj in percorrer_objeto(odds):

        if not isinstance(obj, dict):
            continue

        # ====================================================
        # GOAL LINE
        # ====================================================

        nome = " ".join(
            str(obj.get(k, ""))
            for k in [
                "market",
                "name",
                "key",
                "type",
                "slug"
            ]
        ).lower()

        # ---------------------------------------------
        # Mercado de gols
        # ---------------------------------------------

        if any(x in nome for x in [
            "goal line",
            "goalline",
            "goals",
            "total goals",
            "total_goal"
        ]):

            # Estruturas:
            # line + under
            linha = (
                obj.get("line")
                or obj.get("handicap")
                or obj.get("total")
            )

            under = (
                obj.get("under")
                or obj.get("under_odds")
                or obj.get("under_price")
            )

            if under is not None:
                adicionar(
                    "goals_under",
                    linha,
                    under
                )

            # Alguns formatos usam:
            # outcome
            # selection
            outcomes = (
                obj.get("outcomes")
                or obj.get("selections")
                or []
            )

            if isinstance(outcomes, list):

                for item in outcomes:

                    if not isinstance(item, dict):
                        continue

                    nome_item = " ".join(
                        str(item.get(k, ""))
                        for k in [
                            "name",
                            "label",
                            "selection",
                            "type"
                        ]
                    ).lower()

                    if "under" not in nome_item:
                        continue

                    linha_item = (
                        item.get("line")
                        or item.get("handicap")
                        or linha
                    )

                    odd_item = (
                        item.get("odd")
                        or item.get("price")
                        or item.get("odds")
                    )

                    adicionar(
                        "goals_under",
                        linha_item,
                        odd_item
                    )

        # ====================================================
        # CORNERS
        # ====================================================

        if any(x in nome for x in [
            "corner line",
            "cornerline",
            "corners",
            "total corners",
            "corner"
        ]):

            linha = (
                obj.get("line")
                or obj.get("handicap")
                or obj.get("total")
            )

            over = (
                obj.get("over")
                or obj.get("over_odds")
                or obj.get("over_price")
            )

            if over is not None:
                adicionar(
                    "corners_over",
                    linha,
                    over
                )

            outcomes = (
                obj.get("outcomes")
                or obj.get("selections")
                or []
            )

            if isinstance(outcomes, list):

                for item in outcomes:

                    if not isinstance(item, dict):
                        continue

                    nome_item = " ".join(
                        str(item.get(k, ""))
                        for k in [
                            "name",
                            "label",
                            "selection",
                            "type"
                        ]
                    ).lower()

                    if "over" not in nome_item:
                        continue

                    linha_item = (
                        item.get("line")
                        or item.get("handicap")
                        or linha
                    )

                    odd_item = (
                        item.get("odd")
                        or item.get("price")
                        or item.get("odds")
                    )

                    adicionar(
                        "corners_over",
                        linha_item,
                        odd_item
                    )

        # ====================================================
        # CARTÕES
        # ====================================================

        if any(x in nome for x in [
            "card line",
            "cardline",
            "cards",
            "total cards",
            "booking"
        ]):

            linha = (
                obj.get("line")
                or obj.get("handicap")
                or obj.get("total")
            )

            over = (
                obj.get("over")
                or obj.get("over_odds")
                or obj.get("over_price")
            )

            if over is not None:
                adicionar(
                    "cards_over",
                    linha,
                    over
                )

            outcomes = (
                obj.get("outcomes")
                or obj.get("selections")
                or []
            )

            if isinstance(outcomes, list):

                for item in outcomes:

                    if not isinstance(item, dict):
                        continue

                    nome_item = " ".join(
                        str(item.get(k, ""))
                        for k in [
                            "name",
                            "label",
                            "selection",
                            "type"
                        ]
                    ).lower()

                    if "over" not in nome_item:
                        continue

                    linha_item = (
                        item.get("line")
                        or item.get("handicap")
                        or linha
                    )

                    odd_item = (
                        item.get("odd")
                        or item.get("price")
                        or item.get("odds")
                    )

                    adicionar(
                        "cards_over",
                        linha_item,
                        odd_item
                    )

    # Remove duplicados
    for chave in resultado:

        vistos = set()
        novo = []

        for item in resultado[chave]:

            assinatura = (
                item["line"],
                round(item["odd"], 3)
            )

            if assinatura not in vistos:
                vistos.add(assinatura)
                novo.append(item)

        resultado[chave] = novo

    return resultado


# ============================================================
# COMBINAÇÕES
# ============================================================

def montar_combinacoes(
    fixture,
    historico_casa,
    historico_fora
):

    odds = obter_odds_fixture(
        fixture
    )

    if not odds:
        return []

    mercados = extrair_mercados(
        odds
    )

    gols = mercados["goals_under"]
    cantos = mercados["corners_over"]
    cartoes = mercados["cards_over"]

    if not gols or not cantos or not cartoes:

        logger.info(
            f"{nome_fixture(fixture)} "
            f"sem os 3 mercados necessários."
        )

        return []

    combinacoes = []

    # Junta histórico das duas equipes.
    # Cada equipe traz seus últimos 10.
    historico_total = (
        historico_casa +
        historico_fora
    )

    # Remove partidas duplicadas
    ids = set()
    historico_unico = []

    for jogo in historico_total:

        fid = jogo.get("id")

        if fid and fid in ids:
            continue

        if fid:
            ids.add(fid)

        historico_unico.append(jogo)

    # --------------------------------------------------------
    # Cada linha disponível será testada.
    # --------------------------------------------------------

    for mercado_gol in gols:

        linha_gol = mercado_gol["line"]
        odd_gol = mercado_gol["odd"]

        if linha_gol <= 0:
            continue

        ac_gol, n_gol = historico_under_gols(
            historico_unico,
            linha_gol
        )

        if n_gol < ULTIMOS_JOGOS:
            continue

        pct_gol = percentual(
            ac_gol,
            n_gol
        )

        if pct_gol < ASSERTIVIDADE_MERCADO_MIN:
            continue

        for mercado_canto in cantos:

            linha_canto = mercado_canto["line"]
            odd_canto = mercado_canto["odd"]

            ac_canto, n_canto = (
                historico_over_cantos(
                    historico_unico,
                    linha_canto
                )
            )

            if n_canto < ULTIMOS_JOGOS:
                continue

            pct_canto = percentual(
                ac_canto,
                n_canto
            )

            if pct_canto < ASSERTIVIDADE_MERCADO_MIN:
                continue

            for mercado_cartao in cartoes:

                linha_cartao = mercado_cartao["line"]
                odd_cartao = mercado_cartao["odd"]

                ac_cartao, n_cartao = (
                    historico_over_cartoes(
                        historico_unico,
                        linha_cartao
                    )
                )

                if n_cartao < ULTIMOS_JOGOS:
                    continue

                pct_cartao = percentual(
                    ac_cartao,
                    n_cartao
                )

                if pct_cartao < ASSERTIVIDADE_MERCADO_MIN:
                    continue

                odd_combinada = (
                    odd_gol *
                    odd_canto *
                    odd_cartao
                )

                if not (
                    ODD_MIN <=
                    odd_combinada <=
                    ODD_MAX
                ):
                    continue

                assertividade = (
                    pct_gol +
                    pct_canto +
                    pct_cartao
                ) / 3

                if (
                    assertividade
                    < ASSERTIVIDADE_MINIMA
                ):
                    continue

                distancia_odd = abs(
                    odd_combinada -
                    ODD_ALVO
                )

                # Penaliza combinações com histórico pior
                pontuacao = (
                    distancia_odd * 100
                    -
                    assertividade
                )

                combinacoes.append({

                    "mercados": [

                        {
                            "tipo": "Under gols",
                            "linha": linha_gol,
                            "odd": odd_gol,
                            "acertos": ac_gol,
                            "amostra": n_gol,
                            "assertividade": pct_gol
                        },

                        {
                            "tipo": "Over escanteios",
                            "linha": linha_canto,
                            "odd": odd_canto,
                            "acertos": ac_canto,
                            "amostra": n_canto,
                            "assertividade": pct_canto
                        },

                        {
                            "tipo": "Over cartões",
                            "linha": linha_cartao,
                            "odd": odd_cartao,
                            "acertos": ac_cartao,
                            "amostra": n_cartao,
                            "assertividade": pct_cartao
                        }

                    ],

                    "odd": round(
                        odd_combinada,
                        2
                    ),

                    "assertividade": round(
                        assertividade,
                        2
                    ),

                    "pontuacao": pontuacao,

                    "historico_total": len(
                        historico_unico
                    )
                })

    combinacoes.sort(
        key=lambda x: (
            x["pontuacao"],
            -x["assertividade"]
        )
    )

    return combinacoes


# ============================================================
# AVALIAÇÃO DE UM JOGO
# ============================================================

def avaliar_fixture(fixture):

    home_id, away_id, _, _ = (
        equipes_fixture(fixture)
    )

    if not home_id or not away_id:
        return None

    historico_casa = buscar_historico_time(
        home_id
    )

    historico_fora = buscar_historico_time(
        away_id
    )

    if (
        len(historico_casa) < ULTIMOS_JOGOS
        or
        len(historico_fora) < ULTIMOS_JOGOS
    ):
        logger.info(
            f"{nome_fixture(fixture)} "
            f"sem histórico suficiente."
        )

        return None

    combinacoes = montar_combinacoes(
        fixture,
        historico_casa,
        historico_fora
    )

    if not combinacoes:
        return None

    melhor = combinacoes[0]

    melhor["fixture_id"] = fixture.get(
        "id"
    )

    melhor["fixture"] = fixture

    return melhor


# ============================================================
# FIXTURES JÁ UTILIZADOS
# ============================================================

def fixtures_ativos():

    ativos = set()

    with lock:

        for banca in estado["bancas"].values():

            if banca.get("status") == "pendente":

                fid = banca.get(
                    "fixture_id"
                )

                if fid:
                    ativos.add(
                        str(fid)
                    )

    return ativos


# ============================================================
# PRÓXIMO JOGO ELEGÍVEL
# ============================================================

def encontrar_proximo_jogo(
    jogos,
    ignorar_ids=None
):

    ignorar_ids = ignorar_ids or set()

    for fixture in jogos:

        fid = fixture.get("id")

        if not fid:
            continue

        if str(fid) in ignorar_ids:
            continue

        resultado = avaliar_fixture(
            fixture
        )

        if resultado:

            return resultado

    return None


# ============================================================
# SINAL
# ============================================================

def proximo_numero_sinal():

    with lock:

        numero_sinal = estado[
            "proximo_sinal"
        ]

        estado[
            "proximo_sinal"
        ] += 1

        estado[
            "total_sinais"
        ] += 1

    salvar_estado()

    return numero_sinal


def criar_sinal_para_banca(
    banca_id,
    analise
):

    numero_sinal = proximo_numero_sinal()

    banca = estado[
        "bancas"
    ][str(banca_id)]

    gale = banca.get(
        "gale",
        0
    )

    stake = GALES[gale]

    fixture = analise[
        "fixture"
    ]

    sinal = {

        "id": numero_sinal,

        "fixture_id":
            fixture.get("id"),

        "fixture":
            nome_fixture(fixture),

        "liga":
            liga_fixture(fixture),

        "horario":
            kickoff_fixture(fixture),

        "gale":
            gale,

        "stake":
            stake,

        "odd":
            analise["odd"],

        "assertividade":
            analise["assertividade"],

        "mercados":
            analise["mercados"],

        "historico":
            analise["historico_total"],

        "enviado_em":
            agora().isoformat()
    }

    banca.update({

        "status": "pendente",

        "fixture_id":
            fixture.get("id"),

        "sinal_id":
            numero_sinal,

        "stake":
            stake,

        "fixture":
            sinal["fixture"],

        "mercados":
            sinal["mercados"],

        "odd":
            sinal["odd"],

        "assertividade":
            sinal["assertividade"],

        "historico":
            sinal["historico"],

        "enviado_em":
            sinal["enviado_em"],

        "resultado": None,

        "resolvido_em": None
    })

    salvar_estado()

    return sinal


# ============================================================
# MENSAGEM DE ENTRADA
# ============================================================

def mensagem_entrada(
    banca_id,
    sinal
):

    mercados = sinal["mercados"]

    gols = mercados[0]
    cantos = mercados[1]
    cartoes = mercados[2]

    return f"""
🚨 <b>BANCA {banca_id} — NOVO SINAL #{sinal["id"]:03d}</b>

⚽ <b>{sinal["fixture"]}</b>
🏆 {sinal["liga"]}
⏰ {sinal["horario"]}

🎯 <b>ENTRADA COMBINADA</b>

⚽ Under {gols["linha"]:.1f} gols
💰 Odd: {gols["odd"]:.2f}
📊 Histórico: {gols["acertos"]}/{gols["amostra"]} ({gols["assertividade"]:.1f}%)

🚩 Over {cantos["linha"]:.1f} escanteios
💰 Odd: {cantos["odd"]:.2f}
📊 Histórico: {cantos["acertos"]}/{cantos["amostra"]} ({cantos["assertividade"]:.1f}%)

🟨 Over {cartoes["linha"]:.1f} cartões
💰 Odd: {cartoes["odd"]:.2f}
📊 Histórico: {cartoes["acertos"]}/{cartoes["amostra"]} ({cartoes["assertividade"]:.1f}%)

━━━━━━━━━━━━━━━━━━

💰 <b>ODD COMBINADA: {sinal["odd"]:.2f}</b>

📊 <b>Assertividade histórica:
{sinal["assertividade"]:.1f}%</b>

📚 Base: últimos jogos das equipes

━━━━━━━━━━━━━━━━━━

💵 <b>GESTÃO — BANCA {banca_id}</b>

Entrada: R$ {GALES[0]:.2f}
Gale 1: R$ {GALES[1]:.2f}
Gale 2: R$ {GALES[2]:.2f}
Gale 3: R$ {GALES[3]:.2f}
Gale 4: R$ {GALES[4]:.2f}

⚠️ Exposição máxima:
<b>R$ {sum(GALES):.2f}</b>

🎯 Gale atual:
<b>Gale {sinal["gale"]}</b>

⏳ <b>Aguardando resolução...</b>
""".strip()


# ============================================================
# RESULTADO DO JOGO
# ============================================================

def resolver_mercado(
    resultado_fixture,
    mercado
):

    gols = gols_jogo(
        resultado_fixture
    )

    cantos = escanteios_jogo(
        resultado_fixture
    )

    cartoes = cartoes_jogo(
        resultado_fixture
    )

    tipo = mercado["tipo"]
    linha = mercado["linha"]

    if tipo == "Under gols":

        if gols is None:
            return None

        return gols < linha

    if tipo == "Over escanteios":

        if cantos is None:
            return None

        return cantos > linha

    if tipo == "Over cartões":

        if cartoes is None:
            return None

        return cartoes > linha

    return None


def calcular_resultado(
    fixture,
    mercados
):

    resultados = []

    for mercado in mercados:

        resultado = resolver_mercado(
            fixture,
            mercado
        )

        if resultado is None:
            return None, []

        resultados.append(
            resultado
        )

    if all(resultados):
        return "WIN", resultados

    return "LOSS", resultados


# ============================================================
# BUSCAR RESULTADO
# ============================================================

def buscar_fixture_atualizado(
    fixture_id
):

    if not fixture_id:
        return None

    dados = api_get(
        f"/fixtures/{fixture_id}",
        {
            "include": "stats,events"
        },
        cache_ttl=60
    )

    if not dados:
        return None

    return dados.get(
        "data",
        dados
    )


# ============================================================
# LUCRO
# ============================================================

def calcular_lucro(
    stake,
    odd,
    perdas_anteriores
):

    retorno = stake * odd

    lucro_aposta = retorno - stake

    lucro_ciclo = (
        lucro_aposta -
        perdas_anteriores
    )

    return round(
        lucro_ciclo,
        2
    )


def perdas_banca(
    banca
):

    gale = banca.get(
        "gale",
        0
    )

    return sum(
        GALES[:gale]
    )


# ============================================================
# ASSERTIVIDADE GLOBAL
# ============================================================

def assertividade_global():

    with lock:

        wins = estado["wins"]
        losses = estado["losses"]

        total = wins + losses

        if total == 0:
            return 0.0

        return round(
            wins / total * 100,
            2
        )


# ============================================================
# MENSAGEM DE RESOLUÇÃO
# ============================================================

def mensagem_resolucao(
    banca_id,
    banca,
    fixture,
    resultado,
    detalhes
):

    gols = fixture.get(
        "goals"
    ) or {}

    home = gols.get(
        "home"
    )

    away = gols.get(
        "away"
    )

    placar = (
        f"{home} x {away}"
        if home is not None
        and away is not None
        else "Placar indisponível"
    )

    linhas = []

    for mercado, acertou in zip(
        banca["mercados"],
        detalhes
    ):

        icone = (
            "✅"
            if acertou
            else "❌"
        )

        linhas.append(
            f'{mercado["tipo"]} '
            f'{mercado["linha"]:.1f} '
            f'→ {icone}'
        )

    bloco = "\n".join(
        linhas
    )

    if resultado == "WIN":

        perdas = perdas_banca(
            banca
        )

        lucro = calcular_lucro(
            banca["stake"],
            banca["odd"],
            perdas
        )

        cor = "🟢"

        titulo = "WIN"

    else:

        lucro = -banca["stake"]

        cor = "🔴"

        titulo = "LOSS"

    acc = assertividade_global()

    return f"""
🏁 <b>BANCA {banca_id} — RESOLUÇÃO #{banca["sinal_id"]:03d}</b>

⚽ <b>{banca["fixture"]}</b>

📊 Placar:
<b>{placar}</b>

🎯 <b>MERCADOS</b>

{bloco}

━━━━━━━━━━━━━━━━━━

{cor} <b>RESULTADO: {titulo}</b>

💰 Odd: {banca["odd"]:.2f}
🎯 Gale utilizado: {banca["gale"]}
💵 Entrada: R$ {banca["stake"]:.2f}

📈 Resultado do ciclo:
<b>R$ {lucro:+.2f}</b>

━━━━━━━━━━━━━━━━━━

📊 <b>ASSERTIVIDADE DO ROBÔ</b>

🟢 WIN: {estado["wins"]}
🔴 LOSS: {estado["losses"]}

🎯 <b>Assertividade: {acc:.2f}%</b>
""".strip()


# ============================================================
# PROCESSAR RESULTADOS
# ============================================================

def processar_resultados():

    for banca_id in range(
        1,
        NUM_BANCAS + 1
    ):

        with lock:

            banca = estado[
                "bancas"
            ][str(banca_id)]

            if banca["status"] != "pendente":
                continue

            fixture_id = banca.get(
                "fixture_id"
            )

            gale = banca.get(
                "gale",
                0
            )

        fixture = buscar_fixture_atualizado(
            fixture_id
        )

        if not fixture:
            continue

        status = str(
            fixture.get(
                "status",
                ""
            )
        ).lower()

        # Só resolve terminado
        if status not in [
            "finished",
            "ft",
            "final",
            "ended"
        ]:

            continue

        resultado, detalhes = (
            calcular_resultado(
                fixture,
                banca["mercados"]
            )
        )

        if resultado is None:
            continue

        with lock:

            banca = estado[
                "bancas"
            ][str(banca_id)
            ]

            # Evita resolução duplicada
            if banca["status"] != "pendente":
                continue

            if resultado == "WIN":

                estado["wins"] += 1

            else:

                estado["losses"] += 1

            estado["ciclos"] += 1

            banca["status"] = "resolvido"

            banca["resultado"] = resultado

            banca["resolvido_em"] = (
                agora().isoformat()
            )

        salvar_estado()

        telegram(
            mensagem_resolucao(
                banca_id,
                banca,
                fixture,
                resultado,
                detalhes
            )
        )

        # ----------------------------------------------------
        # PRÓXIMO ESTADO DA BANCA
        # ----------------------------------------------------

        with lock:

            banca = estado[
                "bancas"
            ][str(banca_id)]

            if resultado == "WIN":

                # Volta para entrada inicial
                banca["gale"] = 0
                banca["stake"] = GALES[0]

                banca["ciclo"] += 1

            else:

                if gale < len(GALES) - 1:

                    banca["gale"] = gale + 1

                    banca["stake"] = GALES[
                        gale + 1
                    ]

                else:

                    # Após perder Gale 4,
                    # encerra o ciclo e volta para R$10.
                    banca["gale"] = 0
                    banca["stake"] = GALES[0]
                    banca["ciclo"] += 1

            banca["status"] = "livre"
            banca["fixture_id"] = None
            banca["sinal_id"] = None
            banca["fixture"] = None
            banca["mercados"] = []
            banca["odd"] = None
            banca["assertividade"] = None
            banca["historico"] = None
            banca["enviado_em"] = None
            banca["resultado"] = None
            banca["resolvido_em"] = None

        salvar_estado()


# ============================================================
# PREENCHER BANCAS LIVRES
# ============================================================

def preencher_bancas():

    jogos = buscar_futuros()

    if not jogos:
        logger.warning(
            "Nenhum jogo futuro encontrado."
        )
        return

    ativos = fixtures_ativos()

    # Evita que o mesmo jogo seja
    # usado por duas bancas.
    usados = set(
        ativos
    )

    for banca_id in range(
        1,
        NUM_BANCAS + 1
    ):

        with lock:

            banca = estado[
                "bancas"
            ][str(banca_id)]

            if banca["status"] != "livre":
                continue

        analise = encontrar_proximo_jogo(
            jogos,
            usados
        )

        if not analise:
            logger.info(
                f"Nenhum jogo elegível "
                f"para Banca {banca_id}."
            )
            continue

        fixture_id = str(
            analise["fixture_id"]
        )

        usados.add(
            fixture_id
        )

        sinal = criar_sinal_para_banca(
            banca_id,
            analise
        )

        mensagem = mensagem_entrada(
            banca_id,
            sinal
        )

        sucesso = telegram(
            mensagem
        )

        if sucesso:

            logger.info(
                f"Sinal #{sinal['id']} "
                f"enviado para Banca "
                f"{banca_id}: "
                f"{sinal['fixture']}"
            )

        else:

            logger.warning(
                f"Falha ao enviar Telegram "
                f"do sinal #{sinal['id']}"
            )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def ciclo_analise():

    while True:

        try:

            logger.info(
                "Executando análise pré-jogo..."
            )

            with lock:
                estado[
                    "ultima_analise"
                ] = agora().isoformat()

            salvar_estado()

            # 1 — Resolve jogos antigos
            processar_resultados()

            # 2 — Preenche somente bancas livres
            preencher_bancas()

            with lock:

                pendentes = sum(
                    1
                    for b in estado[
                        "bancas"
                    ].values()
                    if b["status"] == "pendente"
                )

            logger.info(
                f"Bancas ocupadas: "
                f"{pendentes}/{NUM_BANCAS}"
            )

        except Exception as e:

            logger.exception(
                f"Erro no ciclo: {e}"
            )

            with lock:
                estado[
                    "ultimo_erro"
                ] = str(e)

            salvar_estado()

        time.sleep(
            INTERVALO_ANALISE
        )


def ciclo_resultados():

    while True:

        try:
            processar_resultados()

        except Exception as e:

            logger.exception(
                f"Erro monitorando resultados: {e}"
            )

        time.sleep(
            INTERVALO_RESULTADOS
        )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "3 Bancas — Under Gols + Over Escanteios + Over Cartões",
        "bancas": NUM_BANCAS,
        "odd_alvo": ODD_ALVO,
        "odd_range": [
            ODD_MIN,
            ODD_MAX
        ],
        "gestao": GALES,
        "assertividade_minima":
            ASSERTIVIDADE_MINIMA
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "timestamp": agora().isoformat()
    })


@app.route("/status")
def status():

    with lock:

        return jsonify({

            "status": "online",

            "wins":
                estado["wins"],

            "losses":
                estado["losses"],

            "pushes":
                estado["pushes"],

            "assertividade":
                assertividade_global(),

            "total_sinais":
                estado["total_sinais"],

            "ciclos":
                estado["ciclos"],

            "bancas":
                estado["bancas"],

            "ultima_analise":
                estado["ultima_analise"],

            "ultimo_erro":
                estado["ultimo_erro"]
        })


@app.route("/pendentes")
def pendentes():

    resultado = {}

    with lock:

        for id_banca, banca in (
            estado["bancas"].items()
        ):

            if banca["status"] == "pendente":

                resultado[
                    id_banca
                ] = banca

    return jsonify(resultado)


@app.route("/debug/futuros")
def debug_futuros():

    jogos = buscar_futuros()

    saida = []

    for jogo in jogos:

        saida.append({

            "id":
                jogo.get("id"),

            "fixture":
                nome_fixture(jogo),

            "liga":
                liga_fixture(jogo),

            "horario":
                kickoff_fixture(jogo),

            "status":
                jogo.get("status")
        })

    return jsonify({
        "count": len(saida),
        "jogos": saida
    })


@app.route("/debug/sinais")
def debug_sinais():

    jogos = buscar_futuros()

    saida = []

    ativos = fixtures_ativos()

    for jogo in jogos:

        if str(
            jogo.get("id")
        ) in ativos:
            continue

        try:

            analise = avaliar_fixture(
                jogo
            )

            if analise:

                saida.append({

                    "fixture":
                        nome_fixture(jogo),

                    "liga":
                        liga_fixture(jogo),

                    "odd":
                        analise["odd"],

                    "assertividade":
                        analise[
                            "assertividade"
                        ],

                    "mercados":
                        analise[
                            "mercados"
                        ]

                })

        except Exception as e:

            logger.error(
                f"Erro avaliando "
                f"{jogo.get('id')}: {e}"
            )

    return jsonify({
        "count": len(saida),
        "sinais": saida
    })


@app.route("/debug/odds/<int:fixture_id>")
def debug_odds(fixture_id):

    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        cache_ttl=30
    )

    if not dados:
        return jsonify({
            "erro": "Não foi possível obter odds"
        }), 502

    mercados = extrair_mercados(
        dados.get(
            "data",
            dados
        )
    )

    return jsonify({
        "fixture_id":
            fixture_id,

        "mercados_detectados":
            mercados,

        "raw":
            dados
    })


@app.route("/debug/historico/<int:team_id>")
def debug_historico(team_id):

    jogos = buscar_historico_time(
        team_id
    )

    saida = []

    for jogo in jogos:

        saida.append({

            "id":
                jogo.get("id"),

            "data":
                jogo.get(
                    "kickoff_utc"
                ),

            "fixture":
                nome_fixture(jogo),

            "gols":
                gols_jogo(jogo),

            "escanteios":
                escanteios_jogo(jogo),

            "cartoes":
                cartoes_jogo(jogo)
        })

    return jsonify({
        "team_id": team_id,
        "count": len(saida),
        "jogos": saida
    })


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    processar_resultados()
    preencher_bancas()

    return jsonify({
        "status": "executado",
        "bancas": estado["bancas"]
    })


@app.route("/debug/api")
def debug_api():

    dados = api_get(
        "/status",
        cache_ttl=10
    )

    if not dados:
        return jsonify({
            "status": "erro"
        }), 502

    return jsonify(dados)


# ============================================================
# THREADS
# ============================================================

def iniciar_threads():

    thread_analise = threading.Thread(
        target=ciclo_analise,
        daemon=True,
        name="analise"
    )

    thread_resultados = threading.Thread(
        target=ciclo_resultados,
        daemon=True,
        name="resultados"
    )

    thread_analise.start()
    thread_resultados.start()

    logger.info(
        "Threads do robô iniciadas."
    )


iniciar_threads()


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
                        )
