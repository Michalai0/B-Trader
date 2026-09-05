from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from btrader.auth import Authorizer
from btrader.config import Settings
from btrader.errors import AmbiguousExchangeError, ExchangeError, ValidationError
from btrader.models import (
    Direction,
    EntryType,
    Market,
    TradePlan,
    TradeRequest,
    TradeState,
)
from btrader.risk import build_trade_plan, ceil_to_step, floor_to_step
from btrader.store import Store

TERMINAL_ORDER_STATES = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


class TradingService:
    def __init__(
        self,
        settings: Settings,
        exchange: Any,
        store: Store,
        authorizer: Authorizer,
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.store = store
        self.authorizer = authorizer

    async def create_preview(
        self, request: TradeRequest, user_id: int, chat_id: int
    ) -> Tuple[str, TradePlan]:
        self.authorizer.require_allowed(user_id, chat_id)
        if self.store.has_active_symbol(request.market.value, request.symbol):
            raise ValidationError("该市场/交易对已有一个由本工具管理的活动交易")
        reference = await self.exchange.get_price(request.market, request.symbol)
        rules = await self.exchange.get_rules(request.market, request.symbol)
        plan = build_trade_plan(
            request,
            reference,
            rules,
            max_risk_usdt=self.settings.max_risk_usdt,
            max_notional_usdt=self.settings.max_notional_usdt,
            max_leverage=self.settings.max_leverage,
            fee_buffer_bps=self.settings.fee_buffer_bps,
        )
        proposal_id = uuid.uuid4().hex[:10]
        self.store.save_proposal(
            proposal_id,
            user_id,
            chat_id,
            int(time.time()) + self.settings.confirm_ttl_seconds,
            plan.to_dict(),
        )
        return proposal_id, plan

    async def confirm(
        self, proposal_id: str, totp_code: str, user_id: int, chat_id: int
    ) -> Dict[str, Any]:
        self.authorizer.require_allowed(user_id, chat_id)
        self.authorizer.verify_totp_once(totp_code)
        payload = self.store.consume_proposal(proposal_id, user_id, chat_id)
        plan = self._plan_from_dict(payload)
        if self.store.has_active_symbol(plan.request.market.value, plan.request.symbol):
            raise ValidationError("该市场/交易对已有活动交易，确认单不再有效")
        await self.exchange.ensure_safe_to_open(plan.request.market, plan.request.symbol)

        trade = self._new_trade(proposal_id, plan, user_id, chat_id)
        self.store.save_trade(trade)
        try:
            if plan.request.market == Market.FUTURES:
                await self.exchange.set_leverage(plan.request.symbol, plan.request.leverage)
            response = await self.exchange.place_entry(plan, trade["entry_client_id"])
            self._apply_entry_response(trade, response)
        except AmbiguousExchangeError as exc:
            trade["state"] = TradeState.WAITING_ENTRY.value
            trade["order_status_unknown"] = True
            trade["last_error"] = str(exc)
            self.store.save_trade(trade)
            return trade
        except Exception as exc:
            trade["state"] = TradeState.FAILED.value
            trade["last_error"] = str(exc)
            self.store.save_trade(trade)
            raise

        self.store.save_trade(trade)
        if trade["entry_status"] == "FILLED":
            try:
                await self._protect_filled_trade(trade)
            except Exception:
                return self.store.get_trade(trade["id"]) or trade
        elif trade["entry_status"] == "PARTIALLY_FILLED":
            try:
                await self._cancel_remainder_and_protect(trade)
            except Exception:
                return self.store.get_trade(trade["id"]) or trade
        return trade

    def _new_trade(
        self, trade_id: str, plan: TradePlan, user_id: int, chat_id: int
    ) -> Dict[str, Any]:
        request = plan.request
        return {
            "id": trade_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "mode": self.settings.trading_mode,
            "market": request.market.value,
            "symbol": request.symbol,
            "direction": request.direction.value,
            "leverage": request.leverage,
            "entry_type": request.entry_type.value,
            "requested_entry": str(request.entry_price) if request.entry_price else "market",
            "planned_entry": str(plan.entry_price),
            "stop_price": str(plan.stop_price),
            "risk_usdt": str(request.risk_usdt),
            "reward_risk": str(request.reward_risk),
            "take_profit_percent": str(request.take_profit_percent),
            "protect_breakeven": request.protect_breakeven,
            "planned_quantity": str(plan.quantity),
            "quantity": "0",
            "actual_entry": "0",
            "take_profit_price": str(plan.take_profit_price),
            "take_profit_quantity": str(plan.take_profit_quantity),
            "runner_quantity": str(plan.runner_quantity),
            "state": "executing",
            "entry_client_id": f"bt-e-{trade_id}",
            "entry_order_id": "",
            "entry_status": "NEW",
            "stop_order_id": "",
            "tp_order_id": "",
            "runner_stop_order_id": "",
            "tp_client_id": f"bt-tp-{trade_id}",
            "stop_client_id": f"bt-sl-{trade_id}",
            "runner_stop_client_id": f"bt-rs-{trade_id}",
        }

    @staticmethod
    def _apply_entry_response(trade: Dict[str, Any], response: Dict[str, Any]) -> None:
        trade["entry_order_id"] = str(response.get("orderId", trade.get("entry_order_id", "")))
        trade["entry_status"] = response.get("status", "NEW").upper()
        executed = Decimal(str(response.get("executedQty", "0")))
        trade["quantity"] = str(executed)
        if executed > 0:
            avg = Decimal(str(response.get("avgPrice", "0")))
            if avg <= 0:
                quote = Decimal(str(response.get("cummulativeQuoteQty", "0")))
                avg = quote / executed if quote > 0 else Decimal(str(response.get("price", "0")))
            trade["actual_entry"] = str(avg)
        trade["state"] = TradeState.WAITING_ENTRY.value
        trade.pop("order_status_unknown", None)

    async def _cancel_remainder_and_protect(self, trade: Dict[str, Any]) -> None:
        market = Market(trade["market"])
        await self.exchange.cancel_entry(market, trade["symbol"], trade["entry_order_id"])
        response = await self.exchange.get_entry(
            market,
            trade["symbol"],
            order_id=trade["entry_order_id"] or None,
            client_id=trade["entry_client_id"],
        )
        self._apply_entry_response(trade, response)
        if Decimal(trade["quantity"]) > 0:
            await self._protect_filled_trade(trade)
        else:
            trade["state"] = TradeState.CANCELLED.value
            self.store.save_trade(trade)

    async def _protect_filled_trade(self, trade: Dict[str, Any]) -> None:
        market = Market(trade["market"])
        direction = Direction(trade["direction"])
        rules = await self.exchange.get_rules(market, trade["symbol"])
        gross_quantity = Decimal(trade["quantity"])
        if market == Market.SPOT:
            base_commission = await self.exchange.get_spot_base_commission(
                trade["symbol"], trade["entry_order_id"], rules.base_asset
            )
            trade["base_asset_commission"] = str(base_commission)
            gross_quantity -= base_commission
        quantity = floor_to_step(gross_quantity, rules.step_size)
        entry = Decimal(trade["actual_entry"])
        stop = Decimal(trade["stop_price"])
        if quantity <= 0 or entry <= 0:
            raise ExchangeError("成交回报缺少有效数量或成交均价")
        if (direction == Direction.LONG and stop >= entry) or (
            direction == Direction.SHORT and stop <= entry
        ):
            await self._emergency_close(trade, quantity, "实际成交价已经越过止损价")
            return

        distance = abs(entry - stop)
        rr = Decimal(trade["reward_risk"])
        raw_tp = entry + distance * rr if direction == Direction.LONG else entry - distance * rr
        tp_price = (
            floor_to_step(raw_tp, rules.tick_size)
            if direction == Direction.LONG
            else ceil_to_step(raw_tp, rules.tick_size)
        )
        tp_qty = floor_to_step(
            quantity * Decimal(trade["take_profit_percent"]) / Decimal("100"),
            rules.step_size,
        )
        runner_qty = floor_to_step(quantity - tp_qty, rules.step_size)
        if tp_qty < rules.min_qty or runner_qty < rules.min_qty:
            await self._emergency_close(trade, quantity, "实际成交数量无法拆成止盈腿和保护腿")
            return
        if market == Market.SPOT and (
            tp_qty * min(entry, stop) < rules.min_notional
            or runner_qty * min(entry, stop) < rules.min_notional
        ):
            await self._emergency_close(
                trade, quantity, "实际成交数量拆分后低于现货最小名义价值"
            )
            return

        trade.update(
            quantity=str(quantity),
            take_profit_price=str(tp_price),
            take_profit_quantity=str(tp_qty),
            runner_quantity=str(runner_qty),
            actual_risk_usdt=str(quantity * distance),
        )
        if market == Market.FUTURES:
            await self._protect_futures(trade, direction, quantity, tp_qty, stop, tp_price)
        else:
            await self._protect_spot(trade, quantity, tp_qty, runner_qty, stop, tp_price, rules)

    async def _protect_futures(
        self,
        trade: Dict[str, Any],
        direction: Direction,
        quantity: Decimal,
        tp_qty: Decimal,
        stop: Decimal,
        tp_price: Decimal,
    ) -> None:
        try:
            stop_order = await self._existing_futures_algo(trade["stop_client_id"])
            if stop_order is None:
                stop_order = await self.exchange.place_futures_algo(
                    symbol=trade["symbol"],
                    direction=direction,
                    kind="STOP_MARKET",
                    quantity=quantity,
                    trigger_price=stop,
                    client_id=trade["stop_client_id"],
                )
            trade["stop_order_id"] = str(stop_order.get("algoId", ""))
        except AmbiguousExchangeError:
            try:
                stop_order = await self._resolve_with_retries(
                    self.exchange.get_futures_algo, trade["stop_client_id"]
                )
                trade["stop_order_id"] = str(stop_order.get("algoId", ""))
            except Exception:
                await self._emergency_close(trade, quantity, "合约止损单状态未知")
                raise
        except Exception:
            await self._emergency_close(trade, quantity, "合约止损单创建失败")
            raise

        try:
            tp_order = await self._existing_futures_algo(trade["tp_client_id"])
            if tp_order is None:
                tp_order = await self.exchange.place_futures_algo(
                    symbol=trade["symbol"],
                    direction=direction,
                    kind="TAKE_PROFIT_MARKET",
                    quantity=tp_qty,
                    trigger_price=tp_price,
                    client_id=trade["tp_client_id"],
                )
            trade["tp_order_id"] = str(tp_order.get("algoId", ""))
            trade.pop("tp_setup_error", None)
        except AmbiguousExchangeError as exc:
            try:
                tp_order = await self._resolve_with_retries(
                    self.exchange.get_futures_algo, trade["tp_client_id"]
                )
                trade["tp_order_id"] = str(tp_order.get("algoId", ""))
                trade.pop("tp_setup_error", None)
            except Exception:
                trade["tp_setup_error"] = str(exc)
        except Exception as exc:
            trade["tp_setup_error"] = str(exc)
        trade["state"] = TradeState.PROTECTED.value
        self.store.save_trade(trade)

    async def _protect_spot(
        self,
        trade: Dict[str, Any],
        quantity: Decimal,
        tp_qty: Decimal,
        runner_qty: Decimal,
        stop: Decimal,
        tp_price: Decimal,
        rules: Any,
        ) -> None:
        stop_limit = floor_to_step(
            stop * (Decimal("1") - self.settings.stop_limit_buffer_bps / Decimal("10000")),
            rules.tick_size,
        )
        try:
            tp_order = await self._existing_spot_order(
                trade["symbol"], trade["tp_client_id"]
            )
            if tp_order is None:
                oco = await self.exchange.place_spot_oco(
                    symbol=trade["symbol"],
                    quantity=tp_qty,
                    take_profit_price=tp_price,
                    stop_price=stop,
                    stop_limit_price=stop_limit,
                    list_client_id=f"bt-oco-{trade['id']}",
                    take_profit_client_id=trade["tp_client_id"],
                    stop_client_id=trade["stop_client_id"],
                )
                trade["oco_order_list_id"] = str(oco.get("orderListId", ""))
            else:
                trade["oco_order_list_id"] = str(tp_order.get("orderListId", ""))
        except AmbiguousExchangeError:
            try:
                tp_order = await self._resolve_with_retries(
                    self.exchange.get_spot_order,
                    trade["symbol"],
                    trade["tp_client_id"],
                )
                trade["oco_order_list_id"] = str(tp_order.get("orderListId", ""))
            except Exception:
                await self._emergency_close(trade, quantity, "现货 OCO 保护单状态未知")
                raise
        except Exception:
            await self._emergency_close(trade, quantity, "现货 OCO 保护单创建失败")
            raise
        try:
            runner_stop = await self._existing_spot_order(
                trade["symbol"], trade["runner_stop_client_id"]
            )
            if runner_stop is None:
                runner_stop = await self.exchange.place_spot_stop(
                    symbol=trade["symbol"],
                    quantity=runner_qty,
                    stop_price=stop,
                    limit_price=stop_limit,
                    client_id=trade["runner_stop_client_id"],
                )
            trade["runner_stop_order_id"] = str(runner_stop.get("orderId", ""))
        except AmbiguousExchangeError as exc:
            try:
                runner_stop = await self._resolve_with_retries(
                    self.exchange.get_spot_order,
                    trade["symbol"],
                    trade["runner_stop_client_id"],
                )
                trade["runner_stop_order_id"] = str(runner_stop.get("orderId", ""))
            except Exception:
                await self.exchange.emergency_close(
                    Market.SPOT,
                    trade["symbol"],
                    Direction.LONG,
                    runner_qty,
                    f"bt-xr-{trade['id']}",
                )
                trade["runner_quantity"] = "0"
                trade["runner_setup_error"] = str(exc)
        except Exception as exc:
            await self.exchange.emergency_close(
                Market.SPOT,
                trade["symbol"],
                Direction.LONG,
                runner_qty,
                f"bt-xr-{trade['id']}",
            )
            trade["runner_quantity"] = "0"
            trade["runner_setup_error"] = str(exc)
        trade["state"] = TradeState.PROTECTED.value
        self.store.save_trade(trade)

    async def _existing_futures_algo(self, client_id: str) -> Any:
        try:
            return await self.exchange.get_futures_algo(client_id)
        except ExchangeError as exc:
            if "-2013" in str(exc):
                return None
            raise
        except KeyError:
            return None

    async def _existing_spot_order(self, symbol: str, client_id: str) -> Any:
        try:
            return await self.exchange.get_spot_order(symbol, client_id)
        except ExchangeError as exc:
            if "-2013" in str(exc) or "-2011" in str(exc):
                return None
            raise
        except KeyError:
            return None

    @staticmethod
    async def _resolve_with_retries(call: Any, *args: Any) -> Dict[str, Any]:
        last_error: Any = None
        for _ in range(3):
            try:
                return await call(*args)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.2)
        assert last_error is not None
        raise last_error

    async def _emergency_close(
        self, trade: Dict[str, Any], quantity: Decimal, reason: str
    ) -> None:
        try:
            await self.exchange.emergency_close(
                Market(trade["market"]),
                trade["symbol"],
                Direction(trade["direction"]),
                quantity,
                f"bt-x-{trade['id']}",
            )
            trade["state"] = TradeState.FAILED.value
            trade["last_error"] = f"{reason}，已市价紧急平仓"
        except Exception as exc:
            trade["state"] = TradeState.FAILED.value
            trade["last_error"] = f"{reason}，且紧急平仓失败：{exc}；请立即人工处理"
        self.store.save_trade(trade)

    async def monitor_all(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for trade in self.store.active_trades():
            try:
                message = await self._reconcile(trade)
                if message:
                    events.append({"chat_id": trade["chat_id"], "message": message})
                trade.pop("last_monitor_error", None)
            except Exception as exc:
                now = int(time.time())
                last = int(trade.get("last_error_notified_at", 0))
                trade["last_monitor_error"] = str(exc)
                if now - last >= 60:
                    trade["last_error_notified_at"] = now
                    events.append(
                        {
                            "chat_id": trade["chat_id"],
                            "message": f"⚠️ {trade['id']} 监控异常：{exc}",
                        }
                    )
                self.store.save_trade(trade)
        return events

    async def _reconcile(self, trade: Dict[str, Any]) -> str:
        state = trade["state"]
        if state == "executing":
            try:
                return await self._reconcile_entry(trade)
            except ExchangeError as exc:
                age = int(time.time()) - int(trade.get("created_at", int(time.time())))
                if age >= 60 and "-2013" in str(exc):
                    trade["state"] = TradeState.FAILED.value
                    trade["last_error"] = "进程在提交开仓单时中断，币安未找到该客户端订单号"
                    self.store.save_trade(trade)
                    return f"⚠️ {trade['id']} 未找到开仓单，已停止自动管理"
                return ""
        if state == TradeState.WAITING_ENTRY.value:
            return await self._reconcile_entry(trade)
        if Market(trade["market"]) == Market.FUTURES:
            return await self._reconcile_futures(trade)
        return await self._reconcile_spot(trade)

    async def _reconcile_entry(self, trade: Dict[str, Any]) -> str:
        market = Market(trade["market"])
        response = await self.exchange.get_entry(
            market,
            trade["symbol"],
            order_id=trade["entry_order_id"] or None,
            client_id=trade["entry_client_id"],
        )
        self._apply_entry_response(trade, response)
        status = trade["entry_status"]
        if status == "PARTIALLY_FILLED":
            await self._cancel_remainder_and_protect(trade)
            return f"🛡 {trade['id']} 部分成交后已撤销余单，并按实际成交量设置保护"
        if status == "FILLED":
            await self._protect_filled_trade(trade)
            return f"🛡 {trade['id']} 已成交，止损和止盈保护已设置"
        if status in TERMINAL_ORDER_STATES:
            trade["state"] = TradeState.CANCELLED.value
            self.store.save_trade(trade)
            return f"ℹ️ {trade['id']} 开仓单未成交并已结束：{status}"
        self.store.save_trade(trade)
        return ""

    async def _reconcile_futures(self, trade: Dict[str, Any]) -> str:
        quantity = await self.exchange.get_futures_position_quantity(trade["symbol"])
        if quantity <= 0:
            await self._cleanup_futures_orders(trade)
            trade["state"] = TradeState.CLOSED.value
            self.store.save_trade(trade)
            return f"✅ {trade['id']} 合约持仓已平仓"

        if trade["state"] == TradeState.PROTECTED.value:
            runner = Decimal(trade["runner_quantity"])
            step = (await self.exchange.get_rules(Market.FUTURES, trade["symbol"])).step_size
            if quantity <= runner + step / 2 and quantity < Decimal(trade["quantity"]):
                if trade["protect_breakeven"]:
                    new_stop = await self.exchange.place_futures_algo(
                        symbol=trade["symbol"],
                        direction=Direction(trade["direction"]),
                        kind="STOP_MARKET",
                        quantity=quantity,
                        trigger_price=Decimal(trade["actual_entry"]),
                        client_id=f"bt-be-{trade['id']}",
                    )
                    old_stop = trade["stop_order_id"]
                    trade["stop_order_id"] = str(new_stop.get("algoId", ""))
                    if old_stop:
                        try:
                            await self.exchange.cancel_futures_algo(old_stop)
                        except ExchangeError:
                            pass
                trade["state"] = TradeState.RUNNER.value
                self.store.save_trade(trade)
                action = "且剩余止损已推到开仓价" if trade["protect_breakeven"] else ""
                return f"🎯 {trade['id']} 已止盈部分仓位{action}"
        self.store.save_trade(trade)
        return ""

    async def _cleanup_futures_orders(self, trade: Dict[str, Any]) -> None:
        for key in ("stop_order_id", "tp_order_id"):
            order_id = trade.get(key)
            if order_id:
                try:
                    await self.exchange.cancel_futures_algo(order_id)
                except ExchangeError:
                    pass

    async def _reconcile_spot(self, trade: Dict[str, Any]) -> str:
        tp = await self.exchange.get_spot_order(trade["symbol"], trade["tp_client_id"])
        oco_stop = await self.exchange.get_spot_order(
            trade["symbol"], trade["stop_client_id"]
        )
        runner_client = (
            f"bt-be-{trade['id']}"
            if trade["state"] == TradeState.RUNNER.value and trade["protect_breakeven"]
            else trade["runner_stop_client_id"]
        )
        runner = None
        if Decimal(trade["runner_quantity"]) > 0:
            runner = await self.exchange.get_spot_order(trade["symbol"], runner_client)

        if tp.get("status") == "FILLED" and trade["state"] == TradeState.PROTECTED.value:
            if trade["protect_breakeven"] and runner:
                rules = await self.exchange.get_rules(Market.SPOT, trade["symbol"])
                entry = Decimal(trade["actual_entry"])
                limit_price = floor_to_step(
                    entry
                    * (
                        Decimal("1")
                        - self.settings.stop_limit_buffer_bps / Decimal("10000")
                    ),
                    rules.tick_size,
                )
                replaced = await self.exchange.replace_spot_stop(
                    symbol=trade["symbol"],
                    cancel_order_id=trade["runner_stop_order_id"],
                    quantity=Decimal(trade["runner_quantity"]),
                    stop_price=entry,
                    limit_price=limit_price,
                    client_id=f"bt-be-{trade['id']}",
                )
                new_response = replaced.get("newOrderResponse", replaced)
                trade["runner_stop_order_id"] = str(new_response.get("orderId", ""))
            trade["state"] = (
                TradeState.RUNNER.value
                if Decimal(trade["runner_quantity"]) > 0
                else TradeState.CLOSED.value
            )
            self.store.save_trade(trade)
            action = "，剩余止损已推到开仓价" if trade["protect_breakeven"] else ""
            return f"🎯 {trade['id']} 已止盈部分仓位{action}"

        runner_closed = runner and runner.get("status") == "FILLED"
        all_stopped = runner_closed and oco_stop.get("status") == "FILLED"
        runner_after_tp_closed = runner_closed and trade["state"] == TradeState.RUNNER.value
        if all_stopped or runner_after_tp_closed:
            trade["state"] = TradeState.CLOSED.value
            self.store.save_trade(trade)
            return f"✅ {trade['id']} 现货仓位已由止损单卖出"
        self.store.save_trade(trade)
        return ""

    async def cancel(self, item_id: str, user_id: int, chat_id: int) -> str:
        self.authorizer.require_allowed(user_id, chat_id)
        trade = self.store.get_trade(item_id)
        if trade is None:
            if self.store.cancel_proposal(item_id, user_id, chat_id):
                return "确认单已取消"
            raise ValidationError("没有找到可取消的确认单或交易")
        if trade["user_id"] != user_id or trade["chat_id"] != chat_id:
            raise ValidationError("这不是你的交易")
        if trade["state"] != TradeState.WAITING_ENTRY.value:
            raise ValidationError("只能取消尚未完成成交的开仓挂单")
        await self._cancel_remainder_and_protect(trade)
        return "余下开仓挂单已撤销；若已有部分成交，保护单已自动设置"

    @staticmethod
    def _plan_from_dict(data: Dict[str, Any]) -> TradePlan:
        request_data = data["request"]
        request = TradeRequest(
            market=Market(request_data["market"]),
            symbol=request_data["symbol"],
            direction=Direction(request_data["direction"]),
            leverage=int(request_data["leverage"]),
            stop_price=Decimal(request_data["stop_price"]),
            risk_usdt=Decimal(request_data["risk_usdt"]),
            entry_type=EntryType(request_data["entry_type"]),
            entry_price=Decimal(request_data["entry_price"])
            if request_data.get("entry_price")
            else None,
            reward_risk=Decimal(request_data["reward_risk"]),
            take_profit_percent=Decimal(request_data["take_profit_percent"]),
            protect_breakeven=bool(request_data["protect_breakeven"]),
        )
        return TradePlan(
            request=request,
            reference_price=Decimal(data["reference_price"]),
            entry_price=Decimal(data["entry_price"]),
            stop_price=Decimal(data["stop_price"]),
            take_profit_price=Decimal(data["take_profit_price"]),
            quantity=Decimal(data["quantity"]),
            take_profit_quantity=Decimal(data["take_profit_quantity"]),
            runner_quantity=Decimal(data["runner_quantity"]),
            notional_usdt=Decimal(data["notional_usdt"]),
            estimated_margin_usdt=Decimal(data["estimated_margin_usdt"]),
            estimated_price_loss_usdt=Decimal(data["estimated_price_loss_usdt"]),
            estimated_fee_buffer_usdt=Decimal(data["estimated_fee_buffer_usdt"]),
            warnings=tuple(data.get("warnings", [])),
        )


async def resolve_ambiguous_entry(
    exchange: Any,
    market: Market,
    symbol: str,
    client_id: str,
    attempts: int = 3,
) -> Dict[str, Any]:
    """Helper available to callers that need immediate best-effort ambiguity resolution."""
    last_error: Any = None
    for _ in range(attempts):
        try:
            return await exchange.get_entry(market, symbol, client_id=client_id)
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    assert last_error is not None
    raise last_error
