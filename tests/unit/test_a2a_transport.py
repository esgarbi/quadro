from __future__ import annotations

from quadro import LocalA2ANetwork
from quadro.a2a.dispatch import A2ATransport


def test_local_network_satisfies_transport_protocol() -> None:
    network = LocalA2ANetwork()
    assert isinstance(network, A2ATransport)


def test_custom_transport_satisfies_protocol() -> None:
    class StubTransport:
        def request(self, url: str, envelope: dict) -> dict:
            return {"ok": True, "result": {}, "request_id": "x", "error": None}

        def register_endpoint(self, url: str, handler) -> None:
            pass

    transport = StubTransport()
    assert isinstance(transport, A2ATransport)
