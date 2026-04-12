"""Idempotency deduplication store for mutating board intents."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock

from ..errors import ConflictError


class IdempotencyStore:
    """
    SQLite-backed idempotency key store.

    On a mutating intent with an idempotency_key:
    - If the key exists with the same fingerprint: return the cached result.
    - If the key exists with a different fingerprint: raise ConflictError.
    - If the key is new: return None (caller should execute and then store).
    """

    def __init__(self, conn: sqlite3.Connection, lock: RLock) -> None:
        self._conn = conn
        self._lock = lock

    def init_table(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
            self._conn.commit()

    def check(self, key: str, fingerprint: str) -> dict | None:
        """
        Check if a key has been seen before.

        Returns cached result dict on match, raises ConflictError on
        fingerprint mismatch, returns None if key is new.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, result_json FROM idempotency_keys WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ConflictError(
                f"Idempotency key {key!r} already used with a different payload"
            )
        return json.loads(row["result_json"])

    def store(self, key: str, fingerprint: str, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO idempotency_keys (key, fingerprint, result_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    result_json=excluded.result_json
                """,
                (
                    key,
                    fingerprint,
                    json.dumps(result, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
