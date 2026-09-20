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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("robo-favoritos")

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
)

API_KEY = os.getenv("FIVE_DOLLAR_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# ESTRATÉGIA
# ============================================================

# ---------- JOGO 1 ----------
# Vitória simples no favorito

ODD_MIN_SIMPLES = float(
    os.getenv("ODD_MIN_SIMPLES", "1.20")
)

ODD_MAX_SIMPLES = float(
    os.getenv("ODD_MAX_SIMPLES", "1.70")
)

MIN_WIN_FAVORITO = float(
    os.getenv("MIN_WIN_FAVORITO", "0.65")
)

MIN_LOSS_ADVERSARIO = float(
    os.getenv("MIN_LOSS_ADVERSARIO", "0.40")
)


# ---------- JOGO 2 ----------
# Dupla chance no favorito

ODD_MIN_DUPLA = float(
    os.getenv("ODD_MIN_DUPLA", "1.71")
)

ODD_MAX_DUPLA = float(
    os.getenv("ODD_MAX_DUPLA", "2.20")
)

MIN_NAO_PERDE_FAVORITO = float(
    os.getenv("MIN_NAO_PERDE_FAVORITO", "0.70")
)

MIN_NAO_VENCE_ADVERSARIO = float(
    os.getenv("MIN_NAO_VENCE_ADVERSARIO", "0.60")
)


# ---------- HISTÓRICO ----------

QTD_HISTORICO = int(
    os.getenv("QTD_HISTORICO", "20")
)

MIN_JOGOS_HISTORICO = int(
    os.getenv("MIN_JOGOS_HISTORICO", "20")
)


# ---------- JANELA ----------

JANELA_HORAS = float(
    os.getenv("JANELA_HORAS", "12")
)

INTERVALO_ANALISE = int(
    os.getenv("INTERVALO_ANALISE", "120")
)

INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "120")
)


# ============================================================
# RATE LIMIT
# ============================================================

API_MIN_INTERVAL_SECONDS = float(
    os.getenv("API_MIN_INTERVAL_SECONDS", "6.8")
)

API_BACKOFF_429 = int(
    os.getenv("API_BACKOFF_429", "65")
)

ultimo_request = 0.0
request_lock = threading.Lock()


# ============================================================
# ARQUIVOS
# ============================================================

ARQUIVO_SINAL = "sinal_favoritos_v20.json"
ARQUIVO_STATS = "stats_favoritos_v20.json"
ARQUIVO_CACHE = "cache_favoritos_v20.json"


# ============================================================
# LOCKS
# ============================================================

lock_sinal = threading.Lock()
lock_stats = threading.Lock()
lock_cache = threading.Lock()


# ============================================================
# HELPERS JSON
# ============================================================

def carregar_json(arquivo, padrao):
    try:
        if os.path.exists(arquivo):
            with open(arquivo, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("Erro carregando %s: %s", arquivo, e)

    return padrao


def salvar_json(arquivo, dados):
    temp = arquivo + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(
            dados,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp, arquivo)


# ============================================================
# ESTATÍSTICAS
# ============================================================

stats = carregar_json(
    ARQUIVO_STATS,
    {
        "bilhetes_win": 0,
        "bilhetes_loss": 0,
        "jogo_simples_win": 0,
        "jogo_simples_loss": 0,
        "jogo_dupla_win": 0,
        "jogo_dupla_loss": 0,
        "total_sinais": 0
    }
)


def taxa(wins, losses):
    total = wins + losses

    if total == 0:
        return 0.0

    return round((wins / total) * 100, 2)


def taxa_bilhetes():
    return taxa(
        stats["bilhetes_win"],
        stats["bilhetes_loss"]
    )


# ============================================================
# SINAL ATUAL
# ============================================================

sinal_atual = carregar_json(
    ARQUIVO_SINAL,
    None
)


def salvar_sinal():
    with lock_sinal:
        salvar_json(
            ARQUIVO_SINAL,
            sinal_atual
        )


def existe_sinal_pendente():
    return (
        isinstance(sinal_atual, dict)
        and sinal_atual.get("status") == "PENDENTE"
    )


# ============================================================
# CACHE
# ============================================================

cache = carregar_json(
    ARQUIVO_CACHE,
    {
        "historicos": {}
    }
)


def salvar_cache():
    with lock_cache:
        salvar_json(
            ARQUIVO_CACHE,
            cache
        )


# ============================================================
# API
# ============================================================

def headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "robo-favoritos-v20/1.0"
    }


def api_get(endpoint, params=None):
    global ultimo_request

    with request_lock:

        agora = time.time()

        espera = (
            API_MIN_INTERVAL_SECONDS
            - (agora - ultimo_request)
        )

        if espera > 0:
            time.sleep(espera)

        url = BASE_API + endpoint

        try:

            response = requests.get(
                url,
                headers=headers(),
                params=params,
                timeout=30
            )

            ultimo_request = time.time()

            if response.status_code == 429:

                retry = response.headers.get(
                    "Retry-After"
                )

                if retry:
                    try:
                        espera429 = int(retry) + 2
                    except:
                        espera429 = API_BACKOFF_429
                else:
                    espera429 = API_BACKOFF_429

                logger.warning(
                    "Rate limit. Aguardando %ss",
                    espera429
                )

                time.sleep(espera429)

                return api_get(
                    endpoint,
                    params
                )

            response.raise_for_status()

            dados = response.json()

            if not dados.get("success"):
                logger.warning(
                    "API success=0: %s",
                    dados
                )
                return None

            return dados.get("data")

        except Exception as e:

            logger.error(
                "Erro API %s: %s",
                endpoint,
                e
            )

            return None


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(texto):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        r = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        return r.ok

    except Exception as e:

        logger.error(
            "Erro Telegram: %s",
            e
        )

        return False


# ============================================================
# EXTRAÇÃO
# ============================================================

def obter_times(fixture):

    teams = fixture.get("teams") or {}

    home = teams.get("home") or {}
    away = teams.get("away") or {}

    return {
        "home_id": home.get("id"),
        "home": home.get("name", "Mandante"),
        "away_id": away.get("id"),
        "away": away.get("name", "Visitante")
    }


def extrair_gols(fixture):

    goals = fixture.get("goals")

    if not isinstance(goals, dict):
        return None

    try:
        home = int(goals.get("home"))
        away = int(goals.get("away"))

        return home, away

    except:
        return None


# ============================================================
# ODDS 1X2 BET365
# ============================================================

def obter_odds_1x2(fixture_id):

    dados = api_get(
        f"/fixtures/{fixture_id}/odds",
        {
            "bookmakers": "bet365",
            "market": "1x2"
        }
    )

    if not dados:
        return None

    bookmakers = dados.get("bookmakers") or []

    bet365 = None

    for book in bookmakers:
        if book.get("slug") == "bet365":
            bet365 = book
            break

    if not bet365:
        return None

    odds = bet365.get("odds") or {}

    mercado = (
        odds.get("1x2")
        or odds.get("1X2")
    )

    if not mercado:
        return None

    # Preferimos closing.
    fase = (
        mercado.get("closing")
        or mercado.get("opening")
    )

    if not fase:
        return None

    try:

        home = float(fase["home"])
        draw = float(fase["draw"])
        away = float(fase["away"])

    except:
        return None

    return {
        "home": home,
        "draw": draw,
        "away": away
    }


# ============================================================
# IDENTIFICAR FAVORITO
# ============================================================

def identificar_favorito(fixture, odds):

    times = obter_times(fixture)

    if odds["home"] < odds["away"]:

        return {
            "lado": "home",
            "team_id": times["home_id"],
            "nome": times["home"],
            "adversario_id": times["away_id"],
            "adversario": times["away"],
            "odd": odds["home"]
        }

    elif odds["away"] < odds["home"]:

        return {
            "lado": "away",
            "team_id": times["away_id"],
            "nome": times["away"],
            "adversario_id": times["home_id"],
            "adversario": times["home"],
            "odd": odds["away"]
        }

    return None


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico(team_id):

    chave = str(team_id)

    dados_cache = (
        cache.get("historicos", {})
        .get(chave)
    )

    # Cache por 6 horas
    if dados_cache:

        ts = dados_cache.get("ts", 0)

        if time.time() - ts < 21600:
            return dados_cache.get(
                "jogos",
                []
            )

    dados = api_get(
        f"/teams/{team_id}/fixtures",
        {
            "status": "finished",
            "order": "desc",
            "per_page": QTD_HISTORICO
        }
    )

    if not isinstance(dados, list):
        return []

    jogos = dados[:QTD_HISTORICO]

    cache.setdefault(
        "historicos",
        {}
    )[chave] = {
        "ts": time.time(),
        "jogos": jogos
    }

    salvar_cache()

    return jogos


# ============================================================
# DESEMPENHO DE UM TIME
# ============================================================

def analisar_time(team_id):

    jogos = buscar_historico(team_id)

    wins = 0
    draws = 0
    losses = 0
    validos = 0

    gols_pro = 0
    gols_contra = 0

    for jogo in jogos:

        placar = extrair_gols(jogo)

        if placar is None:
            continue

        times = obter_times(jogo)

        home_id = times["home_id"]
        away_id = times["away_id"]

        gh, ga = placar

        if team_id == home_id:

            gp = gh
            gc = ga

        elif team_id == away_id:

            gp = ga
            gc = gh

        else:
            continue

        validos += 1

        gols_pro += gp
        gols_contra += gc

        if gp > gc:
            wins += 1

        elif gp == gc:
            draws += 1

        else:
            losses += 1

    if validos == 0:
        return None

    return {
        "jogos": validos,

        "wins": wins,
        "draws": draws,
        "losses": losses,

        "taxa_win": wins / validos,

        "taxa_draw": draws / validos,

        "taxa_loss": losses / validos,

        "taxa_nao_perde":
            (wins + draws) / validos,

        "taxa_nao_vence":
            (draws + losses) / validos,

        "media_gols_pro":
            gols_pro / validos,

        "media_gols_contra":
            gols_contra / validos
    }


# ============================================================
# ANALISAR JOGO PARA VITÓRIA SIMPLES
# ============================================================

def analisar_simples(fixture, favorito):

    odd = favorito["odd"]

    if not (
        ODD_MIN_SIMPLES
        <= odd
        <= ODD_MAX_SIMPLES
    ):
        return None

    hist_fav = analisar_time(
        favorito["team_id"]
    )

    hist_adv = analisar_time(
        favorito["adversario_id"]
    )

    if not hist_fav or not hist_adv:
        return None

    if (
        hist_fav["jogos"]
        < MIN_JOGOS_HISTORICO
    ):
        return None

    if (
        hist_adv["jogos"]
        < MIN_JOGOS_HISTORICO
    ):
        return None

    if (
        hist_fav["taxa_win"]
        < MIN_WIN_FAVORITO
    ):
        return None

    if (
        hist_adv["taxa_loss"]
        < MIN_LOSS_ADVERSARIO
    ):
        return None

    return {
        "fixture_id": fixture.get("id"),

        "home":
            obter_times(fixture)["home"],

        "away":
            obter_times(fixture)["away"],

        "kickoff":
            fixture.get("kickoff_utc"),

        "favorito":
            favorito["nome"],

        "favorito_lado":
            favorito["lado"],

        "odd":
            odd,

        "mercado":
            "VITORIA_SIMPLES",

        "selecao":
            favorito["nome"],

        "historico_favorito":
            hist_fav,

        "historico_adversario":
            hist_adv
    }


# ============================================================
# ANALISAR JOGO PARA DUPLA CHANCE
# ============================================================

def analisar_dupla(fixture, favorito):

    odd = favorito["odd"]

    if not (
        ODD_MIN_DUPLA
        <= odd
        <= ODD_MAX_DUPLA
    ):
        return None

    hist_fav = analisar_time(
        favorito["team_id"]
    )

    hist_adv = analisar_time(
        favorito["adversario_id"]
    )

    if not hist_fav or not hist_adv:
        return None

    if (
        hist_fav["jogos"]
        < MIN_JOGOS_HISTORICO
    ):
        return None

    if (
        hist_adv["jogos"]
        < MIN_JOGOS_HISTORICO
    ):
        return None

    if (
        hist_fav["taxa_nao_perde"]
        < MIN_NAO_PERDE_FAVORITO
    ):
        return None

    if (
        hist_adv["taxa_nao_vence"]
        < MIN_NAO_VENCE_ADVERSARIO
    ):
        return None

    if favorito["lado"] == "home":

        selecao = "1X"

    else:

        selecao = "X2"

    return {
        "fixture_id": fixture.get("id"),

        "home":
            obter_times(fixture)["home"],

        "away":
            obter_times(fixture)["away"],

        "kickoff":
            fixture.get("kickoff_utc"),

        "favorito":
            favorito["nome"],

        "favorito_lado":
            favorito["lado"],

        # Odd usada para identificar o grau
        # de favoritismo. NÃO é a odd da DC.
        "odd_favorito_1x2":
            odd,

        "mercado":
            "DUPLA_CHANCE",

        "selecao":
            selecao,

        "historico_favorito":
            hist_fav,

        "historico_adversario":
            hist_adv
    }


# ============================================================
# LISTAR PRÓXIMOS JOGOS
# ============================================================

def buscar_proximos_jogos():

    inicio = datetime.now(
        timezone.utc
    )

    fim = inicio + timedelta(
        hours=JANELA_HORAS
    )

    params = {
        "start_time":
            int(inicio.timestamp()),

        "end_time":
            int(fim.timestamp()),

        "status":
            "scheduled",

        "per_page":
            100
    }

    dados = api_get(
        "/fixtures",
        params
    )

    if not isinstance(dados, list):
        return []

    return dados


# ============================================================
# ESCOLHER OS DOIS JOGOS
# ============================================================

def procurar_bilhete():

    if existe_sinal_pendente():

        logger.info(
            "Existe sinal pendente. "
            "Aguardando resolução."
        )

        return

    fixtures = buscar_proximos_jogos()

    if not fixtures:

        logger.info(
            "Nenhum fixture encontrado."
        )

        return

    candidatos_simples = []
    candidatos_dupla = []

    for fixture in fixtures:

        if existe_sinal_pendente():
            return

        fixture_id = fixture.get("id")

        if not fixture_id:
            continue

        logger.info(
            "Analisando %s x %s",
            obter_times(fixture)["home"],
            obter_times(fixture)["away"]
        )

        odds = obter_odds_1x2(
            fixture_id
        )

        if not odds:
            logger.info(
                "Sem 1X2 Bet365."
            )
            continue

        favorito = identificar_favorito(
            fixture,
            odds
        )

        if not favorito:
            continue

        logger.info(
            "Favorito: %s | odd %.2f",
            favorito["nome"],
            favorito["odd"]
        )

        # Vitória simples
        simples = analisar_simples(
            fixture,
            favorito
        )

        if simples:

            logger.info(
                "APROVADO SIMPLES: %s",
                favorito["nome"]
            )

            candidatos_simples.append(
                simples
            )

        # Dupla chance
        dupla = analisar_dupla(
            fixture,
            favorito
        )

        if dupla:

            logger.info(
                "APROVADO DUPLA: %s %s",
                favorito["nome"],
                dupla["selecao"]
            )

            candidatos_dupla.append(
                dupla
            )

        # Assim que tivermos pelo menos
        # um de cada, podemos montar.
        if (
            candidatos_simples
            and candidatos_dupla
        ):

            simples_escolhido = None
            dupla_escolhida = None

            # Evita usar o mesmo fixture
            for s in candidatos_simples:

                for d in candidatos_dupla:

                    if (
                        s["fixture_id"]
                        != d["fixture_id"]
                    ):

                        simples_escolhido = s
                        dupla_escolhida = d
                        break

                if simples_escolhido:
                    break

            if (
                simples_escolhido
                and dupla_escolhida
            ):

                criar_bilhete(
                    simples_escolhido,
                    dupla_escolhida
                )

                return

    logger.info(
        "Ainda não há combinação "
        "de dois jogos aprovada."
    )


# ============================================================
# CRIAR BILHETE
# ============================================================

def criar_bilhete(
    jogo_simples,
    jogo_dupla
):

    global sinal_atual

    if existe_sinal_pendente():
        return

    sinal_id = (
        datetime.now(timezone.utc)
        .strftime("%Y%m%d%H%M%S")
    )

    sinal_atual = {
        "id": sinal_id,
        "versao": "V20",
        "status": "PENDENTE",
        "resultado": None,
        "criado_em":
            datetime.now(timezone.utc)
            .isoformat(),

        "jogo_simples":
            jogo_simples,

        "jogo_dupla":
            jogo_dupla
    }

    salvar_sinal()

    with lock_stats:

        stats["total_sinais"] += 1

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

    hf1 = (
        jogo_simples[
            "historico_favorito"
        ]
    )

    ha1 = (
        jogo_simples[
            "historico_adversario"
        ]
    )

    hf2 = (
        jogo_dupla[
            "historico_favorito"
        ]
    )

    ha2 = (
        jogo_dupla[
            "historico_adversario"
        ]
    )

    mensagem = (
        "🎯 <b>SINAL V20 — BILHETE 2 JOGOS</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⭐ <b>JOGO 1 — VITÓRIA SIMPLES</b>\n\n"

        f"⚽ {jogo_simples['home']} x "
        f"{jogo_simples['away']}\n"

        f"🏆 <b>{jogo_simples['favorito']} vence</b>\n"

        f"💰 Odd Bet365: "
        f"{jogo_simples['odd']:.2f}\n\n"

        f"📊 Favorito venceu "
        f"{hf1['taxa_win'] * 100:.1f}% "
        f"dos últimos {hf1['jogos']}\n"

        f"📉 Adversário perdeu "
        f"{ha1['taxa_loss'] * 100:.1f}% "
        f"dos últimos {ha1['jogos']}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🛡️ <b>JOGO 2 — DUPLA CHANCE</b>\n\n"

        f"⚽ {jogo_dupla['home']} x "
        f"{jogo_dupla['away']}\n"

        f"⭐ Favorito: "
        f"{jogo_dupla['favorito']}\n"

        f"🎟️ <b>{jogo_dupla['selecao']}</b>\n"

        f"📌 Odd 1X2 do favorito: "
        f"{jogo_dupla['odd_favorito_1x2']:.2f}\n"

        "ℹ️ A API não fornece a odd específica "
        "da dupla chance.\n\n"

        f"📊 Favorito não perdeu em "
        f"{hf2['taxa_nao_perde'] * 100:.1f}% "
        f"dos últimos {hf2['jogos']}\n"

        f"📉 Adversário não venceu em "
        f"{ha2['taxa_nao_vence'] * 100:.1f}% "
        f"dos últimos {ha2['jogos']}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        "⏳ <b>STATUS: PENDENTE</b>\n\n"

        "🔒 O robô só enviará outro sinal "
        "depois da resolução deste bilhete."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "BILHETE %s ENVIADO",
        sinal_id
    )


# ============================================================
# BUSCAR RESULTADO
# ============================================================

def buscar_fixture(fixture_id):

    return api_get(
        f"/fixtures/{fixture_id}"
    )


# ============================================================
# RESOLVER VITÓRIA SIMPLES
# ============================================================

def resolver_simples(
    jogo,
    fixture
):

    placar = extrair_gols(fixture)

    if placar is None:
        return None

    gh, ga = placar

    lado = jogo["favorito_lado"]

    if lado == "home":

        return (
            "WIN"
            if gh > ga
            else "LOSS"
        )

    return (
        "WIN"
        if ga > gh
        else "LOSS"
    )


# ============================================================
# RESOLVER DUPLA CHANCE
# ============================================================

def resolver_dupla(
    jogo,
    fixture
):

    placar = extrair_gols(fixture)

    if placar is None:
        return None

    gh, ga = placar

    selecao = jogo["selecao"]

    if selecao == "1X":

        return (
            "WIN"
            if gh >= ga
            else "LOSS"
        )

    if selecao == "X2":

        return (
            "WIN"
            if ga >= gh
            else "LOSS"
        )

    return None


# ============================================================
# VERIFICAR RESULTADO DO BILHETE
# ============================================================

def verificar_resultado():

    global sinal_atual

    if not existe_sinal_pendente():
        return

    simples = sinal_atual[
        "jogo_simples"
    ]

    dupla = sinal_atual[
        "jogo_dupla"
    ]

    fixture1 = buscar_fixture(
        simples["fixture_id"]
    )

    if not fixture1:
        return

    fixture2 = buscar_fixture(
        dupla["fixture_id"]
    )

    if not fixture2:
        return

    # Só liquida quando os dois
    # estiverem oficialmente finished.
    if (
        fixture1.get("status")
        != "finished"
    ):
        return

    if (
        fixture2.get("status")
        != "finished"
    ):
        return

    r1 = resolver_simples(
        simples,
        fixture1
    )

    r2 = resolver_dupla(
        dupla,
        fixture2
    )

    if not r1 or not r2:
        return

    if (
        r1 == "WIN"
        and r2 == "WIN"
    ):
        resultado = "WIN"
    else:
        resultado = "LOSS"

    sinal_atual["resultado_simples"] = r1
    sinal_atual["resultado_dupla"] = r2
    sinal_atual["resultado"] = resultado
    sinal_atual["status"] = "RESOLVIDO"
    sinal_atual["resolvido_em"] = (
        datetime.now(timezone.utc)
        .isoformat()
    )

    salvar_sinal()

    # Estatísticas
    with lock_stats:

        if r1 == "WIN":
            stats[
                "jogo_simples_win"
            ] += 1
        else:
            stats[
                "jogo_simples_loss"
            ] += 1

        if r2 == "WIN":
            stats[
                "jogo_dupla_win"
            ] += 1
        else:
            stats[
                "jogo_dupla_loss"
            ] += 1

        if resultado == "WIN":
            stats[
                "bilhetes_win"
            ] += 1
        else:
            stats[
                "bilhetes_loss"
            ] += 1

        salvar_json(
            ARQUIVO_STATS,
            stats
        )

    placar1 = extrair_gols(
        fixture1
    )

    placar2 = extrair_gols(
        fixture2
    )

    mensagem = (
        "📊 <b>RESULTADO V20</b>\n\n"

        f"⚽ {simples['home']} x "
        f"{simples['away']}\n"

        f"Placar: "
        f"{placar1[0]} x {placar1[1]}\n"

        f"Vitória {simples['favorito']}: "
        f"<b>{r1}</b>\n\n"

        f"⚽ {dupla['home']} x "
        f"{dupla['away']}\n"

        f"Placar: "
        f"{placar2[0]} x {placar2[1]}\n"

        f"Dupla Chance {dupla['selecao']}: "
        f"<b>{r2}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"🎯 RESULTADO DO BILHETE: "
        f"<b>{resultado}</b>\n\n"

        f"📈 Assertividade dos bilhetes: "
        f"<b>{taxa_bilhetes():.2f}%</b>\n"

        f"✅ {stats['bilhetes_win']} WIN\n"
        f"❌ {stats['bilhetes_loss']} LOSS\n\n"

        "🔓 Busca pelo próximo sinal liberada."
    )

    enviar_telegram(
        mensagem
    )

    logger.info(
        "Bilhete resolvido: %s",
        resultado
    )


# ============================================================
# LOOPS
# ============================================================

def loop_analise():

    time.sleep(10)

    while True:

        try:

            if not existe_sinal_pendente():
                procurar_bilhete()

        except Exception:
            logger.exception(
                "Erro no loop de análise"
            )

        time.sleep(
            INTERVALO_ANALISE
        )


def loop_resultados():

    time.sleep(30)

    while True:

        try:

            if existe_sinal_pendente():
                verificar_resultado()

        except Exception:
            logger.exception(
                "Erro verificando resultado"
            )

        time.sleep(
            INTERVALO_RESULTADOS
        )


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    pendente = existe_sinal_pendente()

    return jsonify({
        "online": True,
        "versao": "V20",
        "estrategia": (
            "2 jogos: favorito simples + "
            "dupla chance"
        ),
        "sinal_pendente": pendente,
        "total_sinais":
            stats["total_sinais"],
        "wins":
            stats["bilhetes_win"],
        "losses":
            stats["bilhetes_loss"],
        "assertividade":
            taxa_bilhetes()
    })


@app.route("/status")
def status():

    return jsonify({
        "versao": "V20",

        "sinal_atual":
            sinal_atual,

        "config": {
            "simples": {
                "odd_min":
                    ODD_MIN_SIMPLES,
                "odd_max":
                    ODD_MAX_SIMPLES,
                "min_win_favorito":
                    MIN_WIN_FAVORITO,
                "min_loss_adversario":
                    MIN_LOSS_ADVERSARIO
            },

            "dupla": {
                "odd_min_favorito":
                    ODD_MIN_DUPLA,
                "odd_max_favorito":
                    ODD_MAX_DUPLA,
                "min_nao_perde_favorito":
                    MIN_NAO_PERDE_FAVORITO,
                "min_nao_vence_adversario":
                    MIN_NAO_VENCE_ADVERSARIO
            },

            "historico":
                QTD_HISTORICO,

            "min_jogos":
                MIN_JOGOS_HISTORICO,

            "janela_horas":
                JANELA_HORAS
        },

        "stats":
            stats
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "version": "V20"
    })


# ============================================================
# START
# ============================================================

def iniciar_threads():

    threading.Thread(
        target=loop_analise,
        daemon=True
    ).start()

    threading.Thread(
        target=loop_resultados,
        daemon=True
    ).start()


iniciar_threads()


if __name__ == "__main__":

    logger.info(
        "ROBÔ V20 INICIADO"
    )

    logger.info(
        "Jogo 1: favorito vence %.2f–%.2f",
        ODD_MIN_SIMPLES,
        ODD_MAX_SIMPLES
    )

    logger.info(
        "Jogo 2: dupla chance | "
        "favorito 1X2 %.2f–%.2f",
        ODD_MIN_DUPLA,
        ODD_MAX_DUPLA
    )

    app.run(
        host="0.0.0.0",
        port=PORT
        )
