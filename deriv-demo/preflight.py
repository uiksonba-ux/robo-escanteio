"""Read-only Deriv DEMO preflight. Never places trades or prints secrets."""
import json
import os
import sys
import urllib.parse

import requests
import websocket

BASE = "https://api.derivws.com"
TIMEOUT = 10


def fetch_demo_ws_url(app_id, token, account_id):
    if not all((app_id, token, account_id)):
        raise ValueError("DERIV_APP_ID, DERIV_TOKEN e DERIV_DEMO_ACCOUNT_ID obrigatórios")
    url = BASE + "/trading/v1/options/accounts/" + urllib.parse.quote(account_id, safe="") + "/otp"
    response = requests.post(url, headers={
        "Deriv-App-ID": app_id, "Authorization": "Bearer " + token,
    }, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    endpoint = payload.get("data", {}).get("url")
    if not isinstance(endpoint, str):
        raise RuntimeError("URL OTP ausente")
    from trading_policy import validate_demo_websocket_url
    validate_demo_websocket_url(endpoint)
    return endpoint


def query_catalog(ws_url):
    from trading_policy import validate_demo_websocket_url
    validate_demo_websocket_url(ws_url)
    ws = websocket.create_connection(ws_url, timeout=TIMEOUT)
    try:
        ws.send(json.dumps({"active_symbols": "brief", "req_id": 1}))
        for _ in range(5):
            response = json.loads(ws.recv())
            if response.get("req_id") != 1:
                continue
            if response.get("error"):
                raise RuntimeError("API recusou active_symbols")
            if response.get("msg_type") != "active_symbols":
                raise RuntimeError("Resposta inesperada")
            symbols = response.get("active_symbols")
            if not isinstance(symbols, list):
                raise RuntimeError("Catálogo inválido")
            return [
                {
                    "symbol": item.get("underlying_symbol"),
                    "display_name": item.get("underlying_symbol_name"),
                    "market": item.get("market"),
                    "open": item.get("exchange_is_open"),
                    "suspended": item.get("is_trading_suspended"),
                }
                for item in symbols if isinstance(item, dict)
            ]
        raise TimeoutError("Sem resposta de active_symbols")
    finally:
        ws.close()


def main():
    ws_url = fetch_demo_ws_url(
        os.environ.get("DERIV_APP_ID", "").strip(),
        os.environ.get("DERIV_TOKEN", "").strip(),
        os.environ.get("DERIV_DEMO_ACCOUNT_ID", "").strip(),
    )
    symbols = query_catalog(ws_url)
    eligible = [s for s in symbols if s["market"] != "forex" and s["open"] == 1 and s["suspended"] == 0]
    print(json.dumps({"mode": "DEMO", "read_only": True, "symbols_count": len(symbols),
                      "eligible_non_forex": len(eligible), "sample": eligible[:8]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("DERIV DEMO PRECHECK FAILED: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
