import os
import json
import time
import threading
import html
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# CONFIGURAÇÃO
# ============================================================

app = Flask(__name__)

BASE_API = "https://api.5dollarfootballapi.com/v1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Odds
ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))

# Quantidade máxima de sinais por rodada
QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

# Jogo analisado aproximadamente 3 horas antes
HORAS_ANTES = float(os.getenv("HORAS_ANTES", "3"))
JANELA_MINUTOS = int(os.getenv("JANELA_MINUTOS", "5"))

# Intervalos
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "300"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "300"))

# Histórico mínimo para considerar assertividade
MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))

# Assertividade mínima
ASSERTIVIDADE_MINIMA = float(
    os.getenv("ASSERTIVIDADE_MINIMA", "60")
)

# Arquivo local
ARQUIVO_ESTADO = "bot_state.json"


# ============================================================
# ESTADO
# ============================================================

lock = threading.Lock()

estado = {
    "stats": {
        "combinados_wins": 0,
        "combinados_losses": 0,
        "combinados_push": 0,

        "gols_wins": 0,
        "gols_losses": 0,
        "gols_push": 0,

        "cantos_wins": 0,
        "cantos_losses": 0,
        "cantos_push": 0,

        "total_sinais": 0
    },

    "pendentes": {},

    "historico": []
}


# ============================================================
# PERSISTÊNCIA
# ============================================================

def salvar_estado():
    try:
        with lock:
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

    except Exception as e:
        print("Erro ao salvar estado:", e)


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

            if "stats" in dados:
                estado["stats"].update(
                    dados["stats"]
                )

            if "pendentes" in dados:
                estado["pendentes"] = dados["pendentes"]

            if "historico" in dados:
                estado["historico"] = dados["historico"]

    except Exception as e:
        print("Erro ao carregar estado:", e)


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print("Telegram não configurado.")

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

        resposta = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if resposta.ok:
            return True

        print(
            "Erro Telegram:",
            resposta.status_code,
            resposta.text
        )

    except Exception as e:

        print(
            "Erro ao enviar Telegram:",
            e
        )

    return False


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:
        print("FIVE_DOLLAR_KEY não configurada.")
        return None

    url = f"{BASE_API}{endpoint}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }

    try:

        resposta = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=25
        )

        if resposta.status_code != 200:

            print(
                "Erro API:",
                resposta.status_code,
                resposta.text[:500]
            )

            return None

        return resposta.json()

    except Exception as e:

        print(
            "Erro comunicação API:",
            e
        )

        return None


# ============================================================
# UTILIDADES
# ============================================================

def extrair_lista(data):

    if not data:
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for chave in (
            "data",
            "fixtures",
            "results",
            "matches"
        ):

            valor = data.get(chave)

            if isinstance(valor, list):
                return valor

    return []


def extrair_id(jogo):

    if not isinstance(jogo, dict):
        return None

    for chave in (
        "id",
        "fixture_id",
        "fixtureId"
    ):

        if jogo.get(chave) is not None:
            return str(jogo[chave])

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for chave in (
            "id",
            "fixture_id"
        ):

            if fixture.get(chave) is not None:
                return str(fixture[chave])

    return None


def extrair_times(jogo):

    home = "Casa"
    away = "Fora"

    if not isinstance(jogo, dict):
        return home, away

    teams = jogo.get("teams")

    if isinstance(teams, dict):

        h = teams.get("home")
        a = teams.get("away")

        if isinstance(h, dict):
            home = (
                h.get("name")
                or h.get("team_name")
                or home
            )

        if isinstance(a, dict):
            away = (
                a.get("name")
                or a.get("team_name")
                or away
            )

    home = (
        jogo.get("home_team")
        or jogo.get("home")
        or home
    )

    away = (
        jogo.get("away_team")
        or jogo.get("away")
        or away
    )

    return str(home), str(away)


def extrair_placar(jogo):

    gols_casa = 0
    gols_fora = 0

    if not isinstance(jogo, dict):
        return 0, 0

    score = jogo.get("score")

    if isinstance(score, dict):

        home = score.get("home")
        away = score.get("away")

        if isinstance(home, dict):
            gols_casa = (
                home.get("goals")
                or home.get("current")
                or 0
            )
        else:
            gols_casa = home or 0

        if isinstance(away, dict):
            gols_fora = (
                away.get("goals")
                or away.get("current")
                or 0
            )
        else:
            gols_fora = away or 0

    return (
        int(gols_casa or 0),
        int(gols_fora or 0)
    )


def extrair_cantos(jogo):

    if not isinstance(jogo, dict):
        return 0

    # Possíveis estruturas
    for chave in (
        "corners",
        "corner",
        "total_corners"
    ):

        valor = jogo.get(chave)

        if isinstance(valor, (int, float)):
            return int(valor)

        if isinstance(valor, dict):

            for sub in (
                "total",
                "current",
                "value"
            ):

                if isinstance(
                    valor.get(sub),
                    (int, float)
                ):
                    return int(valor[sub])

    statistics = jogo.get("statistics")

    if isinstance(statistics, dict):

        for chave in (
            "corners",
            "corner"
        ):

            valor = statistics.get(chave)

            if isinstance(
                valor,
                (int, float)
            ):
                return int(valor)

    return 0


def extrair_status(jogo):

    if not isinstance(jogo, dict):
        return ""

    status = jogo.get("status")

    if isinstance(status, dict):

        return str(
            status.get("short")
            or status.get("long")
            or status.get("status")
            or ""
        ).upper()

    return str(status or "").upper()


def eh_finalizado(jogo):

    status = extrair_status(jogo)

    finais = {
        "FT",
        "AET",
        "PEN",
        "FINISHED",
        "FINALIZADO",
        "ENDED"
    }

    return status in finais


# ============================================================
# DATA/HORA
# ============================================================

def extrair_inicio_timestamp(jogo):

    valores = []

    if isinstance(jogo, dict):

        valores.extend([
            jogo.get("start_time"),
            jogo.get("startTime"),
            jogo.get("date"),
            jogo.get("datetime"),
            jogo.get("kickoff")
        ])

        fixture = jogo.get("fixture")

        if isinstance(fixture, dict):

            valores.extend([
                fixture.get("date"),
                fixture.get("start_time")
            ])

    for valor in valores:

        if not valor:
            continue

        if isinstance(valor, (int, float)):

            return float(valor)

        texto = str(valor)

        try:

            texto = texto.replace(
                "Z",
                "+00:00"
            )

            dt = datetime.fromisoformat(
                texto
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.timestamp()

        except Exception:
            pass

    return None


def minutos_ate_jogo(jogo):

    ts = extrair_inicio_timestamp(jogo)

    if ts is None:
        return None

    agora = datetime.now(
        timezone.utc
    ).timestamp()

    return (ts - agora) / 60


def esta_na_janela_3h(jogo):

    minutos = minutos_ate_jogo(jogo)

    if minutos is None:
        return False

    alvo = HORAS_ANTES * 60

    return abs(
        minutos - alvo
    ) <= JANELA_MINUTOS


# ============================================================
# ODDS
# ============================================================

def extrair_mercado_bookmakers(
    data,
    mercado
):

    resultado = []

    if not isinstance(data, dict):
        return resultado

    bookmakers = data.get(
        "bookmakers"
    )

    if not isinstance(bookmakers, list):

        bookmakers = data.get(
            "data",
            []
        )

    if not isinstance(bookmakers, list):
        return resultado

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        nome_bookmaker = (
            bookmaker.get("name")
            or bookmaker.get("bookmaker")
            or "Bookmaker"
        )

        mercados = (
            bookmaker.get("bets")
            or bookmaker.get("markets")
            or bookmaker.get("odds")
            or []
        )

        if isinstance(
            mercados,
            dict
        ):

            mercados = list(
                mercados.values()
            )

        if not isinstance(
            mercados,
            list
        ):
            continue

        for bloco in mercados:

            if not isinstance(
                bloco,
                dict
            ):
                continue

            nome = str(
                bloco.get("name")
                or bloco.get("market")
                or bloco.get("key")
                or ""
            ).lower()

            if mercado == "gols":

                aceito = (
                    "goal" in nome
                    or "goalline" in nome
                    or "total goals" in nome
                    or "gols" in nome
                )

            else:

                aceito = (
                    "corner" in nome
                    or "corners" in nome
                    or "escanteio" in nome
                    or "cantos" in nome
                )

            if aceito:

                resultado.append({
                    "bookmaker": nome_bookmaker,
                    "bloco": bloco
                })

    return resultado


def extrair_preco_do_bloco(bloco):

    if not isinstance(
        bloco,
        dict
    ):
        return None, None

    # Prioridade
    fontes = [
        bloco.get("closing"),
        bloco.get("opening"),
        bloco.get("current"),
        bloco
    ]

    for fonte in fontes:

        if not isinstance(
            fonte,
            dict
        ):
            continue

        linha = (
            fonte.get("line")
            or fonte.get("goal_line")
            or fonte.get("corner_line")
            or fonte.get("handicap")
        )

        over = (
            fonte.get("over")
            or fonte.get("odds")
            or fonte.get("price")
            or fonte.get("value")
        )

        try:

            if isinstance(
                over,
                dict
            ):

                over = (
                    over.get("price")
                    or over.get("odd")
                    or over.get("value")
                )

            if linha is not None:
                linha = float(linha)

            if over is not None:
                over = float(over)

            if (
                linha is not None
                and over is not None
            ):
                return linha, over

        except Exception:
            pass

    return None, None


def obter_melhor_odd(
    fixture_id,
    mercado
):

    if mercado == "gols":
        endpoint_mercado = "goalline"
    else:
        endpoint_mercado = "corner"

    data = api_get(
        f"/fixtures/{fixture_id}/odds",
        params={
            "market": endpoint_mercado,
            "lang": "pt"
        }
    )

    if not data:
        return None

    blocos = extrair_mercado_bookmakers(
        data,
        mercado
    )

    candidatos = []

    for item in blocos:

        linha, odd = extrair_preco_do_bloco(
            item["bloco"]
        )

        if linha is None or odd is None:
            continue

        if not (
            ODD_MIN
            <= odd
            <= ODD_MAX
        ):
            continue

        candidatos.append({
            "bookmaker": item["bookmaker"],
            "linha": linha,
            "odd": odd
        })

    if not candidatos:
        return None

    return max(
        candidatos,
        key=lambda x: x["odd"]
    )


# ============================================================
# FILTROS
# ============================================================

def linha_gols_valida(linha):

    return linha in (
        0.5,
        1.5,
        2.5
    )


def linha_cantos_valida(linha):

    return linha in (
        6.5,
        7.5,
        8.5,
        9.5,
        10.5
    )


def filtrar_gols(odd_info):

    if not odd_info:
        return False

    return linha_gols_valida(
        odd_info["linha"]
    )


def filtrar_cantos(odd_info):

    if not odd_info:
        return False

    return linha_cantos_valida(
        odd_info["linha"]
    )


# ============================================================
# RESOLUÇÃO
# ============================================================

def resolver_linha(
    total,
    linha
):

    total = float(total)
    linha = float(linha)

    # Linha .5 não possui PUSH
    if linha % 1 != 0:

        if total > linha:
            return "WIN"

        return "LOSS"

    # Linha inteira
    if total > linha:
        return "WIN"

    if total == linha:
        return "PUSH"

    return "LOSS"


def resolver_combinado(
    resultado_gols,
    resultado_cantos
):

    resultados = {
        resultado_gols,
        resultado_cantos
    }

    if "LOSS" in resultados:
        return "LOSS"

    if (
        resultado_gols == "WIN"
        and resultado_cantos == "WIN"
    ):
        return "WIN"

    return "PUSH"


# ============================================================
# ASSERTIVIDADE
# ============================================================

def calcular_assertividade(
    wins,
    losses
):

    total = wins + losses

    if total <= 0:
        return 0.0

    return round(
        wins / total * 100,
        2
    )


def estatisticas_combinadas():

    stats = estado["stats"]

    wins = stats[
        "combinados_wins"
    ]

    losses = stats[
        "combinados_losses"
    ]

    pushes = stats[
        "combinados_push"
    ]

    return {
        "wins": wins,
        "losses": losses,
        "push": pushes,
        "assertividade":
            calcular_assertividade(
                wins,
                losses
            )
    }


# ============================================================
# HISTÓRICO
# ============================================================

def obter_historico_7_dias():

    agora = time.time()

    limite = agora - (
        7 * 24 * 60 * 60
    )

    historico = []

    for item in estado.get(
        "historico",
        []
    ):

        try:

            timestamp = float(
                item.get(
                    "timestamp",
                    0
                )
            )

            if timestamp >= limite:
                historico.append(item)

        except Exception:
            continue

    return historico


def assertividade_7_dias():

    historico = (
        obter_historico_7_dias()
    )

    wins = sum(
        1
        for x in historico
        if x.get("resultado")
        == "WIN"
    )

    losses = sum(
        1
        for x in historico
        if x.get("resultado")
        == "LOSS"
    )

    pushes = sum(
        1
        for x in historico
        if x.get("resultado")
        == "PUSH"
    )

    return {
        "sinais": len(historico),
        "wins": wins,
        "losses": losses,
        "push": pushes,
        "assertividade":
            calcular_assertividade(
                wins,
                losses
            )
    }


# ============================================================
# BUSCA DE JOGOS
# ============================================================

def buscar_pre():

    agora = datetime.now(
        timezone.utc
    )

    inicio = agora
    fim = agora

    data = api_get(
        "/fixtures",
        params={
            "start_time":
                inicio.isoformat(),
            "end_time":
                fim.isoformat(),
            "per_page": 100,
            "lang": "pt"
        }
    )

    jogos = extrair_lista(data)

    candidatos = []

    for jogo in jogos:

        if not esta_na_janela_3h(jogo):
            continue

        fid = extrair_id(jogo)

        if not fid:
            continue

        candidatos.append(jogo)

    return candidatos


# ============================================================
# CRIAÇÃO DO SINAL
# ============================================================

def criar_sinal_combinado(jogo):

    fid = extrair_id(jogo)

    if not fid:
        return None

    home, away = extrair_times(jogo)

    gols = obter_melhor_odd(
        fid,
        "gols"
    )

    cantos = obter_melhor_odd(
        fid,
        "cantos"
    )

    if not gols:
        return None

    if not cantos:
        return None

    if not filtrar_gols(gols):
        return None

    if not filtrar_cantos(cantos):
        return None

    # Para um combinado real,
    # as duas pernas precisam estar
    # no mesmo bookmaker.
    if (
        gols["bookmaker"].lower()
        != cantos["bookmaker"].lower()
    ):
        print(
            f"[{fid}] Bookmakers diferentes:"
            f" {gols['bookmaker']} /"
            f" {cantos['bookmaker']}"
        )

        return None

    odd_combinada = (
        gols["odd"]
        * cantos["odd"]
    )

    if not (
        ODD_MIN
        <= odd_combinada
        <= ODD_MAX
    ):
        return None

    historico = (
        assertividade_7_dias()
    )

    sinal = {
        "id": fid,
        "home": home,
        "away": away,

        "bookmaker":
            gols["bookmaker"],

        "gols": {
            "linha": gols["linha"],
            "odd": gols["odd"]
        },

        "cantos": {
            "linha": cantos["linha"],
            "odd": cantos["odd"]
        },

        "odd_combinada":
            round(
                odd_combinada,
                2
            ),

        "assertividade_7d":
            historico[
                "assertividade"
            ],

        "historico_7d":
            historico["sinais"],

        "timestamp":
            time.time(),

        "resultado": None
    }

    return sinal


# ============================================================
# MENSAGEM DO SINAL
# ============================================================

def mensagem_sinal(sinal):

    home = html.escape(
        sinal["home"]
    )

    away = html.escape(
        sinal["away"]
    )

    bookmaker = html.escape(
        sinal["bookmaker"]
    )

    gols_linha = sinal[
        "gols"
    ]["linha"]

    gols_odd = sinal[
        "gols"
    ]["odd"]

    cantos_linha = sinal[
        "cantos"
    ]["linha"]

    cantos_odd = sinal[
        "cantos"
    ]["odd"]

    odd_total = sinal[
        "odd_combinada"
    ]

    assertividade = sinal[
        "assertividade_7d"
    ]

    historico = sinal[
        "historico_7d"
    ]

    return (
        "🔥 <b>SINAL COMBINADO</b>\n"
        "\n"
        f"⚽ <b>{home}</b> x "
        f"<b>{away}</b>\n"
        "\n"
        "🎯 <b>ENTRADA</b>\n"
        f"⚽ Over {gols_linha} Gols "
        f"@ {gols_odd:.2f}\n"
        f"🚩 Over {cantos_linha} "
        f"Escanteios @ {cantos_odd:.2f}\n"
        "\n"
        f"💰 <b>Odd combinada:</b> "
        f"{odd_total:.2f}\n"
        f"🏦 <b>Casa:</b> "
        f"{bookmaker}\n"
        "\n"
        f"📊 <b>Histórico 7 dias:</b> "
        f"{historico} sinais\n"
        f"📈 <b>Assertividade histórica:</b> "
        f"{assertividade:.2f}%\n"
        "\n"
        "⚠️ Assertividade é baseada "
        "no histórico registrado pelo robô."
    )


# ============================================================
# ANALISAR PRÉ-JOGO
# ============================================================

def analisar_pre(jogo):

    fid = extrair_id(jogo)

    if not fid:
        return False

    chave = f"{fid}_COMBINADO"

    if chave in estado["pendentes"]:
        return False

    if not esta_na_janela_3h(jogo):
        return False

    sinal = criar_sinal_combinado(
        jogo
    )

    if not sinal:
        return False

    mensagem = mensagem_sinal(
        sinal
    )

    enviado = enviar_telegram(
        mensagem
    )

    if not enviado:

        print(
            f"[{fid}] Telegram falhou."
        )

        return False

    estado["pendentes"][chave] = sinal

    estado["stats"][
        "total_sinais"
    ] += 1

    salvar_estado()

    print(
        f"[SINAL] {sinal['home']} x "
        f"{sinal['away']}"
    )

    return True


# ============================================================
# BUSCAR JOGO FINAL
# ============================================================

def buscar_jogo_por_id(fid):

    data = api_get(
        f"/fixtures/{fid}",
        params={
            "lang": "pt"
        }
    )

    if not data:
        return None

    if isinstance(data, dict):

        for chave in (
            "data",
            "fixture"
        ):

            valor = data.get(chave)

            if isinstance(
                valor,
                dict
            ):
                return valor

            if isinstance(
                valor,
                list
            ) and valor:

                return valor[0]

    return data


# ============================================================
# FINALIZAR SINAL
# ============================================================

def finalizar(
    chave,
    sinal,
    jogo
):

    gols_casa, gols_fora = (
        extrair_placar(jogo)
    )

    total_gols = (
        gols_casa
        + gols_fora
    )

    total_cantos = (
        extrair_cantos(jogo)
    )

    resultado_gols = resolver_linha(
        total_gols,
        sinal["gols"]["linha"]
    )

    resultado_cantos = resolver_linha(
        total_cantos,
        sinal["cantos"]["linha"]
    )

    resultado = resolver_combinado(
        resultado_gols,
        resultado_cantos
    )

    sinal["resultado"] = resultado

    sinal["resultado_gols"] = (
        resultado_gols
    )

    sinal["resultado_cantos"] = (
        resultado_cantos
    )

    sinal["placar_final"] = (
        f"{gols_casa} x {gols_fora}"
    )

    sinal["total_gols"] = total_gols
    sinal["total_cantos"] = total_cantos

    if resultado == "WIN":

        estado["stats"][
            "combinados_wins"
        ] += 1

    elif resultado == "LOSS":

        estado["stats"][
            "combinados_losses"
        ] += 1

    else:

        estado["stats"][
            "combinados_push"
        ] += 1

    # Estatística individual dos mercados
    if resultado_gols == "WIN":

        estado["stats"][
            "gols_wins"
        ] += 1

    elif resultado_gols == "LOSS":

        estado["stats"][
            "gols_losses"
        ] += 1

    else:

        estado["stats"][
            "gols_push"
        ] += 1

    if resultado_cantos == "WIN":

        estado["stats"][
            "cantos_wins"
        ] += 1

    elif resultado_cantos == "LOSS":

        estado["stats"][
            "cantos_losses"
        ] += 1

    else:

        estado["stats"][
            "cantos_push"
        ] += 1

    sinal_historico = dict(
        sinal
    )

    sinal_historico[
        "timestamp_resultado"
    ] = time.time()

    estado["historico"].append(
        sinal_historico
    )

    # Mantém somente histórico recente
    estado["historico"] = [
        x
        for x in estado["historico"]
        if time.time()
        - float(
            x.get(
                "timestamp",
                time.time()
            )
        )
        <= 30 * 24 * 60 * 60
    ]

    del estado["pendentes"][chave]

    salvar_estado()

    assertividade = (
        estatisticas_combinadas()
    )

    mensagem = (
        "🏁 <b>RESULTADO DO SINAL</b>\n"
        "\n"
        f"⚽ <b>{html.escape(sinal['home'])}</b> "
        f"x "
        f"<b>{html.escape(sinal['away'])}</b>\n"
        "\n"
        f"📊 Resultado: <b>{resultado}</b>\n"
        f"⚽ Gols: {resultado_gols}\n"
        f"🚩 Escanteios: {resultado_cantos}\n"
        "\n"
        f"🔢 Placar: "
        f"{gols_casa} x {gols_fora}\n"
        f"🚩 Total escanteios: "
        f"{total_cantos}\n"
        "\n"
        f"📈 Assertividade geral: "
        f"{assertividade['assertividade']:.2f}%"
    )

    enviar_telegram(
        mensagem
    )


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    pendentes = list(
        estado["pendentes"].items()
    )

    for chave, sinal in pendentes:

        try:

            jogo = buscar_jogo_por_id(
                sinal["id"]
            )

            if not jogo:
                continue

            if not eh_finalizado(jogo):
                continue

            finalizar(
                chave,
                sinal,
                jogo
            )

        except Exception as e:

            print(
                "Erro finalizando",
                chave,
                e
            )


# ============================================================
# STATUS
# ============================================================

def gerar_stats():

    combinado = (
        estatisticas_combinadas()
    )

    sete_dias = (
        assertividade_7_dias()
    )

    return {
        "combinados": combinado,
        "ultimos_7_dias": sete_dias,
        "pendentes": len(
            estado["pendentes"]
        ),
        "total_sinais": estado[
            "stats"
        ]["total_sinais"]
    }


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def loop_bot():

    print(
        "================================"
    )

    print(
        "ROBÔ GOLS + ESCANTEIOS"
    )

    print(
        "INICIANDO..."
    )

    print(
        "================================"
    )

    if TELEGRAM_TOKEN and CHAT_ID:

        enviar_telegram(
            "🟢 <b>ROBÔ ONLINE</b>\n\n"
            "Sistema de sinais "
            "combinados iniciado."
        )

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:

        agora = time.time()

        # ----------------------------------
        # PRÉ-JOGOS
        # ----------------------------------

        if (
            agora - ultimo_pre
            >= INTERVALO_PRE
        ):

            ultimo_pre = agora

            try:

                jogos = buscar_pre()

                enviados = 0

                for jogo in jogos:

                    if enviados >= QTD_POR_RODADA:
                        break

                    if analisar_pre(jogo):

                        enviados += 1

            except Exception as e:

                print(
                    "Erro análise pré:",
                    e
                )

        # ----------------------------------
        # RESULTADOS
        # ----------------------------------

        if (
            agora - ultimo_resultado
            >= INTERVALO_RESULTADOS
        ):

            ultimo_resultado = agora

            try:

                verificar_resultados()

            except Exception as e:

                print(
                    "Erro resultados:",
                    e
                )

        time.sleep(20)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "Gols + Escanteios",
        "modo": "combinado",
        "pendentes": len(
            estado["pendentes"]
        )
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "telegram":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),
        "api":
            bool(API_KEY),
        "pendentes":
            len(
                estado["pendentes"]
            )
    })


@app.route("/stats")
def stats():

    return jsonify(
        gerar_stats()
    )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_estado()


if __name__ == "__main__":

    thread = threading.Thread(
        target=loop_bot,
        daemon=True
    )

    thread.start()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
