"""Deriv DEMO connectivity preflight. Read-only: never places orders.

Environment variables (configure privately, not in Git):
  DERIV_APP_ID, DERIV_TOKEN, DERIV_DEMO_ACCOUNT_ID
Requires: requests, websocket-client (already in parent requirements.txt).
"""
import json
import os
import sys
import urllib.parse

import requests
import websocket

BASE = "https://api.derivws.com"
TIMEOUT = 10


def demo_account_id(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("DERIV_DEMO_ACCOUNT_ID ausente")
    # Demo account prefixes may change. Do not infer account mode from ID alone.
    return value


def fetch_demo_ws_url(app_id, token, account_id):
    """Request a trading WebSocket OTP; reject any non-demo endpoint."""
    url = BASE + "/trading/v1/options/accounts/" + urllib.parse.quote(account_id, safe="") + "/otp"
    response = requests.post(
        url,
        headers={"Deriv-App-ID": app_id, "Authorization": "Bearer " + token},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    # Response layouts can vary. Only accept an explicit demo URL.
    candidates = [payload]
    if isinstance(payload, dict):
        candidates.extend(v for v in payload.values() if isinstance(v, dict))
    for item in candidates:
        for key in ("url", "ws_url", "websocket_url"):
            endpoint = item.get(key) if isinstance(item, dict) else None
            if not isinstance(endpoint, str):
                continue
            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme != "wss" or parsed.hostname not in ("api.derivws.com", "ws.derivws.com"):
                continue
            if not parsed.path.rstrip("/").endswith("/demo"):
                continue
            return endpoint
    raise RuntimeError("OTP não forneceu endpoint DEMO reconhecido; conexão bloqueada")


def query_catalog(ws_url):
    ws = websocket.create_connection(ws_url, timeout=TIMEOUT)
    try:
        ws.send(json.dumps({"active_symbols": "brief", "req_id": 1}))
        for _ in range(5):
            response = json.loads(ws.recv())
            if response.get("req_id") != 1:
                continue
            if response.get("error"):
                raise RuntimeError("API recusou consulta active_symbols")
            if response.get("msg_type") != "active_symbols":
                raise RuntimeError("Resposta inesperada na consulta de símbolos")
            symbols = response.get("active_symbols")
            if not isinstance(symbols, list):
                raise RuntimeError("Catálogo de símbolos inválido")
            return [
                {"symbol": item.get("symbol"), "display_name": item.get("display_name")}
                for item in symbols if isinstance(item, dict)
            ]
        raise TimeoutError("Sem resposta correlacionada de active_symbols")
    finally:
        ws.close()


def main():
    app_id = os.environ.get("DERIV_APP_ID", "").strip()
    token = os.environ.get("DERIV_TOKEN", "").strip()
    account = demo_account_id(os.environ.get("DERIV_DEMO_ACCOUNT_ID"))
    if not app_id or not token:
        raise ValueError("Configure DERIV_APP_ID e DERIV_TOKEN no ambiente privado")
    ws_url = fetch_demo_ws_url(app_id, token, account)
    catalog = query_catalog(ws_url)
    print(json.dumps({
        "mode": "DEMO", "read_only": True,
        "symbols_count": len(catalog),
        "sample": catalog[:8],
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print token, OTP, URL, headers or full API response.
        print("DERIV DEMO PRECHECK FAILED: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
