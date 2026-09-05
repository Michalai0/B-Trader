from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
from typing import Any

import pyotp
from dotenv import load_dotenv

from btrader.auth import Authorizer
from btrader.binance import BinanceClient
from btrader.config import Settings
from btrader.errors import BTraderError
from btrader.models import Market
from btrader.paper import PaperExchange
from btrader.secrets import SECRET_ENV_NAMES, SecretStore
from btrader.service import TradingService
from btrader.store import Store
from btrader.telegram_bot import TelegramController


def _raw_secret_store() -> SecretStore:
    load_dotenv(".env", override=False)
    backend = os.getenv("BTRADER_SECRET_BACKEND", "keyring").strip().lower()
    service = os.getenv("BTRADER_KEYRING_SERVICE", "btrader").strip()
    return SecretStore(backend, service)


def _credentials(secrets: SecretStore) -> dict[str, str]:
    common_key = secrets.get("binance-api-key", required=False)
    common_secret = secrets.get("binance-api-secret", required=False)
    values = {
        "spot_api_key": secrets.get("binance-spot-api-key", required=False) or common_key,
        "spot_api_secret": secrets.get("binance-spot-api-secret", required=False)
        or common_secret,
        "futures_api_key": secrets.get("binance-futures-api-key", required=False)
        or common_key,
        "futures_api_secret": secrets.get("binance-futures-api-secret", required=False)
        or common_secret,
    }
    if not all(values.values()):
        raise BTraderError(
            "缺少币安凭据；分别配置 spot/futures 凭据，或配置通用 binance-api-key/secret"
        )
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btrader", description="B-Trader Telegram bot")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动 Telegram 机器人")
    sub.add_parser("check", help="检查配置和交易所连通性")
    secrets = sub.add_parser("secrets", help="管理系统钥匙串中的密钥")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    set_parser = secrets_sub.add_parser("set", help="安全写入一个密钥")
    set_parser.add_argument("name", choices=sorted(SECRET_ENV_NAMES))
    secrets_sub.add_parser("status", help="只显示密钥是否存在")
    totp = sub.add_parser("totp", help="管理动态二次验证")
    totp_sub = totp.add_subparsers(dest="totp_command", required=True)
    totp_sub.add_parser("init", help="生成并保存 TOTP 密钥")
    return parser


def _build_runtime(settings: Settings) -> tuple[TelegramController, Any]:
    secrets = SecretStore(settings.secret_backend, settings.keyring_service)
    token = secrets.get("telegram-token")
    totp_secret = secrets.get("totp-secret")
    if settings.trading_mode == "paper":
        exchange = PaperExchange()
    else:
        exchange = BinanceClient(settings.trading_mode, **_credentials(secrets))
    store = Store(settings.data_dir / "btrader.db")
    authorizer = Authorizer(
        settings.allowed_user_ids, settings.allowed_chat_ids, totp_secret, store
    )
    service = TradingService(settings, exchange, store, authorizer)
    return TelegramController(token, settings, service, store, authorizer), exchange


async def _check(settings: Settings) -> None:
    secrets = SecretStore(settings.secret_backend, settings.keyring_service)
    secrets.get("telegram-token")
    secrets.get("totp-secret")
    if settings.trading_mode == "paper":
        exchange: Any = PaperExchange()
    else:
        exchange = BinanceClient(settings.trading_mode, **_credentials(secrets))
    try:
        await exchange.ping(Market.SPOT)
        await exchange.ping(Market.FUTURES)
        if settings.trading_mode != "paper":
            await exchange.check_credentials(Market.SPOT)
            await exchange.check_credentials(Market.FUTURES)
    finally:
        await exchange.close()


def main() -> None:
    args = _parser().parse_args()
    command = args.command or "run"
    try:
        if command == "secrets":
            store = _raw_secret_store()
            if args.secrets_command == "set":
                value = getpass.getpass(f"输入 {args.name}（不会回显）：")
                store.set(args.name, value)
                print(f"已将 {args.name} 保存到系统钥匙串")
            else:
                for name in sorted(SECRET_ENV_NAMES):
                    print(f"{name}: {'已配置' if store.get(name, required=False) else '缺失'}")
            return
        if command == "totp":
            store = _raw_secret_store()
            secret = pyotp.random_base32()
            store.set("totp-secret", secret)
            uri = pyotp.TOTP(secret).provisioning_uri(name="Telegram", issuer_name="B-Trader")
            print("TOTP 已保存到系统钥匙串。请立刻添加到验证器：")
            print(uri)
            print(f"手动密钥：{secret}")
            return

        settings = Settings.load()
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if command == "check":
            asyncio.run(_check(settings))
            print(f"配置有效，Binance 现货/合约连通正常；当前模式：{settings.trading_mode}")
            return
        controller, _exchange = _build_runtime(settings)
        controller.run()
    except (BTraderError, RuntimeError) as exc:
        raise SystemExit(f"错误：{exc}") from exc


if __name__ == "__main__":
    main()
