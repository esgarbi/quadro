# Changelog

## Unreleased

### Breaking changes

- **Lifetime model replaced by Sponsor/Lease.** `QuadroRuntime.done_when`,
  `QuadroRuntime.max_cycles`, `RunLoop.done_when`, and
  `RunLoop.max_cycles` are removed. Runtimes must now install a
  :class:`~quadro.sponsor.Sponsor` via `.sponsor(sponsor)`.
  `GoalSponsor(predicate)` is the canonical drop-in replacement for
  `done_when(predicate)`; `TickBudgetSponsor(n)` replaces `max_cycles(n)`.
  See [docs/guides/sponsor-migration.md](docs/guides/sponsor-migration.md).
- **`BuiltPipeline.run(done_when=..., max_cycles=...)` removed.** Route
  built pipelines through `QuadroRuntime.run(pipeline)` instead. Examples
  under `examples/microsoft_agent_framework/*/main_pipeline.py` show the
  new shape.

### Added

- `quadro.sponsor` — new module exposing:
  - :class:`Sponsor` protocol, :class:`Lease`, :class:`LeaseDecision`
    union (`Continue` / `Drain` / `Stop`), :class:`SponsorContext`,
    :class:`MeterReadings`.
  - Leaf sponsors: :class:`GoalSponsor`, :class:`DeadlineSponsor`,
    :class:`TickBudgetSponsor`, :class:`WorkerBudgetSponsor`,
    :class:`LlmTokenBudgetSponsor`, :class:`BoardEventBudgetSponsor`,
    :class:`CallableSponsor`, :class:`QueueDepthSponsor`.
  - External sponsors: :class:`HttpSponsor`, :class:`CallbackSponsor`.
  - Composers: :class:`AllOf`, :class:`AnyOf`, :class:`Priority`.
  - Test fixtures: :class:`AlwaysOnSponsor`, :class:`AlwaysStopSponsor`,
    :class:`ScriptedSponsor`.
- Drain semantics are first-class. `Drain(deadline, reason)` suppresses
  new task assignment while letting in-flight work complete. The runtime
  publishes a drain flag at `_runtime_draining` and a status snapshot at
  `_sponsor_status`.
- Observability: `_sponsor_log` (bounded list of recent decisions) and
  `_sponsor_status` (active lease + draining flag) are persisted to the
  board and surfaced in the UI sidebar as a new **Sponsor** panel.
- `QuadroBoard.add_event_listener` / `remove_event_listener` — subscribe
  to every board event (consumed by `BoardEventMeter`).
- `ChiefAgent.add_wake_listener` / `remove_wake_listener` — subscribe to
  every chief wake (consumed by `WorkerInvocationMeter`).
- `ChiefAgent.set_draining(bool)` and `is_draining()` — drain flag with
  board-published telemetry.
- `quadro.dispatch.is_draining(board_fn)` and `DRAIN_FLAG_KEY` —
  dispatch helpers consult the flag so custom chief policies cooperate
  without per-call-site changes.
- `QuadroRuntime.drain_max_duration(td)` — override the 5-minute default
  fallback deadline used when a Sponsor returns `Drain(deadline=None)`.
- New example `examples/crm_sponsor/` — mocked CRM ticket drives a
  runtime's lifetime, showing the continuity story end-to-end.

### Documentation

- `docs/design/sponsor.md` — design doc with locked API and open-question
  answers.
- `docs/guides/sponsor-authoring.md` — how to write a Sponsor.
- `docs/guides/sponsor-decision-matrix.md` — "I want X, use Y" lookup.
- `docs/guides/sponsor-migration.md` — step-by-step migration guide.
