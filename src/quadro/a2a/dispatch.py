from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

Handler = Callable[[dict], dict]


@runtime_checkable
class A2ATransport(Protocol):
    """
    Transport interface for A2A communication between Quadro components.

    Any class with ``request()`` and ``register_endpoint()`` methods
    satisfying these signatures is a valid transport. ``LocalA2ANetwork``
    is the in-process implementation; ``HttpA2ANetwork`` (planned) will
    provide multi-process deployment over HTTP.
    """

    def request(self, url: str, envelope: dict) -> dict: ...

    def register_endpoint(self, url: str, handler: Handler) -> None: ...


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
