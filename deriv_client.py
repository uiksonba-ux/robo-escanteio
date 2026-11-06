"""Strict DEMO-only adapter for the Deriv Options New API."""
import json
import math
import re
import threading
import time
from decimal import Decimal, ROUND_UP
from urllib.parse import quote, urlparse

import requests
import websocket


REST_BASE = "https://api.derivws.com"
DEMO_WS_PATH = "/trading/v1/options/ws/demo"
_ALLOWED_MINUTES = frozenset({1, 5, 15})
_TIMEOUT = 8


class DerivAPIError(RuntimeError):
    """Sanitized API error; never contains credentials or upstream message text."""

    def __init__(self, code="DerivAPIError"):
        safe_codes = {
            "InvalidToken", "RateLimit", "InputValidationFailed", "ContractNotFound",
            "InsufficientBalance", "ValidationError", "NotFound", "Unauthorized",
            "InternalError", "Timeout", "ConnectionError",
        }
        self.code = code if code in safe_codes else "DerivAPIError"
        super().__init__(self.code)


class DerivOptionsClient:
    """One serialized WebSocket owner bound to exactly one active DEMO account."""

    def __init__(self, app_id, token, account_id, base_stake_brl=2.0,
                 *, base_stake_amount=None, base_stake_currency=None):
        self.app_id = str(app_id or "").strip()
        self.token = str(token or "").strip()
        self.account_id = str(account_id or "").strip()
        if base_stake_amount is None:
            self.base_stake_amount = Decimal(str(base_stake_brl))
            self.base_stake_currency = "BRL"
        else:
            self.base_stake_amount = Decimal(str(base_stake_amount))
            self.base_stake_currency = str(base_stake_currency or "").strip().upper()
        # Keep the original attribute for callers that configure BRL.
        self.base_stake_brl = (
            self.base_stake_amount if self.base_stake_currency == "BRL" else None
        )
        if not self.app_id or not self.token or not self.account_id:
            raise ValueError("Configure DERIV_APP_ID, DERIV_TOKEN e DERIV_DEMO_ACCOUNT_ID")
        if (not self.base_stake_amount.is_finite() or self.base_stake_amount <= 0
                or self.base_stake_currency not in {"BRL", "USD"}):
            raise ValueError("Entrada-base ou moeda inválida")
        self.currency = None
        self.base_stake = None
        self._exchange_rate = None
        self._exchange_rate_at = 0.0
        self._ws = None
        self._lock = threading.RLock()
        self._trade_lock = threading.Lock()
        self._request_id = 1000
        self._generation = 0
        self._active_symbols = {}
        self._active_symbols_at = 0.0
        self._contracts_cache = {}

    def _headers(self):
        return {
            "Deriv-App-ID": self.app_id,
            "Authorization": "Bearer " + self.token,
        }

    def _rest_json(self, method, path, params=None):
        try:
            response = requests.request(
                method,
                REST_BASE + path,
                headers=self._headers(),
                params=params,
                timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise DerivAPIError("ConnectionError") from None
        if response.status_code != 200:
            raise DerivAPIError("Unauthorized" if response.status_code in (401, 403) else "DerivAPIError")
        try:
            payload = response.json()
        except ValueError:
            raise DerivAPIError("DerivAPIError") from None
        if not isinstance(payload, dict) or payload.get("errors"):
            raise DerivAPIError("DerivAPIError")
        return payload

    def _validate_demo_account(self):
        payload = self._rest_json("GET", "/trading/v1/options/accounts")
        data = payload.get("data")
        links = payload.get("links", {})
        meta = payload.get("meta", {})
        if not isinstance(data, list) or not isinstance(links, dict) or not isinstance(meta, dict):
            raise DerivAPIError("ValidationError")
        if links.get("next") or (isinstance(meta.get("total"), int) and meta["total"] != len(data)):
            raise DerivAPIError("ValidationError")
        demos = [
            item for item in data
            if isinstance(item, dict) and item.get("account_type") == "demo"
        ]
        if (len(demos) != 1 or demos[0].get("account_id") != self.account_id
                or demos[0].get("status") != "active"):
            raise DerivAPIError("Unauthorized")
        currency = demos[0].get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z0-9]{2,20}", currency):
            raise DerivAPIError("ValidationError")
        self.currency = currency

    def _refresh_exchange_rate(self):
        if self.currency == self.base_stake_currency:
            rate = Decimal("1")
        else:
            payload = self._rest_json(
                "GET",
                "/wallet/v1/exchange-rate",
                params={
                    "source_currency": self.base_stake_currency,
                    "destination_currency": self.currency,
                },
            )
            raw = payload.get("data", {}).get("exchange_rate")
            try:
                rate = Decimal(str(raw))
            except Exception:
                raise DerivAPIError("ValidationError") from None
        if not rate.is_finite() or rate <= 0:
            raise DerivAPIError("ValidationError")
        self._exchange_rate = rate
        self._exchange_rate_at = time.monotonic()
        self.base_stake = float((self.base_stake_amount * rate).quantize(
            Decimal("0.01"), rounding=ROUND_UP
        ))
        if not math.isfinite(self.base_stake) or self.base_stake <= 0:
            raise DerivAPIError("ValidationError")

    def _new_otp_url(self):
        path = "/trading/v1/options/accounts/" + quote(self.account_id, safe="") + "/otp"
        payload = self._rest_json("POST", path)
        url = payload.get("data", {}).get("url")
        if not isinstance(url, str):
            raise DerivAPIError("ValidationError")
        parsed = urlparse(url)
        if (parsed.scheme != "wss" or parsed.hostname != "api.derivws.com"
                or parsed.path.rstrip("/") != DEMO_WS_PATH
                or not parsed.query):
            raise DerivAPIError("Unauthorized")
        return url

    def _invalidate_socket(self):
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def connect(self):
        with self._lock:
            self._invalidate_socket()
            self._validate_demo_account()
            self._refresh_exchange_rate()
            url = self._new_otp_url()
            parsed = urlparse(url)
            if parsed.path.rstrip("/") != DEMO_WS_PATH:
                raise DerivAPIError("Unauthorized")
            try:
                self._ws = websocket.create_connection(url, timeout=_TIMEOUT, enable_multithread=True)
                self._ws.settimeout(_TIMEOUT)
            except Exception:
                self._invalidate_socket()
                raise DerivAPIError("ConnectionError") from None
            self._generation += 1
            return True, "DEMO"

    def close(self):
        with self._lock:
            self._invalidate_socket()

    def check_connect(self):
        with self._lock:
            return self._ws is not None and bool(getattr(self._ws, "connected", False))

    def get_balance_mode(self):
        return "DEMO" if self.check_connect() else "DISCONNECTED"

    def change_balance(self, mode):
        if str(mode).upper() != "DEMO":
            raise DerivAPIError("Unauthorized")
        return self.check_connect()

    def _ensure_connected(self):
        if not self.check_connect():
            self.connect()

    def _request(self, message, allow_connect=True):
        with self._lock:
            if allow_connect:
                self._ensure_connected()
            if not self.check_connect():
                raise DerivAPIError("ConnectionError")
            self._request_id += 1
            request_id = self._request_id
            request = dict(message)
            request["req_id"] = request_id
            try:
                self._ws.send(json.dumps(request, separators=(",", ":")))
                while True:
                    raw = self._ws.recv()
                    response = json.loads(raw)
                    if not isinstance(response, dict):
                        continue
                    if response.get("req_id") != request_id:
                        continue
                    error = response.get("error")
                    if isinstance(error, dict):
                        code = error.get("code")
                        raise DerivAPIError(code if isinstance(code, str) else "DerivAPIError")
                    if response.get("msg_type") is None:
                        raise DerivAPIError("ValidationError")
                    return response
            except DerivAPIError:
                raise
            except Exception as exc:
                timed_out = isinstance(exc, (TimeoutError, websocket.WebSocketTimeoutException))
                self._invalidate_socket()
                raise DerivAPIError("Timeout" if timed_out else "ConnectionError") from None

    def get_balance(self):
        response = self._request({"balance": 1})
        result = response.get("balance")
        amount = result.get("balance") if isinstance(result, dict) else None
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise DerivAPIError("ValidationError")
        currency = result.get("currency")
        if currency != self.currency:
            raise DerivAPIError("ValidationError")
        return float(amount)

    def get_active_symbols(self, refresh=False):
        now = time.monotonic()
        if not refresh and self._active_symbols and now < self._active_symbols_at:
            return dict(self._active_symbols)
        response = self._request({
            "active_symbols": "brief",
            "contract_type": ["CALL", "PUT"],
        })
        rows = response.get("active_symbols")
        if not isinstance(rows, list):
            raise DerivAPIError("ValidationError")
        symbols = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = row.get("underlying_symbol")
            market = row.get("market")
            if (isinstance(code, str) and code and isinstance(market, str)
                    and market.lower() != "forex"
                    and row.get("exchange_is_open") == 1
                    and row.get("is_trading_suspended") == 0):
                symbols[code] = {
                    "market": market,
                    "name": row.get("underlying_symbol_name"),
                    "open": True,
                }
        self._active_symbols = symbols
        self._active_symbols_at = now + 300
        return dict(symbols)

    def is_symbol_tradeable(self, symbol):
        symbols = self.get_active_symbols()
        return isinstance(symbol, str) and symbol in symbols

    def get_candles(self, symbol, granularity, count, end):
        allowed = {60: 60, 300: 300, 900: 900}
        if granularity not in allowed or isinstance(count, bool) or not isinstance(count, int) or not 60 <= count <= 500:
            raise DerivAPIError("ValidationError")
        if not self.is_symbol_tradeable(symbol):
            raise DerivAPIError("ContractNotFound")
        response = self._request({
            "ticks_history": symbol,
            "end": str(int(end)),
            "count": count,
            "granularity": granularity,
            "style": "candles",
        })
        rows = response.get("candles")
        if not isinstance(rows, list):
            raise DerivAPIError("ValidationError")
        candles = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                epoch = int(row["epoch"])
                candles.append({
                    "from": epoch,
                    "open": float(row["open"]),
                    "close": float(row["close"]),
                    "max": float(row["high"]),
                    "min": float(row["low"]),
                })
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return candles

    @staticmethod
    def _duration_minutes(value):
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value.lower())
        if not match:
            return None
        number = int(match.group(1))
        unit = match.group(2) or "m"
        factor = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}[unit]
        minutes = number * factor
        return minutes if math.isfinite(minutes) else None

    def get_contracts(self, symbol):
        now = time.monotonic()
        cached = self._contracts_cache.get(symbol)
        if cached and now < cached[0]:
            return cached[1]
        response = self._request({"contracts_for": symbol})
        details = response.get("contracts_for")
        available = details.get("available") if isinstance(details, dict) else None
        if not isinstance(available, list):
            raise DerivAPIError("ValidationError")
        self._contracts_cache[symbol] = (now + 300, available)
        return available

    def is_contract_available(self, symbol, contract_type, duration):
        if contract_type not in ("CALL", "PUT") or duration not in _ALLOWED_MINUTES:
            return False
        if not self.is_symbol_tradeable(symbol):
            return False
        try:
            contracts = self.get_contracts(symbol)
        except DerivAPIError:
            return False
        for item in contracts:
            if not isinstance(item, dict) or item.get("contract_type") != contract_type:
                continue
            if item.get("expiry_type") != "intraday":
                continue
            minimum = self._duration_minutes(item.get("min_contract_duration"))
            maximum = self._duration_minutes(item.get("max_contract_duration"))
            if minimum is not None and maximum is not None and minimum <= duration <= maximum:
                return True
        return False

    def _proposal(self, symbol, contract_type, amount, duration):
        if contract_type not in ("CALL", "PUT") or duration not in _ALLOWED_MINUTES:
            raise DerivAPIError("ValidationError")
        if not self.is_contract_available(symbol, contract_type, duration):
            raise DerivAPIError("ContractNotFound")
        stake = float(amount)
        if not math.isfinite(stake) or stake <= 0:
            raise DerivAPIError("ValidationError")
        response = self._request({
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": self.currency,
            "duration": duration,
            "duration_unit": "m",
            "underlying_symbol": symbol,
        })
        proposal = response.get("proposal")
        if not isinstance(proposal, dict):
            raise DerivAPIError("ValidationError")
        proposal_id = proposal.get("id")
        ask_price = proposal.get("ask_price")
        payout = proposal.get("payout")
        if (not isinstance(proposal_id, str) or not proposal_id
                or isinstance(ask_price, bool) or not isinstance(ask_price, (int, float))
                or isinstance(payout, bool) or not isinstance(payout, (int, float))
                or not math.isfinite(ask_price) or not math.isfinite(payout)
                or ask_price <= 0 or payout <= ask_price):
            raise DerivAPIError("ValidationError")
        return proposal_id, float(ask_price), float(payout)

    def get_digital_payout(self, symbol, duration):
        ratios = []
        for contract_type in ("CALL", "PUT"):
            try:
                _, ask_price, payout = self._proposal(symbol, contract_type, self.base_stake, duration)
            except DerivAPIError:
                return None
            ratio = (payout - ask_price) / ask_price
            if not math.isfinite(ratio) or ratio <= 0:
                return None
            ratios.append(ratio)
        return min(ratios) if ratios else None

    def buy_digital_spot_v2(self, symbol, amount, direction, duration):
        contract_type = {"call": "CALL", "put": "PUT"}.get(str(direction).lower())
        if contract_type is None:
            raise DerivAPIError("ValidationError")
        with self._trade_lock:
            self._ensure_connected()
            generation = self._generation
            proposal_id, ask_price, _ = self._proposal(symbol, contract_type, amount, duration)
            if generation != self._generation or not self.check_connect():
                raise DerivAPIError("ConnectionError")
            response = self._request(
                {"buy": proposal_id, "price": float(ask_price)},
                allow_connect=False,
            )
            result = response.get("buy")
            contract_id = result.get("contract_id") if isinstance(result, dict) else None
            if isinstance(contract_id, bool) or not isinstance(contract_id, int) or contract_id <= 0:
                raise DerivAPIError("ValidationError")
            return True, contract_id

    def check_win_digital_v2(self, contract_id):
        if isinstance(contract_id, bool):
            raise DerivAPIError("ValidationError")
        try:
            contract_id = int(contract_id)
        except (TypeError, ValueError, OverflowError):
            raise DerivAPIError("ValidationError") from None
        if contract_id <= 0:
            raise DerivAPIError("ValidationError")
        response = self._request({"proposal_open_contract": 1, "contract_id": contract_id})
        contract = response.get("proposal_open_contract")
        if not isinstance(contract, dict):
            return False, None
        status = contract.get("status")
        if status not in ("won", "lost", "sold", "cancelled"):
            return False, None
        raw_profit = contract.get("profit", "0")
        try:
            profit = float(raw_profit)
        except (TypeError, ValueError, OverflowError):
            raise DerivAPIError("ValidationError") from None
        if not math.isfinite(profit):
            raise DerivAPIError("ValidationError")
        return True, profit
