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
