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
    pass


class IQOptionClient:
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
                    raise IQOptionError("LoginFailed")
                client.change_balance("PRACTICE")
                if not client.check_connect():
                    raise IQOptionError("PracticeConnectionFailed")
                self.api = client
                self._connected = True
                self.usd_to_brl = self._read_usd_to_brl()
                return True, "PRACTICE"
            except Exception as exc:
                self.api = None
                self._connected = False
                # Avoid logging vendor exception text; it may contain account data.
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
        with self._lock:
            try:
                return bool(self._connected and self.api is not None and self.api.check_connect())
            except Exception:
                return False

    def get_balance_mode(self):
        return "PRACTICE" if self.check_connect() else "DISCONNECTED"

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

    @staticmethod
    def _read_usd_to_brl():
        """Use the latest BCB PTAX sell quote, falling back to a recent quote."""
        today = datetime.now().date()
        for days_back in range(0, 8):
            day = today - timedelta(days=days_back)
            date_text = day.strftime("%m-%d-%Y")
            url = (
                "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
                "CotacaoDolarDia(dataCotacao=@dataCotacao)"
            )
            try:
                response = requests.get(
                    url,
                    params={"@dataCotacao": "'" + date_text + "'", "$format": "json"},
                    timeout=5,
                )
                response.raise_for_status()
                rows = response.json().get("value", [])
                rates = [float(row["cotacaoVenda"]) for row in rows
                         if isinstance(row, dict) and row.get("cotacaoVenda")]
                if rates and math.isfinite(rates[-1]) and rates[-1] > 0:
                    return rates[-1]
            except Exception:
                continue
        raise IQOptionError("UsdBrlRateUnavailable")

    def get_active_symbols(self, refresh=False):
        now = time.monotonic()
        if not refresh and self._symbols_cache and now - self._symbols_cache_at < 30:
            return dict(self._symbols_cache)
        with self._lock:
            if not self.check_connect():
                raise IQOptionError("Disconnected")
            raw = self.api.get_all_open_time()
        digital = raw.get("digital", {}) if isinstance(raw, dict) else {}
        if not isinstance(digital, dict):
            raise IQOptionError("DigitalCatalogUnavailable")
        symbols = {}
        for symbol, status in digital.items():
            if not isinstance(symbol, str) or not isinstance(status, dict):
                continue
            if status.get("open") is True:
                symbols[symbol.upper()] = {"market": "digital", "open": True}
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

    def get_digital_payout(self, symbol, duration):
        if int(duration) not in (1, 5, 15) or not self.is_symbol_tradeable(symbol):
            return None
        with self._lock:
            if not self.check_connect():
                return None
            raw = self.api.get_digital_current_profit(symbol, int(duration))
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return value / 100.0 if value > 1 else value

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
