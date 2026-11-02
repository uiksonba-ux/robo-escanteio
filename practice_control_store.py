"""SQLite control relay: desired state is durable; only robot telemetry confirms it."""
import copy
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4


class ControlError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def now():
    return datetime.now(timezone.utc)


def digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _connect(default):
    path = os.getenv("MONITOR_CONTROL_DB", "").strip()
    if not path or not os.path.isabs(path) or path == ":memory:":
        raise ControlError("persistencia_controle_nao_configurada", 503)
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT data FROM state WHERE id=1").fetchone() is None:
            state = {"config": copy.deepcopy(default), "settings_revision": 1,
                     "control": {"revision": 1, "command_id": str(uuid4()),
                                 "desired_paused": True, "expires_at": None, "target_session": None},
                     "ack": None, "settings_ack": None}
            conn.execute("INSERT INTO state VALUES (1, ?)", (json.dumps(state),))
        return conn
    except (sqlite3.Error, OSError):
        raise ControlError("persistencia_controle_indisponivel", 503) from None


def _read(conn):
    return json.loads(conn.execute("SELECT data FROM state WHERE id=1").fetchone()[0])


def _save(conn, state):
    conn.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(state, allow_nan=False),))


def _transaction(default, function):
    conn = _connect(default)
    try:
        state = _read(conn)
        result = function(conn, state)
        _save(conn, state)
        conn.commit()
        return result
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, OverflowError):
        conn.rollback()
        raise ControlError("persistencia_controle_indisponivel", 503) from None
    finally:
        conn.close()


def _fresh(ack):
    if not ack:
        return False
    age = (now() - datetime.fromisoformat(ack["at"])).total_seconds()
    return 0 <= age <= 45


def settings_response(state):
    ack = state.get("settings_ack") or {}
    matches = (_fresh(ack) and ack.get("revision") == state["settings_revision"]
               and ack.get("config_id") == digest(state["config"]))
    return {**copy.deepcopy(state["config"]), "revision": state["settings_revision"],
            "config_id": digest(state["config"]), "persistencia": "sqlite",
            "aplicada_no_robo": matches, "pending": not matches,
            "aplicada_revision": ack.get("revision"), "aplicada_config_id": ack.get("config_id"),
            "aplicada_em": ack.get("at"),
            "effective_config": copy.deepcopy(ack.get("config")) if _fresh(ack) else None}


def control_response(state):
    desired, ack = state["control"], state.get("ack") or {}
    fresh = _fresh(ack)
    matches = (fresh and ack.get("revision") == desired["revision"]
               and ack.get("command_id") == desired["command_id"]
               and ack.get("paused") == desired["desired_paused"])
    expires = desired.get("expires_at")
    expired = bool(expires and now() > datetime.fromisoformat(expires) and not matches)
    return {"schema_version": 1, "conta": "PRACTICE", **copy.deepcopy(desired),
            "confirmed": matches, "pending": not matches, "robot_online": fresh,
            "authorized": bool(fresh and ack.get("authorized")),
            "paused": ack.get("paused") if matches else None,
            "last_paused": ack.get("paused") if fresh else None,
            "applied_revision": ack.get("revision") if fresh else None,
            "lease_expires_at": (now() + timedelta(seconds=30)).isoformat(),
            "settings_revision": state["settings_revision"],
            "settings_config_id": digest(state["config"]),
            "error": "comando_expirado" if expired else (None if fresh else "robo_sem_confirmacao_recente")}


def get_settings(default):
    return _transaction(default, lambda conn, state: settings_response(state))


def get_control(default):
    return _transaction(default, lambda conn, state: control_response(state))


def _command(conn, payload, revision):
    command_id = payload.get("command_id")
    try:
        if not isinstance(command_id, str) or str(UUID(command_id)) != command_id:
            raise ValueError()
        expires = datetime.fromisoformat(payload.get("expires_at", ""))
        if expires.tzinfo is None or not 0 < (expires - now()).total_seconds() <= 120:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ControlError("comando_invalido_ou_expirado") from None
    if type(payload.get("expected_revision")) is not int or payload["expected_revision"] < 1:
        raise ControlError("revisao_invalida")
    if conn.execute("SELECT 1 FROM commands WHERE id=?", (command_id,)).fetchone():
        raise ControlError("comando_repetido", 409)
    if payload["expected_revision"] != revision:
        raise ControlError("revisao_conflitante", 409)
    conn.execute("INSERT INTO commands VALUES (?, ?)", (command_id, now().isoformat()))
    return expires.isoformat()


def put_settings(default, payload):
    def update(conn, state):
        _command(conn, payload, state["settings_revision"])
        if payload.get("expected_config_id") != digest(state["config"]):
            raise ControlError("configuracao_alterada", 409)
        config = {key: copy.deepcopy(payload[key]) for key in ("schema_version", "conta", "enabled", "faixas")}
        if config != state["config"]:
            state["config"] = config
            state["settings_revision"] += 1
        return {"salvo": True, **settings_response(state)}
    return _transaction(default, update)


def put_control(default, payload):
    def update(conn, state):
        expires = _command(conn, payload, state["control"]["revision"])
        action = payload["action"]
        current = control_response(state)
        if action == "resume_new_entries" and not (current["robot_online"] and current["authorized"]):
            raise ControlError("robo_nao_autorizado_ou_sem_confirmacao", 409)
        state["control"] = {"revision": state["control"]["revision"] + 1,
                            "command_id": payload["command_id"],
                            "desired_paused": action == "pause_new_entries", "expires_at": expires,
                            "target_session": state["ack"]["robot_session"] if action == "resume_new_entries" else None}
        return control_response(state)
    return _transaction(default, update)


def acknowledge(default, data):
    """Called only after authenticated, sequence-checked telemetry was accepted."""
    def update(conn, state):
        at = data["atualizado_em"]
        session, sequence = data["robot_session"], data["sequence"]
        sessions = state.setdefault("sessions", {})
        if sequence <= sessions.get(session, 0):
            raise ControlError("telemetria_fora_de_ordem", 409)
        previous_session = state.get("robot_session")
        previous_at = state.get("telemetry_at")
        if previous_at and datetime.fromisoformat(at) < datetime.fromisoformat(previous_at):
            raise ControlError("telemetria_fora_de_ordem", 409)
        if previous_session and previous_session != session:
            state["ack"] = None
            state["settings_ack"] = None
            if not state["control"]["desired_paused"]:
                state["control"] = {"revision": state["control"]["revision"] + 1,
                                    "command_id": str(uuid4()), "desired_paused": True,
                                    "expires_at": None, "target_session": None}
        sessions[session] = sequence
        state["robot_session"], state["telemetry_at"] = session, at
        ack = data.get("control_ack")
        if ack is None or data.get("control_error"):
            state["ack"] = None
        if ack is not None and not data.get("control_error"):
            prior = state.get("ack") or {}
            state["ack"] = None
            desired = state["control"]
            valid = (ack["revision"] == desired["revision"] and ack["command_id"] == desired["command_id"])
            expires = desired.get("expires_at")
            already = prior.get("revision") == desired["revision"] and prior.get("command_id") == desired["command_id"]
            if valid and (not expires or datetime.fromisoformat(at) <= datetime.fromisoformat(expires) or already):
                # Refused resume may acknowledge paused=true; this never confirms desired resume.
                state["ack"] = {**ack, "at": at, "robot_session": data["robot_session"]}
        rev, cid = data.get("config_revision"), data.get("config_id")
        previous_settings = state.get("settings_ack") or {}
        if rev != previous_settings.get("revision") or cid != previous_settings.get("config_id") or rev is None or cid is None:
            state["settings_ack"] = None
        if rev == state["settings_revision"] and cid == digest(state["config"]):
            state["settings_ack"] = {"revision": rev, "config_id": cid, "at": at,
                                     "config": copy.deepcopy(state["config"])}
        elif state.get("settings_ack"):
            state["settings_ack"]["at"] = at
    return _transaction(default, update)
