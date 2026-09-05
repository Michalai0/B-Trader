import time
from decimal import Decimal

import pyotp
import pytest

from btrader.auth import Authorizer
from btrader.models import Direction, EntryType, Market, SymbolRules, TradeRequest
from btrader.paper import PaperExchange
from btrader.service import TradingService
from btrader.store import Store


class FakePublic:
    def __init__(self, price: Decimal):
        self.price = price
        self.rules = SymbolRules(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            market_step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
            min_notional=Decimal("5"),
            base_asset="BTC",
            quote_asset="USDT",
        )

    async def get_price(self, market, symbol):
        return self.price

    async def get_rules(self, market, symbol):
        return self.rules

    async def ping(self, market):
        return None

    async def close(self):
        return None


def make_request(market=Market.FUTURES):
    return TradeRequest(
        market=market,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        leverage=10 if market == Market.FUTURES else 1,
        stop_price=Decimal("60000"),
        risk_usdt=Decimal("100"),
        entry_type=EntryType.MARKET,
        entry_price=None,
        reward_risk=Decimal("1.5"),
        take_profit_percent=Decimal("75"),
        protect_breakeven=True,
    )


def make_service(settings, tmp_path, price=Decimal("61000")):
    public = FakePublic(price)
    exchange = PaperExchange(public)
    store = Store(tmp_path / "state.db")
    secret = pyotp.random_base32()
    auth = Authorizer(frozenset({1001}), frozenset({2002}), secret, store)
    service = TradingService(settings, exchange, store, auth)
    return service, exchange, store, secret, public


@pytest.mark.asyncio
async def test_futures_market_entry_places_stop_and_tp_then_moves_to_breakeven(settings, tmp_path):
    service, exchange, store, secret, public = make_service(settings, tmp_path)
    proposal_id, _ = await service.create_preview(make_request(), 1001, 2002)
    code = pyotp.TOTP(secret).at(int(time.time()))
    trade = await service.confirm(proposal_id, code, 1001, 2002)
    assert trade["state"] == "protected"
    assert trade["stop_order_id"]
    assert trade["tp_order_id"]
    assert Decimal(trade["take_profit_quantity"]) == Decimal("0.075")

    public.price = Decimal(trade["take_profit_price"])
    events = await service.monitor_all()
    updated = store.get_trade(proposal_id)
    assert updated["state"] == "runner"
    assert exchange.orders[updated["stop_order_id"]]["triggerPrice"] == updated["actual_entry"]
    assert any("推到开仓价" in event["message"] for event in events)


@pytest.mark.asyncio
async def test_spot_market_entry_uses_oco_and_runner_stop(settings, tmp_path):
    service, exchange, store, secret, public = make_service(settings, tmp_path)
    proposal_id, _ = await service.create_preview(make_request(Market.SPOT), 1001, 2002)
    code = pyotp.TOTP(secret).at(int(time.time()))
    trade = await service.confirm(proposal_id, code, 1001, 2002)
    assert trade["state"] == "protected"
    assert trade["oco_order_list_id"]
    assert trade["runner_stop_order_id"]

    public.price = Decimal(trade["take_profit_price"])
    await service.monitor_all()
    updated = store.get_trade(proposal_id)
    assert updated["state"] == "runner"
    be_order = exchange.orders[f"bt-be-{proposal_id}"]
    assert be_order["triggerPrice"] == updated["actual_entry"]
