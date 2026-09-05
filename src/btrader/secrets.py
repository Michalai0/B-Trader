from __future__ import annotations

import os
from typing import Dict

import keyring

from btrader.errors import ConfigurationError

SECRET_ENV_NAMES: Dict[str, str] = {
    "telegram-token": "BTRADER_TELEGRAM_BOT_TOKEN",
    "binance-api-key": "BTRADER_BINANCE_API_KEY",
    "binance-api-secret": "BTRADER_BINANCE_API_SECRET",
    "binance-spot-api-key": "BTRADER_BINANCE_SPOT_API_KEY",
    "binance-spot-api-secret": "BTRADER_BINANCE_SPOT_API_SECRET",
    "binance-futures-api-key": "BTRADER_BINANCE_FUTURES_API_KEY",
    "binance-futures-api-secret": "BTRADER_BINANCE_FUTURES_API_SECRET",
    "totp-secret": "BTRADER_TOTP_SECRET",
}


class SecretStore:
    def __init__(self, backend: str, service: str) -> None:
        self.backend = backend
        self.service = service

    def get(self, name: str, required: bool = True) -> str:
        self._validate_name(name)
        if self.backend == "keyring":
            value = keyring.get_password(self.service, name)
        else:
            value = os.getenv(SECRET_ENV_NAMES[name])
        if required and not value:
            source = "系统钥匙串" if self.backend == "keyring" else "环境变量"
            raise ConfigurationError(f"{source}中缺少 {name}")
        return value or ""

    def set(self, name: str, value: str) -> None:
        self._validate_name(name)
        if self.backend != "keyring":
            raise ConfigurationError("只有 keyring 后端支持安全写入密钥")
        if not value:
            raise ConfigurationError("密钥不能为空")
        keyring.set_password(self.service, name, value)

    @staticmethod
    def _validate_name(name: str) -> None:
        if name not in SECRET_ENV_NAMES:
            allowed = ", ".join(sorted(SECRET_ENV_NAMES))
            raise ConfigurationError(f"未知密钥名；可用值：{allowed}")
