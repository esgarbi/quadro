"""Structured logging context for Quadro.

Logs become actionable audit trail fodder only when they carry the
coordination identifiers the board already knows about. This module
exposes three :class:`~contextvars.ContextVar`\\ s —

* ``task_id_var``
* ``chief_cycle_id_var``
* ``agent_id_var``

— along with lightweight context managers that set and reset them safely.

The :class:`QuadroContextFilter` can be attached to any stdlib logging
handler and automatically enriches every :class:`logging.LogRecord` with
the current values:

.. code-block:: python

    import logging
    from quadro.log_context import QuadroContextFilter

    handler = logging.StreamHandler()
    handler.addFilter(QuadroContextFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [task=%(quadro_task_id)s "
        "cycle=%(quadro_chief_cycle_id)s agent=%(quadro_agent_id)s] "
        "%(name)s %(message)s"
    ))

Quadro's own modules (:mod:`quadro.agents.chief`, :mod:`quadro.agents.worker`)
set the vars at the appropriate scopes. Other modules just need to call
:func:`logging.getLogger(...)` as usual and the values flow through.

The same :class:`contextvars.ContextVar` instances are read by the OTel
exporter (:mod:`quadro.integrations.otel`) so traces and logs correlate
without duplicating plumbing.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "quadro_task_id", default=None
)
chief_cycle_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "quadro_chief_cycle_id", default=None
)
agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "quadro_agent_id", default=None
)


class QuadroContextFilter(logging.Filter):
    """Inject Quadro context variables as attributes on every log record.

    Attaching this filter to a handler guarantees that every record sees the
    three ``quadro_*`` attributes, which means formatters can reference them
    unconditionally without raising ``KeyError`` on records that happen to
    come from code that didn't set the context.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.quadro_task_id = task_id_var.get() or "-"
        record.quadro_chief_cycle_id = chief_cycle_id_var.get() or "-"
        record.quadro_agent_id = agent_id_var.get() or "-"
        return True


@contextmanager
def task_scope(task_id: str | None) -> Iterator[None]:
    """Bind ``task_id`` to the current logical scope.

    Also scoped across ``asyncio.to_thread`` boundaries because
    :func:`contextvars.copy_context` is invoked by the stdlib's
    :func:`~asyncio.run_in_executor` wrapper for :func:`asyncio.to_thread`.
    """
    token = task_id_var.set(task_id)
    try:
        yield
    finally:
        task_id_var.reset(token)


@contextmanager
def chief_cycle_scope(cycle_id: str | None) -> Iterator[None]:
    """Bind ``chief_cycle_id`` to the current logical scope."""
    token = chief_cycle_id_var.set(cycle_id)
    try:
        yield
    finally:
        chief_cycle_id_var.reset(token)


@contextmanager
def agent_scope(agent_id: str | None) -> Iterator[None]:
    """Bind ``agent_id`` to the current logical scope."""
    token = agent_id_var.set(agent_id)
    try:
        yield
    finally:
        agent_id_var.reset(token)
