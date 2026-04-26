"""OpenTelemetry exporter for Quadro coordination events.

Quadro's event log is already the authoritative coordination record; this
module is a translation of that record into a format that the rest of the
enterprise observability stack (Jaeger, Grafana Tempo, Honeycomb, Datadog)
can ingest natively.

Design constraint: the core ``quadro`` package stays zero-dependency, so
``opentelemetry`` is declared as an optional extra:

.. code-block:: bash

    pip install "quadro[otel]"

Usage
-----

Attach a :class:`QuadroTracer` once at runtime setup. The tracer wraps the
Chief's :meth:`~quadro.agents.chief.ChiefAgent.wake` entry point and
subscribes to the Board's event log. Neither the Board nor the Chief needs
any knowledge of OpenTelemetry:

.. code-block:: python

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    from quadro.integrations.otel import QuadroTracer

    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(ConsoleSpanExporter())
    )

    tracer = QuadroTracer.install(chief=chief, board=board)
    try:
        runtime.sponsor(...).run(pipeline)
    finally:
        tracer.detach()

What it produces
----------------

* One span per :class:`~quadro.board.records.EventRecord` — i.e. every
  lifecycle transition the board emits (``task_posted``, ``task_assigned``,
  ``task_completed``, ``task_reviewed``, ``task_stale``, ``task_failed``,
  ``task_reassigned``). Attributes: ``quadro.task_id``, ``quadro.from_status``,
  ``quadro.to_status``, ``quadro.agent_id``, ``quadro.event_type``,
  ``quadro.sequence_id``.
* One span per Chief wake cycle. Attributes: ``quadro.chief.trigger``,
  ``quadro.chief.cycles_run``, ``quadro.chief.cycles_in_wake``,
  ``quadro.chief.duration_ms``, ``quadro.chief.consecutive_noops``.

Every span is emitted under the tracer name ``quadro``, so filtering on
``span.name LIKE 'quadro.%'`` in your backend gives you the full coordination
trace without noise from user code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..board.records import EventRecord, TaskStatus

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from ..agents.chief import ChiefAgent
    from ..board.board import QuadroBoard

logger = logging.getLogger(__name__)

_OTEL_IMPORT_ERROR = (
    "OpenTelemetry is required for quadro.integrations.otel.  "
    "Install it with:  pip install 'quadro[otel]'"
)


def _ensure_otel() -> None:
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(_OTEL_IMPORT_ERROR) from exc


def _status_str(s: TaskStatus | str | None) -> str | None:
    if s is None:
        return None
    return s.value if isinstance(s, TaskStatus) else s


class QuadroTracer:
    """Bridge Quadro coordination events to OpenTelemetry spans.

    The tracer wraps :meth:`ChiefAgent.wake` so that every wake cycle becomes
    a span with a real duration, and registers a board event listener so that
    every :class:`EventRecord` becomes a span. The wrapping is fully
    reversible via :meth:`detach`.
    """

    SPAN_NAMESPACE = "quadro"

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        service_name: str = "quadro",
    ) -> None:
        _ensure_otel()
        from opentelemetry import trace as _otel_trace

        self._otel_trace = _otel_trace
        self._tracer: Tracer = tracer or _otel_trace.get_tracer(service_name)
        self._chief: ChiefAgent | None = None
        self._board: QuadroBoard | None = None
        self._original_wake: Callable[..., int] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def install(
        cls,
        *,
        chief: ChiefAgent,
        board: QuadroBoard | None = None,
        tracer: Tracer | None = None,
        service_name: str = "quadro",
    ) -> QuadroTracer:
        """Attach a fresh tracer to the chief (and optionally the board)."""
        instance = cls(tracer=tracer, service_name=service_name)
        instance.attach(chief, board)
        return instance

    def attach(
        self,
        chief: ChiefAgent,
        board: QuadroBoard | None = None,
    ) -> None:
        """Hook up the tracer. Safe to call multiple times on distinct chiefs."""
        if self._chief is not None:
            raise RuntimeError("QuadroTracer already attached; call detach() first")

        self._chief = chief
        self._board = board

        if board is not None:
            board.add_event_listener(self._on_board_event)

        original_wake = chief.wake
        self._original_wake = original_wake
        chief.wake = self._wrap_wake(original_wake, chief)  # type: ignore[method-assign]

    def detach(self) -> None:
        """Remove all hooks. Idempotent."""
        if self._board is not None:
            try:
                self._board.remove_event_listener(self._on_board_event)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to remove board event listener", exc_info=True)
            self._board = None

        if self._chief is not None and self._original_wake is not None:
            try:
                self._chief.wake = self._original_wake  # type: ignore[method-assign]
            except Exception:  # noqa: BLE001
                logger.debug("Failed to restore chief.wake", exc_info=True)
            self._chief = None
            self._original_wake = None

    def __enter__(self) -> QuadroTracer:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.detach()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _wrap_wake(
        self, original: Callable[..., int], chief: ChiefAgent
    ) -> Callable[..., int]:
        tracer = self._tracer
        StatusCode = self._otel_trace.StatusCode
        Status = self._otel_trace.Status

        def wrapped(trigger: str = "worker") -> int:
            with tracer.start_as_current_span(
                f"{self.SPAN_NAMESPACE}.chief_cycle"
            ) as span:
                t0 = time.monotonic()
                span.set_attribute("quadro.chief.trigger", trigger)
                try:
                    cycles_in_wake = original(trigger=trigger)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR))
                    raise
                duration_ms = int((time.monotonic() - t0) * 1000)
                span.set_attribute("quadro.chief.cycles_in_wake", cycles_in_wake)
                span.set_attribute("quadro.chief.cycles_run", chief.cycles_run)
                span.set_attribute("quadro.chief.duration_ms", duration_ms)
                telem = getattr(chief, "_telem", {}) or {}
                span.set_attribute(
                    "quadro.chief.consecutive_noops",
                    int(telem.get("consecutive_noops", 0) or 0),
                )
                return cycles_in_wake

        wrapped.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapped

    def _on_board_event(self, event: EventRecord) -> None:
        # Transitions are point-in-time events from the board's perspective:
        # the backend append has already committed. Emitting a zero-duration
        # span is the standard OTel idiom — it still appears on the trace
        # timeline in every viewer we checked (Jaeger, Tempo, Datadog).
        try:
            ts_ns = int(event.timestamp.timestamp() * 1_000_000_000)
            span_name = f"{self.SPAN_NAMESPACE}.{event.event_type}"
            with self._tracer.start_as_current_span(
                span_name,
                start_time=ts_ns,
            ) as span:
                span.set_attribute("quadro.event_type", event.event_type)
                span.set_attribute("quadro.task_id", event.task_id)
                span.set_attribute("quadro.sequence_id", event.sequence_id)
                from_status = _status_str(event.from_status)
                to_status = _status_str(event.to_status)
                if from_status is not None:
                    span.set_attribute("quadro.from_status", from_status)
                if to_status is not None:
                    span.set_attribute("quadro.to_status", to_status)
                if event.agent_id is not None:
                    span.set_attribute("quadro.agent_id", event.agent_id)
                if event.idempotency_key is not None:
                    span.set_attribute("quadro.idempotency_key", event.idempotency_key)
        except Exception:  # noqa: BLE001
            # OTel exporter failures must never break the board; the event
            # has already been persisted.
            logger.debug(
                "QuadroTracer failed to emit span for event %s",
                event.event_type,
                exc_info=True,
            )
