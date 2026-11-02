"""Authenticated, fail-closed configuration reader for the isolated PRACTICE monitor.

Only enabled by explicit environment configuration. The only write is authenticated
PRACTICE telemetry to the isolated monitor; no IQ Option or Telegram imports.
"""
from dataclasses import dataclass
import hashlib
import json
import math
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener


class _NoRedirect(HTTPRedirectHandler):
    """Never forward a bearer credential to a redirect destination."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = build_opener(_NoRedirect()).open

EXPECTED_BOUNDS = ((0, 49), (50, 59), (60, 69), (70, 79), (80, 100))
ALLOWED_HOST = "baskaraido-monitor-test.onrender.com"


@dataclass(frozen=True)
class RemoteConfigSnapshot:
    config: tuple
    revision: int | None = None
    config_id: str | None = None


def _allowed_config_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc == ALLOWED_HOST
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path == "/api/v1/practice/faixas")

def validate_config(data):
    if not isinstance(data, dict):
        return None
    if (type(data.get("schema_version")) is not int or data.get("schema_version") != 1 or data.get("conta") != "PRACTICE"
            or type(data.get("enabled")) is not bool
            or data.get("aplicada_no_robo") is not False
            or data.get("persistencia") != "memoria_temporaria"):
        return None
    rows = data.get("faixas")
    if not isinstance(rows, list) or len(rows) != 5:
        return None
    parsed = []
    for row, (lower, upper) in zip(rows, EXPECTED_BOUNDS):
        if not isinstance(row, dict) or set(row) != {"min", "max", "gales", "mult"}:
            return None
        if (type(row["min"]) is not int or type(row["max"]) is not int
                or (row["min"], row["max"]) != (lower, upper)
                or type(row["gales"]) is not int or not 0 <= row["gales"] <= 9
                or type(row["mult"]) not in (float, int)
                or not 1.0 <= row["mult"] <= 10.0
                or not math.isfinite(row["mult"])):
            return None
        parsed.append((float(lower), float(upper + 1), row["gales"], float(row["mult"])))
    return (data["enabled"], tuple(parsed))

def read_remote_snapshot(url, token):
    if not token or not _allowed_config_url(url):
        return None
    try:
        req = Request(url, headers={"Authorization": "Bearer " + token}, method="GET")
        with urlopen(req, timeout=3) as response:
            if response.status != 200:
                return None
            body = response.read(4097)
        if len(body) > 4096:
            return None
        data = json.loads(body)
        if not isinstance(data, dict) or type(data.get("aplicada_no_robo")) is not bool:
            return None
        config = validate_config({**data, "aplicada_no_robo": False})
        if config is None:
            return None
        revision, config_id = data.get("revision"), data.get("config_id")
        if revision is not None or config_id is not None:
            if type(revision) is not int or revision < 1 or not isinstance(config_id, str):
                return None
            core = {key: data[key] for key in ("schema_version", "conta", "enabled", "faixas")}
            digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"),
                                               allow_nan=False).encode()).hexdigest()
            if config_id != digest:
                return None
        return RemoteConfigSnapshot(config, revision, config_id)
    except (ValueError, OSError, TimeoutError, TypeError, KeyError, json.JSONDecodeError):
        return None


def read_remote_config(url, token):
    """Compatibility API for consumers that only need immutable band values."""
    snapshot = read_remote_snapshot(url, token)
    return snapshot.config if snapshot else None


def publish_telemetry(config_url, token, payload):
    """Bounded, credential-safe publication; disabled without explicit config."""
    if not token or not _allowed_config_url(config_url):
        return False
    if not isinstance(payload, dict) or payload.get("conta") != "PRACTICE":
        return False
    try:
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > 65536:
            return False
        req = Request("https://" + ALLOWED_HOST + "/api/v1/practice/telemetry",
                      data=body, headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"}, method="PUT")
        with urlopen(req, timeout=3) as response:
            return response.status in (200, 202)
    except (ValueError, OSError, TimeoutError, TypeError):
        return False

def select_band(wins, losses, config, default):
    if config is None:
        return default
    enabled, bands = config
    if not enabled:
        return default
    total = max(0, wins) + max(0, losses)
    pct = 0.0 if not total else 100.0 * max(0, wins) / total
    for low, high, gales, multiplier in bands:
        if low <= pct < high:
            return (gales, multiplier)
    return default

