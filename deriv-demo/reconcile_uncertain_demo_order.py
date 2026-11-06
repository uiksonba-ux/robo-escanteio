"""Read-only reconciliation of the uncertain Deriv DEMO purchase from 2026-11-06 02:02 UTC.

This utility never sends a buy/sell request and never prints credentials or raw API payloads.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import websocket
from deriv_client import DerivOptionsClient, DEMO_WS_PATH

TARGET_FROM = int(datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc).timestamp())
TARGET_TO = int(datetime(2026, 11, 6, 2, 5, tzinfo=timezone.utc).timestamp())
WS_TIMEOUT = 12


def safe_call(ws, request_id, payload):
    body = dict(payload)
    body["req_id"] = request_id
    ws.send(json.dumps(body, separators=(",", ":")))
    while True:
        response = json.loads(ws.recv())
        if not isinstance(response, dict) or response.get("req_id") != request_id:
            continue
        if isinstance(response.get("error"), dict):
            code = response["error"].get("code")
            raise RuntimeError(code if isinstance(code, str) else "API_ERROR")
        return response


def is_r25(row):
    text = " ".join(str(row.get(key) or "") for key in ("shortcode", "longcode"))
    return bool(
        re.search(r"\\bR_?25\\b", text, re.I)
        or re.search(r"Volatility\\s+25\\s+Index", text, re.I)
    )


def main():
    app_id = os.environ.get("DERIV_APP_ID", "").strip()
    token = os.environ.get("DERIV_TOKEN", "").strip()
    account_id = os.environ.get("DERIV_DEMO_ACCOUNT_ID", "").strip()
    if not app_id or not token or not account_id:
        raise RuntimeError("MISSING_REQUIRED_SECRET")

    client = DerivOptionsClient(app_id, token, account_id)
    client._validate_demo_account()
    url = client._new_otp_url()
    parsed = urlparse(url)
    if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com" or parsed.path.rstrip("/") != DEMO_WS_PATH:
        raise RuntimeError("DEMO_ENDPOINT_VALIDATION_FAILED")

    ws = websocket.create_connection(url, timeout=WS_TIMEOUT, enable_multithread=True)
    ws.settimeout(WS_TIMEOUT)
    try:
        statement = safe_call(ws, 1, {
            "statement": 1,
            "action_type": "buy",
            "date_from": TARGET_FROM,
            "date_to": TARGET_TO,
            "description": 1,
            "limit": 999,
        }).get("statement", {})
        transactions = statement.get("transactions") if isinstance(statement, dict) else None
        if not isinstance(transactions, list):
            raise RuntimeError("INVALID_STATEMENT_RESPONSE")

        target = [
            row for row in transactions
            if isinstance(row, dict)
            and row.get("action_type") == "buy"
            and type(row.get("transaction_time")) is int
            and TARGET_FROM <= row["transaction_time"] <= TARGET_TO
            and is_r25(row)
        ]

        reviewed = []
        for index, row in enumerate(target, start=2):
            contract_id = row.get("contract_id")
            contract_summary = {"status": "not_checked", "profit": None}
            if type(contract_id) is int and contract_id > 0:
                response = safe_call(ws, index, {
                    "proposal_open_contract": 1,
                    "contract_id": contract_id,
                }).get("proposal_open_contract", {})
                if isinstance(response, dict):
                    status = response.get("status")
                    profit = response.get("profit")
                    if status in ("won", "lost", "sold", "cancelled") and isinstance(profit, (int, float)) and not isinstance(profit, bool):
                        contract_summary = {"status": status, "profit": round(float(profit), 2)}
                    else:
                        contract_summary = {"status": status if isinstance(status, str) else "unknown", "profit": None}
            reviewed.append({
                "transaction_time_utc": datetime.fromtimestamp(row["transaction_time"], timezone.utc).isoformat(),
                "contract_id": contract_id if type(contract_id) is int else None,
                "stake": row.get("amount") if isinstance(row.get("amount"), (int, float)) else None,
                "contract": row.get("shortcode") if isinstance(row.get("shortcode"), str) else None,
                **contract_summary,
            })

        result = {
            "read_only": True,
            "target": "R_25",
            "window_utc": [
                datetime.fromtimestamp(TARGET_FROM, timezone.utc).isoformat(),
                datetime.fromtimestamp(TARGET_TO, timezone.utc).isoformat(),
            ],
            "matching_transactions": len(reviewed),
            "matches": reviewed,
            "conclusion": (
                "UNIQUE_SETTLED" if len(reviewed) == 1 and reviewed[0]["status"] in ("won", "lost", "sold", "cancelled")
                else "NO_MATCH" if len(reviewed) == 0
                else "AMBIGUOUS_OR_UNSETTLED"
            ),
        }
        print(json.dumps(result, separators=(",", ":")))
        if result["conclusion"] != "UNIQUE_SETTLED":
            raise RuntimeError("RECONCILIATION_REQUIRES_REVIEW")
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe = str(exc)
        if not re.fullmatch(r"[A-Z0-9_]+", safe):
            safe = "UNEXPECTED_RECONCILIATION_ERROR"
        print("DEMO RECONCILIATION FAILED: " + safe, file=sys.stderr)
        sys.exit(1)
