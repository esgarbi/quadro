"""
quadro.ui — Zero-dependency board visualiser.

Serves a live Kanban view of any Quadro board from a single Python file.
Uses only Python stdlib + quadro itself. No npm, no React, no pip installs.

TWO USAGE MODES
───────────────

1. Point at a SQLite file directly (read-only, works alongside a running newsroom):

       python -m quadro.ui path/to/newsroom.db
       python -m quadro.ui path/to/newsroom.db --port 8765 --host 0.0.0.0

2. Programmatic — hook into a running board from application code:

       from quadro.ui import serve_board
       serve_board(board_client, port=8080, background=True)

   Pass col_order to declare the full pipeline upfront (columns always visible):

       serve_board(board_client, port=8080, background=True,
           col_order=["UNASSIGNED","ideating","idea_ready","researching",
                      "research_ready","writing","draft_ready","reviewing","published"])

   Or store "_col_order" in board data at startup — the UI reads it automatically:

       bc.put_data("_col_order", ["UNASSIGNED","ideating",...,"published"])

Then open http://localhost:8080 in any browser.

COLUMN ORDER RESOLUTION (server-side, in priority order)
─────────────────────────────────────────────────────────
1. col_order argument to serve_board()           — explicit, highest priority
2. "_col_order" key in board data                — stored by application at startup
3. Full event history (to_status, oldest first)  — derived automatically
4. Current task statuses                         — last resort fallback

This means pointing the UI at a fresh .db file that was just started will
show all pipeline columns immediately, as long as the application wrote
"_col_order" to board data during setup.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_UI_VERSION = "1.2.0"

_FALLBACK_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"COMPLETE", "HUMAN_REVIEW", "ON_HOLD"}
)


# ─────────────────────────────────────────────────────────────────────────────
#  Column order derivation
# ─────────────────────────────────────────────────────────────────────────────


def _derive_col_order(
    events: list[dict],
    tasks: list[dict],
    explicit: list[str] | None = None,
    terminal_statuses: frozenset[str] | None = None,
) -> list[str]:
    """
    Return a stable pipeline-ordered list of status strings.

    Priority:
    1. explicit list (serve_board col_order arg or _col_order board data key)
    2. full event history — statuses in first-seen order by sequence_id
    3. current task statuses — for any not yet in event log
    4. terminal statuses moved to the right end
    """
    terminals = terminal_statuses or _FALLBACK_TERMINAL_STATUSES

    if explicit is not None:
        seen: set[str] = set(explicit)
        order: list[str] = list(explicit)
    else:
        seen = set()
        order = []
        for ev in sorted(events, key=lambda e: e.get("sequence_id", 0)):
            s = ev.get("to_status")
            if s and s not in seen:
                seen.add(s)
                order.append(s)

    for t in tasks:
        s = t.get("status", "")
        if s and s not in seen:
            seen.add(s)
            order.append(s)

    non_terminal = [s for s in order if s not in terminals]
    terminal = [s for s in order if s in terminals]
    return [*non_terminal, *terminal]


# ─────────────────────────────────────────────────────────────────────────────
#  Board data source — two modes
# ─────────────────────────────────────────────────────────────────────────────


class _DataSource:
    """
    Abstracts over two ways to read board data:
    - From a live BoardClient (programmatic mode)
    - Directly from a SQLite file (CLI mode)
    """

    def __init__(self, *, board_client=None, db_path: str | None = None) -> None:
        if board_client is not None:
            self._client = board_client
            self._conn = None
        elif db_path is not None:
            self._client = None
            self._conn = self._open_sqlite(db_path)
        else:
            raise ValueError("Must provide board_client or db_path")

    @staticmethod
    def _open_sqlite(path: str):
        import sqlite3

        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def full_state(self) -> dict[str, Any]:
        if self._client is not None:
            return self._client.full_state()
        return self._sqlite_full_state()

    def all_events(self) -> list[dict]:
        if self._client is not None:
            return self._client.stream_events(0)
        return self._sqlite_events_since(0)

    def stream_events(self, since_sequence: int = 0) -> list[dict]:
        if self._client is not None:
            return self._client.stream_events(since_sequence)
        return self._sqlite_events_since(since_sequence)

    def task_history(self, task_id: str) -> list[dict]:
        if self._client is not None:
            return self._client.task_history(task_id)
        return self._sqlite_task_history(task_id)

    def _sqlite_full_state(self) -> dict[str, Any]:
        import json as _json

        conn = self._conn
        tasks = []
        for row in conn.execute(
            "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC"
        ):
            d = dict(row)
            d["notes"] = _json.loads(d.pop("notes_json", "[]"))
            tasks.append(d)
        agents = []
        for row in conn.execute("SELECT * FROM agents ORDER BY agent_id ASC"):
            d = dict(row)
            d["capabilities"] = _json.loads(d.pop("capabilities_json", "[]"))
            d.pop("agent_card_json", None)
            agents.append(d)
        data: dict[str, Any] = {}
        for row in conn.execute("SELECT key, value_json FROM data_entries"):
            data[row["key"]] = _json.loads(row["value_json"])
        return {"tasks": tasks, "agents": agents, "data": data}

    def _sqlite_events_since(self, since: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE sequence_id > ? ORDER BY sequence_id ASC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _sqlite_task_history(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY sequence_id ASC", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
#  SSE broadcaster
# ─────────────────────────────────────────────────────────────────────────────


class _EventBroadcaster(threading.Thread):
    def __init__(self, source: _DataSource, poll_interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="quadro-ui-sse")
        self._source = source
        self._interval = poll_interval
        self._cursor = 0
        self._clients: list[Any] = []
        self._lock = threading.Lock()

    def add_client(self, q) -> None:
        with self._lock:
            self._clients.append(q)

    def remove_client(self, q) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def run(self) -> None:
        try:
            events = self._source.stream_events(0)
            if events:
                self._cursor = max(e["sequence_id"] for e in events)
        except Exception:
            pass

        while True:
            try:
                new_events = self._source.stream_events(self._cursor)
                if new_events:
                    self._cursor = max(e["sequence_id"] for e in new_events)
                    payload = json.dumps(new_events)
                    with self._lock:
                        dead = []
                        for q in self._clients:
                            try:
                                q.put_nowait(payload)
                            except Exception:
                                dead.append(q)
                        for q in dead:
                            self._clients.remove(q)
            except Exception as exc:
                logger.debug("SSE poll error: %s", exc)
            time.sleep(self._interval)


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP request handler
# ─────────────────────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):

    source: _DataSource
    broadcaster: _EventBroadcaster
    db_label: str
    col_order: list[str] | None  # explicit column order from serve_board()

    def log_message(self, fmt, *args) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._serve_html()
        elif path == "/api/state":
            self._serve_state()
        elif path == "/api/events":
            self._serve_sse()
        elif m := re.match(r"^/api/task/([^/]+)/history$", path):
            self._serve_task_history(m.group(1))
        else:
            self._json(404, {"error": "not found"})

    def _serve_html(self) -> None:
        body = _HTML_PAGE.replace("__DB_LABEL__", self.db_label)
        body = body.replace("__UI_VERSION__", _UI_VERSION)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_state(self) -> None:
        try:
            state = self.source.full_state()

            # Resolve the explicit column order using a 4-level priority chain:
            #
            # 1. serve_board(col_order=[...])  — caller passed it explicitly
            # 2. board data "_col_order" key   — application stored it at startup
            # 3. full event history             — derived from to_status sequence
            # 4. current task statuses          — last resort (handled inside _derive)
            #
            # This means CLI mode (python -m quadro.ui newsroom.db) automatically
            # gets all pipeline columns from cycle 0 if the application wrote
            # "_col_order" to board data during setup — no extra arguments needed.
            explicit = self.col_order
            if explicit is None:
                board_data = state.get("data", {})
                stored_order = board_data.get("_col_order")
                if isinstance(stored_order, list) and stored_order:
                    explicit = stored_order

            board_terminals = state.get("_terminal_statuses")
            terminal_set = (
                frozenset(board_terminals)
                if board_terminals
                else _FALLBACK_TERMINAL_STATUSES
            )

            all_events = self.source.all_events()
            col_order = _derive_col_order(
                all_events,
                state.get("tasks", []),
                explicit=explicit,
                terminal_statuses=terminal_set,
            )

            chief_telem = state.get("data", {}).get("_chief_telemetry")

            state["_meta"] = {
                "db_label": self.db_label,
                "ui_version": _UI_VERSION,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "col_order": col_order,
                "chief": chief_telem,
            }
            self._json(200, state)
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _serve_task_history(self, task_id: str) -> None:
        try:
            events = self.source.task_history(task_id)
            self._json(200, {"task_id": task_id, "events": events})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _serve_sse(self) -> None:
        import queue

        q: queue.Queue = queue.Queue(maxsize=200)
        self.broadcaster.add_client(q)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
        except Exception:
            self.broadcaster.remove_client(q)
            return

        try:
            while True:
                try:
                    payload = q.get(timeout=25)
                    msg = f"data: {payload}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()
                except Exception:
                    try:
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
        finally:
            self.broadcaster.remove_client(q)

    def _json(self, code: int, body: Any) -> None:
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


# ─────────────────────────────────────────────────────────────────────────────
#  Server lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_board(
    board_client=None,
    *,
    db_path: str | None = None,
    port: int = 8080,
    host: str = "127.0.0.1",
    background: bool = False,
    open_browser: bool = False,
    poll_interval: float = 1.0,
    col_order: list[str] | None = None,
) -> _ThreadingServer:
    """
    Start the Quadro board UI server.

    Args:
        board_client:   A BoardClient instance (programmatic mode).
        db_path:        Path to a SQLite file (CLI / read-only mode).
        port:           TCP port to listen on. Default 8080.
        host:           Interface to bind. Default 127.0.0.1.
        background:     If True, run in a daemon thread and return immediately.
        open_browser:   Open http://host:port in the default browser on startup.
        poll_interval:  Seconds between SSE event polls. Default 1.0.
        col_order:      Optional explicit pipeline column order. When provided,
                        all columns are visible from cycle 0 even if empty.
                        If omitted, the UI reads "_col_order" from board data,
                        then falls back to event history derivation.

    Returns:
        The running _ThreadingServer instance (call .shutdown() to stop).
    """
    if board_client is None and db_path is None:
        raise ValueError("Provide board_client or db_path")

    label = db_path or getattr(board_client, "_board_url", "live board")
    source = _DataSource(board_client=board_client, db_path=db_path)
    broadcaster = _EventBroadcaster(source, poll_interval=poll_interval)
    broadcaster.start()

    class Handler(_Handler):
        pass

    Handler.source = source
    Handler.broadcaster = broadcaster
    Handler.db_label = Path(label).name if db_path else label
    Handler.col_order = col_order

    server = _ThreadingServer((host, port), Handler)
    url = f"http://{host}:{port}"
    logger.info("Quadro UI serving at %s (board: %s)", url, label)
    print(f"  Quadro Board UI  →  {url}", flush=True)

    if open_browser:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    if background:
        t = threading.Thread(
            target=server.serve_forever, daemon=True, name="quadro-ui-http"
        )
        t.start()
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()

    return server


# ─────────────────────────────────────────────────────────────────────────────
#  Embedded HTML page
# ─────────────────────────────────────────────────────────────────────────────

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quadro · __DB_LABEL__</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:    #0f172a; --bg2: #1e293b; --bg3: #334155;
  --border: #334155; --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b;
  --accent: #38bdf8; --green: #4ade80; --amber: #fbbf24;
  --red: #f87171; --purple: #c084fc;
  --mono: "JetBrains Mono","Fira Code","Cascadia Code",monospace;
  --sans: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --radius: 8px; --card-w: 220px;
}
[data-theme="light"] {
  --bg:#f8fafc; --bg2:#f1f5f9; --bg3:#e2e8f0;
  --border:#cbd5e1; --text:#0f172a; --text2:#475569; --text3:#94a3b8;
}
body { background:var(--bg); color:var(--text); font-family:var(--sans);
       font-size:13px; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
header { display:flex; align-items:center; gap:12px; padding:0 16px;
         height:44px; background:var(--bg2); border-bottom:1px solid var(--border);
         flex-shrink:0; }
header h1 { font-size:13px; font-weight:600; letter-spacing:.05em;
            color:var(--accent); font-family:var(--mono); }
.db-label { font-family:var(--mono); font-size:11px; color:var(--text3);
            background:var(--bg3); padding:2px 8px; border-radius:4px; }
.live-dot { width:7px; height:7px; border-radius:50%;
            background:var(--green); box-shadow:0 0 6px var(--green); }
.live-dot.dead { background:var(--red); box-shadow:0 0 6px var(--red); }
.spacer { flex:1; }
.meta { font-size:11px; color:var(--text3); }
#goal-badge { background:var(--bg3); padding:2px 10px; border-radius:4px;
              font-family:var(--mono); font-size:11px; color:var(--text); }
#goal-badge.done { color:var(--green); }
button.icon-btn { background:none; border:1px solid var(--border); color:var(--text2);
                  border-radius:4px; padding:3px 8px; cursor:pointer; font-size:11px; }
button.icon-btn:hover { border-color:var(--accent); color:var(--accent); }
.main { display:flex; flex:1; overflow:hidden; }
#board { flex:1; overflow-x:auto; overflow-y:hidden; padding:12px;
         display:flex; gap:10px; align-items:flex-start; }
.col { flex-shrink:0; width:var(--card-w); display:flex; flex-direction:column;
       gap:6px; max-height:100%; }
.col-header { display:flex; align-items:center; justify-content:space-between;
              padding:4px 6px; border-radius:5px 5px 0 0; }
.col-header .col-name { font-size:10px; font-weight:700; letter-spacing:.1em;
                         text-transform:uppercase; font-family:var(--mono); }
.col-header .col-count { font-size:10px; font-family:var(--mono);
                          background:rgba(0,0,0,.25); border-radius:10px;
                          padding:1px 6px; }
.col-cards { overflow-y:auto; display:flex; flex-direction:column; gap:6px;
             padding:4px 2px 8px; flex:1; }
.col-empty { font-size:10px; color:var(--text3); text-align:center;
             padding:12px 4px; opacity:.5; }
.card { background:var(--bg2); border:1px solid var(--border); border-radius:var(--radius);
        padding:10px 10px 8px; cursor:pointer; transition:border-color .15s,transform .1s;
        border-left:3px solid var(--status-color,var(--border)); }
.card:hover { border-color:var(--accent); transform:translateY(-1px); }
.card-label { font-size:12px; font-weight:500; line-height:1.4;
              display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
              overflow:hidden; margin-bottom:6px; }
.card-meta { display:flex; flex-direction:column; gap:2px; }
.card-id { font-family:var(--mono); font-size:10px; color:var(--text3); }
.card-agent { font-size:10px; color:var(--text2); }
.card-time { font-size:10px; color:var(--text3); }
.card-hb { font-size:10px; }
.card-hb.stale { color:var(--amber); }
.card-hb.ok { color:var(--green); }
.card-hb.completed { color:var(--green); opacity: 0.7; }
.card-priority {
  display: inline-block;
  font-size: 9px;
  font-family: var(--mono);
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--amber);
  color: #000;
  margin-top: 4px;
}
.card-priority[data-p="1"] { background: var(--red); color: #fff; }
.card-priority[data-p="2"] { background: var(--red); color: #fff; }
#chief-pulse {
  width:8px; height:8px; border-radius:50%; flex-shrink:0;
  background:var(--text3); transition:background 0.3s, box-shadow 0.3s;
}
#chief-pulse.thinking {
  background:var(--amber); box-shadow:0 0 8px var(--amber);
  animation:pulse 1s ease-in-out infinite;
}
#chief-pulse.acting { background:#60a5fa; box-shadow:0 0 6px #60a5fa; }
#chief-pulse.sleeping { background:var(--text3); box-shadow:none; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.4;} }
#chief-section.chief-thinking { border-color:var(--amber); }
#chief-section.chief-acting   { border-color:#60a5fa; }
#chief-section.chief-sleeping { border-color:var(--border); }
.chief-status-banner {
  display:flex; align-items:center; gap:8px; padding:6px 10px;
  border-radius:6px; margin-bottom:8px; font-size:11px; font-weight:600;
  font-family:var(--mono); letter-spacing:.04em;
  transition:background 0.4s ease, color 0.4s ease, box-shadow 0.4s ease;
}
.chief-status-banner .chief-status-dot {
  width:10px; height:10px; border-radius:50%; flex-shrink:0;
  transition:background 0.3s, box-shadow 0.3s;
}
.chief-status-banner .chief-status-text { flex:1; }
.chief-status-banner.thinking {
  background:rgba(251,191,36,0.12); color:var(--amber);
  box-shadow:inset 0 0 12px rgba(251,191,36,0.06);
}
.chief-status-banner.thinking .chief-status-dot {
  background:var(--amber); box-shadow:0 0 8px var(--amber);
  animation:pulse 1s ease-in-out infinite;
}
.chief-status-banner.acting {
  background:rgba(96,165,250,0.12); color:#60a5fa;
  box-shadow:inset 0 0 12px rgba(96,165,250,0.06);
}
.chief-status-banner.acting .chief-status-dot {
  background:#60a5fa; box-shadow:0 0 8px #60a5fa;
  animation:acting-pulse 2s ease-in-out infinite;
}
@keyframes acting-pulse { 0%,100%{box-shadow:0 0 4px #60a5fa;} 50%{box-shadow:0 0 12px #60a5fa;} }
.chief-status-banner.sleeping {
  background:rgba(100,116,139,0.08); color:var(--text3);
  box-shadow:none;
}
.chief-status-banner.sleeping .chief-status-dot {
  background:var(--text3); box-shadow:none;
}
.right-panel { width:300px; flex-shrink:0; display:flex; flex-direction:column;
               border-left:1px solid var(--border); overflow:hidden; }
#chief-section { padding:10px 12px; border-bottom:1px solid var(--border); flex-shrink:0;
                 transition:border-color 0.4s ease; }
.chief-row { display:flex; align-items:center; gap:6px; padding:2px 0; font-size:11px; }
.chief-label { color:var(--text3); font-size:10px; font-family:var(--mono); width:52px; flex-shrink:0; }
.chief-val { color:var(--text2); }
.chief-val.thinking { color:var(--amber); font-weight:600; }
.chief-val.acting   { color:#60a5fa; font-weight:600; }
.chief-val.sleeping { color:var(--text3); }
.chief-sparkline { display:flex; align-items:flex-end; gap:1px; height:20px; }
.chief-bar { width:6px; border-radius:2px 2px 0 0; background:var(--accent); opacity:0.6; min-height:2px; }
#agents-section { padding:10px 12px; border-bottom:1px solid var(--border); }
.section-title { font-size:10px; font-weight:700; letter-spacing:.1em;
                 text-transform:uppercase; color:var(--text3); margin-bottom:6px;
                 font-family:var(--mono); }
.agent-row { display:flex; align-items:center; gap:8px; padding:4px 0; }
.agent-dot { width:6px; height:6px; border-radius:50%; flex-shrink:0; }
.agent-dot.IDLE { background:var(--green); }
.agent-dot.BUSY { background:var(--amber); }
.agent-dot.OFFLINE { background:var(--text3); }
.agent-name { font-size:11px; font-weight:500; flex:1; }
.agent-badge { font-size:10px; font-family:var(--mono); }
.agent-badge.IDLE { color:var(--green); }
.agent-badge.BUSY { color:var(--amber); }
.agent-task-id { font-size:10px; color:var(--text3); font-family:var(--mono); }
.agents-summary { font-weight:400; font-size:10px; color:var(--text2);
                  letter-spacing:normal; text-transform:none; margin-left:6px; }
.agents-divider { border:none; border-top:1px solid var(--border); margin:4px 0; }
.agent-group { display:flex; align-items:center; gap:8px; padding:3px 0; }
.agent-group-name { font-size:11px; color:var(--text2); flex-shrink:0; min-width:70px; }
.agent-group-bar { flex:1; height:4px; border-radius:2px; background:var(--bg3); overflow:hidden; }
.agent-group-fill { height:100%; border-radius:2px; background:var(--green); transition:width 0.3s; }
.agent-group-count { font-size:10px; font-family:var(--mono); color:var(--text3);
                     flex-shrink:0; white-space:nowrap; }
#events-section { flex:1; display:flex; flex-direction:column; overflow:hidden;
                  padding:10px 12px; }
#events-list { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:3px; }
.ev-row { display:grid; grid-template-columns:36px 1fr; gap:4px; align-items:start;
          padding:3px 4px; border-radius:4px; }
.ev-row:hover { background:var(--bg2); }
.ev-seq { font-family:var(--mono); font-size:10px; color:var(--text3); text-align:right; }
.ev-body { min-width:0; }
.ev-type { font-size:10px; font-weight:600; }
.ev-type.task_posted     { color:#60a5fa; }
.ev-type.task_assigned   { color:#a78bfa; }
.ev-type.task_completed  { color:#34d399; }
.ev-type.task_reviewed   { color:#2dd4bf; }
.ev-type.task_heartbeat  { color:var(--text3); }
.ev-type.task_stale      { color:var(--amber); }
.ev-type.task_human_review     { color:var(--red); }
.ev-type.task_reassigned { color:#fb923c; }
.ev-transition { font-size:10px; color:var(--text3); white-space:nowrap;
                 overflow:hidden; text-overflow:ellipsis; font-family:var(--mono); }
.ev-time { font-size:10px; color:var(--text3); }
#data-section { padding:8px 12px; border-top:1px solid var(--border);
                max-height:90px; overflow-y:auto; }
.data-row { display:flex; gap:6px; font-size:10px; padding:1px 0; }
.data-key { font-family:var(--mono); color:var(--accent); flex-shrink:0; }
.data-val { color:var(--text2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#drawer-overlay { position:fixed; inset:0; background:rgba(0,0,0,.5);
                  display:none; z-index:100; }
#drawer-overlay.open { display:block; }
#drawer { position:fixed; top:0; right:-520px; height:100%; width:500px;
          background:var(--bg2); border-left:1px solid var(--border);
          z-index:101; transition:right .2s ease; overflow-y:auto;
          display:flex; flex-direction:column; }
#drawer.open { right:0; }
.drawer-header { padding:16px; border-bottom:1px solid var(--border);
                 position:sticky; top:0; background:var(--bg2); z-index:1; }
.drawer-title { font-size:14px; font-weight:600; margin-bottom:4px; line-height:1.4; }
.drawer-meta { font-family:var(--mono); font-size:10px; color:var(--text3); }
.drawer-close { position:absolute; top:12px; right:12px; background:none;
                border:1px solid var(--border); color:var(--text2); border-radius:4px;
                padding:4px 8px; cursor:pointer; font-size:12px; }
.drawer-close:hover { color:var(--text); border-color:var(--text2); }
.drawer-body { padding:16px; flex:1; }
.drawer-section-title { font-size:10px; font-weight:700; letter-spacing:.1em;
                         text-transform:uppercase; color:var(--text3); margin-bottom:8px;
                         font-family:var(--mono); }
.timeline { position:relative; padding-left:20px; }
.timeline::before { content:''; position:absolute; left:6px; top:4px;
                    bottom:4px; width:1px; background:var(--border); }
.tl-event { position:relative; margin-bottom:10px; }
.tl-dot { position:absolute; left:-17px; top:3px; width:8px; height:8px;
          border-radius:50%; border:2px solid var(--bg2); background:var(--text3); }
.tl-dot.final { background:var(--green); }
.tl-time { font-family:var(--mono); font-size:10px; color:var(--text3); }
.tl-transition { font-size:11px; color:var(--text2); font-family:var(--mono);
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tl-type { font-size:10px; font-weight:600; margin-bottom:1px; }
.output-preview { background:var(--bg3); border-radius:6px; padding:10px;
                  font-family:var(--mono); font-size:11px; color:var(--text2);
                  white-space:pre-wrap; word-break:break-word; max-height:200px;
                  overflow-y:auto; line-height:1.5; margin-top:8px; }
::-webkit-scrollbar { width:5px; height:5px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--bg3); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--text3); }
.card.archiving {
  animation: archive-out 400ms ease-in forwards;
  pointer-events: none;
  overflow: hidden;
}
@keyframes archive-out {
  0%   { opacity: 1;   max-height: 200px; transform: translateX(0)    scale(1);    }
  40%  { opacity: 0.6; max-height: 200px; transform: translateX(8px)  scale(0.98); }
  100% { opacity: 0;   max-height: 0;     transform: translateX(24px) scale(0.95);
         margin-bottom: 0; padding-top: 0; padding-bottom: 0; border-width: 0; }
}
</style>
</head>
<body data-theme="dark">
<header>
  <span class="live-dot" id="live-dot"></span>
  <h1>QUADRO</h1>
  <span class="db-label">__DB_LABEL__</span>
  <span id="goal-badge"></span>
  <span class="spacer"></span>
  <span id="chief-pulse" title="Chief status"></span>
  <span id="chief-status-label" class="meta"></span>
  <span class="meta" id="last-updated"></span>
  <button class="icon-btn" id="theme-btn" title="Toggle theme">☀︎</button>
  <button class="icon-btn" id="refresh-btn" title="Refresh now">↺</button>
</header>
<div class="main">
  <div id="board"></div>
  <aside class="right-panel">
    <div id="chief-section">
      <div class="section-title">Chief</div>
      <div id="chief-panel"></div>
    </div>
    <div id="agents-section">
      <div class="section-title">Agents <span id="agents-summary" class="agents-summary"></span></div>
      <div id="agents-list"></div>
    </div>
    <div id="events-section">
      <div class="section-title">Live Events</div>
      <div id="events-list"></div>
    </div>
    <div id="data-section" style="display:none">
      <div class="section-title">Board Data</div>
      <div id="data-list"></div>
    </div>
  </aside>
</div>
<div id="drawer-overlay"></div>
<div id="drawer">
  <div class="drawer-header">
    <button class="drawer-close" id="drawer-close">✕</button>
    <div class="drawer-title" id="drawer-title"></div>
    <div class="drawer-meta" id="drawer-meta"></div>
  </div>
  <div class="drawer-body" id="drawer-body"></div>
</div>
<script>
let _state = null;
let _colOrder = [];
let _colOrderSet = new Set();
let _recentEvents = [];
const MAX_EVENTS = 40;
const _archivedIds = new Set();
const _cardElements = new Map();

let TERMINAL_STATUSES = new Set(['COMPLETE','HUMAN_REVIEW','ON_HOLD']);

const STATUS_COLORS = {
  UNASSIGNED:'#64748b', IN_PROGRESS:'#38bdf8', PENDING_REVIEW:'#fbbf24',
  REVISION_NEEDED:'#fb923c', APPROVED:'#2dd4bf', COMPLETE:'#4ade80',
  STALE:'#f87171', HUMAN_REVIEW:'#ef4444', ON_HOLD:'#c084fc',
  ideating:'#818cf8', idea_ready:'#a78bfa', researching:'#38bdf8',
  research_ready:'#34d399', writing:'#fbbf24', draft_ready:'#fb923c',
  reviewing:'#2dd4bf', published:'#4ade80',
  placed:'#60a5fa', accepted:'#34d399', awaiting_stock:'#fb923c',
  stock_ready:'#2dd4bf', delivering:'#a78bfa', delivered:'#4ade80',
};

function statusColor(s) {
  if (STATUS_COLORS[s]) return STATUS_COLORS[s];
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff;
  return `hsl(${h % 360},60%,65%)`;
}

function timeAgo(isoStr) {
  if (!isoStr) return '';
  const s = Math.floor((Date.now() - new Date(isoStr)) / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m`;
  if (s < 86400) return `${Math.floor(s/3600)}h`;
  return `${Math.floor(s/86400)}d`;
}
function shortId(id) { return id ? id.slice(0,8) : ''; }
function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Column order accumulates — once a column is seen it never disappears.
// Server provides col_order resolved from: explicit arg > board _col_order > event history.
function mergeColOrder(serverOrder) {
  if (!serverOrder || serverOrder.length === 0) return;
  let changed = false;
  for (const s of serverOrder) {
    if (!_colOrderSet.has(s)) { _colOrderSet.add(s); changed = true; }
  }
  if (!changed) return;
  const serverSet = new Set(serverOrder);
  const extras = _colOrder.filter(s => !serverSet.has(s));
  _colOrder = [...serverOrder, ...extras];
  _colOrderSet = new Set(_colOrder);
}

function getArchiveThreshold(data) {
  const v = data && data['_archive_threshold'];
  return (typeof v === 'number' && v > 0) ? Math.floor(v) : 8;
}

function archiveOldest(byStatus, threshold) {
  for (const status of _colOrder) {
    if (!TERMINAL_STATUSES.has(status)) continue;
    const tasks = (byStatus[status] || []).filter(t => !_archivedIds.has(t.task_id));
    if (tasks.length <= threshold) continue;
    tasks.sort((a, b) => new Date(a.updated_at) - new Date(b.updated_at));
    const excess = tasks.slice(0, tasks.length - threshold);
    for (const t of excess) _archivedIds.add(t.task_id);
  }
}

function renderBoard(state) {
  const board = document.getElementById('board');
  const tasks = state.tasks || [];
  const agents = state.agents || [];
  const agentMap = Object.fromEntries(agents.map(a => [a.agent_id, a]));

  const meta = state._meta || {};
  if (meta.col_order) mergeColOrder(meta.col_order);
  if (_colOrder.length === 0) mergeColOrder([...new Set(tasks.map(t => t.status))]);

  const byStatus = {};
  for (const s of _colOrder) byStatus[s] = [];
  for (const t of tasks) {
    if (!byStatus[t.status]) byStatus[t.status] = [];
    byStatus[t.status].push(t);
  }

  const prevSize = _archivedIds.size;
  const threshold = getArchiveThreshold(state.data);
  archiveOldest(byStatus, threshold);

  if (_archivedIds.size > prevSize) {
    for (const [taskId, cardEl] of _cardElements) {
      if (_archivedIds.has(taskId) && cardEl.isConnected) {
        cardEl.classList.add('archiving');
      }
    }
  }

  for (const status of _colOrder) {
    if (byStatus[status]) {
      byStatus[status] = byStatus[status].filter(t => !_archivedIds.has(t.task_id));
    }
  }

  _cardElements.clear();
  board.innerHTML = '';
  for (const status of _colOrder) {
    board.appendChild(renderColumn(status, byStatus[status] || [], agentMap));
  }
}

function renderColumn(status, tasks, agentMap) {
  const color = statusColor(status);
  const col = el('div', 'col');
  const hdr = el('div', 'col-header');
  hdr.style.background = color + '22';
  hdr.style.borderBottom = `2px solid ${color}`;
  const name = el('span', 'col-name', status.replace(/_/g,' '));
  name.style.color = color;

  const totalInStatus = _state ? (_state.tasks || []).filter(
    t => t.status === status
  ).length : tasks.length;
  const archivedCount = totalInStatus - tasks.length;
  const countText = archivedCount > 0
    ? `${tasks.length} +${archivedCount}\u2191`
    : String(tasks.length);
  const count = el('span', 'col-count', countText);
  count.style.color = color;
  if (archivedCount > 0) {
    count.title = `${archivedCount} archived from view (completed earlier)`;
  }
  hdr.appendChild(name); hdr.appendChild(count);
  col.appendChild(hdr);
  const cards = el('div', 'col-cards');
  if (tasks.length === 0) {
    cards.appendChild(el('div', 'col-empty', '—'));
  } else {
    for (const t of tasks) cards.appendChild(renderCard(t, color, agentMap));
  }
  col.appendChild(cards);
  return col;
}

function renderCard(task, color, agentMap) {
  const card = el('div', 'card');
  card.dataset.taskId = task.task_id;
  card.style.setProperty('--status-color', color);
  card.onclick = () => openDrawer(task.task_id);
  card.appendChild(el('div', 'card-label', task.label || task.task_type));
  if (task.priority != null && task.priority < 5) {
    const badge = el('span', 'card-priority');
    badge.textContent = `P${task.priority}`;
    badge.title = `Priority ${task.priority}`;
    badge.dataset.p = String(task.priority);
    card.appendChild(badge);
  }
  const meta = el('div', 'card-meta');
  meta.appendChild(el('div', 'card-id', shortId(task.task_id)));
  if (task.assigned_to) {
    const a = agentMap[task.assigned_to];
    meta.appendChild(el('div', 'card-agent', a ? a.name : task.assigned_to));
  }
  meta.appendChild(el('div', 'card-time', `in state ${timeAgo(task.updated_at)}`));
  if (TERMINAL_STATUSES.has(task.status)) {
    const hb = el('div', 'card-hb completed', '✓ done');
    meta.appendChild(hb);
  } else if (task.assigned_to && task.status !== 'UNASSIGNED') {
    const hb = el('div', 'card-hb');
    if (task.heartbeat_at) {
      const age = Math.floor((Date.now() - new Date(task.heartbeat_at)) / 1000);
      hb.className = age > 120 ? 'card-hb stale' : 'card-hb ok';
      hb.textContent = age > 120
        ? `⚠ heartbeat ${timeAgo(task.heartbeat_at)} ago`
        : `♡ ${timeAgo(task.heartbeat_at)} ago`;
    } else {
      hb.className = 'card-hb stale';
      hb.textContent = '⚠ no heartbeat';
    }
    meta.appendChild(hb);
  }
  card.appendChild(meta);
  _cardElements.set(task.task_id, card);
  return card;
}

const _STATUS_LABELS = {thinking:'Thinking…', acting:'Acting', sleeping:'Sleeping'};

function renderChief(telem) {
  const panel = document.getElementById('chief-panel');
  const section = document.getElementById('chief-section');
  const pulse = document.getElementById('chief-pulse');
  const label = document.getElementById('chief-status-label');

  if (!telem) {
    panel.innerHTML = '<div style="font-size:10px;color:var(--text3)">No telemetry yet</div>';
    pulse.className = 'sleeping';
    section.className = 'chief-sleeping';
    label.textContent = '';
    return;
  }

  const status = telem.status || 'sleeping';
  pulse.className = status;
  section.className = `chief-${status}`;
  label.textContent = status === 'sleeping' ? '' : `chief: ${status}`;

  panel.innerHTML = '';

  const banner = el('div', `chief-status-banner ${status}`);
  banner.appendChild(el('span', 'chief-status-dot'));
  banner.appendChild(el('span', 'chief-status-text', _STATUS_LABELS[status] || status));
  panel.appendChild(banner);

  const r2 = el('div', 'chief-row');
  r2.appendChild(el('span', 'chief-label', 'cycles'));
  const noop = telem.consecutive_noops > 0 ? `  (${telem.consecutive_noops} noop)` : '';
  r2.appendChild(el('span', 'chief-val', `${telem.cycles_run}${noop}`));
  panel.appendChild(r2);

  if (telem.last_cycle_duration_ms != null) {
    const r3 = el('div', 'chief-row');
    r3.appendChild(el('span', 'chief-label', 'last'));
    r3.appendChild(el('span', 'chief-val', `${telem.last_cycle_duration_ms}ms`));
    panel.appendChild(r3);
  }

  const durations = telem.recent_durations_ms || [];
  if (durations.length > 0) {
    const avg = Math.round(durations.reduce((a, b) => a + b, 0) / durations.length);
    const r4 = el('div', 'chief-row');
    r4.appendChild(el('span', 'chief-label', 'avg'));
    r4.appendChild(el('span', 'chief-val', `${avg}ms (n=${durations.length})`));
    panel.appendChild(r4);
  }

  if (telem.last_woke_at) {
    const r5 = el('div', 'chief-row');
    r5.appendChild(el('span', 'chief-label', 'woke'));
    r5.appendChild(el('span', 'chief-val', `${timeAgo(telem.last_woke_at)} ago  [${telem.last_trigger || '?'}]`));
    panel.appendChild(r5);
  }

  if (durations.length > 1) {
    const sparkline = el('div', 'chief-sparkline');
    const max = Math.max(...durations, 1);
    for (const d of durations) {
      const bar = el('div', 'chief-bar');
      bar.style.height = `${Math.max(2, Math.round((d / max) * 20))}px`;
      bar.title = `${d}ms`;
      sparkline.appendChild(bar);
    }
    panel.appendChild(sparkline);
  }
}

function _agentGroup(name) {
  const m = name && name.match(/^(.+?)\s+Worker\s+\d+$/i);
  return m ? m[1] : null;
}

function renderAgents(agents, tasks) {
  const list = document.getElementById('agents-list');
  const summary = document.getElementById('agents-summary');
  list.innerHTML = '';
  if (!agents || agents.length === 0) {
    list.appendChild(el('div', 'agent-row', 'No agents registered'));
    summary.textContent = '';
    return;
  }

  const busyFromTasks = new Map();
  for (const t of (tasks || [])) {
    if (t.assigned_to && !TERMINAL_STATUSES.has(t.status) &&
        !['UNASSIGNED','STALE','PENDING_REVIEW','REVISION_NEEDED','APPROVED','ON_HOLD'].includes(t.status)) {
      busyFromTasks.set(t.assigned_to, t.task_id);
    }
  }

  const busy = [];
  const idle = [];
  for (const a of agents) {
    const busyTaskId = busyFromTasks.get(a.agent_id);
    const isBusy = a.status === 'BUSY' || !!busyTaskId;
    if (isBusy) {
      busy.push({agent: a, taskId: busyTaskId || a.current_task_id});
    } else {
      idle.push(a);
    }
  }

  summary.textContent = `${busy.length} / ${agents.length} busy`;

  for (const {agent: a, taskId} of busy) {
    const row = el('div', 'agent-row');
    row.appendChild(el('span', 'agent-dot BUSY'));
    row.appendChild(el('span', 'agent-name', a.name || a.agent_id));
    if (taskId) {
      row.appendChild(el('span', 'agent-task-id', shortId(taskId)));
    } else {
      row.appendChild(el('span', 'agent-badge BUSY', 'BUSY'));
    }
    list.appendChild(row);
  }

  const groups = new Map();
  const ungrouped = [];
  for (const a of idle) {
    const grp = _agentGroup(a.name || a.agent_id);
    if (grp) {
      if (!groups.has(grp)) groups.set(grp, {idle: 0, total: 0});
      groups.get(grp).idle++;
    } else {
      ungrouped.push(a);
    }
  }
  for (const a of busy) {
    const grp = _agentGroup(a.agent.name || a.agent.agent_id);
    if (grp) {
      if (!groups.has(grp)) groups.set(grp, {idle: 0, total: 0});
    }
  }
  for (const a of agents) {
    const grp = _agentGroup(a.name || a.agent_id);
    if (grp && groups.has(grp)) groups.get(grp).total++;
  }

  if ((groups.size > 0 || ungrouped.length > 0) && busy.length > 0) {
    list.appendChild(el('hr', 'agents-divider'));
  }

  for (const [name, g] of groups) {
    const row = el('div', 'agent-group');
    row.appendChild(el('span', 'agent-group-name', name));
    const bar = el('div', 'agent-group-bar');
    const fill = el('div', 'agent-group-fill');
    fill.style.width = g.total > 0 ? `${Math.round((g.idle / g.total) * 100)}%` : '0%';
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el('span', 'agent-group-count', `${g.idle}/${g.total} idle`));
    list.appendChild(row);
  }

  for (const a of ungrouped) {
    const row = el('div', 'agent-row');
    row.appendChild(el('span', 'agent-dot IDLE'));
    row.appendChild(el('span', 'agent-name', a.name || a.agent_id));
    row.appendChild(el('span', 'agent-badge IDLE', 'IDLE'));
    list.appendChild(row);
  }
}

function renderEvents() {
  const list = document.getElementById('events-list');
  list.innerHTML = '';
  for (const ev of [..._recentEvents].reverse()) {
    const row = el('div', 'ev-row');
    row.title = `${ev.event_type}  task:${shortId(ev.task_id)}`;
    const body = el('div', 'ev-body');
    body.appendChild(el('div', `ev-type ${ev.event_type}`, ev.event_type));
    body.appendChild(el('div', 'ev-transition',
      `${ev.from_status||'—'} → ${ev.to_status||'—'}  ${shortId(ev.task_id)}`));
    body.appendChild(el('div', 'ev-time', fmtTime(ev.timestamp)));
    row.appendChild(el('span', 'ev-seq', `#${ev.sequence_id}`));
    row.appendChild(body);
    list.appendChild(row);
  }
}

function renderData(data) {
  const section = document.getElementById('data-section');
  const list = document.getElementById('data-list');
  // Filter out internal UI keys from the display
  const entries = Object.entries(data || {}).filter(([k]) => !k.startsWith('_'));
  if (entries.length === 0) { section.style.display = 'none'; return; }
  section.style.display = '';
  list.innerHTML = '';
  for (const [k, v] of entries) {
    const row = el('div', 'data-row');
    row.appendChild(el('span', 'data-key', k));
    row.appendChild(el('span', 'data-val',
      typeof v === 'object' ? JSON.stringify(v) : String(v)));
    list.appendChild(row);
  }
}

function updateGoalBadge(tasks, data) {
  const badge = document.getElementById('goal-badge');
  const goal = data && data.newsroom_goal;
  if (goal) {
    const target = goal.target_articles || 0;
    const done = tasks.filter(t => TERMINAL_STATUSES.has(t.status)).length;
    badge.textContent = `${done} / ${target} published`;
    badge.className = done >= target ? 'done' : '';
    return;
  }
  const delivered = tasks.filter(t => t.status === 'delivered').length;
  if (delivered > 0) { badge.textContent = `${delivered} delivered`; badge.className = ''; return; }
  const done = tasks.filter(t => TERMINAL_STATUSES.has(t.status)).length;
  badge.textContent = `${tasks.length} tasks · ${done} done`;
  badge.className = '';
}

async function openDrawer(taskId) {
  const task = _state && _state.tasks.find(t => t.task_id === taskId);
  if (!task) return;
  document.getElementById('drawer-title').textContent = task.label || task.task_type;
  document.getElementById('drawer-meta').textContent =
    `${shortId(taskId)}  ·  ${task.task_type}  ·  ${task.assigned_to || 'unassigned'}`;
  const body = document.getElementById('drawer-body');
  body.innerHTML = '<div style="color:var(--text3);font-size:11px">Loading…</div>';
  document.getElementById('drawer-overlay').className = 'open';
  document.getElementById('drawer').className = 'open';
  try {
    const resp = await fetch(`/api/task/${taskId}/history`);
    renderDrawerBody(body, task, (await resp.json()).events || []);
  } catch(e) {
    body.innerHTML = `<div style="color:var(--red)">HUMAN_REVIEW: ${e}</div>`;
  }
}

function renderDrawerBody(body, task, events) {
  body.innerHTML = '';
  body.appendChild(el('div', 'drawer-section-title', 'Timeline'));
  const tl = el('div', 'timeline');
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    const item = el('div', 'tl-event');
    const dot = el('span', `tl-dot${i === events.length-1 ? ' final' : ''}`);
    if (ev.to_status) dot.style.background = statusColor(ev.to_status);
    item.appendChild(dot);
    item.appendChild(el('div', `tl-type ev-type ${ev.event_type}`, ev.event_type));
    item.appendChild(el('div', 'tl-transition', `${ev.from_status||'—'} → ${ev.to_status||'—'}`));
    item.appendChild(el('div', 'tl-time', fmtTime(ev.timestamp)));
    tl.appendChild(item);
  }
  body.appendChild(tl);
  if (task.output) {
    const sep = el('div'); sep.style.cssText = 'border-top:1px solid var(--border);margin:14px 0 10px;';
    body.appendChild(sep);
    body.appendChild(el('div', 'drawer-section-title', 'Output (preview)'));
    body.appendChild(el('pre', 'output-preview',
      task.output.slice(0,1500) + (task.output.length > 1500 ? '\n…' : '')));
  }
  if (task.notes && task.notes.length) {
    const sep = el('div'); sep.style.cssText = 'border-top:1px solid var(--border);margin:14px 0 10px;';
    body.appendChild(sep);
    body.appendChild(el('div', 'drawer-section-title', 'Notes'));
    for (const note of task.notes) {
      const n = el('div'); n.style.cssText = 'font-size:11px;color:var(--text2);margin-bottom:4px;';
      n.textContent = note; body.appendChild(n);
    }
  }
}

function closeDrawer() {
  document.getElementById('drawer-overlay').className = '';
  document.getElementById('drawer').className = '';
}
document.getElementById('drawer-close').onclick = closeDrawer;
document.getElementById('drawer-overlay').onclick = closeDrawer;

async function fetchState() {
  try {
    const resp = await fetch('/api/state');
    if (!resp.ok) throw new Error(resp.statusText);
    _state = await resp.json();
    renderBoard(_state);
    renderAgents(_state.agents, _state.tasks);
    renderData(_state.data);
    updateGoalBadge(_state.tasks, _state.data);
    if (_state._terminal_statuses && _state._terminal_statuses.length) {
      TERMINAL_STATUSES = new Set(_state._terminal_statuses);
    }
    const meta = _state._meta;
    if (meta) {
      document.getElementById('last-updated').textContent = fmtTime(meta.server_time);
      renderChief(meta.chief || null);
    }
    setLiveDot(true);
  } catch(e) { console.error('State fetch failed:', e); setLiveDot(false); }
}

let _evtSource = null, _sseRetry = 1000;
function connectSSE() {
  if (_evtSource) _evtSource.close();
  _evtSource = new EventSource('/api/events');
  _evtSource.onopen = () => { setLiveDot(true); _sseRetry = 1000; };
  _evtSource.onmessage = (e) => {
    try {
      const evs = JSON.parse(e.data);
      _recentEvents = [..._recentEvents, ...evs].slice(-MAX_EVENTS);
      renderEvents();
      fetchState();
    } catch(err) { console.error(err); }
  };
  _evtSource.onerror = () => {
    setLiveDot(false); _evtSource.close();
    setTimeout(connectSSE, Math.min(_sseRetry *= 1.5, 30000));
  };
}
function setLiveDot(ok) {
  document.getElementById('live-dot').className = ok ? 'live-dot' : 'live-dot dead';
}
document.getElementById('theme-btn').onclick = () => {
  const b = document.body, light = b.dataset.theme === 'light';
  b.dataset.theme = light ? 'dark' : 'light';
  document.getElementById('theme-btn').textContent = light ? '☀︎' : '◑';
};
document.getElementById('refresh-btn').onclick = fetchState;
setInterval(fetchState, 4000);
connectSSE();
fetchState();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────────────────────────────────────


def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m quadro.ui",
        description="Quadro Board UI — zero-dependency live Kanban viewer",
    )
    parser.add_argument("db", nargs="?", help="Path to a Quadro SQLite database file")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="Open browser on startup")
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Wait up to SECONDS for the DB file to appear (0 = fail immediately)",
    )
    args = parser.parse_args()

    if not args.db:
        parser.print_help()
        print("\nExample:")
        print(
            "  python -m quadro.ui examples/microsoft_agent_framework/newsroom/newsroom.db"
        )
        sys.exit(0)

    db = Path(args.db)
    if not db.exists() and args.wait > 0:
        print(f"Waiting up to {args.wait}s for {db} ...", flush=True)
        deadline = time.monotonic() + args.wait
        while not db.exists() and time.monotonic() < deadline:
            time.sleep(1)
    if not db.exists():
        print(f"Error: file not found: {db}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=logging.WARNING)
    serve_board(
        db_path=str(db),
        port=args.port,
        host=args.host,
        open_browser=args.open,
        background=False,
    )


if __name__ == "__main__":
    _cli()
