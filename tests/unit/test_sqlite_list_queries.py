from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.records import TaskRecord, TaskStatus


def test_list_tasks_uses_single_select() -> None:
    backend = SqliteBoardBackend(":memory:")
    backend.init()
    for i in range(5):
        backend.create_task(
            TaskRecord(
                task_id=f"t{i}",
                task_type="draft",
                label=f"b{i}",
                status=TaskStatus.UNASSIGNED,
            )
        )
    statements: list[str] = []

    def trace(q: str) -> None:
        if q.strip().upper().startswith("SELECT"):
            statements.append(q)

    backend._conn.set_trace_callback(trace)
    backend.list_tasks()
    backend._conn.set_trace_callback(None)
    assert len(statements) == 1


def test_list_agents_uses_single_select() -> None:
    from quadro.board.records import AgentRecord, AgentStatus

    backend = SqliteBoardBackend(":memory:")
    backend.init()
    for i in range(4):
        backend.upsert_agent(
            AgentRecord(
                agent_id=f"a{i}",
                name=f"Agent{i}",
                status=AgentStatus.IDLE,
                capabilities=["x"],
                a2a_url=f"http://a{i}",
                agent_card={},
            )
        )
    statements: list[str] = []

    def trace(q: str) -> None:
        if q.strip().upper().startswith("SELECT"):
            statements.append(q)

    backend._conn.set_trace_callback(trace)
    backend.list_agents()
    backend._conn.set_trace_callback(None)
    assert len(statements) == 1
