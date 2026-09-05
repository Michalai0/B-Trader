import time

import pyotp
import pytest

from btrader.auth import Authorizer
from btrader.errors import AuthorizationError
from btrader.store import Store


def test_allowlist_and_totp_replay_protection(tmp_path):
    store = Store(tmp_path / "state.db")
    secret = pyotp.random_base32()
    auth = Authorizer(frozenset({1}), frozenset({2}), secret, store)
    assert auth.is_allowed(1, 2)
    assert not auth.is_allowed(1, 3)
    code = pyotp.TOTP(secret).at(int(time.time()))
    auth.verify_totp_once(code)
    with pytest.raises(AuthorizationError, match="已经使用"):
        auth.verify_totp_once(code)


def test_proposal_is_single_use(tmp_path):
    store = Store(tmp_path / "state.db")
    store.save_proposal("abc", 1, 2, int(time.time()) + 60, {"ok": True})
    assert store.consume_proposal("abc", 1, 2) == {"ok": True}
    with pytest.raises(ValueError, match="已经使用"):
        store.consume_proposal("abc", 1, 2)

