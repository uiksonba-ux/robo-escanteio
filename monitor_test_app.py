"""Isolated PRACTICE monitoring sandbox. No IQ Option or Telegram imports.
This service intentionally has no access to the live robot's state.
"""
import hmac
import os
from datetime import datetime, timezone
from flask import Flask, jsonify, request

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
    return jsonify({
        "schema_version": 1,
        "conta": "PRACTICE",
        "modo": "somente_leitura",
        "origem": "ambiente_de_teste_sem_dados_reais",
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "estatisticas": {"wins": 0, "losses": 0, "assertividade": 0.0},
        "bancas": {f"BANCA {i}": {"wins": 0, "losses": 0, "saldo": None} for i in range(1, 11)},
    })
