from decimal import Decimal

import pytest

from btrader.errors import ValidationError
from btrader.models import Direction, EntryType, Market, SymbolRules, TradeRequest
from btrader.risk import build_trade_plan, ceil_to_step, floor_to_step

RULES = SymbolRules(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    market_step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    max_qty=Decimal("1000"),
    min_notional=Decimal("5"),
)


def request(**overrides):
    values = dict(
        market=Market.FUTURES,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        leverage=10,
        stop_price=Decimal("60000"),
        risk_usdt=Decimal("100"),
        entry_type=EntryType.LIMIT,
        entry_price=Decimal("61000"),
        reward_risk=Decimal("1.5"),
        take_profit_percent=Decimal("75"),
        protect_breakeven=True,
    )
    values.update(overrides)
    return TradeRequest(**values)


def make_plan(trade_request):
    return build_trade_plan(
        trade_request,
        Decimal("61123"),
        RULES,
        max_risk_usdt=Decimal("500"),
        max_notional_usdt=Decimal("50000"),
        max_leverage=20,
    )


def test_long_risk_size_and_default_exit_split():
    plan = make_plan(request())
    assert plan.quantity == Decimal("0.1")
    assert plan.estimated_price_loss_usdt == Decimal("100.0")
    assert plan.take_profit_price == Decimal("62500.0")
    assert plan.take_profit_quantity == Decimal("0.075")
    assert plan.runner_quantity == Decimal("0.025")
    assert plan.estimated_margin_usdt == Decimal("610")


def test_short_target_and_rounding():
    plan = make_plan(
        request(
            symbol="ETHUSDT",
            direction=Direction.SHORT,
            leverage=5,
            entry_price=Decimal("4000"),
            stop_price=Decimal("4200"),
        )
    )
    assert plan.quantity == Decimal("0.5")
    assert plan.take_profit_price == Decimal("3700.0")


def test_stop_must_be_on_loss_side():
    with pytest.raises(ValidationError, match="做多的止损价"):
        make_plan(request(stop_price=Decimal("62000")))


def test_rejects_likely_liquidation_before_stop():
    with pytest.raises(ValidationError, match="强平"):
        make_plan(request(leverage=20, stop_price=Decimal("57000")))


def test_step_helpers():
    assert floor_to_step(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert ceil_to_step(Decimal("1.231"), Decimal("0.01")) == Decimal("1.24")

