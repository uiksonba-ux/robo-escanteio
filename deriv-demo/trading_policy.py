"""Deriv options trade policy. Fail closed: no order submission here."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
import math
from typing import Any

ALLOWED_DURATION_MINUTES = frozenset({1, 5, 15})
# Only contracts with binary-style rise/fall payoff. Verify against contracts_for
# for the selected underlying before quoting; this is not a blanket guarantee.
ALLOWED_CONTRACT_TYPES = frozenset({"CALL", "PUT"})
FORBIDDEN_MARKETS = frozenset({"forex"})
FORBIDDEN_TYPES = frozenset({"multiplier", "cfd"})
DEMO_ONLY = True


# Shared management: accuracy bands select the maximum number of gales.
# Stake amounts are denominated in the authenticated DEMO account currency.
MAX_GALES = 4
GALE_MULTIPLIER = 2
RECOVERY_PERCENT = 1.0
ACCURACY_GALE_BANDS = ((50.0, 4), (60.0, 3), (70.0, 2), (80.0, 1), (100.0, 0))


def gales_for_accuracy(accuracy: float) -> int:
    value = float(accuracy)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Assertividade precisa estar entre 0 e 100")
    for upper_bound, gales in ACCURACY_GALE_BANDS:
        if value < upper_bound:
            return gales
    return 0


def stake_for_gale(base_stake: float, gale_level: int, max_gales: int) -> float:
    base = float(base_stake)
    if not math.isfinite(base) or base <= 0:
        raise ValueError("Entrada-base inválida")
    if isinstance(gale_level, bool) or not isinstance(gale_level, int):
        raise ValueError("Nível de gale inválido")
    if isinstance(max_gales, bool) or not isinstance(max_gales, int) or not 0 <= max_gales <= MAX_GALES:
        raise ValueError("Quantidade de gales inválida")
    if not 0 <= gale_level <= max_gales:
        raise ValueError("Nível excede a faixa de assertividade")
    return float((Decimal(str(base)) * (Decimal(GALE_MULTIPLIER) ** gale_level)).quantize(Decimal("0.01")))


def stake_for_full_recovery(base_stake: float, accumulated_loss: float,
                            expected_profit_per_stake: float) -> float:
    base = Decimal(str(base_stake))
    loss = Decimal(str(accumulated_loss))
    ratio = Decimal(str(expected_profit_per_stake))
    if not base.is_finite() or base <= 0 or not loss.is_finite() or loss < 0:
        raise ValueError("Valores de recuperação inválidos")
    if loss == 0:
        return float(base.quantize(Decimal("0.01")))
    if not ratio.is_finite() or ratio <= 0:
        raise ValueError("Payout efetivo inválido")
    required = base + loss / ratio
    return float(required.quantize(Decimal("0.01"), rounding=ROUND_UP))


@dataclass(frozen=True)
class TradeCandidate:
    market: str
    symbol: str
    contract_type: str
    duration: int
    duration_unit: str
    is_trading_suspended: bool = False
    exchange_is_open: bool = False


def validate_candidate(candidate: TradeCandidate, available_contract_types: set[str]) -> None:
    """Raise on any unsupported trade. Must be called immediately before proposal/buy."""
    if candidate.market.strip().lower() in FORBIDDEN_MARKETS:
        raise ValueError("Forex bloqueado")
    if candidate.contract_type not in ALLOWED_CONTRACT_TYPES:
        raise ValueError("Tipo de contrato não permitido")
    if candidate.contract_type not in available_contract_types:
        raise ValueError("Contrato não disponível para o ativo")
    if candidate.duration_unit != "m" or candidate.duration not in ALLOWED_DURATION_MINUTES:
        raise ValueError("Duração permitida: 1, 5 ou 15 minutos")
    if candidate.is_trading_suspended or not candidate.exchange_is_open:
        raise ValueError("Mercado fechado ou negociação suspensa")


def validate_demo_websocket_url(url: str) -> None:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com":
        raise ValueError("Endpoint não reconhecido")
    if parsed.path.rstrip("/") != "/trading/v1/options/ws/demo":
        raise ValueError("Somente websocket DEMO é permitido")


def safe_proposal_payload(candidate: TradeCandidate, amount: float, currency: str, available_contract_types: set[str]) -> dict[str, Any]:
    validate_candidate(candidate, available_contract_types)
    if isinstance(amount, bool) or not math.isfinite(float(amount)) or amount <= 0:
        raise ValueError("Valor inválido")
    if not isinstance(currency, str) or not currency.isalpha() or len(currency) != 3:
        raise ValueError("Moeda da conta inválida")
    return {
        "proposal": 1,
        "amount": amount,
        "basis": "stake",
        "contract_type": candidate.contract_type,
        "currency": currency,
        "duration": candidate.duration,
        "duration_unit": candidate.duration_unit,
        "underlying_symbol": candidate.symbol,
    }
