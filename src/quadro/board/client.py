from __future__ import annotations

from ..a2a.contracts import A2ARequest
from ..a2a.dispatch import LocalA2ANetwork

_FALLBACK_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"COMPLETE", "HUMAN_REVIEW", "ON_HOLD"}
)


class BoardClient:
    """
    Typed, error-raising wrapper around LocalA2ANetwork + board URL.

    Eliminates the _req() helper and board_fn closure that every example
    and every worker agent currently duplicates. All calls go through
    LocalA2ANetwork.request() — the A2A boundary is never bypassed.

    Usage:
        client = BoardClient(network, "a2a://board")
        task = client.post_task("article", "gut health and anxiety")
        client.update_task(task["task_id"], "ideating", assigned_to="ideation_worker")
        state = client.full_state()
    """

    def __init__(self, network: LocalA2ANetwork, board_url: str) -> None:
        self._network = network
        self._board_url = board_url

    @property
    def network(self) -> LocalA2ANetwork:
        """The A2A network this client sends requests through."""
        return self._network

    @property
    def board_url(self) -> str:
        """The board's A2A URL. Useful for constructing Ombudsman and other components."""
        return self._board_url

    # ── Raw request — raises on error ──────────────────────────────────────────

    def request(self, intent: str, payload: dict) -> dict:
        """Execute any board intent. Raises RuntimeError if ok=False."""
        resp = self._network.request(
            self._board_url,
            A2ARequest(intent=intent, payload=payload).to_dict(),
        )
        if not resp["ok"]:
            raise RuntimeError(resp["error"])
        return resp["result"]

    # ── Task operations ────────────────────────────────────────────────────────

    def post_task(self, task_type: str, label: str, **kwargs) -> dict:
        """
        Post a new task. Returns the created task dict.

        Keyword args passed through to the board:
            priority (int): Task priority. Lower = more urgent. Default 5.
            notes (list[str]): Initial notes.
            task_id (str): Override the generated UUID.
        """
        return self.request(
            "board.post_task",
            {
                "task_type": task_type,
                "label": label,
                **kwargs,
            },
        )["task"]

    def update_task(self, task_id: str, to_status: str, **kwargs) -> dict:
        """Transition a task. Returns the updated task dict."""
        return self.request(
            "board.update_task",
            {
                "task_id": task_id,
                "to_status": to_status,
                **kwargs,
            },
        )["task"]

    def get_task(self, task_id: str) -> dict:
        """Fetch a single task by ID. Returns the task dict."""
        return self.request("board.get_task", {"task_id": task_id})["task"]

    def task_history(self, task_id: str) -> list[dict]:
        """Return all events for a task, ordered by sequence_id."""
        return self.request("board.get_task_history", {"task_id": task_id})["events"]

    # ── Agent operations ───────────────────────────────────────────────────────

    def register_agent(
        self,
        agent_id: str,
        name: str,
        url: str,
        capabilities: list[str],
        version: str = "0.1.0",
        description: str = "",
    ) -> dict:
        """Register an agent on the board. Returns the agent dict."""
        return self.request(
            "board.register_agent",
            {
                "agent_id": agent_id,
                "name": name,
                "url": url,
                "version": version,
                "description": description or name,
                "capabilities": capabilities,
            },
        )["agent"]

    def heartbeat(self, agent_id: str, task_id: str | None = None) -> dict:
        """Post an agent heartbeat. Returns the result dict."""
        payload: dict = {"agent_id": agent_id}
        if task_id:
            payload["task_id"] = task_id
        return self.request("board.post_agent_heartbeat", payload)

    # ── Board state ────────────────────────────────────────────────────────────

    def full_state(self) -> dict:
        """Return tasks, agents, and data entries."""
        return self.request("board.get_full_state", {})

    def stream_events(self, since_sequence: int = 0) -> list[dict]:
        """Return all events since the given sequence ID."""
        return self.request(
            "board.stream_events",
            {
                "since_sequence": since_sequence,
            },
        )["events"]

    # ── Data store ─────────────────────────────────────────────────────────────

    def put_data(self, key: str, value: dict) -> None:
        """Store an arbitrary value under key. Emits no events."""
        self.request("board.put_data", {"key": key, "value": value})

    def get_data(self, key: str) -> object:
        """Retrieve a stored value by key. Returns None if not found."""
        return self.request("board.get_data", {"key": key})["value"]

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(
        self,
        tools: list | None = None,
        *,
        goal_key: str | None = None,
        terminal_statuses: set[str] | None = None,
        max_tasks_per_status: int = 5,
    ) -> str | None:
        """
        Produce a structured board snapshot suitable as the chief's opening prompt.

        Reads the current board state and renders a compact summary covering goal
        progress, active tasks by status, agent availability, and available tools.

        Returns None if there is nothing actionable on the board — the chief policy
        can use this as a signal to return early without calling the LLM.
        """
        state = self.full_state()

        if terminal_statuses:
            _terminal = frozenset(terminal_statuses)
        else:
            board_terminals = state.get("_terminal_statuses")
            if board_terminals:
                _terminal = frozenset(board_terminals)
            else:
                _terminal = _FALLBACK_TERMINAL_STATUSES

        tasks = state.get("tasks", [])
        data = state.get("data", {})
        agents = state.get("agents", [])

        active = [t for t in tasks if t["status"] not in _terminal]
        terminal = [t for t in tasks if t["status"] in _terminal]

        if not active:
            return None

        lines: list[str] = []

        if goal_key and goal_key in data:
            goal = data[goal_key]
            for k, v in goal.items():
                if "target" in k.lower() and isinstance(v, (int, float)):
                    done = len(terminal)
                    label = k.replace("target_", "").replace("_", " ")
                    lines.append(f"Progress: {done}/{v} {label}")
                    break

        by_status: dict[str, list[dict]] = {}
        for t in active:
            by_status.setdefault(t["status"], []).append(t)

        lines.append(f"\nActive tasks: {len(active)}")
        for status, ts in by_status.items():
            lines.append(f"  [{status}] ({len(ts)})")
            for t in ts[:max_tasks_per_status]:
                lines.append(f"    {t['task_id'][:8]}  {t['label'][:60]}")
            if len(ts) > max_tasks_per_status:
                lines.append(f"    \u2026 and {len(ts) - max_tasks_per_status} more")

        idle = sum(1 for a in agents if a.get("status") == "IDLE")
        busy = sum(1 for a in agents if a.get("status") == "BUSY")
        lines.append(f"\nAgents: {idle} idle, {busy} busy")

        if tools:
            tool_names = []
            for t in tools:
                name = (
                    getattr(t, "name", None) or getattr(t, "__name__", None) or str(t)
                )
                tool_names.append(name)
            lines.append(f"\nAvailable tools: {', '.join(tool_names)}")

        return "\n".join(lines)
