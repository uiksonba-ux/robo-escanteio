import unittest
from discover_demo_account import select_demo_account


class TestDiscoverDemoAccount(unittest.TestCase):
    def setUp(self):
        self.payload = {"data": [
            {"account_id": "CR100", "account_type": "real", "status": "active", "currency": "USD"},
            {"account_id": "DOT200", "account_type": "demo", "status": "active", "currency": "USD"},
        ]}

    def test_select_only_demo(self):
        self.assertEqual(select_demo_account(self.payload)["account_id"], "DOT200")

    def test_real_id_rejected(self):
        with self.assertRaises(ValueError):
            select_demo_account(self.payload, "CR100")

    def test_two_demos_require_explicit_id(self):
        payload = {"data": self.payload["data"] + [
            {"account_id": "DOT201", "account_type": "demo", "status": "active"}]}
        with self.assertRaises(ValueError):
            select_demo_account(payload)
        self.assertEqual(select_demo_account(payload, "DOT201")["account_id"], "DOT201")

    def test_inactive_demo_rejected(self):
        payload = {"data": [{"account_id": "DOT200", "account_type": "demo", "status": "inactive"}]}
        with self.assertRaises(ValueError):
            select_demo_account(payload)

    def test_malformed_response_rejected(self):
        for payload in ({}, {"data": None}, {"data": ["invalid"]}):
            with self.assertRaises(ValueError):
                select_demo_account(payload)


if __name__ == "__main__":
    unittest.main()
