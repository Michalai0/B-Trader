import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from btrader.binance import BinanceClient
from btrader.models import Direction


@pytest.mark.asyncio
async def test_futures_algo_uses_new_endpoint_and_signed_encoded_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"algoId": 42, "algoStatus": "NEW"})

    client = BinanceClient(
        "live",
        futures_api_key="key-123",
        futures_api_secret="secret-456",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.place_futures_algo(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            kind="STOP_MARKET",
            quantity=Decimal("0.123"),
            trigger_price=Decimal("60000.1"),
            client_id="bt-sl-test",
        )
    finally:
        await client.close()

    request = captured["request"]
    assert request.url.path == "/fapi/v1/algoOrder"
    assert request.headers["X-MBX-APIKEY"] == "key-123"
    raw_query = request.url.query.decode()
    unsigned, signature = raw_query.rsplit("&signature=", 1)
    expected = hmac.new(b"secret-456", unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    params = parse_qs(raw_query)
    assert params["algoType"] == ["CONDITIONAL"]
    assert params["type"] == ["STOP_MARKET"]
    assert params["reduceOnly"] == ["true"]
    assert params["workingType"] == ["MARK_PRICE"]
    assert result["algoId"] == 42


@pytest.mark.asyncio
async def test_spot_oco_uses_separate_client_ids():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"orderListId": 7})

    client = BinanceClient(
        "live",
        spot_api_key="spot-key",
        spot_api_secret="spot-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.place_spot_oco(
            symbol="BTCUSDT",
            quantity=Decimal("0.075"),
            take_profit_price=Decimal("62500"),
            stop_price=Decimal("60000"),
            stop_limit_price=Decimal("59940"),
            list_client_id="bt-oco-test",
            take_profit_client_id="bt-tp-test",
            stop_client_id="bt-sl-test",
        )
    finally:
        await client.close()

    request = captured["request"]
    assert request.url.path == "/api/v3/orderList/oco"
    params = request.url.params
    assert params["aboveType"] == "LIMIT_MAKER"
    assert params["belowType"] == "STOP_LOSS_LIMIT"
    assert params["aboveClientOrderId"] == "bt-tp-test"
    assert params["belowClientOrderId"] == "bt-sl-test"

