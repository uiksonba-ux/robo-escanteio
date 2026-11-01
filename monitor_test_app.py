"""Isolated PRACTICE monitoring sandbox. No IQ Option or Telegram imports.
This service intentionally has no access to the live robot's state.
"""
import hmac
import os
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import json
import threading

_config_lock = threading.RLock()
_config = {"schema_version": 1, "conta": "PRACTICE", "enabled": True, "faixas": [
    {"min": 0, "max": 49, "gales": 4, "mult": 2.0},
    {"min": 50, "max": 59, "gales": 3, "mult": 2.0},
    {"min": 60, "max": 69, "gales": 2, "mult": 2.0},
    {"min": 70, "max": 79, "gales": 1, "mult": 2.0},
    {"min": 80, "max": 100, "gales": 0, "mult": 2.0},
]}

app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status": "ok", "conta": "PRACTICE", "mode": "isolated_test"})

@app.get("/api/v1/monitor")
def monitor():
    token = os.getenv("MONITOR_API_TOKEN", "").strip()
    if not token:
        return jsonify({"erro": "token_nao_configurado"}), 503
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied, "Bearer " + token):
        return jsonify({"erro": "nao_autorizado"}), 401
    upstream = os.getenv("MONITOR_UPSTREAM_URL", "").strip()
    if upstream:
        parsed = urlparse(upstream)
        upstream_token = os.getenv("MONITOR_UPSTREAM_TOKEN", "").strip()
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or
                parsed.path != "/api/v1/monitor" or not upstream_token):
            return jsonify({"erro": "origem_real_nao_configurada"}), 503
        try:
            req = Request(upstream, headers={"Authorization": "Bearer " + upstream_token}, method="GET")
            with urlopen(req, timeout=12) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("response_too_large")
                data = json.loads(raw)
            if (data.get("schema_version") != 1 or data.get("conta") != "PRACTICE" or
                    data.get("modo") != "somente_leitura" or
                    data.get("origem") != "robo_practice_memoria_volatil" or
                    not isinstance(data.get("estatisticas"), dict) or
                    not isinstance(data.get("bancas"), dict) or
                    any("BANCA " + str(i) not in data["bancas"] for i in range(1, 11))):
                raise ValueError("invalid_practice_contract")
            return jsonify(data), 200
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, OSError):
            return jsonify({"erro": "fonte_practice_indisponivel"}), 502

    return jsonify({
        "schema_version": 1,
        "conta": "PRACTICE",
        "modo": "somente_leitura",
        "origem": "ambiente_de_teste_sem_dados_reais",
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "estatisticas": {"wins": 0, "losses": 0, "assertividade": 0.0},
        "bancas": {f"BANCA {i}": {"wins": 0, "losses": 0, "saldo": None} for i in range(1, 11)},
    })

@app.route("/api/v1/practice/faixas", methods=["GET", "PUT"])
def practice_faixas():
    """Isolated configuration preview. Never forwards writes to the trading robot."""
    token = os.getenv("MONITOR_API_TOKEN", "").strip()
    if not token:
        return jsonify({"erro": "token_nao_configurado"}), 503
    if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
        return jsonify({"erro": "nao_autorizado"}), 401
    if request.method == "GET":
        with _config_lock:
            return jsonify({**_config, "aplicada_no_robo": False, "persistencia": "memoria_temporaria"}), 200
    if request.content_length is None or request.content_length > 4096:
        return jsonify({"erro": "tamanho_invalido"}), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "conta", "enabled", "faixas"}:
        return jsonify({"erro": "formato_invalido"}), 400
    if payload["schema_version"] != 1 or payload["conta"] != "PRACTICE" or type(payload["enabled"]) is not bool:
        return jsonify({"erro": "somente_practice"}), 400
    faixas = payload["faixas"]
    limites = [(0,49), (50,59), (60,69), (70,79), (80,100)]
    if not isinstance(faixas, list) or len(faixas) != 5:
        return jsonify({"erro": "faixas_invalidas"}), 400
    for faixa, (minimo, maximo) in zip(faixas, limites):
        if (not isinstance(faixa, dict) or set(faixa) != {"min", "max", "gales", "mult"} or
            type(faixa["min"]) is not int or type(faixa["max"]) is not int or
            (faixa["min"], faixa["max"]) != (minimo, maximo) or
            type(faixa["gales"]) is not int or not 0 <= faixa["gales"] <= 9 or
            type(faixa["mult"]) not in (float, int) or
            not 1.0 <= faixa["mult"] <= 10.0):
            return jsonify({"erro": "faixas_invalidas"}), 400
    with _config_lock:
        _config.clear()
        _config.update(payload)
    return jsonify({"salvo": True, "conta": "PRACTICE", "aplicada_no_robo": False,
                    "persistencia": "memoria_temporaria"}), 200
