from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from btrader.errors import AmbiguousExchangeError, ExchangeError, RateLimitError, ValidationError
from btrader.models import Direction, EntryType, Market, SymbolRules, TradePlan

LIVE_BASES = {
    Market.SPOT: "https://api.binance.com",
    Market.FUTURES: "https://fapi.binance.com",
}
TESTNET_BASES = {
    Market.SPOT: "https://testnet.binance.vision",
    Market.FUTURES: "https://testnet.binancefuture.com",
}


def _text(value: Decimal) -> str:
    return format(value, "f")


class BinanceClient:
    """Minimal direct Binance REST client using HMAC signed endpoints."""

    def __init__(
        self,
        mode: str,
        api_key: str = "",
        api_secret: str = "",
        *,
        spot_api_key: str = "",
        spot_api_secret: str = "",
        futures_api_key: str = "",
        futures_api_secret: str = "",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.mode = mode
        self._api_keys = {
            Market.SPOT: spot_api_key or api_key,
            Market.FUTURES: futures_api_key or api_key,
        }
        self._secrets = {
            Market.SPOT: (spot_api_secret or api_secret).encode("utf-8"),
            Market.FUTURES: (futures_api_secret or api_secret).encode("utf-8"),
        }
        self.bases = TESTNET_BASES if mode == "testnet" else LIVE_BASES
        self.http = httpx.AsyncClient(timeout=10.0, transport=transport)
        self._time_offsets: Dict[Market, int] = {Market.SPOT: 0, Market.FUTURES: 0}
        self._rules: Dict[tuple[Market, str], SymbolRules] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def _sync_time(self, market: Market) -> None:
        path = "/api/v3/time" if market == Market.SPOT else "/fapi/v1/time"
        before = int(time.time() * 1000)
        response = await self._request(market, "GET", path)
        after = int(time.time() * 1000)
        midpoint = (before + after) // 2
        self._time_offsets[market] = int(response["serverTime"]) - midpoint

    async def _request(
        self,
        market: Market,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        signed: bool = False,
        retry_time_sync: bool = True,
    ) -> Any:
        values = dict(params or {})
        headers: Dict[str, str] = {}
        if signed:
            api_key = self._api_keys[market]
            secret = self._secrets[market]
            if not api_key or not secret:
                raise ExchangeError("币安 API 凭据未配置")
            values["timestamp"] = int(time.time() * 1000) + self._time_offsets[market]
            values.setdefault("recvWindow", 5000)
            payload = urlencode(values)
            values["signature"] = hmac.new(
                secret, payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = api_key
        try:
            response = await self.http.request(
                method, f"{self.bases[market]}{path}", params=values, headers=headers
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if signed and method != "GET":
                raise AmbiguousExchangeError(
                    "币安请求超时，成交状态未知；系统将通过客户端订单号继续核对"
                ) from exc
            raise ExchangeError("连接币安失败，请检查网络") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ExchangeError(f"币安返回了无法解析的响应（HTTP {response.status_code}）") from exc

        if response.status_code in {418, 429}:
            retry_after = response.headers.get("Retry-After", "稍后")
            raise RateLimitError(f"触发币安限流，请在 {retry_after} 秒后重试")
        if response.status_code >= 500:
            if signed and method != "GET":
                raise AmbiguousExchangeError(
                    f"币安服务异常（HTTP {response.status_code}），订单状态未知"
                )
            raise ExchangeError(f"币安服务异常（HTTP {response.status_code}）")
        if response.status_code >= 400 or (isinstance(data, dict) and int(data.get("code", 0)) < 0):
            code = data.get("code", response.status_code) if isinstance(data, dict) else response.status_code
            message = data.get("msg", "请求失败") if isinstance(data, dict) else "请求失败"
            if code == -1021 and signed and retry_time_sync:
                await self._sync_time(market)
                clean = dict(params or {})
                return await self._request(
                    market,
                    method,
                    path,
                    clean,
                    signed=True,
                    retry_time_sync=False,
                )
            raise ExchangeError(f"币安错误 {code}：{message}")
        return data

    async def ping(self, market: Market) -> None:
        path = "/api/v3/ping" if market == Market.SPOT else "/fapi/v1/ping"
        await self._request(market, "GET", path)

    async def get_price(self, market: Market, symbol: str) -> Decimal:
        path = "/api/v3/ticker/price" if market == Market.SPOT else "/fapi/v1/ticker/price"
        data = await self._request(market, "GET", path, {"symbol": symbol})
        return Decimal(data["price"])

    async def get_rules(self, market: Market, symbol: str) -> SymbolRules:
        key = (market, symbol)
        if key in self._rules:
            return self._rules[key]
        path = "/api/v3/exchangeInfo" if market == Market.SPOT else "/fapi/v1/exchangeInfo"
        data = await self._request(market, "GET", path, {"symbol": symbol})
        symbols = data.get("symbols", [])
        if not symbols:
            raise ValidationError(f"币安没有找到交易对 {symbol}")
        item = symbols[0]
        if item.get("status") != "TRADING":
            raise ValidationError(f"交易对 {symbol} 当前不可交易")
        filters = {entry["filterType"]: entry for entry in item["filters"]}
        price_filter = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        min_notional = notional.get("minNotional") or notional.get("notional") or "0"
        rules = SymbolRules(
            tick_size=Decimal(price_filter["tickSize"]),
            step_size=Decimal(lot["stepSize"]),
            market_step_size=Decimal(market_lot.get("stepSize", lot["stepSize"])),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            min_notional=Decimal(min_notional),
            base_asset=item.get("baseAsset", ""),
            quote_asset=item.get("quoteAsset", ""),
        )
        self._rules[key] = rules
        return rules

    async def check_credentials(self, market: Market) -> None:
        if market == Market.SPOT:
            await self._request(
                market,
                "GET",
                "/api/v3/account",
                {"omitZeroBalances": "true"},
                signed=True,
            )
        else:
            await self._request(market, "GET", "/fapi/v3/balance", signed=True)

    async def ensure_safe_to_open(self, market: Market, symbol: str) -> None:
        if market == Market.SPOT:
            orders = await self._request(
                market, "GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True
            )
            if orders:
                raise ValidationError("该现货交易对已有挂单；为避免抢占余额，本工具拒绝叠加")
            return
        mode = await self._request(
            market, "GET", "/fapi/v1/positionSide/dual", signed=True
        )
        if mode.get("dualSidePosition") is True:
            raise ValidationError("当前是双向持仓模式；本版本仅支持单向持仓模式")
        if await self.get_futures_position_quantity(symbol) != 0:
            raise ValidationError("该合约已有持仓；本工具拒绝与现有仓位混合")
        orders = await self._request(
            market, "GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        )
        algos = await self._request(
            market, "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True
        )
        if orders or algos:
            raise ValidationError("该合约已有普通单或条件单；请先处理现有挂单")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            Market.FUTURES,
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    async def place_entry(self, plan: TradePlan, client_id: str) -> Dict[str, Any]:
        request = plan.request
        side = "BUY" if request.direction == Direction.LONG else "SELL"
        params: Dict[str, Any] = {
            "symbol": request.symbol,
            "side": side,
            "type": "MARKET" if request.entry_type == EntryType.MARKET else "LIMIT",
            "quantity": _text(plan.quantity),
            "newClientOrderId": client_id,
        }
        if request.entry_type == EntryType.LIMIT:
            params.update(timeInForce="GTC", price=_text(plan.entry_price))
        if request.market == Market.SPOT:
            params["newOrderRespType"] = "FULL"
            return await self._request(
                Market.SPOT, "POST", "/api/v3/order", params, signed=True
            )
        params.update(newOrderRespType="RESULT", positionSide="BOTH")
        return await self._request(
            Market.FUTURES, "POST", "/fapi/v1/order", params, signed=True
        )

    async def get_entry(
        self, market: Market, symbol: str, *, order_id: Optional[str] = None, client_id: str = ""
    ) -> Dict[str, Any]:
        if market == Market.SPOT:
            path = "/api/v3/order"
            params = {"symbol": symbol}
            params["orderId" if order_id else "origClientOrderId"] = order_id or client_id
        else:
            path = "/fapi/v1/order"
            params = {"symbol": symbol}
            params["orderId" if order_id else "origClientOrderId"] = order_id or client_id
        return await self._request(market, "GET", path, params, signed=True)

    async def cancel_entry(self, market: Market, symbol: str, order_id: str) -> Dict[str, Any]:
        path = "/api/v3/order" if market == Market.SPOT else "/fapi/v1/order"
        return await self._request(
            market, "DELETE", path, {"symbol": symbol, "orderId": order_id}, signed=True
        )

    async def place_futures_algo(
        self,
        *,
        symbol: str,
        direction: Direction,
        kind: str,
        quantity: Decimal,
        trigger_price: Decimal,
        client_id: str,
    ) -> Dict[str, Any]:
        side = "SELL" if direction == Direction.LONG else "BUY"
        return await self._request(
            Market.FUTURES,
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": side,
                "type": kind,
                "positionSide": "BOTH",
                "quantity": _text(quantity),
                "triggerPrice": _text(trigger_price),
                "workingType": "MARK_PRICE",
                "reduceOnly": "true",
                "clientAlgoId": client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    async def cancel_futures_algo(self, algo_id: str) -> None:
        await self._request(
            Market.FUTURES,
            "DELETE",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id},
            signed=True,
        )

    async def get_futures_algo(self, client_id: str) -> Dict[str, Any]:
        return await self._request(
            Market.FUTURES,
            "GET",
            "/fapi/v1/algoOrder",
            {"clientAlgoId": client_id},
            signed=True,
        )

    async def get_futures_position_quantity(self, symbol: str) -> Decimal:
        data = await self._request(
            Market.FUTURES,
            "GET",
            "/fapi/v3/positionRisk",
            {"symbol": symbol},
            signed=True,
        )
        rows = data if isinstance(data, list) else [data]
        both = next((row for row in rows if row.get("positionSide", "BOTH") == "BOTH"), None)
        return abs(Decimal(both["positionAmt"])) if both else Decimal("0")

    async def place_spot_oco(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        take_profit_price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal,
        list_client_id: str,
        take_profit_client_id: str,
        stop_client_id: str,
    ) -> Dict[str, Any]:
        return await self._request(
            Market.SPOT,
            "POST",
            "/api/v3/orderList/oco",
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": _text(quantity),
                "aboveType": "LIMIT_MAKER",
                "abovePrice": _text(take_profit_price),
                "belowType": "STOP_LOSS_LIMIT",
                "belowStopPrice": _text(stop_price),
                "belowPrice": _text(stop_limit_price),
                "belowTimeInForce": "GTC",
                "listClientOrderId": list_client_id,
                "aboveClientOrderId": take_profit_client_id,
                "belowClientOrderId": stop_client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    async def place_spot_stop(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        client_id: str,
    ) -> Dict[str, Any]:
        return await self._request(
            Market.SPOT,
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "timeInForce": "GTC",
                "quantity": _text(quantity),
                "stopPrice": _text(stop_price),
                "price": _text(limit_price),
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    async def get_spot_order(self, symbol: str, client_id: str) -> Dict[str, Any]:
        return await self._request(
            Market.SPOT,
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_id},
            signed=True,
        )

    async def get_spot_base_commission(
        self, symbol: str, order_id: str, base_asset: str
    ) -> Decimal:
        trades = await self._request(
            Market.SPOT,
            "GET",
            "/api/v3/myTrades",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )
        return sum(
            (
                Decimal(str(trade["commission"]))
                for trade in trades
                if trade.get("commissionAsset") == base_asset
            ),
            Decimal("0"),
        )

    async def cancel_spot_order(self, symbol: str, order_id: str) -> None:
        await self._request(
            Market.SPOT,
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    async def replace_spot_stop(
        self,
        *,
        symbol: str,
        cancel_order_id: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        client_id: str,
    ) -> Dict[str, Any]:
        return await self._request(
            Market.SPOT,
            "POST",
            "/api/v3/order/cancelReplace",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "cancelReplaceMode": "STOP_ON_FAILURE",
                "cancelOrderId": cancel_order_id,
                "timeInForce": "GTC",
                "quantity": _text(quantity),
                "stopPrice": _text(stop_price),
                "price": _text(limit_price),
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )

    async def emergency_close(
        self, market: Market, symbol: str, direction: Direction, quantity: Decimal, client_id: str
    ) -> Dict[str, Any]:
        side = "SELL" if direction == Direction.LONG else "BUY"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": _text(quantity),
            "newClientOrderId": client_id,
        }
        if market == Market.FUTURES:
            params.update(reduceOnly="true", positionSide="BOTH", newOrderRespType="RESULT")
            path = "/fapi/v1/order"
        else:
            params["newOrderRespType"] = "FULL"
            path = "/api/v3/order"
        return await self._request(market, "POST", path, params, signed=True)
