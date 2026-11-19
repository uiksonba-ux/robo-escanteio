import unittest
from trading_policy import (TradeCandidate, validate_candidate, validate_demo_websocket_url, safe_proposal_payload, gales_for_accuracy, stake_for_gale, stake_for_full_recovery, brl_to_account_currency)


class TestTradingPolicy(unittest.TestCase):


    def test_brl_entry_converts_to_demo_account_currency(self):
        self.assertEqual(brl_to_account_currency(10, 0.18), 1.8)
        for args in ((0, 0.18), (2, 0), (2, float("nan"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                brl_to_account_currency(*args)

    def test_accuracy_no_longer_changes_initial_gale_count(self):
        for accuracy in (0, 49.99, 50, 79.99, 80, 100):
            with self.subTest(accuracy=accuracy):
                self.assertEqual(gales_for_accuracy(accuracy), 5)

    def test_invalid_accuracy_is_blocked(self):
        for accuracy in (-0.01, 100.01, float("nan"), float("inf")):
            with self.subTest(accuracy=accuracy), self.assertRaises(ValueError):
                gales_for_accuracy(accuracy)

    def test_x2_stake_progression_is_limited_by_accuracy_band(self):
        self.assertEqual([stake_for_gale(10, level, 5) for level in range(6)],
                         [10, 20, 40, 80, 160, 320])
        with self.assertRaises(ValueError):
            stake_for_gale(10, 5, 3)

    def test_recovery_multiplier_is_disabled(self):
        self.assertEqual(stake_for_full_recovery(10, 10, 0.8), 10.0)
        self.assertEqual(stake_for_full_recovery(10, 0, 0), 10.0)
        for args in ((2, 10, 0), (0, 10, 0.8), (2, -1, 0.8)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                stake_for_full_recovery(*args)

    def candidate(self, **overrides):
        fields = dict(market="synthetic_index", symbol="R_50", contract_type="CALL",
                      duration=5, duration_unit="m", exchange_is_open=True)
        fields.update(overrides)
        return TradeCandidate(**fields)

    def test_allowed_durations(self):
        for duration in (1, 5, 15):
            with self.subTest(duration=duration):
                validate_candidate(self.candidate(duration=duration), {"CALL", "PUT"})

    def test_other_durations_blocked(self):
        for duration in (0, 2, 10, 14, 30, 60):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                validate_candidate(self.candidate(duration=duration), {"CALL"})

    def test_forex_blocked(self):
        with self.assertRaises(ValueError):
            validate_candidate(self.candidate(market="forex"), {"CALL"})

    def test_closed_and_suspended_blocked(self):
        for kwargs in ({"exchange_is_open": False}, {"is_trading_suspended": True}):
            with self.assertRaises(ValueError):
                validate_candidate(self.candidate(**kwargs), {"CALL"})

    def test_unavailable_contract_blocked(self):
        with self.assertRaises(ValueError):
            validate_candidate(self.candidate(), {"PUT"})

    def test_other_contracts_blocked(self):
        with self.assertRaises(ValueError):
            validate_candidate(self.candidate(contract_type="MULTUP"), {"MULTUP"})

    def test_real_endpoint_blocked(self):
        for url in ("wss://api.derivws.com/trading/v1/options/ws/real?otp=secret",
                    "wss://evil.example/trading/v1/options/ws/demo?otp=secret",
                    "ws://api.derivws.com/trading/v1/options/ws/demo"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_demo_websocket_url(url)

    def test_demo_endpoint(self):
        validate_demo_websocket_url("wss://api.derivws.com/trading/v1/options/ws/demo?otp=secret")

    def test_payload(self):
        payload = safe_proposal_payload(self.candidate(), 2.5, "USD", {"CALL"})
        self.assertEqual((payload["duration"], payload["duration_unit"]), (5, "m"))


if __name__ == "__main__":
    unittest.main()
