from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from btrader.binance import BinanceClient
from btrader.errors import ValidationError
from btrader.models import Direction, EntryType, Market, SymbolRules, TradePlan


class PaperExchange:
    """In-process paper broker using live public Binance prices and symbol filters."""

    def __init__(self, public_client: Optional[BinanceClient] = None) -> None:
        self.public = public_client or BinanceClient("live")
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[tuple[Market, str], Decimal] = {}
        self._next_id = 1000

    async def close(self) -> None:
        await self.public.close()

    async def ping(self, market: Market) -> None:
        await self.public.ping(market)

    async def check_credentials(self, market: Market) -> None:
        return None

    async def get_price(self, market: Market, symbol: str) -> Decimal:
        return await self.public.get_price(market, symbol)

    async def get_rules(self, market: Market, symbol: str) -> SymbolRules:
        return await self.public.get_rules(market, symbol)

    async def ensure_safe_to_open(self, market: Market, symbol: str) -> None:
        if self.positions.get((market, symbol), Decimal("0")) != 0:
            raise ValidationError("模拟账户中该标的已有持仓")
        if any(
            order["market"] == market.value
            and order["symbol"] == symbol
            and order["status"] in {"NEW", "PARTIALLY_FILLED"}
            for order in self.orders.values()
        ):
            raise ValidationError("模拟账户中该标的已有挂单")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        return None

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    async def place_entry(self, plan: TradePlan, client_id: str) -> Dict[str, Any]:
        order_id = self._id()
        current = await self.get_price(plan.request.market, plan.request.symbol)
        filled = plan.request.entry_type == EntryType.MARKET or self._limit_crossed(
            plan.request.direction, current, plan.entry_price
        )
        price = current if plan.request.entry_type == EntryType.MARKET else plan.entry_price
        order = {
            "orderId": order_id,
            "clientOrderId": client_id,
            "market": plan.request.market.value,
            "symbol": plan.request.symbol,
            "direction": plan.request.direction.value,
            "kind": "ENTRY",
            "status": "FILLED" if filled else "NEW",
            "origQty": str(plan.quantity),
            "executedQty": str(plan.quantity if filled else Decimal("0")),
            "avgPrice": str(price if filled else Decimal("0")),
            "price": str(plan.entry_price),
            "cummulativeQuoteQty": str(plan.quantity * price if filled else Decimal("0")),
        }
        self.orders[order_id] = order
        self.orders[client_id] = order
        if filled:
            self.positions[(plan.request.market, plan.request.symbol)] = plan.quantity
        return dict(order)

    @staticmethod
    def _limit_crossed(direction: Direction, current: Decimal, limit: Decimal) -> bool:
        return current <= limit if direction == Direction.LONG else current >= limit

    async def get_entry(
        self, market: Market, symbol: str, *, order_id: Optional[str] = None, client_id: str = ""
    ) -> Dict[str, Any]:
        order = self.orders[order_id or client_id]
        if order["status"] == "NEW":
            current = await self.get_price(market, symbol)
            if self._limit_crossed(Direction(order["direction"]), current, Decimal(order["price"])):
                order["status"] = "FILLED"
                order["executedQty"] = order["origQty"]
                order["avgPrice"] = order["price"]
                order["cummulativeQuoteQty"] = str(
                    Decimal(order["origQty"]) * Decimal(order["price"])
                )
                self.positions[(market, symbol)] = Decimal(order["origQty"])
        return dict(order)

    async def cancel_entry(self, market: Market, symbol: str, order_id: str) -> Dict[str, Any]:
        order = self.orders[order_id]
        if order["status"] == "NEW":
            order["status"] = "CANCELED"
        return dict(order)

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
        order_id = self._id()
        order = {
            "algoId": order_id,
            "clientAlgoId": client_id,
            "market": Market.FUTURES.value,
            "symbol": symbol,
            "direction": direction.value,
            "kind": kind,
            "quantity": str(quantity),
            "triggerPrice": str(trigger_price),
            "status": "NEW",
        }
        self.orders[order_id] = order
        self.orders[client_id] = order
        return dict(order)

    async def cancel_futures_algo(self, algo_id: str) -> None:
        order = self.orders.get(str(algo_id))
        if order and order["status"] == "NEW":
            order["status"] = "CANCELED"

    async def get_futures_algo(self, client_id: str) -> Dict[str, Any]:
        return dict(self.orders[client_id])

    async def get_futures_position_quantity(self, symbol: str) -> Decimal:
        key = (Market.FUTURES, symbol)
        current = await self.get_price(Market.FUTURES, symbol)
        for order in list(self.orders.values()):
            if (
                order.get("market") != Market.FUTURES.value
                or order.get("symbol") != symbol
                or order.get("status") != "NEW"
                or order.get("kind") not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
            ):
                continue
            direction = Direction(order["direction"])
            trigger = Decimal(order["triggerPrice"])
            is_stop = order["kind"] == "STOP_MARKET"
            crossed = (
                current <= trigger
                if direction == Direction.LONG and is_stop
                else current >= trigger
                if direction == Direction.SHORT and is_stop
                else current >= trigger
                if direction == Direction.LONG
                else current <= trigger
            )
            if crossed:
                position = self.positions.get(key, Decimal("0"))
                self.positions[key] = max(Decimal("0"), position - Decimal(order["quantity"]))
                order["status"] = "FILLED"
        return self.positions.get(key, Decimal("0"))

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
        list_id = self._id()
        tp = self._spot_exit(
            symbol, quantity, take_profit_price, take_profit_client_id, "TAKE_PROFIT", list_id
        )
        stop = self._spot_exit(symbol, quantity, stop_price, stop_client_id, "STOP", list_id)
        self.orders[list_client_id] = {"orderListId": list_id, "status": "NEW"}
        return {"orderListId": list_id, "orders": [dict(tp), dict(stop)]}

    def _spot_exit(
        self,
        symbol: str,
        quantity: Decimal,
        trigger: Decimal,
        client_id: str,
        kind: str,
        list_id: str = "-1",
    ) -> Dict[str, Any]:
        order_id = self._id()
        order = {
            "orderId": order_id,
            "clientOrderId": client_id,
            "orderListId": list_id,
            "market": Market.SPOT.value,
            "symbol": symbol,
            "direction": Direction.LONG.value,
            "kind": kind,
            "triggerPrice": str(trigger),
            "origQty": str(quantity),
            "executedQty": "0",
            "status": "NEW",
        }
        self.orders[order_id] = order
        self.orders[client_id] = order
        return order

    async def place_spot_stop(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        limit_price: Decimal,
        client_id: str,
    ) -> Dict[str, Any]:
        return dict(self._spot_exit(symbol, quantity, stop_price, client_id, "STOP"))

    async def get_spot_order(self, symbol: str, client_id: str) -> Dict[str, Any]:
        order = self.orders[client_id]
        if order["status"] == "NEW":
            current = await self.get_price(Market.SPOT, symbol)
            trigger = Decimal(order["triggerPrice"])
            crossed = current >= trigger if order["kind"] == "TAKE_PROFIT" else current <= trigger
            if crossed:
                order["status"] = "FILLED"
                order["executedQty"] = order["origQty"]
                key = (Market.SPOT, symbol)
                self.positions[key] = max(
                    Decimal("0"),
                    self.positions.get(key, Decimal("0")) - Decimal(order["origQty"]),
                )
                list_id = order.get("orderListId")
                if list_id and list_id != "-1":
                    for sibling in self.orders.values():
                        if (
                            sibling is not order
                            and sibling.get("orderListId") == list_id
                            and sibling.get("status") == "NEW"
                        ):
                            sibling["status"] = "CANCELED"
        return dict(order)

    async def get_spot_base_commission(
        self, symbol: str, order_id: str, base_asset: str
    ) -> Decimal:
        return Decimal("0")

    async def cancel_spot_order(self, symbol: str, order_id: str) -> None:
        order = self.orders.get(str(order_id))
        if order and order["status"] == "NEW":
            order["status"] = "CANCELED"

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
        await self.cancel_spot_order(symbol, cancel_order_id)
        new_order = await self.place_spot_stop(
            symbol=symbol,
            quantity=quantity,
            stop_price=stop_price,
            limit_price=limit_price,
            client_id=client_id,
        )
        return {"cancelResult": "SUCCESS", "newOrderResult": "SUCCESS", "newOrderResponse": new_order}

    async def emergency_close(
        self, market: Market, symbol: str, direction: Direction, quantity: Decimal, client_id: str
    ) -> Dict[str, Any]:
        self.positions[(market, symbol)] = max(
            Decimal("0"), self.positions.get((market, symbol), Decimal("0")) - quantity
        )
        return {"orderId": self._id(), "clientOrderId": client_id, "status": "FILLED"}
