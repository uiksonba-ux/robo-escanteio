import unittest
from trading_policy import (TradeCandidate, validate_candidate, validate_demo_websocket_url, safe_proposal_payload, gales_for_accuracy, stake_for_gale, stake_for_full_recovery)


class TestTradingPolicy(unittest.TestCase):

    def test_accuracy_bands_choose_zero_to_four_gales(self):
        cases = ((0, 4), (49.99, 4), (50, 3), (59.99, 3), (60, 2),
                 (69.99, 2), (70, 1), (79.99, 1), (80, 0), (100, 0))
        for accuracy, expected in cases:
            with self.subTest(accuracy=accuracy):
                self.assertEqual(gales_for_accuracy(accuracy), expected)

    def test_invalid_accuracy_is_blocked(self):
        for accuracy in (-0.01, 100.01, float("nan"), float("inf")):
            with self.subTest(accuracy=accuracy), self.assertRaises(ValueError):
                gales_for_accuracy(accuracy)

    def test_x2_stake_progression_is_limited_by_accuracy_band(self):
        self.assertEqual([stake_for_gale(2, level, 4) for level in range(5)],
                         [2, 4, 8, 16, 32])
        with self.assertRaises(ValueError):
            stake_for_gale(2, 4, 3)

    def test_full_recovery_stake_uses_confirmed_profit_ratio(self):
        self.assertEqual(stake_for_full_recovery(2, 10, 0.8), 14.5)
        self.assertEqual(stake_for_full_recovery(2, 0, 0), 2)
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
        for duration in (0, 2, 10, 30, 60):
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
