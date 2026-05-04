# LLM Adapter Guide

Quadro's substrate package (`src/quadro/`) imports zero LLM frameworks.
Every integration with a specific LLM provider or agent framework lives
as a **sibling package** that imports `quadro` and is imported by user
code — never the other way around. This guide documents the two
protocols that make adapters easy to add and walks through the recipe
for writing a new one.

## What ships today

Three sibling adapter packages live alongside the substrate:

- **`quadro_maf`** — Microsoft Agent Framework. Provides
  `MafReasoner` (reason-step adapter) and `MafChiefRuntime`
  (framework-runtime adapter for `stage(workflow=...)` paths).
  Install via `pip install "quadro[maf]"`.
- **`quadro_langchain`** — LangChain / LangGraph. Provides
  `LangChainReasoner` and `LangChainChiefRuntime` for
  `stage(supervisor=...)` and `stage(graph=...)` paths.
  Install via `pip install "quadro[langchain]"`.
- **`quadro_anthropic`** — Anthropic SDK. Provides
  `AnthropicReasoner` only — Anthropic ships an SDK rather than a
  full agent framework, so there is no `AnthropicChiefRuntime`. For
  Claude-driven chief logic, use `stage(execute_fn=...)` directly.
  Install via `pip install "quadro[anthropic]"`.

A 30-line bare-OpenAI reference adapter at
[`examples/minimal/openai_reasoner.py`](../../examples/minimal/openai_reasoner.py)
demonstrates that the protocol's reach extends beyond the shipped
packages — any SDK that can fulfill prompt-in / response-out plugs in
the same way.

## The two protocols

Adapters implement one or both of two protocols. Reasoners are
mandatory for any saga that uses `reason` steps; framework runtimes
are optional and only matter when a stage wants to delegate to a
native framework primitive (`stage(workflow=...)`,
`stage(supervisor=...)`).

### `Reasoner` — for reason-step LLM dispatch

The `Reasoner` protocol lives at
[`src/quadro/saga/reasoner.py`](../../src/quadro/saga/reasoner.py).
Any object with two attributes qualifies:

```python
from typing import Protocol, Callable

class Reasoner(Protocol):
    reasoner_id: str

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> ReasonResult:
        ...
```

A reason step looks like one LLM call from the saga's perspective,
but the reasoner behind it can do anything — a single API call, a
multi-turn ReAct loop, a hierarchical agent, an entire LangGraph
wrapped behind one async method. The saga sees one input and one
output; the reasoner owns everything in between. See [Building Deep
Agents With Custom Reasoners](saga-authoring.md#building-deep-agents-with-custom-reasoners)
for the full pattern.

### `FrameworkRuntime` — for stage-level framework integration

The `FrameworkRuntime` protocol lives at
[`src/quadro/runtime_plugins/base.py`](../../src/quadro/runtime_plugins/base.py)
and formalises the seam where a framework's native execution
primitives (workflows, graphs, supervisors) plug into Quadro's
governance layer. The protocol has four methods:

```python
from typing import Protocol

class FrameworkRuntime(Protocol):
    def can_handle(self, stage_spec) -> bool: ...
    def decorate_tools(self, descriptors): ...
    async def run_chief_turn(self, board_summary, instructions, tools): ...
    async def run_stage(self, ctx) -> StageOutcome: ...
```

The ownership model is explicit. Quadro core owns governance —
`Lifecycle`, `Sponsor`/`Lease`, drain/stop, board state transitions,
policy decisions. Framework runtimes own execution internals —
workflow/graph execution, tool semantics, framework-specific events.
Runtime plugins translate framework behavior into Quadro's normalized
observability envelope (`quadro.runtime_event.v1`) and report tokens
through `runtime.meters.report_llm_tokens`.

A reasoner-only adapter (like `quadro_anthropic`) implements the
`Reasoner` protocol and skips `FrameworkRuntime` entirely. A
full-stack adapter (like `quadro_maf` and `quadro_langchain`)
implements both, so users can choose between `stage(saga=...)`
backed by the reasoner and `stage(workflow=...)` /
`stage(supervisor=...)` backed by the framework runtime.

## Anatomy of a sibling package

Every shipped adapter follows the same layout. Treat
`src/quadro_anthropic/` as the simplest reference (reasoner-only) and
`src/quadro_maf/` as the fuller one (reasoner + framework runtime):

```
src/quadro_xyz/
├── __init__.py            Re-exports public classes
├── _internal.py           Optional dependency guard, token extraction
├── reasoner.py            XYZReasoner — implements the Reasoner protocol
├── runtime.py             XYZChiefRuntime — implements FrameworkRuntime
│                          (omit if reasoner-only, like quadro_anthropic)
└── py.typed               Marker file
```

The `_internal.py` module hides framework imports behind an
`_ensure_xyz()` guard that raises a clean `ImportError` when the
optional extra isn't installed:

```python
def _ensure_xyz() -> None:
    try:
        import xyz_framework  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "quadro_xyz requires xyz-framework. "
            "Install with: pip install 'quadro[xyz]'"
        ) from exc
```

Public classes call `_ensure_xyz()` in `__init__` so the error fires
at construction time, not deep inside an async dispatch path.

## Recipe — writing a Reasoner adapter

This is the minimal case (one protocol, no framework runtime). Use
[`src/quadro_anthropic/reasoner.py`](../../src/quadro_anthropic/reasoner.py)
as the live reference.

### Step 1. Implement the `Reasoner` protocol

```python
class XYZReasoner:
    """Reason-step adapter for the XYZ provider."""

    reasoner_id: str = "xyz"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        model: str = "xyz-default-model",
    ) -> None:
        _ensure_xyz()
        self._client_factory = client_factory
        self._model = model

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
        step_name: str | None = None,
    ) -> ReasonResult:
        client = self._client_factory()
        raw, tokens = await _call_xyz(client, self._model, prompt, user_message)
        if token_reporter is not None:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        output = schema.model_validate_json(raw) if schema is not None else raw
        return ReasonResult(output=output, tokens_used=tokens, raw_text=raw)
```

A few non-obvious points:

- **`reasoner_id` is the routing key.** When a saga registers
  multiple reasoners, individual `reason` steps select one with
  `via="xyz"`. Keep the id short and lowercase.
- **`token_reporter` is best-effort.** Wrap the call in
  `try/except` so a malformed token counter never fails an
  otherwise-successful step.
- **`schema` may be `None`.** When it is, return the raw cleaned
  text. When it isn't, validate against it and return the validated
  instance.
- **`step_name` is an optional informational hint** added by the
  Estimator milestone for the `CollectingReasoner`. Most reasoners
  ignore it.
- **`client_factory` is a pluggable seam.** Accept a user-owned
  callable that returns a configured client rather than constructing
  one yourself. Users can point any provider — the underlying SDK,
  a mock for tests, a proxy — at the same reasoner.

### Step 2. Re-export the public class

```python
# src/quadro_xyz/__init__.py
from .reasoner import XYZReasoner

__all__ = ["XYZReasoner"]
```

### Step 3. Register the optional extra

In `pyproject.toml`:

```toml
[project.optional-dependencies]
xyz = ["xyz-framework>=1.0,<2.0"]
```

Install with `pip install "quadro[xyz]"`.

### Step 4. Register on a Pipeline

User code constructs the LLM client with the underlying SDK, wires
it through the reasoner, and registers the reasoner on a pipeline:

```python
from quadro import Pipeline, QuadroRuntime
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro_xyz import XYZReasoner

def client_factory():
    from xyz_framework import XYZClient
    return XYZClient(api_key="...")

runtime = QuadroRuntime(SqliteBoardBackend())
pipeline = (
    Pipeline(runtime.board)
    .reasoner(XYZReasoner(client_factory=client_factory))
    .stage("classify", saga=my_saga, active_status="classifying")
    .build()
)
```

`Pipeline.reasoner(...)` accepts any object satisfying the protocol.
Multiple calls register multiple reasoners; saga `reason` steps
select among them with `via="reasoner_id"` when more than one is
registered.

### Step 5. Ship a minimal example

Mirror the structure of
[`examples/anthropic_minimal/`](../../examples/anthropic_minimal/) —
one task, one saga, one reason step, plus the `_format_tokens` /
`_print_token_usage` helpers from the project-wide
[token-usage-in-output convention](saga-authoring.md#surfacing-token-usage-in-examples).

That's it. The new adapter gets phase-1 token records, phase-2 Costs
tab visibility, Estimator cost projection, and sponsor budget
enforcement — all for free. The substrate's plug-in story handles
all of it through the same `Reasoner` protocol.

## Recipe — writing a `FrameworkRuntime` adapter

The fuller case is for adapters that need to plug into Quadro's
stage-level execution paths. This is what makes
`stage(workflow=...)` (MAF) and `stage(supervisor=...)` (LangChain)
work. Use [`src/quadro_maf/runtime.py`](../../src/quadro_maf/runtime.py)
as the live reference.

### Step 1. Implement the four `FrameworkRuntime` methods

```python
class XYZChiefRuntime:
    """Framework-runtime adapter for XYZ workflow primitives."""

    def __init__(self, *, client_factory: Callable[[], Any]) -> None:
        _ensure_xyz()
        self._client_factory = client_factory

    def can_handle(self, stage_spec) -> bool:
        # Return True for stages this runtime owns (e.g. the ones
        # carrying an XYZ workflow object on their stage spec).
        return getattr(stage_spec, "workflow", None) is not None

    def decorate_tools(self, descriptors):
        # Wrap each ToolDescriptor.fn in your framework's tool decorator.
        from xyz_framework import tool as xyz_tool
        return [
            xyz_tool(d.name, description=d.description)(d.fn)
            for d in descriptors
        ]

    async def run_chief_turn(self, board_summary, instructions, tools):
        # Execute one LLM turn for the Chief using framework primitives.
        client = self._client_factory()
        ...

    async def run_stage(self, ctx) -> StageOutcome:
        # Drive the framework's native stage execution
        # (e.g. a workflow or graph) using ctx.task and ctx.board_fn.
        ...
```

The two methods that matter most are `can_handle` (which signals
which stages this runtime owns) and `run_stage` (which actually
drives the framework's native execution). `decorate_tools` and
`run_chief_turn` are only relevant when the chief itself is
LLM-driven through the same framework.

### Step 2. Register on a Pipeline

```python
from quadro import Pipeline
from quadro_xyz import XYZReasoner, XYZChiefRuntime

pipeline = (
    Pipeline(board)
    .reasoner(XYZReasoner(client_factory=client_factory))
    .with_framework_runtime(XYZChiefRuntime(client_factory=client_factory))
    .stage("ideate", saga=ideation_saga, active_status="ideating")
    .stage("research", workflow=research_workflow, active_status="researching")
    .build()
)
```

`Pipeline.with_framework_runtime(...)` registers the runtime; the
pipeline routes each stage through whichever registered runtime
returns `True` from `can_handle(stage_spec)`. Stages with
`saga=` go through the substrate's saga runtime;
stages with `workflow=` / `supervisor=` / `graph=` go through the
matching framework runtime.

## Reference implementations

Read the live code rather than re-deriving the patterns:

| Adapter | Reasoner | FrameworkRuntime | Notes |
|---|---|---|---|
| `quadro_maf` | [`reasoner.py`](../../src/quadro_maf/reasoner.py) | [`runtime.py`](../../src/quadro_maf/runtime.py) | Microsoft Agent Framework. Native `stage(workflow=...)`. |
| `quadro_langchain` | [`reasoner.py`](../../src/quadro_langchain/reasoner.py) | [`runtime.py`](../../src/quadro_langchain/runtime.py) | LangChain / LangGraph. Native `stage(supervisor=...)` and `stage(graph=...)`. |
| `quadro_anthropic` | [`reasoner.py`](../../src/quadro_anthropic/reasoner.py) | _(not provided)_ | Anthropic SDK. Reasoner-only — no framework runtime. |
| `examples/minimal/` | [`openai_reasoner.py`](../../examples/minimal/openai_reasoner.py) | _(not provided)_ | 30-line bare-OpenAI reference. Demonstrates the protocol's reach. |

## Future candidates

Quadro accepts contributions for additional adapters. Candidates
under active discussion:

- **LiteLLM** — a single-client proxy over ~100 providers.
  Attractive because it maximises reach without a deep new
  dependency. The shape would mirror `quadro_anthropic` (reasoner
  only) since LiteLLM is an SDK rather than an agent framework.
- **Native AWS Bedrock** — distinct wire format and SDK from
  OpenAI; warrants its own adapter rather than being shoehorned
  into `quadro_anthropic` or others.
- **Native Google Vertex AI** — same reasoning. The Gemini SDK has
  its own primitives that don't map cleanly onto OpenAI-shaped
  adapters.
- **LlamaIndex** — a community adapter would still be welcome; the
  two protocols (`Reasoner` + `FrameworkRuntime`) are sufficient
  to wire one up without changes to `quadro` core.

## Why this shape, not subclassing

Earlier versions of Quadro (pre-J1) used `Pipeline` subclassing as
the adapter seam. Adapters overrode hooks like `_make_stage_spec`,
`_decorate_tools`, `_run_chief_llm_turn`, and `_make_auto_execute_fn`.
That approach worked but had two problems. First, `Pipeline` had to
import every adapter's hooks at the protocol level — the substrate
package couldn't claim zero LLM-framework imports. Second,
composition was awkward: a saga that wanted to mix MAF and
LangChain reasoners had to subclass twice or instantiate two
pipelines.

The current shape — protocol composition via `Pipeline.reasoner(...)`
and `Pipeline.with_framework_runtime(...)` — fixes both. The
substrate has zero LLM imports, the adapter packages live as
top-level siblings rather than nested inside `quadro.integrations`,
and polyglot composition is one `.reasoner(...)` call per backend.
The framework-neutrality claim is structural, not aspirational.
