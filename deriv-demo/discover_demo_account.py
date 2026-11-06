"""Identify an active Deriv Options DEMO account using the official REST API.

This module NEVER places trades, changes Render settings, or prints credentials.
It deliberately fails closed when the account selection is ambiguous.
"""
import json
import os
import re
import sys
import requests

ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
TIMEOUT_SECONDS = 12
ACCOUNT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class DiscoveryError(ValueError):
    """A safe, fixed diagnostic that never includes upstream data or secrets."""


def select_demo_account(payload, configured_id=""):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DiscoveryError("Resposta de contas inválida; nenhuma conta selecionada")
    accounts = payload["data"]
    if payload.get("error") or payload.get("errors"):
        raise DiscoveryError("API retornou erro; nenhuma conta selecionada")
    links = payload.get("links", {})
    meta = payload.get("meta", {})
    if not isinstance(links, dict) or not isinstance(meta, dict):
        raise DiscoveryError("Metadados de contas inválidos")
    total = meta.get("total", len(accounts))
    if links.get("next") or type(total) is not int or total != len(accounts):
        raise DiscoveryError("Lista de contas incompleta; nenhuma conta selecionada")
    demos = []
    seen_ids = set()
    for item in accounts:
        if not isinstance(item, dict) or item.get("account_type") not in ("demo", "real"):
            raise DiscoveryError("Tipo de conta ausente ou desconhecido; nenhuma conta selecionada")
        account_id = item.get("account_id")
        if not isinstance(account_id, str) or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise DiscoveryError("Identificador de conta inválido; nenhuma conta selecionada")
        if account_id in seen_ids:
            raise DiscoveryError("Identificadores duplicados; nenhuma conta selecionada")
        seen_ids.add(account_id)
        # Count ALL DEMOs, including inactive ones. An explicit ID cannot bypass this.
        if item["account_type"] == "demo":
            demos.append(item)
    if len(demos) != 1:
        raise DiscoveryError("Esperada exatamente uma conta Options DEMO; nenhuma conta selecionada")
    account = demos[0]
    currency = account.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise DiscoveryError("Moeda da conta DEMO ausente ou inválida")
    if account.get("status") != "active":
        raise DiscoveryError("A única conta DEMO não está explicitamente ativa")
    if configured_id and account["account_id"] != configured_id:
        raise DiscoveryError("DERIV_DEMO_ACCOUNT_ID diverge da única conta DEMO ativa")
    return account


def discover_demo_account(app_id, token, configured_id=""):
    if not app_id or not token:
        raise DiscoveryError("Configure os Secrets DERIV_APP_ID e DERIV_TOKEN em Settings > Secrets and variables > Actions")
    try:
        response = requests.get(
            ACCOUNTS_URL,
            headers={"Deriv-App-ID": app_id, "Authorization": "Bearer " + token},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise DiscoveryError("Falha de rede ao consultar contas; nenhuma conta selecionada") from None
    if response.status_code in (401, 403):
        raise DiscoveryError("Autenticação recusada; verifique DERIV_APP_ID e DERIV_TOKEN nos Secrets")
    if response.status_code != 200:
        raise DiscoveryError("Resposta HTTP inesperada ou redirecionamento bloqueado")
    try:
        payload = response.json()
    except ValueError:
        raise DiscoveryError("Resposta JSON inválida; nenhuma conta selecionada") from None
    account = select_demo_account(payload, configured_id)
    if app_id in account["account_id"] or token in account["account_id"]:
        raise DiscoveryError("Identificador de conta inseguro; nenhuma conta selecionada")
    return account


def main():
    try:
        account = discover_demo_account(
            os.environ.get("DERIV_APP_ID", "").strip(),
            os.environ.get("DERIV_TOKEN", "").strip(),
            os.environ.get("DERIV_DEMO_ACCOUNT_ID", "").strip(),
        )
        # Only validated fields may leave the process; never dump API responses.
        result = {
            "status": "DEMO_ACCOUNT_CONFIRMED",
            "account_id": account["account_id"],
            "account_type": "demo",
            "account_status": "active",
            "account_currency": account["currency"],
            "read_only": True,
        }
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write(
                    "Conta Options DEMO confirmada (somente leitura).\n\n"
                    f"`DERIV_DEMO_ACCOUNT_ID={account['account_id']}`\n\n"
                    f"account_type=demo; status=active; currency={account['currency']}. Nenhuma ordem enviada.\n"
                )
        print(json.dumps(result))
        return 0
    except DiscoveryError as exc:
        print("DEMO DISCOVERY FAILED: " + str(exc), file=sys.stderr)
    except Exception:
        # Exception messages can contain credentials or untrusted response bodies.
        print("DEMO DISCOVERY FAILED: erro inesperado; nenhuma conta selecionada", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
