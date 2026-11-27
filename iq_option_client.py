"""IQ Option PRACTICE adapter used by the existing Baskaraido management engine.

This wrapper exposes the small interface robo.py needs and refuses to switch to
REAL. IQ Option's community API is unofficial; keep this adapter isolated.
"""
import logging
import math
import threading
import time
from datetime import datetime, timedelta

import requests
from iqoptionapi.stable_api import IQ_Option

log = logging.getLogger("baskaraido-iq")


class IQOptionError(RuntimeError):
    """Broker/adapter failure with a sanitized, stable diagnostic code."""

    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


# Fiat currency codes used to recognize FX pairs without accepting crypto symbols
# such as BTCUSD as currency pairs.
_FIAT_CODES = frozenset({
    "AED", "ARS", "AUD", "BRL", "CAD", "CHF", "CLP", "CNY", "CNH",
    "COP", "CZK", "DKK", "EGP", "EUR", "GBP", "HKD", "HUF", "IDR",
    "ILS", "INR", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP",
    "PLN", "QAR", "RON", "RUB", "SAR", "SEK", "SGD", "THB", "TRY",
    "TWD", "UAH", "USD", "VND", "ZAR",
})


class IQOptionClient:
    @staticmethod
    def _is_currency_pair(symbol):
        normalized = str(symbol or "").upper()
        if normalized.endswith("-OTC"):
            normalized = normalized[:-4]
        return (
            len(normalized) == 6
            and normalized[:3] in _FIAT_CODES
            and normalized[3:] in _FIAT_CODES
            and normalized[:3] != normalized[3:]
        )

    def __init__(self, email, password, *, base_stake_amount=2.0):
        self.email = str(email or "").strip()
        self.password = str(password or "").strip()
        self.base_stake = float(base_stake_amount)
        self.base_stake_currency = "BRL"
        self.currency = "BRL"
        self.api = None
        self._connected = False
        self._lock = threading.RLock()
        self._trade_lock = threading.Lock()
        self._symbols_cache = {}
        self._symbols_cache_at = 0.0
        self._payout_subscriptions = set()
        self.usd_to_brl = None
        if not self.email or not self.password:
            raise ValueError("Configure IQ_EMAIL e IQ_PASSWORD")
        if not math.isfinite(self.base_stake) or self.base_stake < 2.0:
            raise ValueError("A entrada-base PRACTICE da IQ deve ser de pelo menos R$ 2,00")

    def connect(self):
        with self._lock:
            try:
                client = IQ_Option(self.email, self.password)
                result = client.connect()
                ok = bool(result[0]) if isinstance(result, (tuple, list)) else bool(result)
                if not ok:
                    reason = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else None
                    reason_text = str(reason or "").strip().lower()
                    if "2fa" in reason_text or "two factor" in reason_text:
                        code = "TwoFactorRequired"
                    elif "captcha" in reason_text:
                        code = "CaptchaRequired"
                    elif "password" in reason_text or "credential" in reason_text:
                        code = "CredentialsRejected"
                    else:
                        code = "LoginFailed"
                    raise IQOptionError(code)
                client.change_balance("PRACTICE")
                if not client.check_connect():
                    raise IQOptionError("PracticeConnectionFailed")
                self.api = client
                self._connected = True
                self._payout_subscriptions.clear()
                self._symbols_cache = {}
                self._symbols_cache_at = 0.0
                self.usd_to_brl = self._read_usd_to_brl()
                return True, "PRACTICE"
            except IQOptionError:
                self.api = None
                self._connected = False
                raise
            except Exception as exc:
                self.api = None
                self._connected = False
                # Keep upstream messages out of logs; only expose a stable error class.
                raise IQOptionError(type(exc).__name__) from None

    def close(self):
        with self._lock:
            self._connected = False
            client, self.api = self.api, None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def check_connect(self):
        # This SDK check reads local websocket state. Keep it lock-free so a
        # slow quote/candle request cannot make other scanners start reconnects.
        try:
            return bool(self._connected and self.api is not None and self.api.check_connect())
        except Exception:
            return False

    def get_balance_mode(self):
        # Confirm the broker's selected balance, not just our local intent.
        with self._lock:
            if not self.check_connect():
                return "DISCONNECTED"
            try:
                mode = self.api.get_balance_mode()
            except Exception:
                return "UNKNOWN"
        return mode if mode in ("PRACTICE", "REAL", "TOURNAMENT") else "UNKNOWN"

    def change_balance(self, mode):
        if str(mode).upper() != "PRACTICE":
            raise IQOptionError("PracticeOnly")
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            self.api.change_balance("PRACTICE")
            return True

    def get_balance(self):
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            amount = float(self.api.get_balance())
        if not math.isfinite(amount) or amount < 0:
            raise IQOptionError("InvalidBalance")
        return amount

    def get_portfolio(self):
        """Read open digital positions for the startup safety gate."""
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            result = self.api.get_positions("digital-option")

        if isinstance(result, (tuple, list)):
            if len(result) < 2 or result[0] is not True:
                raise IQOptionError("PortfolioUnavailable")
            positions = result[1]
        else:
            positions = result

        if isinstance(positions, dict):
            positions = positions.get("positions")
        if not isinstance(positions, list):
            raise IQOptionError("PortfolioUnavailable")

        portfolio = []
        for position in positions:
            if not isinstance(position, dict):
                portfolio.append({})
                continue
            expiry = None
            for key in ("expiry_time", "expiration_time", "expiration", "exp"):
                value = position.get(key)
                try:
                    candidate = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if candidate > 0:
                    expiry = candidate // 1000 if candidate > 10_000_000_000 else candidate
                    break
            portfolio.append({"expiry_time": expiry} if expiry is not None else {})
        return portfolio

    @staticmethod
    def _read_usd_to_brl():
        """Read the latest BCB PTAX sell quote with one bounded request."""
        hoje = datetime.now().date()
        data_inicial = (hoje - timedelta(days=14)).strftime("%m-%d-%Y")
        data_final = hoje.strftime("%m-%d-%Y")
        url = (
            "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
            "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
        )
        try:
            response = requests.get(
                url,
                params={
                    "@dataInicial": "'" + data_inicial + "'",
                    "@dataFinalCotacao": "'" + data_final + "'",
                    "$top": "20",
                    "$orderby": "dataHoraCotacao desc",
                    "$format": "json",
                },
                timeout=(2, 4),
            )
            response.raise_for_status()
            rows = response.json().get("value", [])
            for row in rows if isinstance(rows, list) else []:
                try:
                    rate = float(row["cotacaoVenda"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(rate) and rate > 0:
                    return rate
        except Exception:
            pass

        # Fallback reference-rate provider when the BCB PTAX endpoint is
        # unreachable from the hosting region. The USD 100 pre-stop remains
        # fail-closed if neither source returns a valid quote.
        try:
            response = requests.get(
                "https://api.frankfurter.dev/v2/rate/usd/brl",
                timeout=(2, 4),
            )
            response.raise_for_status()
            rate = float(response.json().get("rate"))
            if math.isfinite(rate) and rate > 0:
                return rate
        except Exception:
            pass
        raise IQOptionError("UsdBrlRateUnavailable")

    def get_active_symbols(self, refresh=False):
        """Return currency-pair candidates; payout is checked before any order.

        The community SDK's get_all_open_time() waits 30 seconds for the
        digital-underlying feed and can time out indefinitely when that feed
        is unavailable. Its active-name map is local and non-blocking; live
        digital availability is enforced separately by get_digital_payout().
        """
        now = time.monotonic()
        if not refresh and self._symbols_cache and now - self._symbols_cache_at < 300:
            return dict(self._symbols_cache)
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            try:
                active_map = self.api.get_all_ACTIVES_OPCODE()
            except Exception:
                raise IQOptionError("DigitalCatalogUnavailable") from None
        if not isinstance(active_map, dict):
            raise IQOptionError("DigitalCatalogUnavailable")
        symbols = {}
        for symbol in active_map:
            if isinstance(symbol, str) and self._is_currency_pair(symbol):
                symbols[symbol.upper()] = {"market": "digital", "candidate": True}
        if not symbols:
            raise IQOptionError("DigitalCatalogUnavailable")
        self._symbols_cache = symbols
        self._symbols_cache_at = now
        return dict(symbols)

    def is_symbol_tradeable(self, symbol):
        return str(symbol or "").upper() in self.get_active_symbols()

    def get_candles(self, symbol, granularity, count, end):
        if granularity not in (60, 300, 900) or not 1 <= int(count) <= 500:
            raise IQOptionError("InvalidCandleRequest")
        if not self.is_symbol_tradeable(symbol):
            raise IQOptionError("InstrumentUnavailable")
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            rows = self.api.get_candles(symbol, int(granularity), int(count), int(end))
        return rows if isinstance(rows, list) else []

    def get_digital_payout(self, symbol, duration, direction=None, amount=None):
        # Mantém compatibilidade com a interface do motor herdado; o payout
        # digital da IQ Option é independente de direção e valor da entrada.
        del direction, amount
        duration = int(duration)
        symbol = str(symbol or "").upper()
        if duration not in (1, 5, 15) or not self.is_symbol_tradeable(symbol):
            return None
        with self._lock:
            if not self.check_connect():
                return None
            key = (symbol, duration)
            try:
                if key not in self._payout_subscriptions:
                    self.api.subscribe_strike_list(symbol, duration)
                    self._payout_subscriptions.add(key)
            except Exception:
                return None

        # Never hold the session lock while waiting for the asynchronous quote.
        # This lets health checks and other scanners share the same connection.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            with self._lock:
                if not self.check_connect():
                    return None
                try:
                    raw = self.api.get_digital_current_profit(symbol, duration)
                except Exception:
                    raw = None
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                value = 0.0
            if math.isfinite(value) and value > 0:
                return value / 100.0 if value > 1 else value
            time.sleep(0.1)
        return None

    def is_contract_available(self, symbol, contract_type, duration):
        return (str(contract_type).upper() in ("CALL", "PUT")
                and int(duration) in (1, 5, 15)
                and self.is_symbol_tradeable(symbol))

    def buy_digital_spot_v2(self, symbol, amount, direction, duration):
        if str(direction).lower() not in ("call", "put"):
            raise IQOptionError("InvalidDirection")
        if not self.is_contract_available(symbol, "CALL", duration):
            raise IQOptionError("InstrumentUnavailable")
        amount = float(amount)
        if not math.isfinite(amount) or amount < 2.0:
            raise IQOptionError("MinimumStakeR$2")
        with self._trade_lock, self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            result = self.api.buy_digital_spot_v2(
                symbol, round(amount, 2), str(direction).lower(), int(duration)
            )
        if isinstance(result, (tuple, list)):
            if len(result) >= 2:
                return bool(result[0]), result[1]
            return bool(result[0]), None
        if isinstance(result, int) and not isinstance(result, bool):
            return result > 0, result
        return False, None

    def check_win_digital_v2(self, order_id):
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            return self.api.check_win_digital_v2(order_id)
