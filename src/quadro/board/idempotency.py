"""Idempotency deduplication store for mutating board intents.

The module exposes two symbols:

``IdempotencyStore``
    Runtime-checkable :class:`~typing.Protocol` describing the contract
    that :class:`~quadro.board.board.QuadroBoard` requires. Any object
    with ``check()`` and ``store()`` methods that match the signatures
    below satisfies the Protocol. Backends that implement their own
    durable key-value store (e.g. Postgres, Redis, DynamoDB) can satisfy
    the Protocol without subclassing.

``SqliteIdempotencyStore``
    Concrete SQLite-backed implementation used by the in-process reference
    runtime. The class is kept structurally separate from the Protocol so
    that future backends do not inherit SQLite-specific infrastructure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Protocol, runtime_checkable

from ..errors import ConflictError


@runtime_checkable
class IdempotencyStore(Protocol):
    """Contract for idempotency key deduplication.

    On a mutating intent with an ``idempotency_key``:

    * If the key exists with the same fingerprint -> return the cached result.
    * If the key exists with a different fingerprint -> raise :class:`ConflictError`.
    * If the key is new -> return ``None`` (the caller is responsible for
      executing the intent and calling :meth:`store` with the result).
    """

    def check(self, key: str, fingerprint: str) -> dict | None: ...

    def store(self, key: str, fingerprint: str, result: dict) -> None: ...


class SqliteIdempotencyStore:
    """SQLite-backed implementation of :class:`IdempotencyStore`.

    Shares its connection and lock with a
    :class:`~quadro.board.backends.sqlite.SqliteBoardBackend` so the
    idempotency table lives inside the same transaction boundary as the
    board state.
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
