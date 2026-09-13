import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_API = "https://api.5dollarfootballapi.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Odds permitidas
ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "3.50"))

# Quantidade máxima de sinais na rodada
QTD_POR_RODADA = int(os.getenv("QTD_BILHETES", "8"))

# Verificação do pré-jogo
# 300 = 5 minutos
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "300"))

# Verificação dos resultados
# 300 = 5 minutos
INTERVALO_RESULTADOS = int(
    os.getenv("INTERVALO_RESULTADOS", "300")
)

# ============================================================
# SINAL 3 HORAS ANTES
# ============================================================

HORAS_ANTES = float(
    os.getenv("HORAS_ANTES", "3")
)

# Janela de tolerância:
# 3h antes +/- 5 minutos
JANELA_MINUTOS = int(
    os.getenv("JANELA_MINUTOS", "5")
)

# Arquivo local de histórico
ARQUIVO_ESTADO = os.getenv(
    "ARQUIVO_ESTADO",
    "bot_state.json"
)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "robo-escanteio/3.0"
}


# ============================================================
# LOCK
# ============================================================

lock = threading.RLock()


# ============================================================
# ESTADO
# ============================================================

stats = {
    "gols_wins": 0,
    "gols_losses": 0,
    "gols_push": 0,

    "cantos_wins": 0,
    "cantos_losses": 0,
    "cantos_push": 0,

    "total_sinais": 0
}

# Sinais já enviados
entradas_pre = {}

# Sinais aguardando resultado
pendentes_pre = {}


# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

def primeiro_valor(*valores):

    for valor in valores:

        if valor is not None and valor != "":
            return valor

    return None


def numero(valor, padrao=0.0):

    try:

        if isinstance(valor, str):
            valor = valor.replace(",", ".").strip()

        return float(valor)

    except Exception:

        return padrao


def inteiro(valor, padrao=0):

    try:

        return int(float(numero(valor, padrao)))

    except Exception:

        return padrao


def nome_time(dado, padrao):

    if isinstance(dado, dict):

        return str(
            primeiro_valor(
                dado.get("name"),
                dado.get("short_name"),
                dado.get("title"),
                padrao
            )
        )

    return str(dado) if dado else padrao


# ============================================================
# PERSISTÊNCIA
# ============================================================

def salvar_estado():

    with lock:

        estado = {
            "stats": stats,
            "entradas_pre": entradas_pre,
            "pendentes_pre": pendentes_pre
        }

        arquivo_temporario = (
            ARQUIVO_ESTADO + ".tmp"
        )

        try:

            with open(
                arquivo_temporario,
                "w",
                encoding="utf-8"
            ) as arquivo:

                json.dump(
                    estado,
                    arquivo,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(
                arquivo_temporario,
                ARQUIVO_ESTADO
            )

        except Exception as erro:

            print(
                f"ERRO SALVANDO ESTADO: {erro}",
                flush=True
            )


def carregar_estado():

    global stats
    global entradas_pre
    global pendentes_pre

    if not os.path.exists(ARQUIVO_ESTADO):

        print(
            "Nenhum histórico encontrado. "
            "Iniciando banco vazio.",
            flush=True
        )

        return

    try:

        with open(
            ARQUIVO_ESTADO,
            "r",
            encoding="utf-8"
        ) as arquivo:

            estado = json.load(arquivo)

        stats.update(
            estado.get("stats", {})
        )

        entradas_pre.update(
            estado.get("entradas_pre", {})
        )

        pendentes_pre.update(
            estado.get("pendentes_pre", {})
        )

        print(
            "HISTÓRICO CARREGADO",
            flush=True
        )

        print(
            f"Sinais: {stats['total_sinais']}",
            flush=True
        )

        print(
            f"Pendentes: {len(pendentes_pre)}",
            flush=True
        )

    except Exception as erro:

        print(
            f"ERRO CARREGANDO ESTADO: {erro}",
            flush=True
        )


# ============================================================
# TELEGRAM
# ============================================================

def tg_msg(mensagem):

    if not TOKEN or not CHAT_ID:

        print(
            "TELEGRAM NÃO CONFIGURADO:",
            mensagem,
            flush=True
        )

        return False

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TOKEN}/sendMessage"
        )

        resposta = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

        if resposta.status_code != 200:

            print(
                f"ERRO TELEGRAM "
                f"HTTP {resposta.status_code}: "
                f"{resposta.text[:500]}",
                flush=True
            )

            return False

        return True

    except Exception as erro:

        print(
            f"ERRO TELEGRAM: {erro}",
            flush=True
        )

        return False


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None):

    if not API_KEY:

        print(
            "ERRO: FIVE_DOLLAR_KEY não configurada.",
            flush=True
        )

        return None

    url = (
        f"{BASE_API}/"
        f"{endpoint.lstrip('/')}"
    )

    try:

        resposta = requests.get(
            url,
            headers=HEADERS,
            params=params or {},
            timeout=25
        )

        print(
            f"API {endpoint} "
            f"HTTP {resposta.status_code}",
            flush=True
        )

        if resposta.status_code != 200:

            print(
                f"RESPOSTA API: "
                f"{resposta.text[:1000]}",
                flush=True
            )

            return None

        return resposta.json()

    except Exception as erro:

        print(
            f"ERRO API {endpoint}: {erro}",
            flush=True
        )

        return None


# ============================================================
# FIXTURE
# ============================================================

def extrair_id(jogo):

    fixture = jogo.get(
        "fixture",
        {}
    ) or {}

    return str(
        primeiro_valor(
            jogo.get("id"),
            fixture.get("id"),
            jogo.get("fixture_id"),
            ""
        )
    )


def extrair_times(jogo):

    teams = jogo.get(
        "teams",
        {}
    ) or {}

    casa = nome_time(
        primeiro_valor(
            jogo.get("home_team"),
            jogo.get("home"),
            teams.get("home")
        ),
        "Casa"
    )

    fora = nome_time(
        primeiro_valor(
            jogo.get("away_team"),
            jogo.get("away"),
            teams.get("away")
        ),
        "Fora"
    )

    return casa, fora


def extrair_placar(jogo):

    gols = jogo.get(
        "goals",
        {}
    ) or {}

    score = jogo.get(
        "score",
        {}
    ) or {}

    casa = inteiro(
        primeiro_valor(
            jogo.get("home_score"),
            gols.get("home"),
            score.get("home"),
            0
        )
    )

    fora = inteiro(
        primeiro_valor(
            jogo.get("away_score"),
            gols.get("away"),
            score.get("away"),
            0
        )
    )

    return casa, fora


def extrair_cantos(jogo):

    cantos = primeiro_valor(
        jogo.get("corners"),
        jogo.get("corner")
    )

    # Caso venha diretamente como número
    if isinstance(
        cantos,
        (int, float, str)
    ):

        return inteiro(cantos)

    # Caso venha como objeto
    if isinstance(cantos, dict):

        total = primeiro_valor(
            cantos.get("total"),
            cantos.get("value")
        )

        if total is not None:
            return inteiro(total)

        casa = inteiro(
            primeiro_valor(
                cantos.get("home"),
                0
            )
        )

        fora = inteiro(
            primeiro_valor(
                cantos.get("away"),
                0
            )
        )

        return casa + fora

    return 0


def extrair_status(jogo):

    fixture = jogo.get(
        "fixture",
        {}
    ) or {}

    status_obj = fixture.get(
        "status",
        {}
    ) or {}

    return str(
        primeiro_valor(
            jogo.get("status"),
            status_obj.get("short"),
            status_obj.get("long"),
            ""
        )
    ).lower()


def eh_finalizado(jogo):

    status = extrair_status(jogo)

    finais = [
        "ft",
        "finished",
        "finish",
        "aet",
        "pen",
        "full_time"
    ]

    return any(
        x in status
        for x in finais
    )


def eh_agendado(jogo):

    status = extrair_status(jogo)

    agendados = [
        "scheduled",
        "not started",
        "ns",
        "upcoming",
        "fixture"
    ]

    # Se não houver status, deixamos passar
    # para a validação do horário.
    if not status:
        return True

    return any(
        x in status
        for x in agendados
    )


# ============================================================
# HORÁRIO DO JOGO
# ============================================================

def extrair_inicio_timestamp(jogo):

    fixture = jogo.get(
        "fixture",
        {}
    ) or {}

    valor = primeiro_valor(
        jogo.get("start_time"),
        jogo.get("start_timestamp"),
        jogo.get("timestamp"),
        jogo.get("date"),

        fixture.get("timestamp"),
        fixture.get("date"),
        fixture.get("start_time")
    )

    if valor is None:
        return None

    # Unix timestamp
    if isinstance(
        valor,
        (int, float)
    ):

        return int(valor)

    texto = str(valor).strip()

    # Timestamp como texto
    if texto.isdigit():

        return int(texto)

    # ISO
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

        return int(
            dt.timestamp()
        )

    except Exception as erro:

        print(
            f"ERRO CONVERTENDO DATA "
            f"'{valor}': {erro}",
            flush=True
        )

        return None


# ============================================================
# JANELA EXATA DE 3 HORAS
# ============================================================

def esta_na_janela_3h(jogo):

    inicio = extrair_inicio_timestamp(
        jogo
    )

    if not inicio:
        return False

    agora = int(time.time())

    segundos_ate_jogo = (
        inicio - agora
    )

    alvo = (
        HORAS_ANTES * 60 * 60
    )

    tolerancia = (
        JANELA_MINUTOS * 60
    )

    minimo = alvo - tolerancia
    maximo = alvo + tolerancia

    return (
        minimo
        <= segundos_ate_jogo
        <= maximo
    )


def minutos_ate_jogo(jogo):

    inicio = extrair_inicio_timestamp(
        jogo
    )

    if not inicio:
        return None

    agora = int(time.time())

    return (
        inicio - agora
    ) / 60


# ============================================================
# BUSCAR JOGOS DE HOJE
# ============================================================

def unix_hoje():

    agora = datetime.now(
        timezone.utc
    )

    inicio = agora.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    fim = agora.replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0
    )

    return (
        int(inicio.timestamp()),
        int(fim.timestamp())
    )


def extrair_lista(dados):

    if not dados:
        return []

    if isinstance(
        dados,
        list
    ):
        return dados

    if not isinstance(
        dados,
        dict
    ):
        return []

    data = dados.get(
        "data"
    )

    if isinstance(
        data,
        list
    ):
        return data

    if isinstance(
        data,
        dict
    ):

        for chave in [
            "fixtures",
            "data",
            "response"
        ]:

            valor = data.get(
                chave
            )

            if isinstance(
                valor,
                list
            ):
                return valor

    response = dados.get(
        "response"
    )

    if isinstance(
        response,
        list
    ):
        return response

    return []


def buscar_pre():

    inicio, fim = unix_hoje()

    dados = api_get(
        "fixtures",
        {
            "start_time": inicio,
            "end_time": fim,
            "per_page": 100,
            "lang": "pt"
        }
    )

    jogos = extrair_lista(
        dados
    )

    agora = int(
        time.time()
    )

    alvo = int(
        HORAS_ANTES * 3600
    )

    tolerancia = (
        JANELA_MINUTOS * 60
    )

    minimo = alvo - tolerancia
    maximo = alvo + tolerancia

    jogos_3h = []

    for jogo in jogos:

        if not isinstance(
            jogo,
            dict
        ):
            continue

        if not eh_agendado(
            jogo
        ):
            continue

        inicio_jogo = (
            extrair_inicio_timestamp(
                jogo
            )
        )

        if not inicio_jogo:
            continue

        segundos = (
            inicio_jogo - agora
        )

        # ====================================================
        # JOGO NA JANELA DE 3 HORAS
        # ====================================================

        if (
            minimo
            <= segundos
            <= maximo
        ):

            jogos_3h.append(
                jogo
            )

            casa, fora = (
                extrair_times(jogo)
            )

            print(
                f"JOGO NA JANELA 3H: "
                f"{casa} x {fora} | "
                f"{segundos / 60:.1f} min",
                flush=True
            )

    print(
        f"PRE: {len(jogos)} jogos hoje | "
        f"{len(jogos_3h)} na janela de "
        f"{HORAS_ANTES:g}h",
        flush=True
    )

    return jogos_3h


# ============================================================
# ODDS
# ============================================================

def extrair_mercado_bookmakers(
    data,
    mercado
):

    if not isinstance(
        data,
        dict
    ):
        return []

    raiz = data.get(
        "data",
        data
    )

    if not isinstance(
        raiz,
        dict
    ):
        return []

    bookmakers = raiz.get(
        "bookmakers",
        []
    )

    if not isinstance(
        bookmakers,
        list
    ):
        return []

    resultados = []

    for bookmaker in bookmakers:

        if not isinstance(
            bookmaker,
            dict
        ):
            continue

        odds = bookmaker.get(
            "odds",
            {}
        )

        if not isinstance(
            odds,
            dict
        ):
            continue

        if mercado == "goal":

            bloco = odds.get(
                "goal_line"
            )

        else:

            bloco = odds.get(
                "corner_line"
            )

        if not isinstance(
            bloco,
            dict
        ):
            continue

        resultados.append(
            {
                "bookmaker": primeiro_valor(
                    bookmaker.get("name"),
                    bookmaker.get("slug"),
                    "bookmaker"
                ),
                "bloco": bloco
            }
        )

    return resultados


def extrair_preco_do_bloco(
    bloco
):

    if not isinstance(
        bloco,
        dict
    ):
        return None

    # Preferimos closing.
    # Caso não exista, usamos opening.
    for momento in [
        "closing",
        "opening",
        "current"
    ]:

        dados = bloco.get(
            momento
        )

        if not isinstance(
            dados,
            dict
        ):
            continue

        linha = numero(
            dados.get("line"),
            -999
        )

        over = numero(
            dados.get("over"),
            0
        )

        if linha == -999:
            continue

        if over <= 0:
            continue

        return {
            "linha": linha,
            "odd": over,
            "momento": momento
        }

    return None


def obter_melhor_odd(
    jogo_id,
    mercado
):

    parametro = (
        "goalline"
        if mercado == "goal"
        else "corner"
    )

    dados = api_get(
        f"fixtures/{jogo_id}/odds",
        {
            "market": parametro
        }
    )

    if not dados:
        return None

    mercados = (
        extrair_mercado_bookmakers(
            dados,
            mercado
        )
    )

    melhores = []

    for item in mercados:

        preco = (
            extrair_preco_do_bloco(
                item["bloco"]
            )
        )

        if not preco:
            continue

        odd = preco["odd"]

        if not (
            ODD_MIN <= odd <= ODD_MAX
        ):
            continue

        preco["bookmaker"] = (
            item["bookmaker"]
        )

        melhores.append(
            preco
        )

    if not melhores:
        return None

    # Maior odd dentro da faixa
    melhores.sort(
        key=lambda x: x["odd"],
        reverse=True
    )

    return melhores[0]


# ============================================================
# FILTRO GOLS
# ============================================================

def filtra_over_gols(jogo):

    jogo_id = extrair_id(
        jogo
    )

    if not jogo_id:
        return None

    resultado = obter_melhor_odd(
        jogo_id,
        "goal"
    )

    if not resultado:
        return None

    linha = resultado["linha"]
    odd = resultado["odd"]

    linhas_permitidas = [
        0.5,
        1.5,
        2.5
    ]

    permitida = any(
        abs(linha - x) < 0.01
        for x in linhas_permitidas
    )

    if not permitida:
        return None

    return {
        "odd": odd,
        "linha": linha,
        "mercado": (
            f"Over {linha:g} Gols"
        ),
        "bookmaker": (
            resultado["bookmaker"]
        )
    }


# ============================================================
# FILTRO ESCANTEIOS
# ============================================================

def filtra_over_cantos(jogo):

    jogo_id = extrair_id(
        jogo
    )

    if not jogo_id:
        return None

    resultado = obter_melhor_odd(
        jogo_id,
        "corner"
    )

    if not resultado:
        return None

    linha = resultado["linha"]
    odd = resultado["odd"]

    linhas_permitidas = [
        6.5,
        7.5,
        8.5,
        9.0,
        9.5,
        10.0,
        10.5
    ]

    permitida = any(
        abs(linha - x) < 0.01
        for x in linhas_permitidas
    )

    if not permitida:
        return None

    return {
        "odd": odd,
        "linha": linha,
        "mercado": (
            f"Over {linha:g} Escanteios"
        ),
        "bookmaker": (
            resultado["bookmaker"]
        )
    }


# ============================================================
# RESULTADO DE UMA LINHA
# ============================================================

def resolver_linha(
    total,
    linha
):

    total = float(total)
    linha = float(linha)

    # ========================================================
    # LINHA INTEIRA
    # ========================================================
    # Exemplo:
    # Over 9.0
    #
    # 10 ou mais = WIN
    # exatamente 9 = PUSH
    # 8 ou menos = LOSS

    if linha.is_integer():

        if total > linha:
            return "WIN"

        if total == linha:
            return "PUSH"

        return "LOSS"

    # ========================================================
    # LINHA .5
    # ========================================================
    # Exemplo:
    # Over 9.5
    #
    # 10 ou mais = WIN
    # 9 ou menos = LOSS
    #
    # Não existe PUSH.

    if total > linha:
        return "WIN"

    return "LOSS"


# ============================================================
# ASSERTIVIDADE
# ============================================================

def calcular_assertividade(
    wins,
    losses
):

    # PUSH não entra no cálculo.
    operacoes_validas = (
        wins + losses
    )

    if operacoes_validas <= 0:
        return 0.0

    return (
        wins /
        operacoes_validas
    ) * 100


def calcular_stats():

    gw = stats["gols_wins"]
    gl = stats["gols_losses"]
    gp = stats["gols_push"]

    cw = stats["cantos_wins"]
    cl = stats["cantos_losses"]
    cp = stats["cantos_push"]

    geral_wins = (
        gw + cw
    )

    geral_losses = (
        gl + cl
    )

    geral_push = (
        gp + cp
    )

    return {
        "total_sinais": stats[
            "total_sinais"
        ],

        "gols": {
            "wins": gw,
            "losses": gl,
            "push": gp,
            "assertividade": round(
                calcular_assertividade(
                    gw,
                    gl
                ),
                2
            )
        },

        "escanteios": {
            "wins": cw,
            "losses": cl,
            "push": cp,
            "assertividade": round(
                calcular_assertividade(
                    cw,
                    cl
                ),
                2
            )
        },

        "geral": {
            "wins": geral_wins,
            "losses": geral_losses,
            "push": geral_push,
            "assertividade": round(
                calcular_assertividade(
                    geral_wins,
                    geral_losses
                ),
                2
            )
        },

        "pendentes": len(
            pendentes_pre
        )
    }


# ============================================================
# MENSAGEM DE ESTATÍSTICAS
# ============================================================

def mensagem_stats():

    dados = calcular_stats()

    gols = dados["gols"]
    cantos = dados["escanteios"]
    geral = dados["geral"]

    return (
        "📊 <b>RESULTADO DO ROBÔ</b>\n\n"

        f"⚽ <b>GOLS</b>\n"
        f"🟢 WIN: {gols['wins']}\n"
        f"🔴 LOSS: {gols['losses']}\n"
        f"🟡 PUSH: {gols['push']}\n"
        f"🎯 Assertividade: "
        f"{gols['assertividade']:.2f}%\n\n"

        f"🚩 <b>ESCANTEIOS</b>\n"
        f"🟢 WIN: {cantos['wins']}\n"
        f"🔴 LOSS: {cantos['losses']}\n"
        f"🟡 PUSH: {cantos['push']}\n"
        f"🎯 Assertividade: "
        f"{cantos['assertividade']:.2f}%\n\n"

        f"🏆 <b>GERAL</b>\n"
        f"🟢 WIN: {geral['wins']}\n"
        f"🔴 LOSS: {geral['losses']}\n"
        f"🟡 PUSH: {geral['push']}\n"
        f"🎯 Assertividade: "
        f"{geral['assertividade']:.2f}%"
    )


# ============================================================
# ANALISAR E ENVIAR SINAL
# ============================================================

def analisar_pre(jogo):

    fid = extrair_id(
        jogo
    )

    if not fid:
        return False

    # ========================================================
    # SEGURANÇA:
    # SÓ ACEITA JOGOS NA JANELA DE 3 HORAS
    # ========================================================

    if not esta_na_janela_3h(
        jogo
    ):
        return False

    casa, fora = (
        extrair_times(jogo)
    )

    minutos = minutos_ate_jogo(
        jogo
    )

    gerou = False

    # ========================================================
    # GOLS
    # ========================================================

    key_gols = (
        f"{fid}_GOLS"
    )

    if key_gols not in entradas_pre:

        resultado = (
            filtra_over_gols(jogo)
        )

        if resultado:

            odd = resultado["odd"]
            linha = resultado["linha"]
            mercado = resultado["mercado"]

            entradas_pre[
                key_gols
            ] = True

            pendentes_pre[
                key_gols
            ] = {
                "fid": fid,
                "tipo": "GOLS",
                "home": casa,
                "away": fora,
                "odd": odd,
                "mercado": mercado,
                "linha": linha,
                "enviado_em": datetime.now(
                    timezone.utc
                ).isoformat()
            }

            stats[
                "total_sinais"
            ] += 1

            tg_msg(
                "⚽ <b>PRE - "
                f"{mercado.upper()}</b>\n\n"

                f"🏟️ {casa} x {fora}\n"

                f"⏰ Início em "
                f"{minutos:.0f} minutos\n"

                f"💰 {mercado} "
                f"@ {odd:.2f}"
            )

            salvar_estado()

            gerou = True

    # ========================================================
    # ESCANTEIOS
    # ========================================================

    key_cantos = (
        f"{fid}_CANTOS"
    )

    if key_cantos not in entradas_pre:

        resultado = (
            filtra_over_cantos(jogo)
        )

        if resultado:

            odd = resultado["odd"]
            linha = resultado["linha"]
            mercado = resultado["mercado"]

            entradas_pre[
                key_cantos
            ] = True

            pendentes_pre[
                key_cantos
            ] = {
                "fid": fid,
                "tipo": "CANTOS",
                "home": casa,
                "away": fora,
                "odd": odd,
                "mercado": mercado,
                "linha": linha,
                "enviado_em": datetime.now(
                    timezone.utc
                ).isoformat()
            }

            stats[
                "total_sinais"
            ] += 1

            tg_msg(
                "🚩 <b>PRE - "
                f"{mercado.upper()}</b>\n\n"

                f"🏟️ {casa} x {fora}\n"

                f"⏰ Início em "
                f"{minutos:.0f} minutos\n"

                f"💰 {mercado} "
                f"@ {odd:.2f}"
            )

            salvar_estado()

            gerou = True

    return gerou


# ============================================================
# BUSCAR FIXTURE POR ID
# ============================================================

def buscar_jogo_por_id(
    fid
):

    dados = api_get(
        f"fixtures/{fid}"
    )

    if not dados:
        return None

    data = dados.get(
        "data"
    )

    if isinstance(
        data,
        dict
    ):
        return data

    # Alguns formatos podem retornar
    # diretamente o fixture
    if isinstance(
        dados,
        dict
    ):

        if "id" in dados:
            return dados

    return None


# ============================================================
# FINALIZAR SINAL
# ============================================================

def finalizar(
    key,
    jogo
):

    if key not in pendentes_pre:
        return False

    if not eh_finalizado(
        jogo
    ):
        return False

    info = pendentes_pre[
        key
    ]

    casa_gols, fora_gols = (
        extrair_placar(jogo)
    )

    total_gols = (
        casa_gols + fora_gols
    )

    total_cantos = (
        extrair_cantos(jogo)
    )

    linha = numero(
        info.get("linha"),
        0
    )

    # ========================================================
    # RESOLVER RESULTADO
    # ========================================================

    if info["tipo"] == "GOLS":

        resultado = resolver_linha(
            total_gols,
            linha
        )

    else:

        resultado = resolver_linha(
            total_cantos,
            linha
        )

    # ========================================================
    # GUARDAR RESULTADO NO HISTÓRICO
    # ========================================================

    info["resultado"] = resultado

    info["total_gols"] = (
        total_gols
    )

    info["total_cantos"] = (
        total_cantos
    )

    info["placar_final"] = (
        f"{casa_gols} x {fora_gols}"
    )

    info["finalizado_em"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    # ========================================================
    # ESTATÍSTICAS
    # ========================================================

    if info["tipo"] == "GOLS":

        if resultado == "WIN":

            stats[
                "gols_wins"
            ] += 1

            emoji = "🟢"

        elif resultado == "LOSS":

            stats[
                "gols_losses"
            ] += 1

            emoji = "🔴"

        else:

            stats[
                "gols_push"
            ] += 1

            emoji = "🟡"

    else:

        if resultado == "WIN":

            stats[
                "cantos_wins"
            ] += 1

            emoji = "🟢"

        elif resultado == "LOSS":

            stats[
                "cantos_losses"
            ] += 1

            emoji = "🔴"

        else:

            stats[
                "cantos_push"
            ] += 1

            emoji = "🟡"

    # ========================================================
    # REMOVE DOS PENDENTES
    # ========================================================

    pendentes_pre.pop(
        key,
        None
    )

    salvar_estado()

    # ========================================================
    # ESTATÍSTICAS ATUAIS
    # ========================================================

    dados = calcular_stats()

    if info["tipo"] == "GOLS":

        assertividade = (
            dados["gols"][
                "assertividade"
            ]
        )

    else:

        assertividade = (
            dados["escanteios"][
                "assertividade"
            ]
        )

    geral = (
        dados["geral"][
            "assertividade"
        ]
    )

    # ========================================================
    # MENSAGEM
    # ========================================================

    if info["tipo"] == "GOLS":

        tg_msg(
            f"{emoji} <b>{resultado} - "
            f"{info['mercado']}</b>\n\n"

            f"🏟️ {info['home']} x "
            f"{info['away']}\n"

            f"⚽ Placar: "
            f"{casa_gols} x {fora_gols}\n"

            f"⚽ Total: "
            f"{total_gols} gols\n"

            f"📌 Linha: "
            f"{linha:g}\n"

            f"💰 Odd: "
            f"{info['odd']:.2f}\n\n"

            f"🎯 Assertividade Gols: "
            f"{assertividade:.2f}%\n"

            f"🏆 Assertividade Geral: "
            f"{geral:.2f}%"
        )

    else:

        tg_msg(
            f"{emoji} <b>{resultado} - "
            f"{info['mercado']}</b>\n\n"

            f"🏟️ {info['home']} x "
            f"{info['away']}\n"

            f"🚩 Escanteios: "
            f"{total_cantos}\n"

            f"📌 Linha: "
            f"{linha:g}\n"

            f"💰 Odd: "
            f"{info['odd']:.2f}\n\n"

            f"🎯 Assertividade Escanteios: "
            f"{assertividade:.2f}%\n"

            f"🏆 Assertividade Geral: "
            f"{geral:.2f}%"
        )

    print(
        f"RESULTADO: {resultado} | "
        f"{info['home']} x {info['away']} | "
        f"{info['mercado']}",
        flush=True
    )

    return True


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def iniciar_bot():

    tg_msg(
        "🤖 <b>TURBO ONLINE</b>\n\n"
        f"⏰ Sinais: "
        f"{HORAS_ANTES:g}h antes\n"
        f"🎟️ Máximo: "
        f"{QTD_POR_RODADA} sinais\n"
        f"⚽ Gols + 🚩 Escanteios\n"
        f"🟢 WIN / 🔴 LOSS / 🟡 PUSH\n"
        f"📊 Assertividade automática"
    )

    print(
        "======================================",
        flush=True
    )

    print(
        "BOT TURBO INICIADO",
        flush=True
    )

    print(
        f"SINAIS: {HORAS_ANTES:g}H ANTES",
        flush=True
    )

    print(
        f"JANELA: +/- {JANELA_MINUTOS} MIN",
        flush=True
    )

    print(
        f"QTD POR RODADA: "
        f"{QTD_POR_RODADA}",
        flush=True
    )

    print(
        "======================================",
        flush=True
    )

    ultimo_pre = 0
    ultimo_resultado = 0

    while True:

        try:

            agora = time.time()

            # =================================================
            # SINAIS 3 HORAS ANTES
            # =================================================

            if (
                agora - ultimo_pre
                >= INTERVALO_PRE
            ):

                ultimo_pre = agora

                gerados = 0

                jogos = buscar_pre()

                for jogo in jogos:

                    if (
                        gerados
                        >= QTD_POR_RODADA
                    ):
                        break

                    try:

                        if analisar_pre(
                            jogo
                        ):

                            gerados += 1

                            # Pequena pausa
                            # para não bombardear
                            # a API
                            time.sleep(1)

                    except Exception as erro:

                        print(
                            "ERRO ANALISANDO "
                            f"JOGO: {erro}",
                            flush=True
                        )

                print(
                    f"TURBO: "
                    f"{gerados} NOVOS SINAIS",
                    flush=True
                )

            # =================================================
            # RESULTADOS
            # =================================================

            if (
                agora - ultimo_resultado
                >= INTERVALO_RESULTADOS
            ):

                ultimo_resultado = agora

                pendentes = list(
                    pendentes_pre.items()
                )

                if pendentes:

                    print(
                        f"CONFERINDO "
                        f"{len(pendentes)} "
                        f"RESULTADOS",
                        flush=True
                    )

                for key, info in pendentes:

                    try:

                        fid = info[
                            "fid"
                        ]

                        jogo = (
                            buscar_jogo_por_id(
                                fid
                            )
                        )

                        if jogo and eh_finalizado(
                            jogo
                        ):

                            finalizar(
                                key,
                                jogo
                            )

                            time.sleep(1)

                    except Exception as erro:

                        print(
                            f"ERRO RESULTADO "
                            f"{fid}: {erro}",
                            flush=True
                        )

            # =================================================
            # SLEEP
            # =================================================

            time.sleep(20)

        except Exception as erro:

            print(
                f"ERRO LOOP PRINCIPAL: "
                f"{erro}",
                flush=True
            )

            time.sleep(30)


# ============================================================
# ROTAS FLASK
# ============================================================

@app.route("/")
def home():

    dados = calcular_stats()

    return jsonify({
        "status": "TURBO ONLINE",

        "config": {
            "horas_antes": HORAS_ANTES,
            "janela_minutos": JANELA_MINUTOS,
            "sinais_por_rodada": QTD_POR_RODADA,
            "odd_min": ODD_MIN,
            "odd_max": ODD_MAX
        },

        "estatisticas": dados
    })


@app.route("/health")
def health():

    return jsonify({
        "online": True,
        "api_configurada": bool(
            API_KEY
        ),
        "telegram_configurado": bool(
            TOKEN and CHAT_ID
        ),
        "horas_antes": HORAS_ANTES,
        "janela_minutos": JANELA_MINUTOS,
        "pendentes": len(
            pendentes_pre
        )
    })


@app.route("/stats")
def stats_route():

    return jsonify(
        calcular_stats()
    )


# ============================================================
# THREAD
# ============================================================

def iniciar_thread():

    thread = threading.Thread(
        target=iniciar_bot,
        daemon=True
    )

    thread.start()


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_estado()

iniciar_thread()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
