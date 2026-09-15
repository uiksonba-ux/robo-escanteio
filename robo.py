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

BASE_API = os.getenv(
    "BASE_API",
    "https://api.5dollarfootballapi.com/v1"
).rstrip("/")

FIVE_DOLLAR_API_KEY = os.getenv("FIVE_DOLLAR_API_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

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


# ============================================================
# PARÂMETROS DO ROBÔ
# ============================================================

ENTRADA_INICIAL = float(
    os.getenv("ENTRADA_INICIAL", "10")
)

MULTIPLICADOR_GALE = float(
    os.getenv("MULTIPLICADOR_GALE", "2")
)

MAX_GALES = int(
    os.getenv("MAX_GALES", "2")
)

TOTAL_BANCAS = int(
    os.getenv("TOTAL_BANCAS", "3")
)

JANELA_HORAS = int(
    os.getenv("JANELA_HORAS", "24")
)

INTERVALO_ANALISE_SEGUNDOS = int(
    os.getenv(
        "INTERVALO_ANALISE_SEGUNDOS",
        "900"
    )
)

ODD_MINIMA = float(
    os.getenv("ODD_MINIMA", "1.80")
)

ODD_MAXIMA = float(
    os.getenv("ODD_MAXIMA", "2.50")
)

ODD_ALVO = float(
    os.getenv("ODD_ALVO", "2.00")
)

MERCADO_ESCANTEIOS = os.getenv(
    "MERCADO_ESCANTEIOS",
    "over_8.5"
)

MERCADO_GOLS = os.getenv(
    "MERCADO_GOLS",
    "under_4.5"
)

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
    os.getenv(
        "MIN_PROBABILIDADE_HISTORICA",
        "0.62"
    )
)

MAX_JOGOS_ODDS_POR_CICLO = int(
    os.getenv(
        "MAX_JOGOS_ODDS_POR_CICLO",
        "25"
    )
)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# LOCK
# ============================================================

lock = threading.Lock()


# ============================================================
# ESTRUTURAS PADRÃO
# ============================================================

stats_padrao = {
    "total_sinais": 0,
    "resolvidos": 0,
    "wins": 0,
    "losses": 0,
    "assertividade": 0.0,
    "lucro_teorico": 0.0,

    "por_banca": {},

    "por_gale": {
        "0": {
            "entradas": 0,
            "wins": 0,
            "losses": 0
        },
        "1": {
            "entradas": 0,
            "wins": 0,
            "losses": 0
        },
        "2": {
            "entradas": 0,
            "wins": 0,
            "losses": 0
        }
    },

    "por_mercado": {
        "combinado": {
            "entradas": 0,
            "wins": 0,
            "losses": 0
        }
    },

    "ultima_atualizacao": None
}


cache_padrao = {
    "fixtures": [],
    "ultima_consulta": None
}


# ============================================================
# JSON
# ============================================================

def carregar_json(arquivo, padrao):

    if not os.path.exists(arquivo):
        return padrao.copy()

    try:

        with open(
            arquivo,
            "r",
            encoding="utf-8"
        ) as f:

            dados = json.load(f)

            return dados

    except Exception as e:

        logging.error(
            "Erro carregando %s: %s",
            arquivo,
            e
        )

        return padrao.copy()


def salvar_json(arquivo, dados):

    try:

        temporario = arquivo + ".tmp"

        with open(
            temporario,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                dados,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporario,
            arquivo
        )

        return True

    except Exception as e:

        logging.error(
            "Erro salvando %s: %s",
            arquivo,
            e
        )

        return False


# ============================================================
# CARREGAMENTO
# ============================================================

stats = carregar_json(
    ARQUIVO_STATS,
    stats_padrao
)

sinais = carregar_json(
    ARQUIVO_SINAIS,
    {}
)

cache = carregar_json(
    ARQUIVO_CACHE,
    cache_padrao
)


# ============================================================
# GARANTIR ESTRUTURAS
# ============================================================

def garantir_estrutura_stats():

    global stats

    if not isinstance(stats, dict):
        stats = stats_padrao.copy()

    stats.setdefault(
        "total_sinais",
        0
    )

    stats.setdefault(
        "resolvidos",
        0
    )

    stats.setdefault(
        "wins",
        0
    )

    stats.setdefault(
        "losses",
        0
    )

    stats.setdefault(
        "assertividade",
        0.0
    )

    stats.setdefault(
        "lucro_teorico",
        0.0
    )

    stats.setdefault(
        "por_banca",
        {}
    )

    stats.setdefault(
        "por_gale",
        {}
    )

    for gale in range(MAX_GALES + 1):

        stats["por_gale"].setdefault(
            str(gale),
            {
                "entradas": 0,
                "wins": 0,
                "losses": 0
            }
        )

    stats.setdefault(
        "por_mercado",
        {}
    )

    stats["por_mercado"].setdefault(
        "combinado",
        {
            "entradas": 0,
            "wins": 0,
            "losses": 0
        }
    )


garantir_estrutura_stats()


# ============================================================
# BANCAS
# ============================================================

def inicializar_bancas():

    alterou = False

    for i in range(1, TOTAL_BANCAS + 1):

        chave = str(i)

        if chave not in stats["por_banca"]:

            stats["por_banca"][chave] = {
                "status": "livre",
                "gale": 0,
                "entrada_atual": ENTRADA_INICIAL,
                "total_entradas": 0,
                "wins": 0,
                "losses": 0,
                "lucro": 0.0,
                "sinal_atual": None
            }

            alterou = True

    if alterou:

        salvar_json(
            ARQUIVO_STATS,
            stats
        )


inicializar_bancas()


# ============================================================
# API
# ============================================================

def api_headers():

    headers = {
        "Accept": "application/json"
    }

    if FIVE_DOLLAR_API_KEY:

        headers["Authorization"] = (
            f"Bearer {FIVE_DOLLAR_API_KEY}"
        )

    return headers


def api_get(endpoint, params=None):

    url = f"{BASE_API}/{endpoint.lstrip('/')}"

    try:

        logging.info(
            "API GET %s | params=%s",
            url,
            params
        )

        resposta = requests.get(
            url,
            headers=api_headers(),
            params=params,
            timeout=30
        )

        if resposta.status_code != 200:

            logging.error(
                "API retornou status %s: %s",
                resposta.status_code,
                resposta.text[:3000]
            )

            return None

        try:

            return resposta.json()

        except Exception:

            logging.error(
                "API não retornou JSON: %s",
                resposta.text[:1000]
            )

            return None

    except requests.RequestException as e:

        logging.error(
            "Erro requisitando API: %s",
            e
        )

        return None


# ============================================================
# EXTRAIR LISTA
# ============================================================

def extrair_lista(dados):

    if dados is None:
        return []

    if isinstance(dados, list):
        return dados

    if not isinstance(dados, dict):
        return []

    for chave in [
        "fixtures",
        "data",
        "results",
        "matches",
        "items"
    ]:

        valor = dados.get(chave)

        if isinstance(valor, list):
            return valor

    return []


# ============================================================
# FIXTURES PRÓXIMAS 24H
# ============================================================

def obter_fixtures_proximas_24h():

    agora = datetime.now(timezone.utc)

    fim = agora + timedelta(
        hours=JANELA_HORAS
    )

    # ========================================================
    # CORREÇÃO PRINCIPAL:
    # A API EXIGE UNIX TIMESTAMP EM SEGUNDOS UTC.
    # ========================================================

    start_timestamp = int(
        agora.timestamp()
    )

    end_timestamp = int(
        fim.timestamp()
    )

    params = {
        "start_time": start_timestamp,
        "end_time": end_timestamp,
        "status": "scheduled",
        "per_page": 100,
        "include": "odds"
    }

    logging.info(
        "Buscando fixtures: %s -> %s",
        start_timestamp,
        end_timestamp
    )

    logging.info(
        "Janela UTC: %s -> %s",
        agora.isoformat(),
        fim.isoformat()
    )

    dados = api_get(
        "fixtures",
        params=params
    )

    if dados is None:

        logging.error(
            "Não foi possível obter fixtures."
        )

        return []

    jogos = extrair_lista(
        dados
    )

    cache["fixtures"] = jogos

    cache["ultima_consulta"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    salvar_json(
        ARQUIVO_CACHE,
        cache
    )

    logging.info(
        "Fixtures encontrados nas próximas %s horas: %s",
        JANELA_HORAS,
        len(jogos)
    )

    return jogos


# ============================================================
# ID DO JOGO
# ============================================================

def obter_id_fixture(jogo):

    if not isinstance(jogo, dict):
        return None

    for campo in [
        "id",
        "fixture_id",
        "match_id"
    ]:

        if jogo.get(campo) is not None:

            return str(
                jogo.get(campo)
            )

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for campo in [
            "id",
            "fixture_id"
        ]:

            if fixture.get(campo) is not None:

                return str(
                    fixture.get(campo)
                )

    return None


# ============================================================
# NOME DOS TIMES
# ============================================================

def obter_nome_times(jogo):

    casa = "Casa"
    fora = "Fora"

    if not isinstance(jogo, dict):
        return casa, fora

    teams = jogo.get("teams")

    if isinstance(teams, dict):

        home = teams.get("home")
        away = teams.get("away")

        if isinstance(home, dict):

            casa = (
                home.get("name")
                or home.get("team_name")
                or casa
            )

        elif isinstance(home, str):

            casa = home

        if isinstance(away, dict):

            fora = (
                away.get("name")
                or away.get("team_name")
                or fora
            )

        elif isinstance(away, str):

            fora = away

    casa = (
        jogo.get("home_team")
        or jogo.get("home_name")
        or casa
    )

    fora = (
        jogo.get("away_team")
        or jogo.get("away_name")
        or fora
    )

    return casa, fora


# ============================================================
# DATA DO JOGO
# ============================================================

def obter_data_jogo(jogo):

    if not isinstance(jogo, dict):
        return None

    for campo in [
        "start_time",
        "kickoff",
        "scheduled",
        "date",
        "start_at"
    ]:

        valor = jogo.get(campo)

        if valor is not None:

            return valor

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):

        for campo in [
            "start_time",
            "date",
            "scheduled"
        ]:

            valor = fixture.get(campo)

            if valor is not None:

                return valor

    return None


# ============================================================
# PARSER GENÉRICO DE ODDS
# ============================================================

def procurar_valores_recursivamente(
    obj,
    resultados=None,
    caminho=""
):

    if resultados is None:
        resultados = []

    if isinstance(obj, dict):

        for chave, valor in obj.items():

            novo_caminho = (
                f"{caminho}.{chave}"
                if caminho
                else str(chave)
            )

            resultados.append(
                (
                    str(chave).lower(),
                    valor,
                    novo_caminho
                )
            )

            procurar_valores_recursivamente(
                valor,
                resultados,
                novo_caminho
            )

    elif isinstance(obj, list):

        for indice, item in enumerate(obj):

            novo_caminho = (
                f"{caminho}[{indice}]"
            )

            procurar_valores_recursivamente(
                item,
                resultados,
                novo_caminho
            )

    return resultados


# ============================================================
# CONVERSÃO PARA FLOAT
# ============================================================

def para_float(valor):

    try:

        if valor is None:
            return None

        if isinstance(
            valor,
            bool
        ):
            return None

        if isinstance(
            valor,
            (int, float)
        ):

            return float(valor)

        texto = str(valor).strip()

        texto = texto.replace(
            ",",
            "."
        )

        return float(texto)

    except Exception:

        return None


# ============================================================
# EXTRAIR ODD DE ESTRUTURA
# ============================================================

def extrair_odd_de_objeto(obj):

    if isinstance(obj, dict):

        for campo in [
            "price",
            "odds",
            "odd",
            "value",
            "decimal",
            "coefficient"
        ]:

            if campo in obj:

                valor = para_float(
                    obj.get(campo)
                )

                if (
                    valor is not None
                    and valor > 1.0
                ):

                    return valor

        for chave in [
            "closing",
            "opening",
            "pre_match",
            "prematch",
            "before",
            "inplay"
        ]:

            sub = obj.get(chave)

            if isinstance(
                sub,
                dict
            ):

                valor = extrair_odd_de_objeto(
                    sub
                )

                if valor is not None:
                    return valor

    valor = para_float(obj)

    if (
        valor is not None
        and valor > 1.0
    ):

        return valor

    return None


# ============================================================
# IDENTIFICAR MERCADO
# ============================================================

def texto_contem_mercado(
    chave,
    mercado
):

    chave = str(
        chave
    ).lower()

    mercado = mercado.lower()

    if mercado == "over_8.5":

        termos = [
            "over",
            "8.5",
            "corner",
            "corners"
        ]

        return (
            "over" in chave
            and "8.5" in chave
            and (
                "corner" in chave
                or "corners" in chave
            )
        )

    if mercado == "under_4.5":

        termos = [
            "under",
            "4.5",
            "goal",
            "goals",
            "total"
        ]

        return (
            "under" in chave
            and "4.5" in chave
            and (
                "goal" in chave
                or "goals" in chave
                or "goal_line" in chave
                or "goalline" in chave
                or "total" in chave
            )
        )

    return mercado in chave


# ============================================================
# BUSCAR ODD
# ============================================================

def encontrar_odd(
    dados,
    mercado
):

    encontrados = []

    itens = procurar_valores_recursivamente(
        dados
    )

    # --------------------------------------------------------
    # PRIMEIRA TENTATIVA:
    # encontrar chave explicitamente compatível
    # --------------------------------------------------------

    for chave, valor, caminho in itens:

        if texto_contem_mercado(
            chave,
            mercado
        ):

            odd = extrair_odd_de_objeto(
                valor
            )

            if odd is not None:

                encontrados.append(
                    (
                        odd,
                        caminho
                    )
                )

    if encontrados:

        encontrados.sort(
            key=lambda x: abs(
                x[0] - ODD_ALVO
            )
        )

        odd, caminho = encontrados[0]

        logging.info(
            "Odd encontrada para %s: %.2f | %s",
            mercado,
            odd,
            caminho
        )

        return odd

    # --------------------------------------------------------
    # SEGUNDA TENTATIVA:
    # procurar objetos que tenham linha + over/under
    # --------------------------------------------------------

    def procurar_linha(obj, mercado):

        if isinstance(obj, dict):

            texto = json.dumps(
                obj,
                ensure_ascii=False
            ).lower()

            if mercado == "over_8.5":

                if (
                    "8.5" in texto
                    and "over" in texto
                    and (
                        "corner" in texto
                        or "corners" in texto
                    )
                ):

                    # Tenta encontrar "over"
                    for chave in [
                        "over",
                        "over_8.5",
                        "price",
                        "odds"
                    ]:

                        if chave in obj:

                            valor = (
                                extrair_odd_de_objeto(
                                    obj[chave]
                                )
                            )

                            if valor:
                                return valor

            if mercado == "under_4.5":

                if (
                    "4.5" in texto
                    and "under" in texto
                    and (
                        "goal" in texto
                        or "goals" in texto
                        or "goal_line" in texto
                        or "goalline" in texto
                    )
                ):

                    for chave in [
                        "under",
                        "under_4.5",
                        "price",
                        "odds"
                    ]:

                        if chave in obj:

                            valor = (
                                extrair_odd_de_objeto(
                                    obj[chave]
                                )
                            )

                            if valor:
                                return valor

            for valor in obj.values():

                resultado = procurar_linha(
                    valor,
                    mercado
                )

                if resultado:
                    return resultado

        elif isinstance(obj, list):

            for item in obj:

                resultado = procurar_linha(
                    item,
                    mercado
                )

                if resultado:
                    return resultado

        return None

    resultado = procurar_linha(
        dados,
        mercado
    )

    if resultado:

        logging.info(
            "Odd encontrada por fallback para %s: %.2f",
            mercado,
            resultado
        )

        return resultado

    logging.warning(
        "Não encontrei odd para mercado %s",
        mercado
    )

    return None


# ============================================================
# OBTER ODDS DO JOGO
# ============================================================

def obter_odds_fixture(
    fixture_id
):

    dados = api_get(
        f"fixtures/{fixture_id}/odds"
    )

    if dados is None:
        return None

    odd_escanteios = encontrar_odd(
        dados,
        MERCADO_ESCANTEIOS
    )

    odd_gols = encontrar_odd(
        dados,
        MERCADO_GOLS
    )

    if (
        odd_escanteios is None
        or odd_gols is None
    ):

        logging.warning(
            "Mercados não encontrados no fixture %s | escanteios=%s | gols=%s",
            fixture_id,
            odd_escanteios,
            odd_gols
        )

        # Log controlado para diagnóstico
        try:

            logging.info(
                "Estrutura odds fixture %s: %s",
                fixture_id,
                json.dumps(
                    dados,
                    ensure_ascii=False
                )[:5000]
            )

        except Exception:
            pass

        return None

    odd_combinada = (
        odd_escanteios
        * odd_gols
    )

    return {
        "escanteios": round(
            odd_escanteios,
            3
        ),
        "gols": round(
            odd_gols,
            3
        ),
        "combinada": round(
            odd_combinada,
            3
        )
    }


# ============================================================
# HISTÓRICO
# ============================================================

def extrair_historico(jogo):

    if not isinstance(jogo, dict):
        return []

    for campo in [
        "history",
        "recent_matches",
        "last_matches",
        "form",
        "historical_matches",
        "previous_matches"
    ]:

        valor = jogo.get(campo)

        if isinstance(
            valor,
            list
        ):

            return valor

    return []


# ============================================================
# EXTRAIR ESTATÍSTICAS HISTÓRICAS
# ============================================================

def estatisticas_historicas(
    historico
):

    if not historico:
        return None

    jogos_validos = []

    for jogo in historico:

        if not isinstance(
            jogo,
            dict
        ):
            continue

        gols = None
        escanteios = None

        # ----------------------------------------------------
        # GOLS
        # ----------------------------------------------------

        goals = jogo.get(
            "goals"
        )

        if isinstance(
            goals,
            dict
        ):

            home = para_float(
                goals.get("home")
            )

            away = para_float(
                goals.get("away")
            )

            if (
                home is not None
                and away is not None
            ):

                gols = (
                    home + away
                )

        # Formatos alternativos

        if gols is None:

            total_goals = (
                jogo.get(
                    "total_goals"
                )
                or jogo.get(
                    "goals_total"
                )
            )

            if total_goals is not None:

                gols = para_float(
                    total_goals
                )

        # ----------------------------------------------------
        # ESCANTEIOS
        # ----------------------------------------------------

        corners = jogo.get(
            "corners"
        )

        if isinstance(
            corners,
            dict
        ):

            home = para_float(
                corners.get("home")
            )

            away = para_float(
                corners.get("away")
            )

            if (
                home is not None
                and away is not None
            ):

                escanteios = (
                    home + away
                )

        if escanteios is None:

            total_corners = (
                jogo.get(
                    "total_corners"
                )
                or jogo.get(
                    "corners_total"
                )
            )

            if total_corners is not None:

                escanteios = para_float(
                    total_corners
                )

        if (
            gols is not None
            and escanteios is not None
        ):

            jogos_validos.append(
                {
                    "gols": gols,
                    "escanteios": escanteios
                }
            )

    if len(jogos_validos) < MIN_JOGOS_HISTORICO:

        return None

    total = len(
        jogos_validos
    )

    media_gols = (
        sum(
            x["gols"]
            for x in jogos_validos
        )
        / total
    )

    media_escanteios = (
        sum(
            x["escanteios"]
            for x in jogos_validos
        )
        / total
    )

    over_corners = sum(
        1
        for x in jogos_validos
        if x["escanteios"] > 8.5
    )

    under_goals = sum(
        1
        for x in jogos_validos
        if x["gols"] < 4.5
    )

    combinado = sum(
        1
        for x in jogos_validos
        if (
            x["escanteios"] > 8.5
            and x["gols"] < 4.5
        )
    )

    prob_over_corners = (
        over_corners / total
    )

    prob_under_goals = (
        under_goals / total
    )

    prob_combinada = (
        combinado / total
    )

    return {
        "jogos": total,
        "media_gols": round(
            media_gols,
            3
        ),
        "media_escanteios": round(
            media_escanteios,
            3
        ),
        "over_corners": round(
            prob_over_corners,
            4
        ),
        "under_goals": round(
            prob_under_goals,
            4
        ),
        "combinada": round(
            prob_combinada,
            4
        )
    }


# ============================================================
# ANALISAR JOGO
# ============================================================

def analisar_jogo(
    jogo,
    odds
):

    historico = extrair_historico(
        jogo
    )

    estatisticas = (
        estatisticas_historicas(
            historico
        )
    )

    if estatisticas is None:

        logging.info(
            "Jogo sem histórico suficiente."
        )

        return None

    if (
        estatisticas["media_escanteios"]
        < MIN_MEDIA_ESCANTEIOS
    ):

        return None

    if (
        estatisticas["media_gols"]
        > MAX_MEDIA_GOLS
    ):

        return None

    if (
        estatisticas["combinada"]
        < MIN_PROBABILIDADE_HISTORICA
    ):

        return None

    odd_combinada = (
        odds["combinada"]
    )

    if (
        odd_combinada < ODD_MINIMA
        or odd_combinada > ODD_MAXIMA
    ):

        return None

    # Score apenas para ordenar candidatos.
    # Não é uma garantia de acerto.

    proximidade_odd = 1 - min(
        abs(
            odd_combinada - ODD_ALVO
        ) / ODD_ALVO,
        1
    )

    score = (
        estatisticas["combinada"]
        * 0.65
        +
        proximidade_odd
        * 0.35
    )

    return {
        "score": round(
            score,
            4
        ),
        "probabilidade_historica": round(
            estatisticas["combinada"]
            * 100,
            2
        ),
        "estatisticas": estatisticas
    }


# ============================================================
# BANCA LIVRE
# ============================================================

def encontrar_banca_livre():

    for i in range(
        1,
        TOTAL_BANCAS + 1
    ):

        banca = stats[
            "por_banca"
        ].get(
            str(i)
        )

        if not banca:
            continue

        if banca.get(
            "status"
        ) == "livre":

            return i

    return None


# ============================================================
# EVITAR DUPLICAÇÃO
# ============================================================

def fixture_ja_tem_sinal(
    fixture_id
):

    for sinal in sinais.values():

        if sinal.get(
            "fixture_id"
        ) == str(
            fixture_id
        ):

            if sinal.get(
                "status"
            ) == "ativo":

                return True

    return False


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(
    mensagem
):

    if not TELEGRAM_TOKEN:
        logging.error(
            "TELEGRAM_TOKEN não configurado."
        )
        return False

    if not CHAT_ID:
        logging.error(
            "CHAT_ID não configurado."
        )
        return False

    url = (
        "https://api.telegram.org/"
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

        if resposta.status_code != 200:

            logging.error(
                "Telegram erro %s: %s",
                resposta.status_code,
                resposta.text
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
# FORMATAR SINAL
# ============================================================

def formatar_sinal(
    sinal
):

    return f"""
<b>🚨 NOVO SINAL COMBINADO</b>

⚽ <b>{sinal['casa']} x {sinal['fora']}</b>

🕐 {sinal['data_jogo']}

🎯 <b>Mercados</b>

⚽ Over 8.5 escanteios
⚽ Odd: {sinal['odd_escanteios']:.2f}

⚽ Under 4.5 gols
⚽ Odd: {sinal['odd_gols']:.2f}

🔥 <b>Odd combinada: {sinal['odd_combinada']:.2f}</b>

📊 Assertividade histórica:
<b>{sinal['probabilidade_historica']:.2f}%</b>

📚 Jogos históricos:
{sinal['jogos_historico']}

🏦 <b>Banca {sinal['banca']}</b>

💰 Entrada:
<b>R$ {sinal['entrada']:.2f}</b>

🔁 Gale:
<b>{sinal['gale']}</b>

📌 Status:
<b>AGUARDANDO RESULTADO</b>
"""


# ============================================================
# CRIAR SINAL
# ============================================================

def criar_sinal(
    jogo,
    odds,
    analise,
    banca_num
):

    fixture_id = obter_id_fixture(
        jogo
    )

    casa, fora = obter_nome_times(
        jogo
    )

    data_jogo = obter_data_jogo(
        jogo
    )

    banca = stats[
        "por_banca"
    ][
        str(banca_num)
    ]

    gale = int(
        banca.get(
            "gale",
            0
        )
    )

    entrada = float(
        banca.get(
            "entrada_atual",
            ENTRADA_INICIAL
        )
    )

    sinal_id = (
        f"{fixture_id}_"
        f"{int(time.time())}_"
        f"{banca_num}"
    )

    estatisticas = (
        analise["estatisticas"]
    )

    sinal = {
        "id": sinal_id,

        "fixture_id": str(
            fixture_id
        ),

        "casa": casa,
        "fora": fora,

        "data_jogo": data_jogo,

        "mercado_escanteios":
            MERCADO_ESCANTEIOS,

        "mercado_gols":
            MERCADO_GOLS,

        "odd_escanteios":
            odds["escanteios"],

        "odd_gols":
            odds["gols"],

        "odd_combinada":
            odds["combinada"],

        "probabilidade_historica":
            analise[
                "probabilidade_historica"
            ],

        "jogos_historico":
            estatisticas["jogos"],

        "media_escanteios":
            estatisticas[
                "media_escanteios"
            ],

        "media_gols":
            estatisticas[
                "media_gols"
            ],

        "prob_over_corners":
            estatisticas[
                "over_corners"
            ],

        "prob_under_goals":
            estatisticas[
                "under_goals"
            ],

        "prob_combinada":
            estatisticas[
                "combinada"
            ],

        "score":
            analise["score"],

        "banca":
            banca_num,

        "gale":
            gale,

        "entrada":
            entrada,

        "status":
            "ativo",

        "resultado":
            None,

        "data_criacao":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "data_resolucao":
            None
    }

    sinais[sinal_id] = sinal

    # --------------------------------------------------------
    # Atualizar banca
    # --------------------------------------------------------

    banca["status"] = "ocupada"

    banca["sinal_atual"] = sinal_id

    banca["total_entradas"] = (
        banca.get(
            "total_entradas",
            0
        )
        + 1
    )

    # --------------------------------------------------------
    # Estatísticas
    # --------------------------------------------------------

    stats[
        "total_sinais"
    ] += 1

    stats[
        "por_gale"
    ][
        str(gale)
    ][
        "entradas"
    ] += 1

    stats[
        "por_mercado"
    ][
        "combinado"
    ][
        "entradas"
    ] += 1

    stats[
        "ultima_atualizacao"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    salvar_json(
        ARQUIVO_SINAIS,
        sinais
    )

    salvar_json(
        ARQUIVO_STATS,
        stats
    )

    mensagem = formatar_sinal(
        sinal
    )

    enviado = enviar_telegram(
        mensagem
    )

    if enviado:

        logging.info(
            "Sinal enviado: %s",
            sinal_id
        )

    else:

        logging.error(
            "Sinal criado mas não enviado ao Telegram: %s",
            sinal_id
        )

    return sinal


# ============================================================
# GERAR NOVOS SINAIS
# ============================================================

def gerar_sinais():

    banca_livre = encontrar_banca_livre()

    if banca_livre is None:

        logging.info(
            "Todas as %s bancas estão ocupadas.",
            TOTAL_BANCAS
        )

        return

    fixtures = (
        obter_fixtures_proximas_24h()
    )

    if not fixtures:

        logging.info(
            "Nenhum fixture encontrado."
        )

        return

    candidatos = []

    jogos_analisados = 0

    for jogo in fixtures:

        if (
            jogos_analisados
            >= MAX_JOGOS_ODDS_POR_CICLO
        ):

            break

        fixture_id = (
            obter_id_fixture(
                jogo
            )
        )

        if not fixture_id:
            continue

        if fixture_ja_tem_sinal(
            fixture_id
        ):

            continue

        # ----------------------------------------------------
        # Evitar jogos que já começaram
        # ----------------------------------------------------

        status = str(
            jogo.get(
                "status",
                ""
            )
        ).lower()

        if status:

            if status not in [
                "scheduled",
                "not_started",
                "upcoming",
                "pending"
            ]:

                logging.info(
                    "Fixture %s ignorado por status: %s",
                    fixture_id,
                    status
                )

                continue

        # ----------------------------------------------------
        # Buscar odds
        # ----------------------------------------------------

        odds = obter_odds_fixture(
            fixture_id
        )

        jogos_analisados += 1

        if not odds:
            continue

        # ----------------------------------------------------
        # Análise
        # ----------------------------------------------------

        analise = analisar_jogo(
            jogo,
            odds
        )

        if analise is None:
            continue

        candidatos.append(
            (
                jogo,
                odds,
                analise
            )
        )

    if not candidatos:

        logging.info(
            "Nenhum candidato aprovado pelos filtros."
        )

        return

    candidatos.sort(
        key=lambda x: x[2]["score"],
        reverse=True
    )

    # --------------------------------------------------------
    # Preencher somente UMA banca por ciclo.
    # O próximo ciclo preencherá a próxima banca livre.
    # --------------------------------------------------------

    banca_livre = encontrar_banca_livre()

    if banca_livre is None:
        return

    for jogo, odds, analise in candidatos:

        fixture_id = obter_id_fixture(
            jogo
        )

        if fixture_ja_tem_sinal(
            fixture_id
        ):

            continue

        criar_sinal(
            jogo,
            odds,
            analise,
            banca_livre
        )

        break


# ============================================================
# RESULTADO DO JOGO
# ============================================================

def extrair_resultado(
    jogo
):

    if not isinstance(
        jogo,
        dict
    ):

        return None

    gols_total = None
    corners_total = None

    # --------------------------------------------------------
    # GOLS
    # --------------------------------------------------------

    goals = jogo.get(
        "goals"
    )

    if isinstance(
        goals,
        dict
    ):

        home = para_float(
            goals.get("home")
        )

        away = para_float(
            goals.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            gols_total = (
                home + away
            )

    # --------------------------------------------------------
    # ESCANTEIOS
    # --------------------------------------------------------

    corners = jogo.get(
        "corners"
    )

    if isinstance(
        corners,
        dict
    ):

        home = para_float(
            corners.get("home")
        )

        away = para_float(
            corners.get("away")
        )

        if (
            home is not None
            and away is not None
        ):

            corners_total = (
                home + away
            )

    if (
        gols_total is None
        or corners_total is None
    ):

        return None

    return {
        "gols": gols_total,
        "escanteios": corners_total
    }


# ============================================================
# RESOLVER SINAL
# ============================================================

def resolver_sinal(
    sinal,
    resultado
):

    if sinal.get(
        "status"
    ) != "ativo":

        return False

    gols = resultado[
        "gols"
    ]

    escanteios = resultado[
        "escanteios"
    ]

    ganhou_escanteios = (
        escanteios > 8.5
    )

    ganhou_gols = (
        gols < 4.5
    )

    ganhou = (
        ganhou_escanteios
        and ganhou_gols
    )

    banca_num = int(
        sinal["banca"]
    )

    gale = int(
        sinal["gale"]
    )

    entrada = float(
        sinal["entrada"]
    )

    banca = stats[
        "por_banca"
    ][
        str(banca_num)
    ]

    # ========================================================
    # WIN
    # ========================================================

    if ganhou:

        sinal["status"] = "resolvido"

        sinal["resultado"] = "WIN"

        sinal["data_resolucao"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        lucro = (
            entrada
            * (
                sinal["odd_combinada"]
                - 1
            )
        )

        stats[
            "resolvidos"
        ] += 1

        stats[
            "wins"
        ] += 1

        stats[
            "lucro_teorico"
        ] += lucro

        banca["wins"] += 1

        banca["lucro"] += lucro

        stats[
            "por_gale"
        ][
            str(gale)
        ][
            "wins"
        ] += 1

        stats[
            "por_mercado"
        ][
            "combinado"
        ][
            "wins"
        ] += 1

        # ----------------------------------------------------
        # RESET DA BANCA
        # ----------------------------------------------------

        banca["status"] = "livre"

        banca["gale"] = 0

        banca[
            "entrada_atual"
        ] = ENTRADA_INICIAL

        banca[
            "sinal_atual"
        ] = None

        mensagem = f"""
<b>✅ SINAL RESOLVIDO — WIN</b>

⚽ <b>{sinal['casa']} x {sinal['fora']}</b>

🎯 Over 8.5 escanteios:
<b>{escanteios:.0f}</b>

🎯 Under 4.5 gols:
<b>{gols:.0f}</b>

🔥 Odd:
<b>{sinal['odd_combinada']:.2f}</b>

💰 Entrada:
<b>R$ {entrada:.2f}</b>

💵 Resultado teórico:
<b>+ R$ {lucro:.2f}</b>

🏦 Banca:
<b>{banca_num}</b>

🔄 Banca resetada para:
<b>R$ {ENTRADA_INICIAL:.2f}</b>
"""

        enviar_telegram(
            mensagem
        )

    # ========================================================
    # LOSS
    # ========================================================

    else:

        sinal["status"] = "resolvido"

        sinal["resultado"] = "LOSS"

        sinal["data_resolucao"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        stats[
            "resolvidos"
        ] += 1

        stats[
            "losses"
        ] += 1

        stats[
            "lucro_teorico"
        ] -= entrada

        banca["losses"] += 1

        banca["lucro"] -= entrada

        stats[
            "por_gale"
        ][
            str(gale)
        ][
            "losses"
        ] += 1

        stats[
            "por_mercado"
        ][
            "combinado"
        ][
            "losses"
        ] += 1

        # ----------------------------------------------------
        # AINDA TEM GALE
        # ----------------------------------------------------

        if gale < MAX_GALES:

            novo_gale = (
                gale + 1
            )

            nova_entrada = (
                entrada
                * MULTIPLICADOR_GALE
            )

            banca["status"] = "livre"

            banca["gale"] = novo_gale

            banca[
                "entrada_atual"
            ] = nova_entrada

            banca[
                "sinal_atual"
            ] = None

            mensagem = f"""
<b>❌ SINAL RESOLVIDO — LOSS</b>

⚽ <b>{sinal['casa']} x {sinal['fora']}</b>

🎯 Escanteios:
<b>{escanteios:.0f}</b>

🎯 Gols:
<b>{gols:.0f}</b>

💰 Entrada perdida:
<b>R$ {entrada:.2f}</b>

🏦 Banca:
<b>{banca_num}</b>

🔁 Próximo Gale:
<b>{novo_gale}</b>

💵 Próxima entrada:
<b>R$ {nova_entrada:.2f}</b>

📌 A banca está liberada para procurar outro jogo.
"""

            enviar_telegram(
                mensagem
            )

        # ----------------------------------------------------
        # FIM DOS GALES
        # ----------------------------------------------------

        else:

            banca["status"] = "livre"

            banca["gale"] = 0

            banca[
                "entrada_atual"
            ] = ENTRADA_INICIAL

            banca[
                "sinal_atual"
            ] = None

            mensagem = f"""
<b>❌ LOSS FINAL DA SEQUÊNCIA</b>

⚽ <b>{sinal['casa']} x {sinal['fora']}</b>

🎯 Escanteios:
<b>{escanteios:.0f}</b>

🎯 Gols:
<b>{gols:.0f}</b>

🏦 Banca:
<b>{banca_num}</b>

🔁 Foram utilizados os {MAX_GALES} Gales.

🔄 Sequência resetada.

💰 Próxima entrada:
<b>R$ {ENTRADA_INICIAL:.2f}</b>
"""

            enviar_telegram(
                mensagem
            )

    # ========================================================
    # ASSERTIVIDADE
    # ========================================================

    resolvidos = stats[
        "resolvidos"
    ]

    if resolvidos > 0:

        stats[
            "assertividade"
        ] = round(
            (
                stats["wins"]
                / resolvidos
            )
            * 100,
            2
        )

    stats[
        "ultima_atualizacao"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    salvar_json(
        ARQUIVO_SINAIS,
        sinais
    )

    salvar_json(
        ARQUIVO_STATS,
        stats
    )

    return True


# ============================================================
# VERIFICAR RESULTADOS
# ============================================================

def verificar_resultados():

    ativos = [
        sinal
        for sinal in sinais.values()
        if sinal.get(
            "status"
        ) == "ativo"
    ]

    if not ativos:
        return

    logging.info(
        "Verificando %s sinais ativos.",
        len(ativos)
    )

    for sinal in ativos:

        fixture_id = sinal.get(
            "fixture_id"
        )

        if not fixture_id:
            continue

        dados = api_get(
            f"fixtures/{fixture_id}"
        )

        if dados is None:
            continue

        # ----------------------------------------------------
        # Às vezes a API devolve fixture diretamente,
        # às vezes dentro de data/fixture.
        # ----------------------------------------------------

        jogo = dados

        if isinstance(
            dados,
            dict
        ):

            if isinstance(
                dados.get("data"),
                dict
            ):

                jogo = dados["data"]

            elif isinstance(
                dados.get("fixture"),
                dict
            ):

                jogo = dados["fixture"]

        resultado = (
            extrair_resultado(
                jogo
            )
        )

        if resultado is None:

            logging.info(
                "Resultado ainda não disponível para fixture %s.",
                fixture_id
            )

            continue

        # ----------------------------------------------------
        # Verificar se o jogo realmente terminou
        # ----------------------------------------------------

        status = str(
            jogo.get(
                "status",
                ""
            )
        ).lower()

        status_final = any(
            palavra in status
            for palavra in [
                "finished",
                "final",
                "ended",
                "closed",
                "ft"
            ]
        )

        if not status_final:

            # Se houver placar final preenchido,
            # ainda permitimos somente quando a API
            # indicar explicitamente valores válidos.

            logging.info(
                "Fixture %s possui resultado parcial/status=%s.",
                fixture_id,
                status
            )

            continue

        resolver_sinal(
            sinal,
            resultado
        )


# ============================================================
# LOOP DO ROBÔ
# ============================================================

def loop_robo():

    logging.info(
        "Robô iniciado."
    )

    logging.info(
        "Janela: próximas %s horas",
        JANELA_HORAS
    )

    logging.info(
        "Mercados: %s + %s",
        MERCADO_ESCANTEIOS,
        MERCADO_GOLS
    )

    logging.info(
        "Odd: %.2f até %.2f | alvo %.2f",
        ODD_MINIMA,
        ODD_MAXIMA,
        ODD_ALVO
    )

    logging.info(
        "Bancas: %s",
        TOTAL_BANCAS
    )

    while True:

        try:

            with lock:

                # Primeiro resolve sinais existentes
                verificar_resultados()

                # Depois procura novos sinais
                gerar_sinais()

        except Exception as e:

            logging.exception(
                "Erro no loop principal: %s",
                e
            )

        time.sleep(
            INTERVALO_ANALISE_SEGUNDOS
        )


# ============================================================
# THREAD
# ============================================================

thread_robo = threading.Thread(
    target=loop_robo,
    daemon=True
)

thread_robo.start()


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "robo combinado gols + escanteios",
        "janela_horas": JANELA_HORAS,
        "mercado_escanteios":
            MERCADO_ESCANTEIOS,
        "mercado_gols":
            MERCADO_GOLS,
        "odd_minima":
            ODD_MINIMA,
        "odd_maxima":
            ODD_MAXIMA,
        "bancas":
            TOTAL_BANCAS,
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    bancas = {}

    for numero, banca in (
        stats[
            "por_banca"
        ].items()
    ):

        bancas[numero] = {
            "status":
                banca.get(
                    "status"
                ),

            "gale":
                banca.get(
                    "gale"
                ),

            "entrada_atual":
                banca.get(
                    "entrada_atual"
                ),

            "sinal_atual":
                banca.get(
                    "sinal_atual"
                )
        }

    return jsonify({
        "online": True,
        "bancas": bancas,
        "total_sinais":
            stats["total_sinais"],
        "resolvidos":
            stats["resolvidos"],
        "wins":
            stats["wins"],
        "losses":
            stats["losses"],
        "assertividade":
            stats["assertividade"],
        "lucro_teorico":
            round(
                stats[
                    "lucro_teorico"
                ],
                2
            )
    })


# ============================================================
# STATS
# ============================================================

@app.route("/stats")
def rota_stats():

    return jsonify(
        stats
    )


# ============================================================
# BANCAS
# ============================================================

@app.route("/bancas")
def rota_bancas():

    return jsonify(
        stats[
            "por_banca"
        ]
    )


# ============================================================
# SINAIS
# ============================================================

@app.route("/sinais")
def rota_sinais():

    return jsonify(
        sinais
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    porta = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=porta
    )
