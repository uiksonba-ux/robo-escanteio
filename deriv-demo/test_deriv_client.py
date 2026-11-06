import unittest
from unittest.mock import Mock

from deriv_client import DerivAPIError, DerivOptionsClient


class TestDerivOptionsClientReadOnly(unittest.TestCase):
    def test_otp_url_is_restricted_to_demo_options_endpoint(self):
        client = DerivOptionsClient.__new__(DerivOptionsClient)
        client.account_id = "DOT_TEST"
        for url in (
            "wss://api.derivws.com/trading/v1/options/ws/real?otp=secret",
            "wss://evil.example/trading/v1/options/ws/demo?otp=secret",
            "wss://api.derivws.com/trading/v1/options/ws/demo",
        ):
            with self.subTest(url=url):
                client._rest_json = Mock(return_value={"data": {"url": url}})
                with self.assertRaises(DerivAPIError):
                    client._new_otp_url()

    def test_candles_are_normalized_for_existing_strategy(self):
        client = DerivOptionsClient.__new__(DerivOptionsClient)
        client.is_symbol_tradeable = Mock(return_value=True)
        client._request = Mock(return_value={
            "candles": [{
                "epoch": 1_800_000_000,
                "open": "1.25",
                "high": "1.50",
                "low": "1.00",
                "close": "1.40",
            }]
        })
        result = client.get_candles("R_50", 60, 100, 1_800_000_100)
        self.assertEqual(result, [{
            "from": 1_800_000_000,
            "open": 1.25,
            "close": 1.4,
            "max": 1.5,
            "min": 1.0,
        }])

    def test_only_supported_candle_periods_are_allowed(self):
        client = DerivOptionsClient.__new__(DerivOptionsClient)
        client.is_symbol_tradeable = Mock(return_value=True)
        for granularity in (30, 120, 600):
            with self.subTest(granularity=granularity):
                with self.assertRaises(DerivAPIError):
                    client.get_candles("R_50", granularity, 100, 1_800_000_100)

    def test_explicit_usd_base_stake_is_not_fx_converted(self):
        client = DerivOptionsClient(
            "app", "token", "demo-account",
            base_stake_amount=0.35, base_stake_currency="USD",
        )
        client.currency = "USD"
        client._rest_json = Mock(side_effect=AssertionError("no conversion needed"))

        client._refresh_exchange_rate()

        self.assertEqual(client.base_stake, 0.35)
        client._rest_json.assert_not_called()

    def test_legacy_brl_base_stake_still_converts_to_account_currency(self):
        client = DerivOptionsClient("app", "token", "demo-account", base_stake_brl=2.0)
        client.currency = "USD"
        client._rest_json = Mock(return_value={"data": {"exchange_rate": "0.18"}})

        client._refresh_exchange_rate()

        self.assertEqual(client.base_stake, 0.36)
        client._rest_json.assert_called_once_with(
            "GET",
            "/wallet/v1/exchange-rate",
            params={"source_currency": "BRL", "destination_currency": "USD"},
        )


if __name__ == "__main__":
    unittest.main()
