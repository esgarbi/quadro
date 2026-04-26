from __future__ import annotations

from quadro.llm_token_budget_reporting import build_ticket_records


class _CaptureLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, msg: str, *args) -> None:  # noqa: ANN001
        self.messages.append(msg % args if args else msg)


def test_build_ticket_records_maps_by_ticket_id_not_task_position() -> None:
    tickets = [
        {"id": "T-001", "subject": "A", "body": "Body A"},
        {"id": "T-002", "subject": "B", "body": "Body B"},
    ]
    final_tasks = [
        {
            "task_id": "task-2",
            "objective": "Ticket T-002: B",
            "status": "classified",
            "output": (
                '{"urgency":"high","category":"billing","suggested_reply":"reply b"}'
            ),
        },
        {
            "task_id": "task-1",
            "objective": "Ticket T-001: A",
            "status": "classified",
            "output": '{"urgency":"low","category":"other","suggested_reply":"reply a"}',
        },
    ]

    records = build_ticket_records(final_tasks, tickets)

    assert [r["ticket_id"] for r in records] == ["T-002", "T-001"]
    assert records[0]["subject"] == "B"
    assert records[1]["subject"] == "A"


def test_build_ticket_records_prefers_metadata_ticket_id() -> None:
    tickets = [{"id": "T-001", "subject": "A", "body": "Body A"}]
    final_tasks = [
        {
            "task_id": "task-1",
            "metadata": {"ticket_id": "T-001"},
            "objective": "Ticket WRONG: Ignored",
            "status": "classified",
            "output": '{"urgency":"medium","category":"account","suggested_reply":"ok"}',
        }
    ]

    records = build_ticket_records(final_tasks, tickets)

    assert records[0]["ticket_id"] == "T-001"
    assert records[0]["subject"] == "A"
    assert records[0]["category"] == "account"


def test_build_ticket_records_warns_for_unresolved_tasks() -> None:
    logger = _CaptureLogger()
    tickets = [{"id": "T-001", "subject": "A", "body": "Body A"}]
    final_tasks = [{"task_id": "task-1", "status": "classified", "output": "{}"}]

    records = build_ticket_records(final_tasks, tickets, logger=logger)

    assert records[0]["ticket_id"] is None
    assert records[0]["subject"] is None
    assert logger.messages
    assert "could not resolve" in logger.messages[0]
