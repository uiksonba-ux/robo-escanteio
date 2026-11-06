"""Offline safety tests for read-only Options DEMO account discovery."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import discover_demo_account as discovery


def account(account_id="DOT200", account_type="demo", status="active", currency="USD"):
    return {"account_id": account_id, "account_type": account_type, "status": status, "currency": currency}


def payload(*accounts):
    return {"data": list(accounts)}


class TestSelectDemoAccount(unittest.TestCase):
    def test_selects_the_only_active_demo_alongside_real_accounts(self):
        demo = account()
        result = discovery.select_demo_account(payload(account("CR100", "real"), demo))
        self.assertEqual(result["account_id"], "DOT200")
        self.assertEqual(result["account_type"], "demo")
        self.assertEqual(result["status"], "active")

    def test_a_configured_id_can_only_confirm_the_unique_demo(self):
        data = payload(account("CR100", "real"), account())
        self.assertEqual(discovery.select_demo_account(data, "DOT200")["account_id"], "DOT200")
        for configured_id in ("CR100", "DOT201"):
            with self.subTest(configured_id=configured_id), self.assertRaises(ValueError):
                discovery.select_demo_account(data, configured_id)

    def test_multiple_demos_are_rejected_even_with_a_configured_id(self):
        for second_status in ("active", "inactive", "disabled", None):
            data = payload(account(), account("DOT201", status=second_status))
            for configured_id in ("", "DOT200", "DOT201"):
                with self.subTest(status=second_status, configured_id=configured_id):
                    with self.assertRaises(ValueError):
                        discovery.select_demo_account(data, configured_id)

    def test_real_only_empty_and_inactive_demo_are_rejected(self):
        for data in (payload(), payload(account("CR100", "real")),
                     payload(account(status="inactive")), payload(account(status=None)),
                     payload(account(status="ACTIVE"))):
            with self.subTest(data=data), self.assertRaises(ValueError):
                discovery.select_demo_account(data)

    def test_missing_or_invalid_demo_currency_is_rejected(self):
        for currency in (None, "", "usd", "US", "USDX", 1, True):
            item = account(currency=currency)
            with self.subTest(currency=currency), self.assertRaises(ValueError):
                discovery.select_demo_account(payload(item))
        item = account()
        del item["currency"]
        with self.assertRaises(ValueError):
            discovery.select_demo_account(payload(item))

    def test_missing_demo_status_is_rejected(self):
        demo = account()
        del demo["status"]
        with self.assertRaises(ValueError):
            discovery.select_demo_account(payload(demo))

    def test_malformed_response_or_any_ambiguous_item_is_rejected(self):
        cases = [None, [], {}, {"data": None}, {"data": {}}, {"data": "invalid"}]
        cases.extend(payload(account(), item) for item in (None, "invalid", [], 123, True))
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                discovery.select_demo_account(data)

    def test_unknown_or_missing_type_cannot_be_ignored_alongside_a_demo(self):
        for account_type in (None, "", "DEMO", "virtual", "unknown", 1, False):
            data = payload(account(), account("OTHER201", account_type))
            with self.subTest(account_type=account_type), self.assertRaises(ValueError):
                discovery.select_demo_account(data)
        other = account("OTHER201", "real")
        del other["account_type"]
        with self.assertRaises(ValueError):
            discovery.select_demo_account(payload(account(), other))

    def test_invalid_ids_on_either_demo_or_real_accounts_are_rejected(self):
        invalid_ids = (None, "", " ", " DOT201", "DOT201 ", "DOT/201", "DOT\\201",
                       "DOT\n201", "DOT201\n", "::warning::ID", "<script>",
                       "DÓT201", "-DOT201", "_DOT201", "D" * 129, 123, True)
        for account_type in ("demo", "real"):
            for invalid_id in invalid_ids:
                data = payload(account(invalid_id, account_type))
                if account_type == "real":
                    data["data"].append(account())
                with self.subTest(account_type=account_type, invalid_id=invalid_id):
                    with self.assertRaises(ValueError):
                        discovery.select_demo_account(data)
        for account_type in ("demo", "real"):
            item = account("OTHER201", account_type)
            del item["account_id"]
            with self.subTest(missing_id_type=account_type), self.assertRaises(ValueError):
                discovery.select_demo_account(payload(account(), item))

    def test_valid_id_boundaries(self):
        for account_id in ("A", "0", "DOT_200-valid", "D" * 128):
            with self.subTest(account_id=account_id):
                self.assertEqual(discovery.select_demo_account(payload(account(account_id)))["account_id"], account_id)

    def test_duplicate_ids_are_rejected_even_for_real_accounts(self):
        cases = [payload(account(), account()),
                 payload(account(), account("DOT200", "real")),
                 payload(account(), account("CR100", "real"), account("CR100", "real"))]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                discovery.select_demo_account(data)

    def test_api_error_payloads_are_rejected_even_if_data_looks_valid(self):
        for key, value in (("error", {"message": "upstream-sensitive-details"}),
                           ("errors", [{"message": "upstream-sensitive-details"}])):
            data = payload(account())
            data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                discovery.select_demo_account(data)

    def test_partial_paginated_responses_cannot_prove_uniqueness(self):
        for metadata in ({"links": {"next": "https://example.invalid/page/2"}},
                         {"meta": {"total": 2}}):
            data = payload(account())
            data.update(metadata)
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                discovery.select_demo_account(data)


class TestReadOnlyRequest(unittest.TestCase):
    app_id = "1048123"
    token = "private_test_token_do_not_print"

    def response(self, data=None, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload(account()) if data is None else data
        return response

    @patch.object(discovery.requests, "get")
    def test_uses_one_fixed_get_with_redirects_disabled(self, get):
        get.return_value = self.response()
        with patch.object(discovery.requests, "post") as post, \
             patch.object(discovery.requests, "put") as put, \
             patch.object(discovery.requests, "delete") as delete:
            self.assertEqual(discovery.discover_demo_account(self.app_id, self.token)["account_id"], "DOT200")
        get.assert_called_once_with(
            "https://api.derivws.com/trading/v1/options/accounts",
            headers={"Deriv-App-ID": self.app_id, "Authorization": "Bearer " + self.token},
            timeout=12, allow_redirects=False,
        )
        post.assert_not_called()
        put.assert_not_called()
        delete.assert_not_called()

    @patch.object(discovery.requests, "get")
    def test_missing_credentials_fail_before_network_access(self, get):
        for app_id, token in (("", self.token), (self.app_id, ""), ("", "")):
            with self.subTest(app_id_present=bool(app_id), token_present=bool(token)):
                with self.assertRaises(ValueError):
                    discovery.discover_demo_account(app_id, token)
        get.assert_not_called()

    @patch.object(discovery.requests, "get")
    def test_every_non_200_status_is_rejected_without_parsing_response(self, get):
        for status in (201, 204, 301, 302, 307, 308, 400, 401, 403, 404, 429, 500):
            response = self.response(status=status)
            get.return_value = response
            with self.subTest(status=status), self.assertRaises(ValueError):
                discovery.discover_demo_account(self.app_id, self.token)
            response.json.assert_not_called()

    @patch.object(discovery.requests, "get")
    def test_transport_and_json_errors_are_sanitized(self, get):
        for error in (requests.Timeout(self.token), requests.ConnectionError(self.token),
                      requests.RequestException(self.token)):
            get.side_effect = error
            with self.subTest(error_type=type(error).__name__):
                with self.assertRaises(ValueError) as caught:
                    discovery.discover_demo_account(self.app_id, self.token)
                self.assertNotIn(self.token, str(caught.exception))
        get.side_effect = None
        response = self.response()
        response.json.side_effect = ValueError(self.token)
        get.return_value = response
        with self.assertRaises(ValueError) as caught:
            discovery.discover_demo_account(self.app_id, self.token)
        self.assertNotIn(self.token, str(caught.exception))

    @patch.object(discovery.requests, "get")
    def test_account_id_cannot_reflect_either_credential(self, get):
        for credential in (self.app_id, self.token):
            get.return_value = self.response(payload(account(credential)))
            with self.subTest(credential_kind="app" if credential == self.app_id else "token"):
                with self.assertRaises(ValueError):
                    discovery.discover_demo_account(self.app_id, self.token)


class TestCommandLine(unittest.TestCase):
    app_id = "1048123"
    token = "private_test_token_do_not_print"

    def invoke(self, get, summary_path, data=None, error=None):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload(account()) if data is None else copy.deepcopy(data)
        get.return_value = response
        get.side_effect = error
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {"DERIV_APP_ID": self.app_id, "DERIV_TOKEN": self.token,
                       "GITHUB_STEP_SUMMARY": str(summary_path)}
        with patch.dict(os.environ, environment, clear=True), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = discovery.main()
        return result, stdout.getvalue(), stderr.getvalue()

    @patch.object(discovery.requests, "get")
    def test_success_prints_only_confirmation_and_writes_safe_summary(self, get):
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.md"
            result, stdout, stderr = self.invoke(get, summary_path)
            self.assertEqual(result, 0)
            confirmation = json.loads(stdout)
            self.assertEqual(set(confirmation), {"account_id", "account_type", "status", "account_status", "read_only"})
            self.assertEqual(confirmation["account_id"], "DOT200")
            self.assertEqual(confirmation["account_type"], "demo")
            self.assertEqual(confirmation["account_status"], "active")
            self.assertEqual(confirmation["status"], "DEMO_ACCOUNT_CONFIRMED")
            self.assertIs(confirmation["read_only"], True)
            self.assertEqual(stderr, "")
            summary = summary_path.read_text(encoding="utf-8")
            self.assertIn("DOT200", summary)
            for credential in (self.app_id, self.token):
                self.assertNotIn(credential, stdout + stderr + summary)

    @patch.object(discovery.requests, "get")
    def test_failures_print_no_account_or_credentials_and_no_success_summary(self, get):
        scenarios = [(payload(account(), account("DOT201")), None),
                     (payload(account(self.token)), None),
                     (payload(account(self.app_id)), None),
                     (None, requests.Timeout("Bearer " + self.token)),
                     ({"data": [account()], "error": {"message": self.token}}, None)]
        for data, error in scenarios:
            with self.subTest(error_type=type(error).__name__ if error else "payload"):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    summary_path = Path(temporary_directory) / "summary.md"
                    result, stdout, stderr = self.invoke(get, summary_path, data, error)
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout, "")
                    self.assertTrue(stderr.strip())
                    self.assertNotIn(self.token, stderr)
                    self.assertNotIn(self.app_id, stderr)
                    self.assertNotIn("DOT200", stderr)
                    self.assertNotIn("DOT201", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertTrue(not summary_path.exists() or not summary_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
