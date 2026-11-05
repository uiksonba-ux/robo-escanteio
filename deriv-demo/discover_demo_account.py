"""Identify an active Deriv Options DEMO account using the official REST API.

This module NEVER places trades, changes Render settings, or prints credentials.
It deliberately fails closed when the account selection is ambiguous.
"""
import json
import os
import sys
import requests

ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
TIMEOUT_SECONDS = 12


def select_demo_account(payload, configured_id=""):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Resposta de contas inválida")
    accounts = payload["data"]
    demos = [
        item for item in accounts
        if isinstance(item, dict)
        and item.get("account_type") == "demo"
        and item.get("status") == "active"
        and isinstance(item.get("account_id"), str)
        and item["account_id"].strip()
    ]
    if configured_id:
        matches = [item for item in demos if item["account_id"] == configured_id]
        if len(matches) != 1:
            raise ValueError("DERIV_DEMO_ACCOUNT_ID não identifica uma única conta DEMO ativa")
        return matches[0]
    if len(demos) != 1:
        raise ValueError("Esperada uma única conta Options DEMO ativa; escolha o ID manualmente")
    return demos[0]


def discover_demo_account(app_id, token, configured_id=""):
    if not app_id or not token:
        raise ValueError("DERIV_APP_ID e DERIV_TOKEN obrigatórios")
    response = requests.get(
        ACCOUNTS_URL,
        headers={"Deriv-App-ID": app_id, "Authorization": "Bearer " + token},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return select_demo_account(response.json(), configured_id)


def main():
    account = discover_demo_account(
        os.environ.get("DERIV_APP_ID", "").strip(),
        os.environ.get("DERIV_TOKEN", "").strip(),
        os.environ.get("DERIV_DEMO_ACCOUNT_ID", "").strip(),
    )
    # Account ID is not an authentication secret; never output tokens or headers.
    print(json.dumps({
        "status": "DEMO_ACCOUNT_CONFIRMED",
        "account_id": account["account_id"],
        "account_type": "demo",
        "currency": account.get("currency"),
        "read_only": True,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("DEMO DISCOVERY FAILED: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
