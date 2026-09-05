from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from btrader.errors import ValidationError
from btrader.models import Direction, EntryType, Market, SymbolRules, TradePlan, TradeRequest

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _entry_price(request: TradeRequest, reference_price: Decimal, tick: Decimal) -> Decimal:
    if request.entry_type == EntryType.MARKET:
        return reference_price
    assert request.entry_price is not None
    if request.direction == Direction.LONG:
        return floor_to_step(request.entry_price, tick)
    return ceil_to_step(request.entry_price, tick)


def _stop_price(request: TradeRequest, entry: Decimal, tick: Decimal) -> Decimal:
    if request.direction == Direction.LONG:
        stop = ceil_to_step(request.stop_price, tick)
        if stop >= entry:
            raise ValidationError("做多的止损价必须低于开仓价")
        return stop
    stop = floor_to_step(request.stop_price, tick)
    if stop <= entry:
        raise ValidationError("做空的止损价必须高于开仓价")
    return stop


def _take_profit(direction: Direction, entry: Decimal, distance: Decimal, rr: Decimal, tick: Decimal) -> Decimal:
    raw = entry + distance * rr if direction == Direction.LONG else entry - distance * rr
    return floor_to_step(raw, tick) if direction == Direction.LONG else ceil_to_step(raw, tick)


def build_trade_plan(
    request: TradeRequest,
    reference_price: Decimal,
    rules: SymbolRules,
    *,
    max_risk_usdt: Decimal,
    max_notional_usdt: Decimal,
    max_leverage: int,
    fee_buffer_bps: Decimal = ZERO,
) -> TradePlan:
    if reference_price <= ZERO:
        raise ValidationError("市场参考价无效")
    if request.risk_usdt <= ZERO or request.risk_usdt > max_risk_usdt:
        raise ValidationError(f"止损金额必须大于 0 且不超过 {max_risk_usdt} USDT")
    if request.reward_risk <= ZERO:
        raise ValidationError("盈亏比必须大于 0")
    if not ZERO < request.take_profit_percent < HUNDRED:
        raise ValidationError("止盈仓位比例必须在 0 到 100 之间")
    if request.leverage < 1 or request.leverage > max_leverage:
        raise ValidationError(f"杠杆必须在 1 到 {max_leverage} 倍之间")
    if request.market == Market.SPOT and request.direction != Direction.LONG:
        raise ValidationError("现货模式只支持买入做多；做空请使用合约")
    if request.market == Market.SPOT and request.leverage != 1:
        raise ValidationError("现货杠杆必须为 1")

    entry = _entry_price(request, reference_price, rules.tick_size)
    stop = _stop_price(request, entry, rules.tick_size)
    distance = abs(entry - stop)
    fee_per_unit = entry * fee_buffer_bps / TEN_THOUSAND
    raw_quantity = request.risk_usdt / (distance + fee_per_unit)
    step = rules.market_step_size if request.entry_type == EntryType.MARKET else rules.step_size
    quantity = floor_to_step(raw_quantity, step)
    if quantity < rules.min_qty:
        raise ValidationError("按止损金额计算出的仓位小于交易所最小下单量")
    if quantity > rules.max_qty:
        raise ValidationError("按止损金额计算出的仓位大于交易所最大下单量")

    notional = quantity * entry
    if notional < rules.min_notional:
        raise ValidationError(
            f"名义价值 {notional.normalize()} USDT 小于交易所最低要求 {rules.min_notional}"
        )
    if notional > max_notional_usdt:
        raise ValidationError(
            f"名义价值 {notional.normalize()} USDT 超过本地上限 {max_notional_usdt}"
        )

    tp_quantity = floor_to_step(
        quantity * request.take_profit_percent / HUNDRED, rules.step_size
    )
    runner_quantity = floor_to_step(quantity - tp_quantity, rules.step_size)
    if tp_quantity < rules.min_qty or runner_quantity < rules.min_qty:
        raise ValidationError("75% 止盈腿或剩余仓位小于交易所最小下单量；请提高风险金额")
    if request.market == Market.SPOT:
        lower_price = min(entry, stop)
        if (
            tp_quantity * lower_price < rules.min_notional
            or runner_quantity * lower_price < rules.min_notional
        ):
            raise ValidationError("现货止盈腿或剩余保护腿低于交易所最低名义价值")

    warnings = []
    distance_ratio = distance / entry
    if request.market == Market.FUTURES and distance_ratio * Decimal(request.leverage) >= Decimal("0.8"):
        raise ValidationError("止损距离相对杠杆过大，可能在止损前被强平；请降低杠杆或收紧止损")
    if request.entry_type == EntryType.MARKET:
        warnings.append("市价成交会有滑点，实际最大亏损可能高于预览")
    if fee_buffer_bps == ZERO:
        warnings.append("止损金额未计手续费、资金费和滑点")

    tp_price = _take_profit(
        request.direction, entry, distance, request.reward_risk, rules.tick_size
    )
    price_loss = distance * quantity
    fee_buffer = fee_per_unit * quantity
    return TradePlan(
        request=request,
        reference_price=reference_price,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=tp_price,
        quantity=quantity,
        take_profit_quantity=tp_quantity,
        runner_quantity=runner_quantity,
        notional_usdt=notional,
        estimated_margin_usdt=notional / Decimal(request.leverage),
        estimated_price_loss_usdt=price_loss,
        estimated_fee_buffer_usdt=fee_buffer,
        warnings=tuple(warnings),
    )
