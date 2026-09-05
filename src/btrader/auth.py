from __future__ import annotations

import time
from typing import FrozenSet

import pyotp

from btrader.errors import AuthorizationError
from btrader.store import Store


class Authorizer:
    def __init__(
        self,
        allowed_user_ids: FrozenSet[int],
        allowed_chat_ids: FrozenSet[int],
        totp_secret: str,
        store: Store,
    ) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.allowed_chat_ids = allowed_chat_ids
        self.totp = pyotp.TOTP(totp_secret)
        self.store = store

    def is_allowed(self, user_id: int, chat_id: int) -> bool:
        return user_id in self.allowed_user_ids and chat_id in self.allowed_chat_ids

    def require_allowed(self, user_id: int, chat_id: int) -> None:
        if not self.is_allowed(user_id, chat_id):
            raise AuthorizationError("此 Telegram 用户或会话不在白名单中")

    def verify_totp_once(self, code: str) -> None:
        if len(code) != 6 or not code.isdigit():
            raise AuthorizationError("二次验证码必须是 6 位数字")
        now = int(time.time())
        matched_counter = None
        current_counter = now // self.totp.interval
        for counter in (current_counter - 1, current_counter, current_counter + 1):
            if self.totp.verify(code, for_time=counter * self.totp.interval):
                matched_counter = counter
                break
        if matched_counter is None:
            raise AuthorizationError("二次验证码错误或已过期")
        if matched_counter <= self.store.get_auth_counter():
            raise AuthorizationError("该二次验证码已经使用过")
        self.store.set_auth_counter(matched_counter)

