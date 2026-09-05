from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


class Market(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class EntryType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TradeState(str, Enum):
    WAITING_ENTRY = "waiting_entry"
    PROTECTED = "protected"
    RUNNER = "runner"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class TradeRequest:
    market: Market
    symbol: str
    direction: Direction
    leverage: int
    stop_price: Decimal
    risk_usdt: Decimal
    entry_type: EntryType
    entry_price: Optional[Decimal]
    reward_risk: Decimal
    take_profit_percent: Decimal
    protect_breakeven: bool


@dataclass(frozen=True)
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    market_step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    base_asset: str = ""
    quote_asset: str = ""


@dataclass(frozen=True)
class TradePlan:
    request: TradeRequest
    reference_price: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    quantity: Decimal
    take_profit_quantity: Decimal
    runner_quantity: Decimal
    notional_usdt: Decimal
    estimated_margin_usdt: Decimal
    estimated_price_loss_usdt: Decimal
    estimated_fee_buffer_usdt: Decimal
    warnings: tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
