from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class Store:
    """Small durable store for confirmations and the trade state machine."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def save_proposal(
        self,
        proposal_id: str,
        user_id: int,
        chat_id: int,
        expires_at: int,
        payload: Dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO proposals(id, user_id, chat_id, expires_at, status, payload_json)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (proposal_id, user_id, chat_id, expires_at, json.dumps(payload, ensure_ascii=False)),
            )

    def consume_proposal(self, proposal_id: str, user_id: int, chat_id: int) -> Dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError("确认单不存在")
            if row["user_id"] != user_id or row["chat_id"] != chat_id:
                raise PermissionError("这不是你的确认单")
            if row["status"] != "pending":
                raise ValueError("确认单已经使用或取消")
            if row["expires_at"] < now:
                self._connection.execute(
                    "UPDATE proposals SET status = 'expired' WHERE id = ?", (proposal_id,)
                )
                raise ValueError("确认单已过期，请重新预览")
            changed = self._connection.execute(
                "UPDATE proposals SET status = 'consumed' WHERE id = ? AND status = 'pending'",
                (proposal_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("确认单已经使用")
            return json.loads(row["payload_json"])

    def cancel_proposal(self, proposal_id: str, user_id: int, chat_id: int) -> bool:
        with self._lock, self._connection:
            return (
                self._connection.execute(
                    """
                    UPDATE proposals SET status = 'cancelled'
                    WHERE id = ? AND user_id = ? AND chat_id = ? AND status = 'pending'
                    """,
                    (proposal_id, user_id, chat_id),
                ).rowcount
                == 1
            )

    def save_trade(self, trade: Dict[str, Any]) -> None:
        now = int(time.time())
        trade.setdefault("created_at", now)
        trade["updated_at"] = now
        payload = json.dumps(trade, ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO trades(id, user_id, chat_id, state, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    trade["id"],
                    trade["user_id"],
                    trade["chat_id"],
                    trade["state"],
                    trade["created_at"],
                    trade["updated_at"],
                    payload,
                ),
            )

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def active_trades(self) -> Iterable[Dict[str, Any]]:
        terminal = ("closed", "cancelled", "failed")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM trades
                WHERE state NOT IN (?, ?, ?) ORDER BY created_at ASC
                """,
                terminal,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def recent_trades(self, user_id: int, limit: int = 10) -> Iterable[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM trades WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def has_active_symbol(self, market: str, symbol: str) -> bool:
        return any(
            trade["market"] == market and trade["symbol"] == symbol
            for trade in self.active_trades()
        )

    def get_auth_counter(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM auth_state WHERE key = 'last_totp_counter'"
            ).fetchone()
        return int(row["value"]) if row else -1

    def set_auth_counter(self, counter: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO auth_state(key, value) VALUES ('last_totp_counter', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(counter),),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
