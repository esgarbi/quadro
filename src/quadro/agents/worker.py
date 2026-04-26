from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable
from uuid import uuid4

from ..a2a.contracts import A2ARequest, A2AResponse, validate_request_envelope
from ..a2a.dispatch import A2ATransport
from ..board.client import BoardClient
from ..board.records import AgentStatus
from ..log_context import agent_scope, task_scope
from .hydration import hydrate_worker_context


class WorkerAgent:
    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        capabilities: list[str],
        url: str,
        board_url: str,
        network: A2ATransport,
        execute_fn: Callable[[dict, Callable[[str, dict], dict]], str],
        reviewer_mode: bool = False,
        chief_url: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.capabilities = capabilities
        self.url = url
        self.board_url = board_url
        self.network = network
        self.execute_fn = execute_fn
        self.reviewer_mode = reviewer_mode
        self._chief_url = chief_url
        self._board_client = BoardClient(network, board_url)
        self.network.register_endpoint(url, self.handle_request)

    def register(self) -> dict:
        response = self.network.request(
            self.board_url,
            A2ARequest(
                intent="board.register_agent",
                payload={
                    "agent_id": self.agent_id,
                    "name": self.name,
                    "url": self.url,
                    "version": "0.1.0",
                    "description": f"{self.name} worker",
                    "capabilities": self.capabilities,
                    "status": AgentStatus.IDLE.value,
                },
            ).to_dict(),
        )
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]

    def handle_request(self, envelope: dict) -> dict:
        request_id = envelope.get("request_id", uuid4().hex[:12])
        try:
            validate_request_envelope(envelope)
            if envelope["intent"] != "worker.execute_task":
                raise ValueError(f"Worker does not support intent {envelope['intent']}")
            result = self._execute_task(envelope["payload"])
            return A2AResponse(request_id=request_id, ok=True, result=result).to_dict()
        except Exception as exc:  # noqa: BLE001
            return A2AResponse(
                request_id=request_id, ok=False, error=str(exc)
            ).to_dict()

    def _board_request(self, intent: str, payload: dict) -> dict:
        return self._board_client.request(intent, payload)

    def _wake_chief(self) -> None:
        """Signal the Chief that the board has changed. Fire-and-forget."""
        if self._chief_url:
            try:
                self.network.request(
                    self._chief_url,
                    A2ARequest(intent="chief.wake", payload={}).to_dict(),
                )
            except Exception:  # noqa: BLE001
                pass  # Best-effort. Board event will also wake Chief.

    def _execute_task(self, payload: dict) -> dict:
        task_id = payload["task_id"]
        with task_scope(task_id), agent_scope(self.agent_id):
            return self._execute_task_inner(task_id)

    def _execute_task_inner(self, task_id: str) -> dict:
        task = self._board_request("board.get_task", {"task_id": task_id})["task"]
        context = hydrate_worker_context(task, notes=task.get("notes", []))
        self._board_request(
            "board.post_agent_heartbeat",
            {"agent_id": self.agent_id, "task_id": task["task_id"]},
        )
        _exec_log = logging.getLogger(__name__)

        try:
            raw = self.execute_fn(context, self._board_request)
            if asyncio.iscoroutine(raw):

                def _run_coro(coro):
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(coro)
                    finally:
                        try:
                            pending = asyncio.all_tasks(loop)
                            if pending:
                                loop.run_until_complete(
                                    asyncio.gather(*pending, return_exceptions=True)
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        loop.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    output = pool.submit(_run_coro, raw).result()
            else:
                output = raw
        except Exception as exc:
            _exec_log.error(
                "Worker %s: execute_fn failed for task %s: %s",
                self.agent_id,
                task["task_id"][:8],
                exc,
            )
            try:
                self._board_request(
                    "board.update_task",
                    {
                        "task_id": task["task_id"],
                        "to_status": "HUMAN_REVIEW",
                        "notes_append": f"Worker error: {str(exc)[:500]}",
                    },
                )
            except Exception as board_exc:  # noqa: BLE001
                _exec_log.error(
                    "Worker %s: failed to mark task %s as HUMAN_REVIEW: %s",
                    self.agent_id,
                    task["task_id"][:8],
                    board_exc,
                )
            self._wake_chief()
            raise
        if self.reviewer_mode:
            if task["status"] != "IN_PROGRESS":
                raise RuntimeError(
                    f"Reviewer expects task {task['task_id']} in IN_PROGRESS; got {task['status']}"
                )
            # execute_fn may return "REVISION_NEEDED" to reject or any other value to approve.
            to_status = "REVISION_NEEDED" if output == "REVISION_NEEDED" else "APPROVED"
            update_result = self._board_request(
                "board.update_task",
                {
                    "task_id": task["task_id"],
                    "to_status": to_status,
                    "assigned_to": self.agent_id,
                    "notes_append": output,
                },
            )
            result: dict = {"mode": "review", "task": update_result["task"]}
            self._wake_chief()
            return result

        # If execute_fn already transitioned the task away from IN_PROGRESS (operational
        # workers that call board_fn directly), skip worker.post_result — the work is done.
        current = self._board_request("board.get_task", {"task_id": task["task_id"]})[
            "task"
        ]
        if current["status"] != "IN_PROGRESS":
            self._wake_chief()
            return {"mode": "execution", "task": current}

        post_result = self._board_request(
            "worker.post_result",
            {"task_id": task["task_id"], "agent_id": self.agent_id, "output": output},
        )
        self._wake_chief()
        return {"mode": "execution", "task": post_result["task"]}

    # ── Builder factory ────────────────────────────────────────────────────────

    @classmethod
    def builder(
        cls, agent_id: str, board_client: "BoardClient"
    ) -> "WorkerAgentBuilder":
        return WorkerAgentBuilder(agent_id, board_client)


class WorkerAgentBuilder:
    """
    Fluent builder for WorkerAgent.

    Usage:
        worker = (
            WorkerAgent.builder("ideation_worker", board_client)
            .name("Ideation Worker")
            .capability("ideation")
            .at("a2a://workers/ideation")
            .execute(run_ideation)
            .wakes("a2a://chief")
            .build()
        )
    """

    def __init__(self, agent_id: str, board_client: "BoardClient") -> None:
        self._agent_id = agent_id
        self._board_client = board_client
        self._name: str = agent_id
        self._capabilities: list[str] = []
        self._url: str | None = None
        self._execute_fn = None
        self._reviewer_mode: bool = False
        self._chief_url: str | None = None

    def name(self, name: str) -> "WorkerAgentBuilder":
        self._name = name
        return self

    def capability(self, *caps: str) -> "WorkerAgentBuilder":
        """Add one or more capabilities. Chainable."""
        self._capabilities.extend(caps)
        return self

    def at(self, url: str) -> "WorkerAgentBuilder":
        """Set the worker's A2A URL."""
        self._url = url
        return self

    def execute(self, fn) -> "WorkerAgentBuilder":
        """Set the execute_fn."""
        self._execute_fn = fn
        return self

    def reviewer(self) -> "WorkerAgentBuilder":
        """Enable reviewer mode."""
        self._reviewer_mode = True
        return self

    def wakes(self, chief_url: str) -> "WorkerAgentBuilder":
        """Set the chief URL to signal on completion."""
        self._chief_url = chief_url
        return self

    def build(self) -> "WorkerAgent":
        if not self._url:
            raise ValueError(f"WorkerAgent {self._agent_id!r} requires .at(url)")
        if self._execute_fn is None:
            raise ValueError(f"WorkerAgent {self._agent_id!r} requires .execute(fn)")
        return WorkerAgent(
            agent_id=self._agent_id,
            name=self._name,
            capabilities=self._capabilities,
            url=self._url,
            board_url=self._board_client._board_url,
            network=self._board_client._network,
            execute_fn=self._execute_fn,
            reviewer_mode=self._reviewer_mode,
            chief_url=self._chief_url,
        )
