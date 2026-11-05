import unittest
from trading_policy import TradeCandidate, validate_candidate, validate_demo_websocket_url, safe_proposal_payload


class TestTradingPolicy(unittest.TestCase):
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
