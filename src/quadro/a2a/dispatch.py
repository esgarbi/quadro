from __future__ import annotations

from collections.abc import Callable

Handler = Callable[[dict], dict]


class LocalA2ANetwork:
    """
    In-process A2A transport adapter used for tests and demos.
    Components communicate only through typed request envelopes.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register_endpoint(self, url: str, handler: Handler) -> None:
        self._handlers[url] = handler

    def request(self, url: str, envelope: dict) -> dict:
        handler = self._handlers.get(url)
        if not handler:
            raise KeyError(f"No endpoint registered for {url}")
        return handler(envelope)
