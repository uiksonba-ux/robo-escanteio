"""Deriv options trade policy. Fail closed: no order submission here."""
from dataclasses import dataclass
from typing import Any

ALLOWED_DURATION_MINUTES = frozenset({1, 5, 15})
# Only contracts with binary-style rise/fall payoff. Verify against contracts_for
# for the selected underlying before quoting; this is not a blanket guarantee.
ALLOWED_CONTRACT_TYPES = frozenset({"CALL", "PUT"})
FORBIDDEN_MARKETS = frozenset({"forex"})
FORBIDDEN_TYPES = frozenset({"multiplier", "cfd"})
DEMO_ONLY = True


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
    if amount <= 0:
        raise ValueError("Valor inválido")
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
