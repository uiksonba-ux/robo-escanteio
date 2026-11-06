"""Read-only reconciliation of the uncertain Deriv DEMO purchase from 2026-11-06 02:02 UTC.

This utility never sends a buy/sell request and never prints credentials or raw API payloads.
"""
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import websocket
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deriv_client import DerivOptionsClient, DEMO_WS_PATH
from discover_demo_account import discover_demo_account

TARGET_FROM = int(datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc).timestamp())
TARGET_TO = int(datetime(2026, 11, 6, 2, 5, tzinfo=timezone.utc).timestamp())
TARGET_ORDER_TIME = int(datetime(2026, 11, 6, 2, 2, 8, tzinfo=timezone.utc).timestamp())
TARGET_STAKE = 0.37
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
        re.search(r"(?:^|_)R_?25(?:_|$)", text, re.I)
        or re.search(r"Volatility\\s+25\\s+Index", text, re.I)
    )


def main():
    app_id = os.environ.get("DERIV_APP_ID", "").strip()
    token = os.environ.get("DERIV_TOKEN", "").strip()
    if not app_id or not token:
        raise RuntimeError("MISSING_REQUIRED_SECRET")

    account = discover_demo_account(app_id, token)
    client = DerivOptionsClient(app_id, token, account["account_id"])
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

        # Match the exact order logged by Render: R_25 CALL, sent at
        # 02:02:08 UTC for 0.37 USD. Statement descriptions can use a display
        # name rather than the internal symbol, so correlate by time and stake.
        # Match only the asset and stake from Render's uncertain R_25 CALL.
        target = [
            row for row in transactions
            if isinstance(row, dict)
            and row.get("action_type") == "buy"
            and type(row.get("transaction_time")) is int
            and abs(row["transaction_time"] - TARGET_ORDER_TIME) <= 120
            and is_r25(row)
            and isinstance(row.get("amount"), (int, float))
            and abs(abs(float(row["amount"])) - TARGET_STAKE) < 0.005
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
                "status": contract_summary["status"],
                "settled": contract_summary["status"] in ("won", "lost", "sold", "cancelled"),
            })

        result = {
            "read_only": True,
            "target": "R_25 CALL, 0.37 USD",
            "correlation_window_seconds": 120,
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
