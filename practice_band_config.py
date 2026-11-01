"""Authenticated, fail-closed configuration reader for the isolated PRACTICE monitor.

Only enabled by explicit environment configuration. Never writes to the monitor,
IQ Option, or Telegram. Returns None on any failure.
"""
import hmac
import json
import math
from urllib.parse import urlparse
from urllib.request import Request, urlopen

EXPECTED_BOUNDS = ((0, 49), (50, 59), (60, 69), (70, 79), (80, 100))
ALLOWED_HOST = "baskaraido-monitor-test.onrender.com"

def validate_config(data):
    if not isinstance(data, dict):
        return None
    if (data.get("schema_version") != 1 or data.get("conta") != "PRACTICE"
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
                or not math.isfinite(row["mult"])
                or not 1.0 <= row["mult"] <= 10.0):
            return None
        parsed.append((float(lower), float(upper + 1), row["gales"], float(row["mult"])))
    return (data["enabled"], tuple(parsed))

def read_remote_config(url, token):
    if not url or not token:
        return None
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/api/v1/practice/faixas"):
        return None
    try:
        req = Request(url, headers={"Authorization": "Bearer " + token}, method="GET")
        with urlopen(req, timeout=3) as response:
            if response.status != 200:
                return None
            body = response.read(4097)
        if len(body) > 4096:
            return None
        return validate_config(json.loads(body))
    except (ValueError, OSError, TimeoutError, TypeError, KeyError, json.JSONDecodeError):
        return None

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
