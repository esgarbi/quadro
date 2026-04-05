from __future__ import annotations

from dataclasses import dataclass

from .contracts import A2ARequest
from .dispatch import LocalA2ANetwork


@dataclass
class EventSubscriber:
    network: LocalA2ANetwork
    board_url: str
    cursor: int = 0

    def poll(self) -> list[dict]:
        envelope = A2ARequest(
            intent="board.stream_events",
            payload={"since_sequence": self.cursor},
        ).to_dict()
        response = self.network.request(self.board_url, envelope)
        if not response["ok"]:
            raise RuntimeError(response["error"])
        events = response["result"]["events"]
        if events:
            self.cursor = max(event["sequence_id"] for event in events)
        return events
