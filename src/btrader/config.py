from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv

from btrader.errors import ConfigurationError


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} 必须是 true 或 false")


def _decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ConfigurationError(f"{name} 不是有效数字") from exc


def _ids(name: str) -> FrozenSet[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    try:
        return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} 只能包含用逗号分隔的整数 ID") from exc


@dataclass(frozen=True)
class Settings:
    trading_mode: str
    live_trading_enabled: bool
    allowed_user_ids: FrozenSet[int]
    allowed_chat_ids: FrozenSet[int]
    secret_backend: str
    keyring_service: str
    max_risk_usdt: Decimal
    max_notional_usdt: Decimal
    max_leverage: int
    default_rr: Decimal
    default_take_profit_percent: Decimal
    default_protect_breakeven: bool
    confirm_ttl_seconds: int
    monitor_interval_seconds: int
    stop_limit_buffer_bps: Decimal
    fee_buffer_bps: Decimal
    data_dir: Path
    log_level: str

    @classmethod
    def load(cls, env_file: str = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        try:
            settings = cls(
                trading_mode=os.getenv("BTRADER_TRADING_MODE", "paper").strip().lower(),
                live_trading_enabled=_bool("BTRADER_LIVE_TRADING_ENABLED", False),
                allowed_user_ids=_ids("BTRADER_ALLOWED_USER_IDS"),
                allowed_chat_ids=_ids("BTRADER_ALLOWED_CHAT_IDS"),
                secret_backend=os.getenv("BTRADER_SECRET_BACKEND", "keyring").strip().lower(),
                keyring_service=os.getenv("BTRADER_KEYRING_SERVICE", "btrader").strip(),
                max_risk_usdt=_decimal("BTRADER_MAX_RISK_USDT", "500"),
                max_notional_usdt=_decimal("BTRADER_MAX_NOTIONAL_USDT", "10000"),
                max_leverage=int(os.getenv("BTRADER_MAX_LEVERAGE", "20")),
                default_rr=_decimal("BTRADER_DEFAULT_RR", "1.5"),
                default_take_profit_percent=_decimal(
                    "BTRADER_DEFAULT_TAKE_PROFIT_PERCENT", "75"
                ),
                default_protect_breakeven=_bool(
                    "BTRADER_DEFAULT_PROTECT_BREAKEVEN", True
                ),
                confirm_ttl_seconds=int(os.getenv("BTRADER_CONFIRM_TTL_SECONDS", "120")),
                monitor_interval_seconds=int(
                    os.getenv("BTRADER_MONITOR_INTERVAL_SECONDS", "2")
                ),
                stop_limit_buffer_bps=_decimal("BTRADER_STOP_LIMIT_BUFFER_BPS", "10"),
                fee_buffer_bps=_decimal("BTRADER_FEE_BUFFER_BPS", "0"),
                data_dir=Path(os.getenv("BTRADER_DATA_DIR", "data")).expanduser(),
                log_level=os.getenv("BTRADER_LOG_LEVEL", "INFO").upper(),
            )
        except ValueError as exc:
            raise ConfigurationError(f"配置中的整数无效：{exc}") from exc
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.trading_mode not in {"paper", "testnet", "live"}:
            raise ConfigurationError("BTRADER_TRADING_MODE 只能是 paper/testnet/live")
        if self.trading_mode == "live" and not self.live_trading_enabled:
            raise ConfigurationError(
                "实盘被安全锁阻止；确认无误后设置 BTRADER_LIVE_TRADING_ENABLED=true"
            )
        if self.secret_backend not in {"keyring", "env"}:
            raise ConfigurationError("BTRADER_SECRET_BACKEND 只能是 keyring 或 env")
        if self.max_risk_usdt <= 0 or self.max_notional_usdt <= 0:
            raise ConfigurationError("风险和名义价值上限必须大于 0")
        if self.max_leverage < 1 or self.max_leverage > 125:
            raise ConfigurationError("最大杠杆必须在 1 到 125 之间")
        if self.default_rr <= 0:
            raise ConfigurationError("默认盈亏比必须大于 0")
        if not Decimal("0") < self.default_take_profit_percent < Decimal("100"):
            raise ConfigurationError("默认止盈比例必须在 0 到 100 之间")
        if self.confirm_ttl_seconds < 30:
            raise ConfigurationError("确认有效期不能少于 30 秒")
        if self.monitor_interval_seconds < 1:
            raise ConfigurationError("监控间隔不能少于 1 秒")
        if not Decimal("0") <= self.stop_limit_buffer_bps <= Decimal("100"):
            raise ConfigurationError("现货止损限价缓冲必须在 0 到 100 bps 之间")
