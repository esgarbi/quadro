# LLM Adapter Guide

Quadro keeps the core runtime zero-dependency and treats any integration
with a specific LLM provider or agent framework as an **adapter**: an
opt-in module that builds on top of `quadro.pipeline.Pipeline` and a
caller-supplied client factory. This page documents the two seams that
make adapters easy to add, and sketches what a new adapter looks like.

## The two seams

### 1. `Pipeline` — framework-agnostic orchestration

`src/quadro/pipeline.py` declares the neutral building blocks every
adapter reuses:

- `StageSpec` — describes one pipeline stage (capability, status
  transitions, `execute_fn`, etc.). Adapters may subclass to attach
  framework-specific fields (the Microsoft Agent Framework adapter
  does this with `MafStageSpec` in `src/quadro/integrations/maf.py`;
  the LangChain adapter with `LangChainStageSpec` in
  `src/quadro/integrations/langchain.py`).
- `ToolDescriptor` — framework-neutral description of a chief tool.
- `generate_tool_descriptors(...)` — derives tool descriptors from a
  lifecycle graph so the chief tool surface is computed once and each
  adapter just wraps them in its framework's decorator.
- `Pipeline` — the declarative builder. It owns the board, the workers,
  and the chief. Each adapter overrides **three hooks**:
    - `_make_stage_spec(capability, **kwargs)` — return your adapter's
      `StageSpec` subclass.
    - `_decorate_tools(descriptors)` — wrap each `ToolDescriptor.fn`
      with whatever decorator your agent framework expects
      (e.g. MAF's `@tool`, LangChain's `Tool(...)`, OpenAI function
      schemas).
    - `_run_chief_llm_turn(board_summary, instructions, tools)` —
      execute one LLM turn for the Chief using your framework's
      workflow primitives.
    - `_make_auto_execute_fn(spec)` (optional) — auto-generate an
      `execute_fn` for a "prompt-in / schema-out" stage.

Nothing else has to be overridden; the rest of the pipeline
construction (`.workers()`, `.capacity()`, `.wakes()`, `.stage()`,
`.chief()`, `.build()`) is shared.

### Runtime plugins (control-plane direction)

Quadro also supports a runtime-plugin seam under
`src/quadro/runtime_plugins/` for native framework entrypoints (for
example, `stage(workflow=...)` for Microsoft Agent Framework workflows).
The ownership model is explicit:

- Quadro core owns governance (`Lifecycle`, `Sponsor/Lease`, drain/stop,
  board state transitions, and policy decisions).
- Framework runtimes own execution internals (workflow/graph execution,
  tool semantics, and framework-specific events).
- Runtime plugins only translate framework behavior into Quadro's
  normalized observability envelope (`quadro.runtime_event.v1`) and
  optional token reporting (`runtime.meters.report_llm_tokens`).

Backwards compatibility is preserved: existing adapter hooks remain
supported, and runtime plugins are additive so adapters can migrate
incrementally.

The Microsoft Agent Framework Newsroom example demonstrates both paths:
`main_pipeline.py` uses native `stage(workflow=...)`, while `main.py`
keeps the manual WorkerPool/Chief wiring as a compatibility/reference
implementation.

LangChain now supports native runtime stages as well via
`stage(supervisor=...)` and `stage(graph=...)` when using
`LangChainPipeline`. See
`examples/langchain/supervisor_stage_minimal/main.py` for a minimal
governed supervisor proof under Sponsor/Lease.

### 2. `client_factory` — pluggable LLM provider

The Microsoft Agent Framework adapter at `src/quadro/integrations/maf.py`
and the LangChain adapter at `src/quadro/integrations/langchain.py`
both route every LLM call through a `client_factory` callable:

```python
from quadro.integrations.maf import configure

configure(
    api_key="sk-...",
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
)
```

```python
from quadro.integrations.langchain import configure

configure(
    api_key="sk-...",
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
)
```

or, more directly:

```python
def _my_factory():
    # Return any OpenAI-compatible client.
    from openai import OpenAI
    return OpenAI()

configure(client_factory=_my_factory)
```

Both `MafPipeline` and `LangChainPipeline` also accept a per-pipeline
factory via `.llm(...)` and expose the same surface. Any
OpenAI-compatible API (Together, Groq, Anthropic via proxy, Ollama,
vLLM) works through the same seam because the underlying clients
(`agent-framework`'s `OpenAIChatClient`, `langchain-openai`'s
`ChatOpenAI`) both speak `/v1/chat/completions`. Native Anthropic,
Bedrock, Vertex, or Cohere would require a new adapter (see below)
because the wire format differs.

## Recipe: adding a new adapter

Use this as a template. Replace `XYZ` with the framework or provider
you're wiring up.

### Step 1. Subclass `Pipeline`

```python
from quadro.pipeline import Pipeline, StageSpec

class XYZStageSpec(StageSpec):
    prompt: str | None = None
    output_schema: type | None = None

class XYZPipeline(Pipeline):
    def _make_stage_spec(self, capability: str, **kwargs) -> StageSpec:
        return XYZStageSpec(capability, **kwargs)

    def _decorate_tools(self, descriptors):
        from xyz_framework import tool as xyz_tool
        return [xyz_tool(d.name, description=d.description)(d.fn) for d in descriptors]

    async def _run_chief_llm_turn(self, board_summary, instructions, tools):
        # Implement one LLM turn using your framework's API.
        ...

    def _make_auto_execute_fn(self, spec):
        async def _execute(context, board_fn):
            # Call your provider with spec.prompt, validate against spec.output_schema,
            # write the transition to the board via board_fn, return the output.
            ...
        return _execute
```

### Step 2. Expose a client factory seam

```python
_client_factory: Callable | None = None

def configure(*, client_factory=None, api_key=None, model=None):
    global _client_factory
    if client_factory is not None:
        _client_factory = client_factory
        return

    def _factory():
        from xyz_framework import XYZClient
        return XYZClient(api_key=api_key, model=model)

    _client_factory = _factory
```

### Step 3. Register as an optional extra

In `pyproject.toml`:

```toml
[project.optional-dependencies]
xyz = ["xyz-framework>=1.0,<2.0"]
```

Install with `pip install "quadro[xyz]"`. Keep `quadro.integrations.xyz`
guarded by an `_ensure_xyz()` helper that raises a descriptive
`ImportError` if the framework isn't installed, matching the existing
pattern in `src/quadro/integrations/maf.py:_ensure_maf`.

### Step 4. Ship usage docs + a minimal example

Mirror the structure of `examples/microsoft_agent_framework/` so users
have a runnable end-to-end pipeline to copy from.

## When a concrete adapter lands upstream

Quadro ships two reference adapters — Microsoft Agent Framework
(`MafPipeline`) and LangChain (`LangChainPipeline`) — and accepts
contributions for others. Candidates under active discussion:

- **LiteLLM** — a single-client proxy over ~100 providers. Attractive
  next step because it maximises reach without a deep new dependency.
- **Native Anthropic / Bedrock / Vertex** — each has a non-OpenAI wire
  format and a distinct SDK; they warrant separate adapters rather than
  being shoehorned into the existing ones.
- **LlamaIndex** — a community adapter would still be welcome; the
  existing two seams (`Pipeline` subclass + `client_factory`) are
  sufficient to wire one up without changes to `quadro` core.
