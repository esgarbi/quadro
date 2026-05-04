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
# Deterministic examples (no API key needed)
python examples/cooperation/main.py
python examples/ordering_minimal/main.py

# LLM-backed examples (require OPENAI_API_KEY or ANTHROPIC_API_KEY)
python examples/newsroom/main_pipeline.py
python examples/ordering/main_pipeline.py
python examples/anthropic_minimal/main.py
```

## Board UI

```bash
# In one terminal, run an example:
python examples/newsroom/main_pipeline.py

# In another terminal, watch the Kanban view live:
python -m quadro.ui examples/newsroom/newsroom.db --open
```
