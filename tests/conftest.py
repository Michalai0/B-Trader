from decimal import Decimal

import pytest

from btrader.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        trading_mode="paper",
        live_trading_enabled=False,
        allowed_user_ids=frozenset({1001}),
        allowed_chat_ids=frozenset({2002}),
        secret_backend="env",
        keyring_service="btrader-test",
        max_risk_usdt=Decimal("500"),
        max_notional_usdt=Decimal("50000"),
        max_leverage=20,
        default_rr=Decimal("1.5"),
        default_take_profit_percent=Decimal("75"),
        default_protect_breakeven=True,
        confirm_ttl_seconds=120,
        monitor_interval_seconds=2,
        stop_limit_buffer_bps=Decimal("10"),
        fee_buffer_bps=Decimal("0"),
        data_dir=tmp_path,
        log_level="INFO",
    )

