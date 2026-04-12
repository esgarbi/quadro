import pytest

from quadro import ValidationError
from quadro.a2a.contracts import A2ARequest, validate_request_envelope


def test_typed_request_envelope_validation() -> None:
    envelope = A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    validate_request_envelope(envelope)


def test_intent_whitelist_validation() -> None:
    envelope = A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    envelope["intent"] = "unknown.intent"
    with pytest.raises(ValidationError):
        validate_request_envelope(envelope)
