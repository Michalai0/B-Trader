from __future__ import annotations

import re
import shlex
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from btrader.config import Settings
from btrader.errors import ValidationError
from btrader.models import Direction, EntryType, Market, TradeRequest

MARKETS = {
    "spot": Market.SPOT,
    "s": Market.SPOT,
    "现货": Market.SPOT,
    "futures": Market.FUTURES,
    "future": Market.FUTURES,
    "f": Market.FUTURES,
    "合约": Market.FUTURES,
}
DIRECTIONS = {
    "long": Direction.LONG,
    "buy": Direction.LONG,
    "多": Direction.LONG,
    "买": Direction.LONG,
    "short": Direction.SHORT,
    "sell": Direction.SHORT,
    "空": Direction.SHORT,
    "卖": Direction.SHORT,
}
KEYS = {
    "sl": "sl",
    "stop": "sl",
    "止损": "sl",
    "risk": "risk",
    "风险": "risk",
    "entry": "entry",
    "开仓": "entry",
    "rr": "rr",
    "盈亏比": "rr",
    "tp": "tp",
    "止盈比例": "tp",
    "protect": "protect",
    "保本": "protect",
    "leverage": "leverage",
    "lev": "leverage",
    "杠杆": "leverage",
}


def _decimal(name: str, value: str) -> Decimal:
    try:
        return Decimal(value.rstrip("uU%"))
    except InvalidOperation as exc:
        raise ValidationError(f"{name} 不是有效数字：{value}") from exc


def _bool(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on", "是", "开", "要"}:
        return True
    if normalized in {"0", "false", "no", "off", "否", "关", "不要"}:
        return False
    raise ValidationError(f"protect 只能是 yes/no：{value}")


def parse_trade_command(text: str, settings: Settings) -> TradeRequest:
    try:
        parts: List[str] = shlex.split(text)
    except ValueError as exc:
        raise ValidationError(f"命令引号不完整：{exc}") from exc
    if parts and parts[0].split("@", 1)[0].lower() in {"/trade", "trade"}:
        parts = parts[1:]
    if len(parts) < 3:
        raise ValidationError(
            "格式：/trade 合约 BTCUSDT 多 10x sl=60000 risk=100 entry=market"
        )
    try:
        market = MARKETS[parts[0].lower()]
        direction = DIRECTIONS[parts[2].lower()]
    except KeyError as exc:
        raise ValidationError("市场或方向无效；使用 现货/合约 和 多/空") from exc
    symbol = parts[1].upper().replace("/", "")
    if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
        raise ValidationError("交易对格式无效，例如 BTCUSDT")

    remaining = parts[3:]
    positional_leverage = None
    if remaining and "=" not in remaining[0]:
        positional_leverage = remaining.pop(0).lower().rstrip("x倍")
    values: Dict[str, str] = {}
    for token in remaining:
        if "=" not in token:
            raise ValidationError(f"参数必须使用 key=value：{token}")
        key, value = token.split("=", 1)
        normalized = KEYS.get(key.lower())
        if not normalized:
            raise ValidationError(f"未知参数：{key}")
        if normalized in values:
            raise ValidationError(f"参数重复：{key}")
        values[normalized] = value

    leverage_raw = values.get("leverage", positional_leverage or ("1" if market == Market.SPOT else ""))
    if not leverage_raw:
        raise ValidationError("合约命令必须提供杠杆，例如 10x 或 leverage=10")
    try:
        leverage = int(leverage_raw.lower().rstrip("x倍"))
    except ValueError as exc:
        raise ValidationError("杠杆必须是整数") from exc
    for required in ("sl", "risk", "entry"):
        if required not in values:
            raise ValidationError(f"缺少必填参数：{required}")

    entry_raw = values["entry"].lower()
    if entry_raw in {"market", "mkt", "市价", "实时"}:
        entry_type = EntryType.MARKET
        entry_price = None
    else:
        entry_type = EntryType.LIMIT
        entry_price = _decimal("entry", values["entry"])

    return TradeRequest(
        market=market,
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        stop_price=_decimal("sl", values["sl"]),
        risk_usdt=_decimal("risk", values["risk"]),
        entry_type=entry_type,
        entry_price=entry_price,
        reward_risk=_decimal("rr", values.get("rr", str(settings.default_rr))),
        take_profit_percent=_decimal(
            "tp", values.get("tp", str(settings.default_take_profit_percent))
        ),
        protect_breakeven=_bool(
            values.get("protect", "yes" if settings.default_protect_breakeven else "no")
        ),
    )

