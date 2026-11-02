"""Authenticated PRACTICE relay. Never imports or starts a trading worker.

Controls/settings/replay guards require a configured persistent SQLite file.
Financial telemetry is volatile and never substitutes for a fresh robot ACK.
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

import practice_control_store as control_store

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
app.config["PREFERRED_URL_SCHEME"] = "https"


@app.before_request
def require_api_https():
    if request.path.startswith("/api/") and not request.is_secure:
        return jsonify({"erro": "https_obrigatorio"}), 426


def _now():
    return datetime.now(timezone.utc)


def _config_id(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _authorized(*names):
    configured = [os.getenv(name, "").strip() for name in
                  ("MONITOR_API_TOKEN", "MONITOR_CONTROL_TOKEN", "MONITOR_ROBOT_TOKEN")]
    nonempty = [token for token in configured if token]
    if len(nonempty) != len(set(nonempty)):
        return jsonify({"erro": "tokens_devem_ser_distintos"}), 503
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
        for key in ("ocupada", "em_recuperacao"):
            if key in data:
                if type(data[key]) is not bool:
                    raise ValueError("invalid_bank_state")
                result[key] = data[key]
        if "ativo" in data:
            if data["ativo"] is not None and (not isinstance(data["ativo"], str) or len(data["ativo"]) > 60):
                raise ValueError("invalid_bank_asset")
            result["ativo"] = data["ativo"]
        for key in ("entrada_atual", "gales", "multiplicador", "resultado_liquido"):
            if key in data:
                if data[key] is not None and (type(data[key]) not in (int, float) or not math.isfinite(data[key])):
                    raise ValueError("invalid_bank_value")
                result[key] = data[key]
    return result


def _operational_data(data):
    result = {}
    if "iq_conectada" in data:
        if type(data["iq_conectada"]) is not bool:
            raise ValueError("invalid_connection")
        result["iq_conectada"] = data["iq_conectada"]
    if "saldo_practice" in data:
        value = data["saldo_practice"]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid_practice_balance")
        result["saldo_practice"] = value
    if "ativos" in data:
        values = data["ativos"]
        if not isinstance(values, list) or len(values) > 200 or any(not isinstance(v, str) or len(v) > 60 for v in values):
            raise ValueError("invalid_assets")
        result["ativos"] = values
    if "control_error" in data:
        error = data["control_error"]
        if error is not None and (not isinstance(error, str) or len(error) > 160):
            raise ValueError("invalid_control_error")
        result["control_error"] = error
    if "control_ack" in data and data["control_ack"] is None:
        result["control_ack"] = None
    if "control_ack" in data and data["control_ack"] is not None:
        ack = data["control_ack"]
        if (not isinstance(ack, dict) or set(ack) != {"revision", "command_id", "paused", "authorized"}
                or not _count(ack["revision"]) or ack["revision"] < 1
                or type(ack["paused"]) is not bool or type(ack["authorized"]) is not bool
                or not isinstance(ack["command_id"], str) or str(UUID(ack["command_id"])) != ack["command_id"]
                or (ack["paused"] is False and ack["authorized"] is False)):
            raise ValueError("invalid_control_ack")
        result["control_ack"] = copy.deepcopy(ack)
    management = data.get("gestao")
    if management is not None:
        if not isinstance(management, dict):
            raise ValueError("invalid_management")
        safe = {}
        for key in ("gales_proximos", "multiplicador_proximo", "prejuizo_acumulado", "recuperacao_planejada", "entrada_recuperacao_planejada"):
            if key in management:
                value = management[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid_management_value")
                safe[key] = value
        for key in ("recuperacao_estado", "recuperacao_em_curso", "payout_fonte"):
            if key in management:
                value = management[key]
                if value is not None and (not isinstance(value, str) or len(value) > 120):
                    raise ValueError("invalid_management_state")
                safe[key] = value
        result["gestao"] = safe
    operations = data.get("operacoes")
    if operations is not None:
        if not isinstance(operations, list) or len(operations) > 100:
            raise ValueError("invalid_operations")
        result["operacoes"] = []
        allowed = {"id", "banca", "ativo", "direcao", "estado", "gale", "gales", "nivel", "multiplicador", "valor", "entrada", "em_recuperacao", "payout", "payout_fonte", "resultado_liquido"}
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("invalid_operation")
            safe = {}
            for key in allowed & operation.keys():
                value = operation[key]
                if value is None or type(value) is bool or (isinstance(value, str) and len(value) <= 120):
                    safe[key] = value
                elif type(value) in (int, float) and math.isfinite(value):
                    safe[key] = value
                else:
                    raise ValueError("invalid_operation_value")
            result["operacoes"].append(safe)
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
    extras = _operational_data(data)
    # Construct a whitelist response: never relay arbitrary fields or credentials.
    return {"schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
            "origem": "robo_practice_memoria_volatil", "estatisticas": stats, "bancas": banks, **extras}



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
    if not isinstance(faixas, list) or len(faixas) != 5:
        return False
    next_lower = 0
    for faixa in faixas:
        if (not isinstance(faixa, dict) or set(faixa) != {"min", "max", "gales", "mult"}
                or type(faixa["min"]) is not int or type(faixa["max"]) is not int
                or faixa["min"] != next_lower or not faixa["min"] <= faixa["max"] <= 100
                or type(faixa["gales"]) is not int or not 0 <= faixa["gales"] <= 9
                or type(faixa["mult"]) not in (float, int) or not 1.0 <= faixa["mult"] <= 10.0
                or not math.isfinite(faixa["mult"])):
            return False
        next_lower = faixa["max"] + 1
    return next_lower == 101


def _control_guard(write=False):
    if not request.is_secure:
        return jsonify({"erro": "https_obrigatorio"}), 426
    if not all(os.getenv(key, "").strip() for key in
               ("MONITOR_API_TOKEN", "MONITOR_CONTROL_TOKEN", "MONITOR_ROBOT_TOKEN")):
        return jsonify({"erro": "tokens_controle_nao_configurados"}), 503
    denied = _authorized("MONITOR_CONTROL_TOKEN") if write else _authorized("MONITOR_API_TOKEN", "MONITOR_ROBOT_TOKEN")
    if denied:
        return denied
    if not os.getenv("MONITOR_CONTROL_DB", "").strip():
        return jsonify({"erro": "persistencia_controle_nao_configurada"}), 503
    return None


def _store_result(function, status=200):
    try:
        return jsonify(function()), status
    except control_store.ControlError as error:
        return jsonify({"erro": error.code}), error.status


@app.route("/api/v1/practice/control", methods=["GET", "PUT"])
def practice_control():
    denied = _control_guard(request.method == "PUT")
    if denied:
        return denied
    if request.method == "GET":
        return _store_result(lambda: control_store.get_control(_config))
    payload = request.get_json(silent=True)
    keys = {"schema_version", "conta", "action", "expected_revision", "command_id", "expires_at"}
    if (not isinstance(payload, dict) or set(payload) != keys
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["conta"] != "PRACTICE"
            or payload["action"] not in ("pause_new_entries", "resume_new_entries")):
        return jsonify({"erro": "comando_practice_invalido"}), 400
    return _store_result(lambda: control_store.put_control(_config, payload), 202)


@app.route("/api/v1/practice/settings", methods=["GET", "PUT"])
def practice_settings():
    denied = _control_guard(request.method == "PUT")
    if denied:
        return denied
    if request.method == "GET":
        return _store_result(lambda: control_store.get_settings(_config))
    if request.content_length is None or request.content_length > 4096:
        return jsonify({"erro": "tamanho_invalido"}), 413
    payload = request.get_json(silent=True)
    keys = _CORE_KEYS | {"expected_revision", "expected_config_id", "command_id", "expires_at"}
    if not isinstance(payload, dict) or set(payload) != keys or not _valid_config(payload):
        return jsonify({"erro": "faixas_invalidas_somente_practice"}), 400
    return _store_result(lambda: control_store.put_settings(_config, payload), 202)


@app.route("/api/v1/practice/faixas", methods=["GET", "PUT"])
def practice_faixas():
    # Legacy reads remain available; every write uses the same durable control path.
    if request.method == "PUT":
        return practice_settings()
    denied = _authorized("MONITOR_API_TOKEN", "MONITOR_ROBOT_TOKEN")
    if denied:
        return denied
    if os.getenv("MONITOR_CONTROL_DB", "").strip():
        return _store_result(lambda: control_store.get_settings(_config))
    with _config_lock:
        return jsonify(_configuration_response())


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
        if os.getenv("MONITOR_CONTROL_DB", "").strip():
            try:
                control_store.acknowledge(_config, result)
            except control_store.ControlError as error:
                return jsonify({"erro": error.code}), error.status
        _sessions[session] = sequence
        if len(_sessions) > 100:
            del _sessions[next(iter(_sessions))]
        _telemetry, _received_at = result, _now()
        _applied = {"revision": revision, "config_id": config_id, "at": _received_at.isoformat()}
    return jsonify({"recebido": True, "conta": "PRACTICE", "sequence": sequence})

