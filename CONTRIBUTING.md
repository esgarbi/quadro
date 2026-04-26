# Contributing to Quadro

## Setup

```bash
# Fork the repo on GitHub first, then:
git clone https://github.com/<your-github-username>/quadro.git
cd quadro
pip install -e ".[dev]"
```

## Running the test suite

```bash
pytest
```

All tests use `LocalA2ANetwork` and `SqliteBoardBackend(":memory:")`. No external
processes, no HTTP, no real databases required.

## Test conventions

- **Unit tests** go in `tests/unit/`. Test a single component in isolation.
- **Integration tests** go in `tests/integration/`. Test component interactions
  through the A2A layer.
- All board access in tests goes through `network.request()` with typed A2A envelopes.
  Do not call board methods directly or access private methods (prefixed `_`).
- Use `A2ARequest` from `quadro.a2a.contracts` to build envelopes.

## Architecture invariants

These must hold in all new code and tests:

1. **Board is the single source of truth.** All coordination state is persisted
   on the board. No state is held in agents between invocations.
2. **A2A-only boundaries.** No direct method calls between board, chief, and workers.
   All cross-component calls go through `network.request()`.
3. **Single transition, single event.** Every valid state transition emits exactly
   one immutable event. Invalid transitions emit nothing.
4. **Chief serialization.** Only one chief decision loop runs at a time.
5. **Frozen event taxonomy.** Only the event types in `FROZEN_EVENT_TYPES` are valid.
   Adding a new event type is a versioning decision, not a convenience.

## Running the examples

```bash
python examples/core/newsroom_cooperation/main.py
python examples/core/ordering_system/main.py
```

## Board UI

```bash
python examples/core/newsroom_cooperation/main.py
python -m quadro.ui newsroom.db --open
```
