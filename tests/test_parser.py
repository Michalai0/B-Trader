from decimal import Decimal

import pytest

from btrader.errors import ValidationError
from btrader.models import Direction, EntryType, Market
from btrader.parser import parse_trade_command


def test_parses_chinese_futures_with_defaults(settings):
    result = parse_trade_command(
        "/trade 合约 BTCUSDT 多 10x sl=60000 risk=100u entry=market", settings
    )
    assert result.market == Market.FUTURES
    assert result.direction == Direction.LONG
    assert result.leverage == 10
    assert result.entry_type == EntryType.MARKET
    assert result.reward_risk == Decimal("1.5")
    assert result.take_profit_percent == Decimal("75")
    assert result.protect_breakeven is True


def test_parses_custom_short_limit(settings):
    result = parse_trade_command(
        "/trade futures ETHUSDT short lev=5 sl=4200 risk=100 entry=4000 rr=2 tp=60% protect=no",
        settings,
    )
    assert result.entry_type == EntryType.LIMIT
    assert result.entry_price == Decimal("4000")
    assert result.reward_risk == Decimal("2")
    assert result.take_profit_percent == Decimal("60")
    assert result.protect_breakeven is False


def test_spot_forces_one_x(settings):
    result = parse_trade_command(
        "/trade 现货 BTCUSDT 买 sl=60000 risk=100 entry=61000", settings
    )
    assert result.market == Market.SPOT
    assert result.leverage == 1


def test_rejects_unknown_parameter(settings):
    with pytest.raises(ValidationError, match="未知参数"):
        parse_trade_command(
            "/trade 合约 BTCUSDT 多 10x sl=60000 risk=100 entry=market foo=1", settings
        )

