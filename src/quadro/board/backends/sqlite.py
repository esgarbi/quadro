from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock

from ..records import AgentRecord, AgentStatus, EventRecord, TaskRecord, TaskStatus
from .base import BoardBackend


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    raw_status = row["status"]
    try:
        status: TaskStatus | str = TaskStatus(raw_status)
    except ValueError:
        status = raw_status
    return TaskRecord(
        task_id=row["task_id"],
        task_type=row["task_type"],
        label=row["label"],
        priority=row["priority"],
        status=status,
        assigned_to=row["assigned_to"],
        output=row["output"],
        notes=json.loads(row["notes_json"]),
        continuation_token=row["continuation_token"],
        heartbeat_at=_parse_dt(row["heartbeat_at"]),
        context_snapshot_hash=row["context_snapshot_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _parse_event_status(raw: str | None) -> TaskStatus | str | None:
    if not raw:
        return None
    try:
        return TaskStatus(raw)
    except ValueError:
        return raw


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        sequence_id=row["sequence_id"],
        event_type=row["event_type"],
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        from_status=_parse_event_status(row["from_status"]),
        to_status=_parse_event_status(row["to_status"]),
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
    )


def _agent_from_row(row: sqlite3.Row) -> AgentRecord:
    return AgentRecord(
        agent_id=row["agent_id"],
        name=row["name"],
        status=AgentStatus(row["status"]),
        capabilities=json.loads(row["capabilities_json"]),
        a2a_url=row["a2a_url"],
        agent_card=json.loads(row["agent_card_json"]),
        current_task_id=row["current_task_id"],
        version=row["version"],
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
    )


class SqliteBoardBackend(BoardBackend):
    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._lock = RLock()  # RLock: reentrant so reads+writes can both use it safely
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL,
                    assigned_to TEXT,
                    output TEXT,
                    notes_json TEXT NOT NULL,
                    continuation_token TEXT,
                    heartbeat_at TEXT,
                    context_snapshot_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)
            self._conn.commit()
            # Legacy DBs: column was named `brief` before Track label rename
            cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
            ]
            if "brief" in cols and "label" not in cols:
                self._conn.execute("ALTER TABLE tasks RENAME COLUMN brief TO label")
                self._conn.commit()
            if "priority" not in cols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 5"
                )
                self._conn.commit()
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    a2a_url TEXT NOT NULL,
                    agent_card_json TEXT NOT NULL,
                    current_task_id TEXT,
                    version TEXT,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT,
                    from_status TEXT,
                    to_status TEXT,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_entries (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archived_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL,
                    assigned_to TEXT,
                    output TEXT,
                    notes_json TEXT NOT NULL,
                    continuation_token TEXT,
                    heartbeat_at TEXT,
                    context_snapshot_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)
            self._conn.commit()

    def create_task(self, task: TaskRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, task_type, label, priority, status, assigned_to, output, notes_json,
                    continuation_token, heartbeat_at, context_snapshot_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.task_type,
                    task.label,
                    task.priority,
                    str(task.status),
                    task.assigned_to,
                    task.output,
                    json.dumps(task.notes),
                    task.continuation_token,
                    task.heartbeat_at.isoformat() if task.heartbeat_at else None,
                    task.context_snapshot_hash,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def update_task(self, task: TaskRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE tasks SET
                    task_type=?, label=?, priority=?, status=?, assigned_to=?, output=?, notes_json=?,
                    continuation_token=?, heartbeat_at=?, context_snapshot_hash=?, updated_at=?
                WHERE task_id=?
                """,
                (
                    task.task_type,
                    task.label,
                    task.priority,
                    str(task.status),
                    task.assigned_to,
                    task.output,
                    json.dumps(task.notes),
                    task.continuation_token,
                    task.heartbeat_at.isoformat() if task.heartbeat_at else None,
                    task.context_snapshot_hash,
                    task.updated_at.isoformat(),
                    task.task_id,
                ),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                row = self._conn.execute(
                    "SELECT * FROM archived_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
        if not row:
            return None
        return _task_from_row(row)

    def list_tasks(self) -> list[TaskRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC"
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_tasks_by_status(self, statuses: set[str]) -> list[TaskRecord]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders})"
                " ORDER BY priority ASC, created_at ASC",
                tuple(statuses),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def upsert_agent(self, agent: AgentRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agents (
                    agent_id, name, status, capabilities_json, a2a_url, agent_card_json,
                    current_task_id, version, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name,
                    status=excluded.status,
                    capabilities_json=excluded.capabilities_json,
                    a2a_url=excluded.a2a_url,
                    agent_card_json=excluded.agent_card_json,
                    current_task_id=excluded.current_task_id,
                    version=excluded.version,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    agent.agent_id,
                    agent.name,
                    agent.status.value,
                    json.dumps(agent.capabilities),
                    agent.a2a_url,
                    json.dumps(agent.agent_card),
                    agent.current_task_id,
                    agent.version,
                    agent.last_seen_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
        if not row:
            return None
        return _agent_from_row(row)

    def list_agents(self) -> list[AgentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY agent_id ASC"
            ).fetchall()
        return [_agent_from_row(row) for row in rows]

    def append_event(self, event: EventRecord) -> int:
        def _status_str(s: TaskStatus | str | None) -> str | None:
            if s is None:
                return None
            return s.value if isinstance(s, TaskStatus) else s

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (
                    event_type, task_id, agent_id, from_status, to_status, payload_json, idempotency_key, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_type,
                    event.task_id,
                    event.agent_id,
                    _status_str(event.from_status),
                    _status_str(event.to_status),
                    json.dumps(event.payload),
                    event.idempotency_key,
                    event.timestamp.isoformat(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events_since(self, sequence_id: int) -> list[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE sequence_id > ? ORDER BY sequence_id ASC",
                (sequence_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_events_for_task(self, task_id: str) -> list[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY sequence_id ASC",
                (task_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_events_for_agent(self, agent_id: str) -> list[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE agent_id=? ORDER BY sequence_id ASC",
                (agent_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def put_data(self, key: str, value: object) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO data_entries (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value), datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get_data(self, key: str) -> object | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM data_entries WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"])

    def list_data(self, prefix: str | None = None) -> dict[str, object]:
        with self._lock:
            if prefix is None:
                rows = self._conn.execute(
                    "SELECT key, value_json FROM data_entries ORDER BY key ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT key, value_json FROM data_entries
                    WHERE key LIKE ? || '%'
                    ORDER BY key ASC
                    """,
                    (prefix,),
                ).fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def delete_data(self, key: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM data_entries WHERE key=?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    def archive_task(self, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "INSERT INTO archived_tasks SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            )
            self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            self._conn.commit()
            return True

    def get_archived_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM archived_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if not row:
            return None
        return _task_from_row(row)
