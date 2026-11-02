"""Authenticated, volatile PRACTICE relay. Never imports or starts a trading worker.

Run one process (for example gunicorn --workers 1 --threads 4 monitor_test_app:app).
Desired settings and telemetry reset on restart; content hashes prevent false acks.
"""
import copy
import hashlib
import hmac
import ipaddress
import json
import math
import os
import threading
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID

from flask import Flask, jsonify, request

_config_lock = threading.RLock()
_config = {"schema_version": 1, "conta": "PRACTICE", "enabled": True, "faixas": [
    {"min": 0, "max": 49, "gales": 4, "mult": 2.0},
    {"min": 50, "max": 59, "gales": 3, "mult": 2.0},
    {"min": 60, "max": 69, "gales": 2, "mult": 2.0},
    {"min": 70, "max": 79, "gales": 1, "mult": 2.0},
    {"min": 80, "max": 100, "gales": 0, "mult": 2.0},
]}
_revision = 1
_telemetry = None
_received_at = None
_applied = None
_sessions = {}
_BANK_NAMES = {f"BANCA {i}" for i in range(1, 11)}
_CORE_KEYS = {"schema_version", "conta", "enabled", "faixas"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 65536


def _now():
    return datetime.now(timezone.utc)


def _config_id(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _authorized(*names):
    supplied = request.headers.get("Authorization", "").encode("utf-8")
    tokens = [os.getenv(name, "").strip() for name in names]
    if not any(tokens):
        return jsonify({"erro": "token_nao_configurado"}), 503
    if not any(token and hmac.compare_digest(supplied, ("Bearer " + token).encode("utf-8"))
               for token in tokens):
        return jsonify({"erro": "nao_autorizado"}), 401
    return None


@app.after_request
def private_response(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"erro": "tamanho_invalido"}), 413


@app.get("/health")
def health():
    return jsonify({"status": "ok", "conta": "PRACTICE", "mode": "isolated_test"})


def _count(value):
    return type(value) is int and 0 <= value <= 2**31 - 1


def _stats(data, bank=False):
    if not isinstance(data, dict) or not _count(data.get("wins")) or not _count(data.get("losses")):
        raise ValueError("invalid_counts")
    wins, losses = data["wins"], data["losses"]
    result = {"wins": wins, "losses": losses,
              "assertividade": 100.0 * wins / (wins + losses) if wins + losses else 0.0}
    if bank:
        saldo = data.get("saldo")
        if saldo is not None and (type(saldo) not in (int, float) or not math.isfinite(saldo)):
            raise ValueError("invalid_balance")
        result["saldo"] = saldo
    return result


def _monitor_data(data):
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data.get("schema_version") != 1 or data.get("conta") != "PRACTICE"
            or data.get("modo") != "somente_leitura"
            or data.get("origem") != "robo_practice_memoria_volatil"
            or not isinstance(data.get("bancas"), dict) or set(data["bancas"]) != _BANK_NAMES):
        raise ValueError("invalid_practice_contract")
    stats = _stats(data.get("estatisticas"))
    banks = {name: _stats(data["bancas"][name], bank=True) for name in sorted(_BANK_NAMES)}
    if any(sum(bank[key] for bank in banks.values()) != stats[key] for key in ("wins", "losses")):
        raise ValueError("inconsistent_totals")
    # Construct a whitelist response: never relay arbitrary fields or credentials.
    return {"schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
            "origem": "robo_practice_memoria_volatil", "estatisticas": stats, "bancas": banks}



class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect_rejected")


# urlopen remains a patchable seam for offline upstream contract tests.
def urlopen(request, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _upstream_url(value):
    try:
        parsed = urlparse(value)
        allowed = {host.strip().lower() for host in
                   os.getenv("MONITOR_UPSTREAM_ALLOWED_HOSTS", "").split(",") if host.strip()}
        if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.hostname
                or parsed.hostname.lower() not in allowed or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/api/v1/monitor"
                or parsed.hostname in {"localhost", "metadata.google.internal"}
                or "." not in parsed.hostname):
            return False
        try:
            ipaddress.ip_address(parsed.hostname)
            return False
        except ValueError:
            return True
    except ValueError:
        return False


@app.get("/api/v1/monitor")
def monitor():
    denied = _authorized("MONITOR_API_TOKEN")
    if denied:
        return denied
    with _config_lock:
        if _telemetry is not None:
            data = copy.deepcopy(_telemetry)
            source_time = datetime.fromisoformat(data["atualizado_em"])
            data.update({"recebido_em": _received_at.isoformat(),
                         "stale": (_now() - min(source_time, _received_at)).total_seconds() > 45})
            return jsonify(data)
    upstream = os.getenv("MONITOR_UPSTREAM_URL", "").strip()
    if upstream:
        upstream_token = os.getenv("MONITOR_UPSTREAM_TOKEN", "").strip()
        if not _upstream_url(upstream) or not upstream_token:
            return jsonify({"erro": "origem_real_nao_configurada"}), 503
        try:
            req = Request(upstream, headers={"Authorization": "Bearer " + upstream_token}, method="GET")
            with urlopen(req, timeout=3) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("response_too_large")
                result = _monitor_data(json.loads(raw))
            result.update({"stale": False, "recebido_em": _now().isoformat()})
            return jsonify(result)
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, OSError, OverflowError, AttributeError):
            return jsonify({"erro": "fonte_practice_indisponivel"}), 502
    return jsonify({
        "schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
        "origem": "ambiente_de_teste_sem_dados_reais", "stale": True,
        "atualizado_em": None, "recebido_em": None,
        "estatisticas": {"wins": 0, "losses": 0, "assertividade": 0.0},
        "bancas": {name: {"wins": 0, "losses": 0, "saldo": None} for name in sorted(_BANK_NAMES)},
    })


def _configuration_response():
    config_id = _config_id(_config)
    applied = _applied or {}
    matches = applied.get("revision") == _revision and applied.get("config_id") == config_id
    return {**copy.deepcopy(_config), "revision": _revision, "config_id": config_id,
            "aplicada_no_robo": matches, "persistencia": "memoria_temporaria",
            "aplicada_revision": applied.get("revision"), "aplicada_config_id": applied.get("config_id"),
            "aplicada_em": applied.get("at")}


def _valid_config(payload):
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("conta") != "PRACTICE" or type(payload.get("enabled")) is not bool):
        return False
    faixas = payload.get("faixas")
    limites = [(0, 49), (50, 59), (60, 69), (70, 79), (80, 100)]
    if not isinstance(faixas, list) or len(faixas) != 5:
        return False
    for faixa, (minimo, maximo) in zip(faixas, limites):
        if (not isinstance(faixa, dict) or set(faixa) != {"min", "max", "gales", "mult"}
                or type(faixa["min"]) is not int or type(faixa["max"]) is not int
                or (faixa["min"], faixa["max"]) != (minimo, maximo)
                or type(faixa["gales"]) is not int or not 0 <= faixa["gales"] <= 9
                or type(faixa["mult"]) not in (float, int) or not 1.0 <= faixa["mult"] <= 10.0
                or not math.isfinite(faixa["mult"])):
            return False
    return True


@app.route("/api/v1/practice/faixas", methods=["GET", "PUT"])
def practice_faixas():
    global _revision
    denied = _authorized("MONITOR_API_TOKEN", "MONITOR_ROBOT_TOKEN") if request.method == "GET" else _authorized("MONITOR_API_TOKEN")
    if denied:
        return denied
    if request.method == "GET":
        with _config_lock:
            return jsonify(_configuration_response())
    if request.content_length is None or request.content_length > 4096:
        return jsonify({"erro": "tamanho_invalido"}), 413
    payload = request.get_json(silent=True)
    if (not isinstance(payload, dict) or not _CORE_KEYS.issubset(payload)
            or set(payload) - _CORE_KEYS - {"expected_revision", "expected_config_id"}):
        return jsonify({"erro": "formato_invalido"}), 400
    if not _valid_config(payload):
        return jsonify({"erro": "faixas_invalidas_somente_practice"}), 400
    if ("expected_revision" in payload and
            (type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1)):
        return jsonify({"erro": "revisao_invalida"}), 400
    if "expected_config_id" in payload and (not isinstance(payload["expected_config_id"], str)
                                             or len(payload["expected_config_id"]) != 64):
        return jsonify({"erro": "revisao_invalida"}), 400
    config = {key: payload[key] for key in _CORE_KEYS}
    with _config_lock:
        if (("expected_revision" in payload and payload["expected_revision"] != _revision)
                or ("expected_config_id" in payload and payload["expected_config_id"] != _config_id(_config))):
            return jsonify({"erro": "configuracao_alterada", **_configuration_response()}), 409
        if config != _config:
            _config.clear()
            _config.update(copy.deepcopy(config))
            _revision += 1
        return jsonify({"salvo": True, **_configuration_response()})


@app.put("/api/v1/practice/telemetry")
def practice_telemetry():
    global _telemetry, _received_at, _applied
    denied = _authorized("MONITOR_ROBOT_TOKEN")
    if denied:
        return denied
    data = request.get_json(silent=True)
    try:
        result = _monitor_data(data)
        session = data.get("robot_session")
        if not isinstance(session, str) or str(UUID(session)) != session:
            raise ValueError("invalid_session")
        sequence = data.get("sequence")
        if not _count(sequence) or sequence < 1:
            raise ValueError("invalid_sequence")
        updated = datetime.fromisoformat(data["atualizado_em"])
        if updated.tzinfo is None or updated > _now() + timedelta(seconds=60):
            raise ValueError("invalid_timestamp")
        revision, config_id = data.get("config_revision"), data.get("config_id")
        if ((revision is not None or config_id is not None) and
                (type(revision) is not int or revision < 1 or not isinstance(config_id, str)
                 or len(config_id) != 64 or any(c not in "0123456789abcdef" for c in config_id))):
            raise ValueError("invalid_config_ack")
        result.update({"robot_session": session, "sequence": sequence,
                       "atualizado_em": updated.isoformat(), "config_revision": revision, "config_id": config_id})
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return jsonify({"erro": "telemetria_practice_invalida"}), 400
    with _config_lock:
        if sequence <= _sessions.get(session, 0):
            return jsonify({"erro": "telemetria_fora_de_ordem"}), 409
        if _telemetry is not None and session != _telemetry["robot_session"]:
            previous_time = datetime.fromisoformat(_telemetry["atualizado_em"])
            if updated <= previous_time:
                return jsonify({"erro": "telemetria_fora_de_ordem"}), 409
        _sessions[session] = sequence
        if len(_sessions) > 100:
            del _sessions[next(iter(_sessions))]
        _telemetry, _received_at = result, _now()
        _applied = {"revision": revision, "config_id": config_id, "at": _received_at.isoformat()}
    return jsonify({"recebido": True, "conta": "PRACTICE", "sequence": sequence})
