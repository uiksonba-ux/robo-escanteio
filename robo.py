import os
import time
import json
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FIVE_DOLLAR_KEY = os.getenv("FIVE_DOLLAR_KEY")

PORT = int(os.getenv("PORT", "10000"))

# Estratégia
ODD_MIN = float(os.getenv("ODD_MIN", "1.35"))
ODD_MAX = float(os.getenv("ODD_MAX", "10.00"))

QTD_POR_RODADA = int(os.getenv("QTD_POR_RODADA", "8"))

HORAS_MIN = float(os.getenv("HORAS_MIN", "0.5"))
HORAS_MAX = float(os.getenv("HORAS_MAX", "12"))

MINIMO_HISTORICO = int(os.getenv("MINIMO_HISTORICO", "5"))
ASSERTIVIDADE_MINIMA = float(os.getenv("ASSERTIVIDADE_MINIMA", "60"))

HISTORICO_DIAS = int(os.getenv("HISTORICO_DIAS", "7"))

# Intervalos
INTERVALO_PRE = int(os.getenv("INTERVALO_PRE", "300"))
INTERVALO_RESULTADOS = int(os.getenv("INTERVALO_RESULTADOS", "180"))

# Cache
HISTORICO_TTL = int(os.getenv("HISTORICO_TTL", "1800"))
FUTUROS_TTL = int(os.getenv("FUTUROS_TTL", "300"))

# Máximo de páginas históricas
MAX_PAGINAS = int(os.getenv("MAX_PAGINAS", "20"))

MONITOR_ATIVO = os.getenv(
    "MONITOR_ATIVO",
    "true"
).lower() in ("1", "true", "yes", "sim")


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("robo-combinado")


# ============================================================
# ESTADO
# ============================================================

ARQUIVO_ESTADO = "estado.json"

lock = threading.RLock()
api_lock = threading.Lock()

session = requests.Session()

estado = {
    "stats": {
        "wins": 0,
        "losses": 0,
        "push": 0
    },

    "pendentes": {},

    "historico_cache": {
        "timestamp": 0,
        "ligas": [],
        "jogos": []
    },

    "futuros_cache": {
        "timestamp": 0,
        "jogos": []
    },

    "api": {
        "limit": None,
        "remaining": None,
        "reset": None,
        "ultima_chamada": None,
        "ultimo_status": None
    }
}


# ============================================================
# PERSISTÊNCIA
# ============================================================

def salvar_estado():
    with lock:
        try:
            with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
                json.dump(
                    estado,
                    f,
                    ensure_ascii=False,
                    indent=2
                )
        except Exception:
            log.exception("Erro ao salvar estado.")


def carregar_estado():
    global estado

    if not os.path.exists(ARQUIVO_ESTADO):
        return

    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            salvo = json.load(f)

        if isinstance(salvo, dict):
            estado.update(salvo)

        log.info("Estado carregado.")

    except Exception:
        log.exception("Erro ao carregar estado.")


# ============================================================
# UTILITÁRIOS
# ============================================================

def agora_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def numero(valor):
    if valor is None:
        return None

    if isinstance(valor, bool):
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    try:
        texto = str(valor)
        texto = texto.replace("%", "")
        texto = texto.replace(",", ".")
        return float(texto)
    except Exception:
        return None


def primeiro_dict(valor):
    if isinstance(valor, dict):
        return valor

    if isinstance(valor, list) and valor:
        if isinstance(valor[0], dict):
            return valor[0]

    return None


# ============================================================
# RATE LIMIT
# ============================================================

def atualizar_rate_limit(response):
    try:
        limit = response.headers.get("X-RateLimit-Limit")
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")

        with lock:
            if limit is not None:
                estado["api"]["limit"] = int(limit)

            if remaining is not None:
                estado["api"]["remaining"] = int(remaining)

            if reset is not None:
                try:
                    estado["api"]["reset"] = float(reset)
                except Exception:
                    estado["api"]["reset"] = None

            estado["api"]["ultima_chamada"] = agora_utc().isoformat()
            estado["api"]["ultimo_status"] = response.status_code

    except Exception:
        pass


def espera_rate_limit():
    with lock:
        remaining = estado["api"].get("remaining")
        reset = estado["api"].get("reset")

    if remaining is None:
        return

    # Não esperar se ainda há chamadas disponíveis.
    if remaining > 0:
        return

    agora_ts = time.time()

    if reset:
        espera = max(0, reset - agora_ts + 1)

        if espera > 0:
            log.warning(
                "Rate limit esgotado. Aguardando %.1fs.",
                espera
            )

            # Máximo de 90 segundos por espera.
            time.sleep(min(espera, 90))


# ============================================================
# API
# ============================================================

def api_get(endpoint, params=None, tentativas=2):
    if not FIVE_DOLLAR_KEY:
        log.error("FIVE_DOLLAR_KEY não configurada.")
        return None

    # Todas as chamadas passam pelo mesmo bloqueio.
    with api_lock:

        for tentativa in range(1, tentativas + 1):

            espera_rate_limit()

            headers = {
                "X-API-Key": FIVE_DOLLAR_KEY,
                "Accept": "application/json"
            }

            try:
                url = f"{BASE_API}/{endpoint}"

                response = session.get(
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=30
                )

                atualizar_rate_limit(response)

                if response.status_code == 429:

                    retry_after = response.headers.get(
                        "Retry-After"
                    )

                    if retry_after:
                        try:
                            espera = float(retry_after)
                        except Exception:
                            espera = 30
                    else:
                        espera = 30

                    log.warning(
                        "Rate limit atingido. Aguardando %.1fs...",
                        espera
                    )

                    # NÃO faz várias tentativas rápidas.
                    time.sleep(min(espera + 1, 120))

                    continue

                if response.status_code >= 500:

                    log.warning(
                        "Erro servidor API: HTTP %s",
                        response.status_code
                    )

                    if tentativa < tentativas:
                        time.sleep(5)

                    continue

                if response.status_code != 200:

                    log.error(
                        "API HTTP %s: %s",
                        response.status_code,
                        response.text[:500]
                    )

                    return None

                try:
                    return response.json()

                except Exception:
                    log.error(
                        "API retornou JSON inválido."
                    )

                    return None

            except requests.RequestException as exc:

                log.warning(
                    "Falha API %s tentativa %s/%s: %s",
                    endpoint,
                    tentativa,
                    tentativas,
                    exc
                )

                if tentativa < tentativas:
                    time.sleep(3)

        return None


# ============================================================
# EXTRAÇÃO
# ============================================================

def extrair_lista(payload):
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, list):
        return data

    for chave in (
        "fixtures",
        "results",
        "items"
    ):
        valor = payload.get(chave)

        if isinstance(valor, list):
            return valor

    return []


def extrair_objeto(payload):
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")

    if isinstance(data, dict):
        return data

    for chave in (
        "fixture",
        "result",
        "item"
    ):
        valor = payload.get(chave)

        if isinstance(valor, dict):
            return valor

    lista = extrair_lista(payload)

    if lista:
        return lista[0]

    return None


# ============================================================
# FUTUROS
# ============================================================

def buscar_futuros():
    agora = agora_utc()

    inicio = agora + timedelta(
        hours=HORAS_MIN
    )

    fim = agora + timedelta(
        hours=HORAS_MAX
    )

    payload = api_get(
        "fixtures",
        {
            "start_time": iso(inicio),
            "end_time": iso(fim),
            "status": "NS",
            "include": "odds"
        }
    )

    if not payload:
        return []

    jogos = extrair_lista(payload)

    log.info(
        "Jogos futuros encontrados: %s",
        len(jogos)
    )

    return jogos


def futuros_cache():
    agora_ts = time.time()

    with lock:
        timestamp = estado["futuros_cache"]["timestamp"]
        jogos = estado["futuros_cache"]["jogos"]

    if (
        jogos
        and agora_ts - timestamp < FUTUROS_TTL
    ):
        return jogos

    jogos = buscar_futuros()

    with lock:
        estado["futuros_cache"] = {
            "timestamp": agora_ts,
            "jogos": jogos
        }

    salvar_estado()

    return jogos


# ============================================================
# ODDS
# ============================================================

def numero_odds(valor):
    if isinstance(valor, dict):
        for chave in (
            "price",
            "odd",
            "value"
        ):
            n = numero(valor.get(chave))
            if n is not None:
                return n

    return numero(valor)


def extrair_linha_preco(item, mercado):
    if not isinstance(item, dict):
        return None, None

    # Mercado específico.
    candidatos = []

    if mercado == "goals":
        candidatos = [
            item.get("goal_line"),
            item.get("goals"),
            item.get("total_goals")
        ]

    elif mercado == "corners":
        candidatos = [
            item.get("corner_line"),
            item.get("corners"),
            item.get("total_corners")
        ]

    for candidato in candidatos:

        if isinstance(candidato, dict):

            linha = numero(
                candidato.get("line")
            )

            preco = numero_odds(
                candidato
            )

            if linha is not None:
                return linha, preco

        else:

            linha = numero(candidato)

            if linha is not None:
                return linha, None

    # Estrutura alternativa de odds.
    odds = item.get("odds")

    if isinstance(odds, dict):

        bloco = odds.get(mercado)

        if isinstance(bloco, dict):

            linha = numero(
                bloco.get("line")
            )

            preco = numero_odds(
                bloco.get("over")
            )

            if linha is not None:
                return linha, preco

    return None, None


def obter_mercados(jogo):
    """
    Tenta encontrar os mercados diretamente no fixture.
    Não faz uma chamada extra por jogo.
    """

    odds = jogo.get("odds")

    if isinstance(odds, dict):

        gols = odds.get("goals")
        cantos = odds.get("corners")

        if isinstance(gols, dict):
            linha_gols = numero(
                gols.get("line")
            )

            odd_gols = numero_odds(
                gols.get("over")
            )
        else:
            linha_gols = None
            odd_gols = None

        if isinstance(cantos, dict):
            linha_cantos = numero(
                cantos.get("line")
            )

            odd_cantos = numero_odds(
                cantos.get("over")
            )
        else:
            linha_cantos = None
            odd_cantos = None

        if linha_gols and linha_cantos:
            return {
                "goal_line": linha_gols,
                "goal_odd": odd_gols,
                "corner_line": linha_cantos,
                "corner_odd": odd_cantos
            }

    # Estrutura direta.
    goal_line, goal_odd = extrair_linha_preco(
        jogo,
        "goals"
    )

    corner_line, corner_odd = extrair_linha_preco(
        jogo,
        "corners"
    )

    if goal_line is None or corner_line is None:
        return None

    return {
        "goal_line": goal_line,
        "goal_odd": goal_odd,
        "corner_line": corner_line,
        "corner_odd": corner_odd
    }


def odd_combinada(mercados):
    if not mercados:
        return None

    odd_gols = mercados.get("goal_odd")
    odd_cantos = mercados.get("corner_odd")

    if odd_gols is None or odd_cantos is None:
        return None

    try:
        return round(
            float(odd_gols) * float(odd_cantos),
            2
        )
    except Exception:
        return None


# ============================================================
# IDENTIFICAÇÃO DO JOGO
# ============================================================

def fixture_id(jogo):
    if not isinstance(jogo, dict):
        return None

    for chave in (
        "id",
        "fixture_id"
    ):
        if jogo.get(chave) is not None:
            return jogo.get(chave)

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):
        return fixture.get("id")

    return None


def nomes_times(jogo):
    casa = "Casa"
    fora = "Fora"

    teams = jogo.get("teams")

    if isinstance(teams, dict):

        h = teams.get("home")
        a = teams.get("away")

        if isinstance(h, dict):
            casa = h.get("name") or casa

        if isinstance(a, dict):
            fora = a.get("name") or fora

    return casa, fora


def liga_id(jogo):
    league = jogo.get("league")

    if isinstance(league, dict):
        return league.get("id")

    return jogo.get("league_id")


def data_jogo(jogo):
    for chave in (
        "start_time",
        "date",
        "fixture_date"
    ):
        valor = jogo.get(chave)

        if valor:
            return str(valor)

    fixture = jogo.get("fixture")

    if isinstance(fixture, dict):
        return fixture.get("date")

    return None


# ============================================================
# HISTÓRICO
# ============================================================

def buscar_historico_liga(liga):
    agora = agora_utc()

    inicio = agora - timedelta(
        days=HISTORICO_DIAS
    )

    fim = agora

    pagina = 1
    todos = []

    while pagina <= MAX_PAGINAS:

        payload = api_get(
            f"leagues/{liga}/fixtures",
            {
                "start_time": iso(inicio),
                "end_time": iso(fim),
                "status": "finished",
                "include": "odds",
                "page": pagina
            }
        )

        if not payload:
            break

        jogos = extrair_lista(payload)

        if not jogos:
            break

        todos.extend(jogos)

        # Evita páginas desnecessárias.
        if len(jogos) < 50:
            break

        pagina += 1

    log.info(
        "Histórico liga %s: %s jogos",
        liga,
        len(todos)
    )

    return todos


def obter_ligas_dos_futuros(jogos):
    ligas = set()

    for jogo in jogos:

        lid = liga_id(jogo)

        if lid is not None:
            ligas.add(str(lid))

    return list(ligas)


def construir_historico():
    futuros = futuros_cache()

    ligas = obter_ligas_dos_futuros(
        futuros
    )

    if not ligas:
        log.warning(
            "Nenhuma liga encontrada nos próximos jogos."
        )

        return [] 

    todos = []

    for liga in ligas:

        # Se a API já estiver perto do limite,
        # não dispara uma sequência enorme.
        with lock:
            remaining = estado["api"].get(
                "remaining"
            )

        if remaining is not None and remaining <= 1:
            log.warning(
                "Poucas chamadas restantes. "
                "Histórico pausado."
            )
            break

        jogos = buscar_historico_liga(
            liga
        )

        todos.extend(jogos)

    return todos


def historico_cache():
    agora_ts = time.time()

    with lock:
        timestamp = estado["historico_cache"]["timestamp"]
        jogos = estado["historico_cache"]["jogos"]

    if (
        jogos
        and agora_ts - timestamp < HISTORICO_TTL
    ):
        return jogos

    jogos = construir_historico()

    with lock:
        estado["historico_cache"] = {
            "timestamp": agora_ts,
            "ligas": obter_ligas_dos_futuros(
                futuros_cache()
            ),
            "jogos": jogos
        }

    salvar_estado()

    return jogos


# ============================================================
# RESULTADO HISTÓRICO
# ============================================================

def gols_jogo(jogo):
    goals = jogo.get("goals")

    if not isinstance(goals, dict):
        return None

    casa = numero(
        goals.get("home")
    )

    fora = numero(
        goals.get("away")
    )

    if casa is None or fora is None:
        return None

    return casa + fora


def cantos_jogo(jogo):
    """
    Procura corners diretamente no fixture.
    """

    corners = jogo.get("corners")

    if isinstance(corners, dict):

        total = numero(
            corners.get("total")
        )

        if total is not None:
            return total

    total = numero(
        jogo.get("corner_total")
    )

    if total is not None:
        return total

    # Algumas respostas podem trazer
    # estatísticas por equipe.
    stats = jogo.get("stats")

    if isinstance(stats, dict):

        valor = stats.get("corners")

        total = numero(valor)

        if total is not None:
            return total

    return None


def classificar_historico(jogo):
    mercados = obter_mercados(jogo)

    if not mercados:
        return None

    gols = gols_jogo(jogo)
    cantos = cantos_jogo(jogo)

    if gols is None or cantos is None:
        return None

    linha_gols = mercados.get(
        "goal_line"
    )

    linha_cantos = mercados.get(
        "corner_line"
    )

    if linha_gols is None or linha_cantos is None:
        return None

    if (
        gols > linha_gols
        and cantos > linha_cantos
    ):
        resultado = "WIN"

    elif (
        gols == linha_gols
        or cantos == linha_cantos
    ):
        resultado = "PUSH"

    else:
        resultado = "LOSS"

    return {
        "goal_line": linha_gols,
        "corner_line": linha_cantos,
        "resultado": resultado
    }


def calcular_historico(jogos):
    grupos = {}

    for jogo in jogos:

        classificacao = classificar_historico(
            jogo
        )

        if not classificacao:
            continue

        chave = (
            classificacao["goal_line"],
            classificacao["corner_line"]
        )

        if chave not in grupos:
            grupos[chave] = {
                "wins": 0,
                "losses": 0,
                "push": 0
            }

        resultado = classificacao[
            "resultado"
        ]

        if resultado == "WIN":
            grupos[chave]["wins"] += 1

        elif resultado == "LOSS":
            grupos[chave]["losses"] += 1

        else:
            grupos[chave]["push"] += 1

    resultado_final = []

    for chave, dados in grupos.items():

        decisivos = (
            dados["wins"]
            + dados["losses"]
        )

        if decisivos < MINIMO_HISTORICO:
            continue

        assertividade = (
            dados["wins"]
            / decisivos
            * 100
        )

        if assertividade < ASSERTIVIDADE_MINIMA:
            continue

        resultado_final.append({
            "goal_line": chave[0],
            "corner_line": chave[1],
            "wins": dados["wins"],
            "losses": dados["losses"],
            "push": dados["push"],
            "decisivos": decisivos,
            "assertividade": round(
                assertividade,
                2
            )
        })

    resultado_final.sort(
        key=lambda x: (
            x["assertividade"],
            x["decisivos"]
        ),
        reverse=True
    )

    return resultado_final


# ============================================================
# SINAIS
# ============================================================

def gerar_sinais():
    futuros = futuros_cache()

    if not futuros:
        log.info(
            "Nenhum jogo futuro encontrado."
        )
        return []

    historico = historico_cache()

    if not historico:
        log.info(
            "Histórico ainda não disponível."
        )
        return []

    filtros = calcular_historico(
        historico
    )

    if not filtros:
        log.info(
            "Nenhuma combinação atingiu "
            "os filtros históricos."
        )
        return []

    mapa = {
        (
            f["goal_line"],
            f["corner_line"]
        ): f
        for f in filtros
    }

    candidatos = []

    for jogo in futuros:

        fid = fixture_id(jogo)

        if fid is None:
            continue

        mercados = obter_mercados(jogo)

        if not mercados:
            continue

        linha_gols = mercados[
            "goal_line"
        ]

        linha_cantos = mercados[
            "corner_line"
        ]

        filtro = mapa.get(
            (
                linha_gols,
                linha_cantos
            )
        )

        if not filtro:
            continue

        odd = odd_combinada(
            mercados
        )

        if odd is None:
            continue

        if not (
            ODD_MIN
            <= odd
            <= ODD_MAX
        ):
            continue

        casa, fora = nomes_times(
            jogo
        )

        candidato = {
            "fixture_id": fid,
            "casa": casa,
            "fora": fora,
            "data": data_jogo(jogo),
            "goal_line": linha_gols,
            "corner_line": linha_cantos,
            "odd_gols": mercados.get(
                "goal_odd"
            ),
            "odd_cantos": mercados.get(
                "corner_odd"
            ),
            "odd_combinada": odd,
            "wins": filtro["wins"],
            "losses": filtro["losses"],
            "push": filtro["push"],
            "decisivos": filtro[
                "decisivos"
            ],
            "assertividade": filtro[
                "assertividade"
            ]
        }

        candidatos.append(
            candidato
        )

    # Ranking:
    # 1. assertividade
    # 2. amostra
    # 3. odd
    candidatos.sort(
        key=lambda x: (
            x["assertividade"],
            x["decisivos"],
            x["odd_combinada"]
        ),
        reverse=True
    )

    return candidatos[
        :QTD_POR_RODADA
    ]


# ============================================================
# TELEGRAM
# ============================================================

def telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning(
            "Telegram não configurado."
        )
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

        if not response.ok:
            log.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:500]
            )
            return False

        data = response.json()

        if not data.get("ok"):
            log.error(
                "Telegram recusou mensagem: %s",
                data
            )
            return False

        return True

    except Exception:
        log.exception(
            "Erro enviando Telegram."
        )
        return False


def texto_sinal(s):
    return (
        "🔥 <b>SINAL COMBINADO</b>\n\n"

        f"⚽ <b>{s['casa']}</b> x "
        f"<b>{s['fora']}</b>\n\n"

        "🎯 <b>Mercado:</b>\n"
        f"Over {s['goal_line']} Gols + "
        f"Over {s['corner_line']} Escanteios\n\n"

        f"💰 <b>Odd combinada:</b> "
        f"{s['odd_combinada']:.2f}\n\n"

        f"📊 <b>Assertividade histórica:</b> "
        f"{s['assertividade']:.2f}%\n"

        f"📚 <b>Amostra:</b> "
        f"{s['decisivos']} jogos decisivos\n"

        f"✅ Wins: {s['wins']}\n"
        f"❌ Losses: {s['losses']}\n"
        f"↔️ Push: {s['push']}\n\n"

        f"📈 <b>Histórico:</b> últimos "
        f"{HISTORICO_DIAS} dias\n\n"

        "⚠️ <b>Sinal estatístico.</b>\n"
        "A entrada deve ser avaliada conforme "
        "sua gestão de banca."
    )


# ============================================================
# PENDÊNCIAS / RESULTADOS
# ============================================================

def sinal_key(sinal):
    return str(
        sinal["fixture_id"]
    )


def registrar_sinais(sinais):
    novos = 0

    with lock:

        for sinal in sinais:

            chave = sinal_key(
                sinal
            )

            if chave in estado[
                "pendentes"
            ]:
                continue

            estado[
                "pendentes"
            ][chave] = {
                **sinal,
                "criado_em":
                    agora_utc().isoformat(),
                "status": "PENDENTE"
            }

            novos += 1

    if novos:
        salvar_estado()

    return novos


def buscar_fixture(fid):
    payload = api_get(
        f"fixtures/{fid}"
    )

    return extrair_objeto(
        payload
    )


def resolver_sinal(sinal, jogo):
    if not jogo:
        return None

    status = str(
        jogo.get("status", "")
    ).lower()

    if status not in (
        "ft",
        "finished",
        "final"
    ):
        return None

    goals = jogo.get("goals")

    if isinstance(goals, dict):
        casa = numero(
            goals.get("home")
        )
        fora = numero(
            goals.get("away")
        )
    else:
        casa = None
        fora = None

    if casa is None or fora is None:
        return None

    gols = casa + fora

    cantos = cantos_jogo(
        jogo
    )

    if cantos is None:
        return None

    linha_gols = sinal[
        "goal_line"
    ]

    linha_cantos = sinal[
        "corner_line"
    ]

    if (
        gols > linha_gols
        and cantos > linha_cantos
    ):
        return "WIN"

    if (
        gols == linha_gols
        or cantos == linha_cantos
    ):
        return "PUSH"

    return "LOSS"


def verificar_resultados():
    with lock:
        pendentes = list(
            estado[
                "pendentes"
            ].values()
        )

    if not pendentes:
        return

    log.info(
        "Verificando %s sinais pendentes.",
        len(pendentes)
    )

    resolvidos = []

    for sinal in pendentes:

        # Se API estiver sem chamadas,
        # deixa para o próximo ciclo.
        with lock:
            remaining = estado[
                "api"
            ].get("remaining")

        if (
            remaining is not None
            and remaining <= 0
        ):
            break

        fid = sinal[
            "fixture_id"
        ]

        jogo = buscar_fixture(
            fid
        )

        resultado = resolver_sinal(
            sinal,
            jogo
        )

        if resultado is None:
            continue

        resolvidos.append(
            (
                str(fid),
                resultado,
                sinal
            )
        )

    if not resolvidos:
        return

    mensagens = []

    with lock:

        for fid, resultado, sinal in resolvidos:

            estado[
                "pendentes"
            ].pop(
                fid,
                None
            )

            estado[
                "stats"
            ][
                "wins"
                if resultado == "WIN"
                else "losses"
                if resultado == "LOSS"
                else "push"
            ] += 1

            mensagens.append(
                (
                    resultado,
                    sinal
                )
            )

    salvar_estado()

    for resultado, sinal in mensagens:

        if resultado == "WIN":
            emoji = "✅"
        elif resultado == "LOSS":
            emoji = "❌"
        else:
            emoji = "↔️"

        texto = (
            f"{emoji} <b>RESULTADO "
            f"{resultado}</b>\n\n"

            f"⚽ {sinal['casa']} x "
            f"{sinal['fora']}\n\n"

            f"🎯 Over {sinal['goal_line']} "
            f"Gols + Over "
            f"{sinal['corner_line']} "
            f"Escanteios\n"

            f"💰 Odd: "
            f"{sinal['odd_combinada']:.2f}\n\n"

            f"📊 Histórico do sinal: "
            f"{sinal['assertividade']:.2f}%"
        )

        telegram(texto)


# ============================================================
# CICLO DE SINAIS
# ============================================================

def ciclo_pre():
    try:
        log.info(
            "===== CICLO PRÉ-JOGO ====="
        )

        sinais = gerar_sinais()

        log.info(
            "Candidatos encontrados: %s",
            len(sinais)
        )

        novos = registrar_sinais(
            sinais
        )

        log.info(
            "Novos sinais: %s",
            novos
        )

        if novos:

            with lock:
                novos_lista = [
                    s
                    for s in sinais
                    if str(
                        s["fixture_id"]
                    ) in estado[
                        "pendentes"
                    ]
                ]

            # Envia somente os que foram
            # realmente registrados.
            enviados = 0

            for sinal in novos_lista:

                # Evita spam:
                # só envia se ainda não existia
                # no momento anterior.
                if telegram(
                    texto_sinal(sinal)
                ):
                    enviados += 1

            log.info(
                "Telegram: %s sinais enviados.",
                enviados
            )

    except Exception:
        log.exception(
            "Erro no ciclo pré-jogo."
        )


def ciclo_resultados():
    try:
        log.info(
            "===== CICLO RESULTADOS ====="
        )

        verificar_resultados()

    except Exception:
        log.exception(
            "Erro no ciclo de resultados."
        )


# ============================================================
# MONITOR
# ============================================================

def monitor():
    log.info(
        "Monitor iniciado."
    )

    ultimo_pre = 0
    ultimo_resultados = 0

    while MONITOR_ATIVO:

        agora_ts = time.time()

        try:

            if (
                agora_ts - ultimo_pre
                >= INTERVALO_PRE
            ):
                ciclo_pre()
                ultimo_pre = time.time()

            if (
                agora_ts - ultimo_resultados
                >= INTERVALO_RESULTADOS
            ):
                ciclo_resultados()
                ultimo_resultados = time.time()

        except Exception:
            log.exception(
                "Erro no monitor."
            )

        # NÃO usar sleep longo.
        # O monitor pode ser interrompido.
        time.sleep(5)


# ============================================================
# ROTAS
# ============================================================

@app.route("/")
def home():

    with lock:

        api_info = dict(
            estado["api"]
        )

        stats = dict(
            estado["stats"]
        )

        pendentes = len(
            estado[
                "pendentes"
            ]
        )

        hist_timestamp = estado[
            "historico_cache"
        ]["timestamp"]

        futuros_timestamp = estado[
            "futuros_cache"
        ]["timestamp"]

    return jsonify({
        "bot":
            "Robo Futebol Combinado v5",

        "status":
            "online",

        "estrategia":
            "Over Gols + Over Escanteios",

        "historico_dias":
            HISTORICO_DIAS,

        "minimo_historico":
            MINIMO_HISTORICO,

        "assertividade_minima":
            ASSERTIVIDADE_MINIMA,

        "odd_min":
            ODD_MIN,

        "odd_max":
            ODD_MAX,

        "pendentes":
            pendentes,

        "stats":
            stats,

        "api":
            api_info,

        "cache_historico":
            {
                "timestamp":
                    hist_timestamp,
                "idade_segundos":
                    round(
                        time.time()
                        - hist_timestamp,
                        1
                    )
                    if hist_timestamp
                    else None
            },

        "cache_futuros":
            {
                "timestamp":
                    futuros_timestamp,
                "idade_segundos":
                    round(
                        time.time()
                        - futuros_timestamp,
                        1
                    )
                    if futuros_timestamp
                    else None
            },

        "telegram_configurado":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),

        "api_configurada":
            bool(
                FIVE_DOLLAR_KEY
            )
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "api_configurada":
            bool(FIVE_DOLLAR_KEY),
        "telegram_configurado":
            bool(
                TELEGRAM_TOKEN
                and CHAT_ID
            ),
        "monitor":
            MONITOR_ATIVO,
        "hora_utc":
            agora_utc().isoformat()
    })


@app.route("/status")
def status():

    with lock:

        return jsonify({
            "api": estado["api"],
            "stats": estado["stats"],
            "pendentes": len(
                estado[
                    "pendentes"
                ]
            ),

            "historico_cache": {
                "jogos": len(
                    estado[
                        "historico_cache"
                    ]["jogos"]
                ),
                "timestamp":
                    estado[
                        "historico_cache"
                    ]["timestamp"]
            },

            "futuros_cache": {
                "jogos": len(
                    estado[
                        "futuros_cache"
                    ]["jogos"]
                ),
                "timestamp":
                    estado[
                        "futuros_cache"
                    ]["timestamp"]
            }
        })


@app.route("/debug/api")
def debug_api():

    # Uma ÚNICA chamada.
    payload = api_get(
        "status"
    )

    if payload is None:

        with lock:
            info = dict(
                estado["api"]
            )

        return jsonify({
            "ok": False,
            "erro":
                "Não foi possível consultar a API.",
            "api":
                info
        }), 502

    return jsonify({
        "ok": True,
        "api":
            estado["api"],
        "resposta":
            payload
    })


@app.route("/debug/rodar-agora")
def debug_rodar_agora():

    ciclo_pre()

    with lock:

        return jsonify({
            "ok": True,
            "pendentes":
                len(
                    estado[
                        "pendentes"
                    ]
                ),
            "api":
                estado["api"]
        })


@app.route("/debug/historico")
def debug_historico():

    jogos = historico_cache()

    filtros = calcular_historico(
        jogos
    )

    return jsonify({
        "jogos_historico":
            len(jogos),

        "combinacoes_aprovadas":
            len(filtros),

        "filtros":
            filtros,

        "api":
            estado["api"]
    })


@app.route("/debug/futuros")
def debug_futuros():

    jogos = futuros_cache()

    return jsonify({
        "jogos":
            len(jogos),

        "lista": [
            {
                "fixture_id":
                    fixture_id(j),
                "casa":
                    nomes_times(j)[0],
                "fora":
                    nomes_times(j)[1],
                "liga":
                    liga_id(j),
                "data":
                    data_jogo(j),
                "mercados":
                    obter_mercados(j)
            }
            for j in jogos[:50]
        ],

        "api":
            estado["api"]
    })


@app.route("/debug/sinais")
def debug_sinais():

    sinais = gerar_sinais()

    return jsonify({
        "quantidade":
            len(sinais),
        "sinais":
            sinais,
        "api":
            estado["api"]
    })


@app.route("/pendentes")
def pendentes():

    with lock:
        lista = list(
            estado[
                "pendentes"
            ].values()
        )

    return jsonify(lista)


# ============================================================
# INICIALIZAÇÃO
# ============================================================

carregar_estado()


def iniciar_monitor():

    if not MONITOR_ATIVO:
        log.warning(
            "MONITOR_ATIVO está desativado."
        )
        return

    thread = threading.Thread(
        target=monitor,
        daemon=True,
        name="monitor"
    )

    thread.start()

    log.info(
        "Thread do monitor iniciada."
    )


# Gunicorn importa este arquivo.
# O Flask fica disponível imediatamente.
# O monitor roda em background.
iniciar_monitor()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )
