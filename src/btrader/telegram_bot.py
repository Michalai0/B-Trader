from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from btrader.auth import Authorizer
from btrader.config import Settings
from btrader.errors import BTraderError
from btrader.models import TradePlan
from btrader.parser import parse_trade_command
from btrader.service import TradingService
from btrader.store import Store

LOGGER = logging.getLogger(__name__)

HELP_TEXT = """B-Trader 命令

/trade 合约 BTCUSDT 多 10x sl=60000 risk=100 entry=market
/trade 合约 ETHUSDT 空 5x sl=4200 risk=100 entry=4000 rr=2 tp=60 protect=no
/trade 现货 BTCUSDT 买 sl=60000 risk=100 entry=61000

必填：市场、标的、方向、合约杠杆、sl、risk、entry。
entry=market 为市价；数字为 GTC 限价。默认 rr=1.5、tp=75、protect=yes。

/confirm <确认单ID> <动态验证码>  二次确认并下单
/cancel <确认单ID或交易ID>       取消确认或未完全成交的开仓挂单
/status                         查看最近交易
/whoami                         查看 Telegram user/chat ID
/help                           显示帮助
"""


def _number(value: Decimal, places: int = 8) -> str:
    result = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return result or "0"


def format_preview(proposal_id: str, plan: TradePlan, mode: str, ttl: int) -> str:
    request = plan.request
    market = "合约" if request.market.value == "futures" else "现货"
    direction = "多" if request.direction.value == "long" else "空"
    entry_kind = "市价（参考价）" if request.entry_type.value == "market" else "限价"
    protect = "是" if request.protect_breakeven else "否"
    warnings = "\n".join(f"⚠️ {item}" for item in plan.warnings)
    mode_label = {"paper": "模拟", "testnet": "测试网", "live": "实盘"}[mode]
    return (
        f"📋 下单预览 [{mode_label}]\n"
        f"确认单：`{proposal_id}`（{ttl} 秒内有效）\n"
        f"{market} {request.symbol} {direction} {request.leverage}x\n"
        f"开仓：{entry_kind} {_number(plan.entry_price)}\n"
        f"止损：{_number(plan.stop_price)}\n"
        f"止盈：{_number(plan.take_profit_price)}（{request.reward_risk}R，平 {request.take_profit_percent}%）\n"
        f"数量：{_number(plan.quantity)}；剩余：{_number(plan.runner_quantity)}\n"
        f"名义价值：{_number(plan.notional_usdt, 2)} USDT\n"
        f"预计保证金：{_number(plan.estimated_margin_usdt, 2)} USDT\n"
        f"价格止损亏损：{_number(plan.estimated_price_loss_usdt, 2)} USDT\n"
        f"止盈后推保本：{protect}\n"
        f"{warnings}\n\n"
        f"确认：`/confirm {proposal_id} 123456`（将 123456 换成验证器动态码）"
    )


def format_trade(trade: Dict[str, Any]) -> str:
    state_names = {
        "executing": "提交中",
        "waiting_entry": "等待开仓成交",
        "protected": "持仓已保护",
        "runner": "止盈后剩余仓位",
        "closed": "已结束",
        "cancelled": "已取消",
        "failed": "失败",
    }
    text = (
        f"{trade['id']} {trade['market']} {trade['symbol']} {trade['direction']} "
        f"{trade['leverage']}x — {state_names.get(trade['state'], trade['state'])}"
    )
    if Decimal(str(trade.get("actual_entry", "0"))) > 0:
        text += (
            f"\n成交 {_number(Decimal(trade['actual_entry']))} × "
            f"{_number(Decimal(trade['quantity']))}；止损 {trade['stop_price']}；"
            f"止盈 {trade['take_profit_price']}"
        )
    if trade.get("tp_setup_error"):
        text += "\n⚠️ 止损已设置，但止盈单创建失败，请人工检查"
    if trade.get("last_error"):
        text += f"\n⚠️ {trade['last_error']}"
    return text


class TelegramController:
    def __init__(
        self,
        token: str,
        settings: Settings,
        service: TradingService,
        store: Store,
        authorizer: Authorizer,
    ) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self.authorizer = authorizer
        if not settings.allowed_user_ids or not settings.allowed_chat_ids:
            LOGGER.warning(
                "Telegram 白名单为空：除 /whoami 外的所有命令都会被拒绝"
            )
        self.application = Application.builder().token(token).build()
        self.application.add_handler(CommandHandler("start", self.help))
        self.application.add_handler(CommandHandler("help", self.help))
        self.application.add_handler(CommandHandler("whoami", self.whoami))
        self.application.add_handler(CommandHandler("trade", self.trade))
        self.application.add_handler(CommandHandler("confirm", self.confirm))
        self.application.add_handler(CommandHandler("cancel", self.cancel))
        self.application.add_handler(CommandHandler("status", self.status))
        self.application.add_error_handler(self.on_error)
        if self.application.job_queue is None:
            raise RuntimeError("python-telegram-bot job-queue 依赖未安装")
        self.application.job_queue.run_repeating(
            self.monitor,
            interval=self.settings.monitor_interval_seconds,
            first=1,
            name="trade-monitor",
        )

    @staticmethod
    def _ids(update: Update) -> tuple[int, int]:
        if update.effective_user is None or update.effective_chat is None:
            raise BTraderError("无法识别 Telegram 用户或会话")
        return update.effective_user.id, update.effective_chat.id

    async def _allowed(self, update: Update) -> bool:
        user_id, chat_id = self._ids(update)
        if not self.authorizer.is_allowed(user_id, chat_id):
            if update.effective_message:
                await update.effective_message.reply_text("拒绝访问：用户或会话不在白名单")
            return False
        return True

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        await update.effective_message.reply_text(HELP_TEXT)

    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id, chat_id = self._ids(update)
        await update.effective_message.reply_text(f"user_id={user_id}\nchat_id={chat_id}")

    async def trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        user_id, chat_id = self._ids(update)
        try:
            request = parse_trade_command(update.effective_message.text or "", self.settings)
            proposal_id, plan = await self.service.create_preview(request, user_id, chat_id)
            await update.effective_message.reply_text(
                format_preview(
                    proposal_id, plan, self.settings.trading_mode, self.settings.confirm_ttl_seconds
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except BTraderError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")

    async def confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        user_id, chat_id = self._ids(update)
        if len(context.args) != 2:
            await update.effective_message.reply_text("格式：/confirm <确认单ID> <6位动态码>")
            return
        proposal_id, code = context.args
        try:
            try:
                await update.effective_message.delete()
            except Exception:
                pass
            trade = await self.service.confirm(proposal_id, code, user_id, chat_id)
            await context.bot.send_message(chat_id=chat_id, text=format_trade(trade))
        except BTraderError as exc:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {exc}")
        except Exception:
            LOGGER.exception("Unexpected confirmation failure")
            await context.bot.send_message(chat_id=chat_id, text="❌ 下单过程出现内部错误，请检查本地日志和币安账户")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        if len(context.args) != 1:
            await update.effective_message.reply_text("格式：/cancel <确认单ID或交易ID>")
            return
        user_id, chat_id = self._ids(update)
        try:
            message = await self.service.cancel(context.args[0], user_id, chat_id)
            await update.effective_message.reply_text(f"✅ {message}")
        except BTraderError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        user_id, _ = self._ids(update)
        trades = list(self.store.recent_trades(user_id, 10))
        if not trades:
            await update.effective_message.reply_text("还没有交易记录")
            return
        await update.effective_message.reply_text(
            "\n\n".join(format_trade(trade) for trade in trades)
        )

    async def monitor(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for event in await self.service.monitor_all():
            await context.bot.send_message(chat_id=event["chat_id"], text=event["message"])

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.error("Telegram handler error: %s", type(context.error).__name__)

    def run(self) -> None:
        self.application.run_polling(drop_pending_updates=True)
