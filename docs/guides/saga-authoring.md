# Saga Authoring Guide

This guide shows how to write a Quadro saga from a blank file to a tested
pipeline stage. It uses the `examples/minimal/` shape as the reference because
that example is the smallest complete saga-backed pipeline in the repository.

## Quick Reference

Step kinds and modifiers at a glance — jump to the section you need.

| Construct | Purpose | Section |
|---|---|---|
| `deterministic` | Pure Python work, sync or async | [Deterministic](#deterministic) |
| `reason` | One LLM reasoning episode through a registered Reasoner | [Reason](#reason) |
| `gate` | Predicate-driven branch selection | [Gate](#gate) |
| `guard` | Pre-condition; halts saga with `guard_failed:<step>` if false | [Guard](#guard) |
| `expect` | Post-condition; halts saga with `expect_failed:<step>` if false | [Expect](#expect) |
| `evidence` | Best-effort audit capture, never fails the saga | [Evidence](#evidence) |
| `stamp` | Ordered audit marker with timestamp | [Stamp](#stamp) |
| `parallel` | Concurrent branch-local mini-sagas with `all`/`any`/`n_of_m` joins | [Parallel](#parallel) |
| `.retry(...)` | Typed retry loop with fixed or exponential backoff | [Retry](#retry) |
| `.deadline(...)` | Per-attempt wall-clock timeout | [Deadline](#deadline) |
| `.idempotent(...)` | Saga-wide idempotency key declaration | [Idempotent](#idempotent) |
| `.compensate(...)` | Rollback function for a completed step | [Compensation](#compensation) |
| Token usage in examples | Project-wide convention: every example surfaces cost in its output | [Surfacing Token Usage](#surfacing-token-usage-in-examples) |

## When To Use A Saga

Quadro stages support four authoring shapes. Pick the one that fits the work:

**`stage(execute_fn=...)`** — a single Python function. Right when the stage
is one call: validate input, do one thing, write the result. No checkpointing,
no rollback, no LLM. Most "simple worker" stages live here.

**`stage(workflow=...)`** — a native Microsoft Agent Framework workflow.
Right when you have an existing MAF workflow and want Quadro to drive its
lifecycle without the saga DSL in between. The `quadro_maf` adapter handles
dispatch. See `examples/workflow_stage_minimal/`.

**`stage(supervisor=...)` or `stage(graph=...)`** — a native LangGraph
supervisor or graph. Right for the same reason as `workflow=` but with
LangChain. The `quadro_langchain` adapter handles dispatch. See
`examples/supervisor_stage_minimal/`.

**`stage(saga=...)`** — a Quadro saga. Right when the stage has structure
the framework-native paths do not capture: multiple deterministic side effects
that need named telemetry boundaries, LLM reasoning interleaved with
validation, audit capture (`evidence`, `stamp`) for governance, retry or
deadline modifiers, branching with `gate`, or rollback via `.compensate(...)`.

A saga is overkill for a stage that calls one function and posts one result —
use `execute_fn` there. A saga earns its keep when at least two of these are
true: the stage has more than one side effect, you need to resume after a
worker crash without repeating completed work, you need rollback if a later
step fails, you need an audit trail of what happened inside the stage, or
you need typed retry/deadline behavior on individual operations. The
newsroom and ordering examples are the canonical scale points: both stages
clear all five conditions.

The other reason to choose a saga is **bring-your-own LLM stack**. Reason
steps dispatch through any object implementing the `Reasoner` protocol —
30 lines of code wrapping any SDK. See
[Building Deep Agents With Custom Reasoners](#building-deep-agents-with-custom-reasoners)
below.

## What Is A Saga?

A saga is a declarative description of one pipeline stage's work. It names the
steps that should run, the order in which they run, the conditions that can halt
or branch the work, and the compensations that should undo completed side
effects if a later step fails. The saga runner owns the execution bookkeeping:
it persists progress to the Board, resumes from the saved program counter, emits
runtime telemetry, and hands LLM calls to a registered `Reasoner`.

Use a saga when the stage has more structure than "call one function and post a
result." Typical signals are multi-step side effects, checkpoint/resume needs,
LLM reasoning mixed with deterministic validation, rollback requirements, or
branching that should be visible in audit output.

## Hello Saga

The smallest useful saga has three parts:

1. A deterministic step that reads task input.
2. A reason step that asks an LLM for an answer.
3. A deterministic step that persists the answer and transitions the task.

The minimal example declares that shape like this:

```python
from quadro import Saga


def _extract_question(ctx):
    notes = ctx.task.get("notes") or []
    return notes[0] if notes else "What is Quadro?"


def _persist_answer(ctx):
    board_fn = ctx.task["_board_fn"]
    answer = ctx.step["summarize"]
    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "answered",
            "output": answer,
        },
    )
    return {"persisted": True}


summarize_saga = (
    Saga("summarize")
    .deterministic("extract_question", _extract_question)
    .reason(
        "summarize",
        prompt="You are a concise technical writer. Answer in one paragraph.",
        user_message=lambda ctx: ctx.step["extract_question"],
    )
    .deterministic("persist_answer", _persist_answer)
    .build()
)
```

The important pattern is the `ctx.step` handoff. Each completed step's output is
stored under its step name, so later steps read earlier results by name. That
same completed-step map is persisted to the Board, which is how a saga can
resume after a worker crash without repeating earlier steps.

To run the saga in a pipeline, register a reasoner and attach the saga to a
stage:

```python
pipeline = (
    Pipeline(board)
    .reasoner(OpenAIReasoner(client=OpenAI(), model=model))
    .workers(1)
    .stage("summarize", saga=summarize_saga, active_status="summarizing")
    .build()
)
```

The stage's `active_status` must match the lifecycle phase the chief dispatches
into. If your saga's final step calls `board.update_task` itself, leave
`success_status` unset so the pipeline does not attempt a second transition.

## Building Deep Agents With Custom Reasoners

The Reasoner protocol is the most powerful seam in the saga DSL. Any object
implementing `reasoner_id` and `async reason()` plugs into a pipeline and
becomes the LLM execution layer for every reason step that targets it. The
substrate has no opinion about what happens behind the protocol.

This is how you build a deep agent inside Quadro without forcing it through
the saga DSL itself. The reason step looks like a single LLM call from the
saga's perspective, but the reasoner can do whatever the agent needs: maintain
internal state across calls, run a multi-turn ReAct loop, manage its own tool
registry, recurse into sub-agents, query a vector store, or wrap an entire
LangGraph supervisor. The saga sees one input and one output; the reasoner
owns everything in between.

The shipping `quadro_maf` and `quadro_langchain` reasoners are deliberately
thin — they hand the prompt and user message straight to the framework's LLM
client and return the result. Custom reasoners can do far more:

```python
class ReActAgentReasoner:
    """Reasoner that runs a multi-turn ReAct loop with tool use."""

    reasoner_id = "react_agent"

    def __init__(self, *, client, tools, max_iterations=10):
        self._client = client
        self._tools = tools
        self._max_iter = max_iterations

    async def reason(self, *, prompt, user_message, schema, token_reporter):
        messages = [{"role": "system", "content": prompt}]
        messages.append({"role": "user", "content": str(user_message)})
        total_tokens = 0

        for _ in range(self._max_iter):
            response = await self._client.chat(
                messages=messages,
                tools=self._tools,
            )
            total_tokens += response.usage.total_tokens

            if response.tool_calls:
                for call in response.tool_calls:
                    result = await self._tools[call.name](**call.arguments)
                    messages.append({"role": "tool", "content": json.dumps(result)})
                continue

            if token_reporter is not None:
                try:
                    token_reporter(total_tokens)
                except Exception:
                    pass

            raw = response.content
            output = schema.model_validate_json(raw) if schema else raw
            return ReasonResult(output=output, tokens_used=total_tokens, raw_text=raw)

        raise RuntimeError(f"ReAct agent exceeded {self._max_iter} iterations")
```

A saga that uses this reasoner reads the same as any other:

```python
saga = (
    Saga("research")
    .deterministic("frame_question", frame_question)
    .reason(
        "investigate",
        prompt=Path("prompts/researcher.md"),
        user_message=lambda ctx: ctx.step["frame_question"],
        schema=ResearchFindings,
        via="react_agent",  # routes to the ReAct reasoner
    )
    .deterministic("persist", persist_findings)
    .build()
)
```

The point is that the saga DSL is not in tension with deep agents — it
complements them. Use the saga to express the stage's audit-grade structure
(what happened, in what order, with what compensations); use the reasoner to
express the agent's reasoning depth (how the answer is actually produced).
You get the governance and rollback semantics of the saga and the open-ended
agent loop of your choice, without one constraining the other.

For the contrast: see `examples/minimal/openai_reasoner.py` for the simplest
possible reasoner — 30 lines, one LLM call, no agent loop. Both shapes
implement the same protocol. The substrate does not distinguish them.

## SagaContext

Every step receives a `SagaContext` named `ctx` by convention. It contains:

- `ctx.task`: the current task record, including internal `_board_fn` access.
- `ctx.step`: outputs from completed steps, keyed by step name.
- `ctx.evidence`: records captured by evidence steps.
- `ctx.now`: a fresh UTC timestamp for the current dispatch.

Step outputs should be JSON-compatible. Pydantic models are serialized by the
runtime when saga state is persisted, but plain dict/list/string/number shapes
are easier to resume and inspect.

## The Eight Step Kinds

The builder ships eight step kinds: `deterministic`, `reason`, `gate`,
`guard`, `expect`, `evidence`, `stamp`, and `parallel`. The first seven are
single-path steps. `parallel` runs branch-local mini-sagas concurrently and
joins their outputs.

### Deterministic

Use `deterministic` for pure Python work: parse input, call Board intents,
validate local data, compose payloads, or persist outputs.

```python
def parse_order(ctx):
    return json.loads(ctx.task["notes"][0])


saga = Saga("order").deterministic("parse_order", parse_order).build()
```

A deterministic function may be sync or async. It is called as `fn(ctx)`, and
the return value is stored under `ctx.step["parse_order"]` for later steps.

Keep deterministic steps small. If one step both mutates inventory and ships a
package, split it into two named steps so telemetry and compensation records can
say which side effect happened.

### Reason

Use `reason` for one LLM reasoning episode. The saga runtime resolves the prompt
text, serializes the user message, and calls the registered `Reasoner`.

```python
saga = (
    Saga("triage")
    .deterministic("extract_ticket", extract_ticket)
    .reason(
        "classify",
        prompt=Path("prompts/classify.md"),
        user_message=lambda ctx: {"ticket": ctx.step["extract_ticket"]},
        schema=TicketClassification,
    )
    .build()
)
```

If `schema` is provided, the reasoner must return an instance of that schema.
If `schema` is `None`, the output is raw cleaned text.

When multiple reasoners are registered, pass `via="reasoner_id"` to select one:

```python
.reason(
    "editorial_decision",
    prompt="Approve or request revision.",
    user_message=lambda ctx: ctx.step["draft"],
    schema=ReviewDecision,
    via="maf",
)
```

`via` values must match a reasoner registered on the pipeline. Missing values
fail at dispatch time with a clear runtime error.

### Gate

Use `gate` to choose between two named branches. The `when` callable receives
the current context and returns a boolean. The runtime records the chosen target
as `{"chosen": "<step_name>"}` and jumps the program counter.

```python
saga = (
    Saga("review")
    .reason("decide", prompt=..., user_message=..., schema=ReviewDecision)
    .gate(
        "route",
        when=lambda ctx: ctx.step["decide"].approved,
        on_true="publish",
        on_false="request_revision",
    )
    .deterministic("publish", publish)
    .deterministic("request_revision", request_revision)
    .build()
)
```

Both targets must be declared somewhere in the same saga. Forward references are
allowed because validation runs in `build()`.

Gate arms do not require an explicit merge step. The runtime treats the
unchosen target as a barrier so a linear declaration like `gate -> publish ->
request_revision` does not accidentally run both arms.

### Guard

Use `guard` for a precondition. If the check returns false, the saga halts with
`terminal_reason="guard_failed:<name>"` and emits `saga.guard_failed`.

```python
saga = (
    Saga("publish")
    .deterministic("load_draft", load_draft)
    .guard("has_title", check=lambda ctx: bool(ctx.step["load_draft"]["title"]))
    .deterministic("publish", publish)
    .build()
)
```

Guards are best for conditions that should stop the stage before a side effect
happens. If the condition should run after a side effect or after an LLM output,
use `expect`.

### Expect

Use `expect` for a postcondition or invariant. Its shape matches `guard`, but it
emits `saga.expect_failed` and the terminal reason starts with
`expect_failed:`.

```python
saga = (
    Saga("research")
    .reason("plan", prompt=..., user_message=..., schema=ResearchPlan)
    .expect("has_queries", invariant=lambda ctx: bool(ctx.step["plan"].queries))
    .deterministic("persist_plan", persist_plan)
    .build()
)
```

Use `expect` to make assumptions auditable. A failing expectation should mean
"the previous step returned an unusable result," not "the user made a bad
request."

### Evidence

Use `evidence` for best-effort audit capture. The capture function's return
value is written into saga state under `state.evidence[name]`.

```python
saga = (
    Saga("loan_review")
    .deterministic("load_application", load_application)
    .evidence("credit_snapshot", capture=lambda ctx: ctx.task["credit_report"])
    .reason("assess", prompt=..., user_message=...)
    .build()
)
```

Evidence capture failures are logged and skipped. They do not fail the saga.
That makes evidence appropriate for observability and governance breadcrumbs,
not load-bearing state that later steps need to run.

### Stamp

Use `stamp` for ordered audit markers. A stamp appends
`{"key": name, "value": value, "timestamp": ...}` to `state.stamps`.

```python
saga = (
    Saga("release")
    .deterministic("deploy", deploy)
    .stamp("deployed_version", capture=lambda ctx: ctx.step["deploy"]["version"])
    .build()
)
```

Unlike evidence, stamp capture failures propagate. A stamp should represent a
marker you expect to exist if the saga continues.

### Parallel

Use `parallel` when independent branch-local work can run concurrently inside a
single saga step.

```python
saga = (
    Saga("briefing")
    .parallel(
        "fanout",
        join="all",
        branches=[
            lambda b: b.deterministic("market", gather_market_notes),
            lambda b: b.deterministic("customer", gather_customer_notes),
        ],
    )
    .deterministic(
        "merge",
        lambda ctx: {
            "market": ctx.step["fanout"]["market"],
            "customer": ctx.step["fanout"]["customer"],
        },
    )
    .build()
)
```

Supported joins:

- `join="all"` waits for every branch and returns every branch output.
- `join="any"` returns the first successful branch and cancels the rest.
- `join=("n_of_m", n)` returns once `n` branches have succeeded and cancels
  the remaining pending branches.

Each branch is built with a fresh branch-local `SagaBuilder`. Nested parallel
steps are rejected. Branch outputs are keyed by branch name, which defaults to
the branch's first step name.

## The Three Modifiers

Modifiers attach to the most recently added step. They do not create new steps;
they enrich how the preceding step dispatches.

### Retry

Use `retry` for transient failures:

```python
saga = (
    Saga("sync")
    .deterministic("call_api", call_api)
    .retry(attempts=3, on=(ConnectionError,), backoff="exponential")
    .build()
)
```

Retries catch only the exception types listed in `on`. Other exceptions fail on
the first attempt. `backoff="fixed"` retries immediately. `backoff="exponential"`
sleeps 1, 2, 4 seconds and caps at 30 seconds.

When combined with `deadline`, retry is the outer loop and the deadline applies
per attempt.

### Deadline

Use `deadline` to bound wall-clock time for one step attempt:

```python
saga = (
    Saga("fetch")
    .deterministic("slow_call", slow_call)
    .deadline(within=timedelta(seconds=10))
    .build()
)
```

If the step exceeds the deadline, the saga halts with
`terminal_reason="deadline_exceeded:<step>"` unless a retry policy catches
`asyncio.TimeoutError` and another attempt remains.

### Idempotent

Use `idempotent` to declare the saga-wide idempotency key template:

```python
saga = (
    Saga("order")
    .idempotent(by="order_id")
    .deterministic("accept", accept_order)
    .build()
)
```

The key is stored on the built saga. It is reserved for lifecycle-spanning
deduplication semantics and should be treated as part of the saga contract even
when today's local step bodies do not read it directly.

## Compensation

Compensation is the rollback pattern. Register a compensation for any completed
step whose side effect must be undone if a later step fails.

```python
saga = (
    Saga("order")
    .deterministic("accept_order", accept_order)
    .compensate("accept_order", undo=cancel_acceptance)
    .deterministic("reserve_inventory", reserve_inventory)
    .compensate("reserve_inventory", undo=release_inventory)
    .deterministic("ship_package", ship_package)
    .compensate("ship_package", undo=recall_shipment)
    .build()
)
```

The ordering minimal example is the reference:
`examples/ordering_minimal/main.py`.

Its forward steps do concrete side effects:

- `accept_order` moves the task from `placed` to `accepted`.
- `reserve_inventory` debits warehouse inventory.
- `ship_package` marks the order delivered.

Each step returns a record of what it did. The compensation reads that record
from `ctx.step["step_name"]` and reverses the side effect.

Write compensations to be idempotent. If a worker crashes mid-rollback, the
runtime may re-enter the compensation walk. The ordering example records
Board-level compensation markers so a repeated compensation can return early.

Compensation failure modes:

- `on_failure="continue"` logs the failed compensation and keeps walking.
- `on_failure="halt"` stops the rollback walk immediately.

Use `halt` only when an earlier compensation depends on the failed compensation
having succeeded. Most compensations should use the default `continue` so the
system undoes as much work as it safely can.

## Reasoners

The `Reasoner` protocol is the seam between the saga runtime and any LLM stack.
The runtime never imports OpenAI, MAF, LangChain, or another provider. It calls
an object with a `reasoner_id` and an async `reason()` method.

The protocol shape is:

```python
class Reasoner(Protocol):
    reasoner_id: str

    async def reason(
        self,
        *,
        prompt: str,
        user_message: str,
        schema: type | None,
        token_reporter: Callable[[int], None] | None,
    ) -> ReasonResult:
        ...
```

`examples/minimal/openai_reasoner.py` is the smallest concrete adapter. It:

- Accepts a user-owned OpenAI client.
- Sends `prompt` as the system message.
- Sends `user_message` as the user message.
- Validates JSON against `schema` when one is provided.
- Reports token totals through `token_reporter`.
- Returns `ReasonResult(output=..., tokens_used=..., raw_text=...)`.

To write your own reasoner, mirror that file:

```python
class MyReasoner:
    reasoner_id = "my_provider"

    def __init__(self, *, client):
        self._client = client

    async def reason(self, *, prompt, user_message, schema, token_reporter):
        raw, tokens = await call_my_model(self._client, prompt, user_message)
        if token_reporter is not None:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        output = schema.model_validate_json(raw) if schema is not None else raw
        return ReasonResult(output=output, tokens_used=tokens, raw_text=raw)
```

Token reporting is best-effort telemetry; wrapping the call in try/except keeps
a malformed token counter from failing an otherwise-successful step. For
multi-turn reasoners, deep agents, and other shapes that do more than one LLM
call inside a single reason step, see
[Building Deep Agents With Custom Reasoners](#building-deep-agents-with-custom-reasoners)
above.

## Surfacing Token Usage In Examples

Every example in the repository ends by surfacing the run's token usage to
stdout. This is a deliberate convention, not a stylistic preference. Quadro's
core framing is **"measure waste, not just monitor it"** — the substrate
captures cost-of-work data structurally, and that data should be visible at
every output surface, not buried behind UI clicks.

The convention applies to every example folder under `examples/` that runs
at least one reason step. Substrate-only examples that have no LLM calls
(like `examples/cooperation/`) are exempt because they have no tokens to
report.

### The pattern

After the saga completes, read the per-task records phase one of the
reporting rollout persists, and print a short report. The data lives on
the Board's data store under keys of the form `_token_record:{task_id}:{step_name}`,
and `BoardClient.token_records(task_id=...)` is the canonical reader.

```python
def _format_tokens(n: int) -> str:
    """Format a token count with K/M suffix.

    Mirrors the Board UI's `formatTokens` convention so the numbers shown
    here read identically to the Costs tab.
    """
    if n < 1000:
        return f"{n:,}"
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"


def _print_token_usage(client, task_id: str) -> None:
    """Print the same token data the Board UI's Costs tab would show."""
    records = client.token_records(task_id=task_id)
    if not records:
        return

    total = sum(int(r.get("token_total") or 0) for r in records)
    print("\n=== Token usage ===\n")
    print(f"Total: {_format_tokens(total)} tokens across {len(records)} reason step(s)")
    print()
    for r in records:
        step = r.get("step_name") or ""
        stage = r.get("stage") or ""
        reasoner = r.get("reasoner_id") or ""
        tokens = _format_tokens(int(r.get("token_total") or 0))
        print(f"  {step:<24}  {stage:<14}  {reasoner:<10}  {tokens:>8}")
```

The output is one prominent total followed by a per-step table. For sagas
that span multiple stages, add a small "By stage" rollup beneath the table
when more than one stage is present — this mirrors the Costs tab's per-stage
breakdown for long-running pipelines like newsroom.

### Why these specific shapes

The K/M suffix formatting matches the Board UI's `formatTokens` JavaScript
helper exactly. Numbers shown in the example output read identically to the
numbers in the Costs tab when the operator opens
`python -m quadro.ui <example>.db --open`. Three views, one source of truth:
the CLI report, the per-task drawer's "Token usage" section, and the Costs
tab's per-stage breakdown all read the same `_token_record:` keys on the
Board.

The per-step table mirrors the drawer's Token usage section column order
(step, stage, reasoner, tokens) so an operator who has used the UI sees the
same shape in the CLI. Visual consistency across surfaces is the design
discipline.

The reference implementation lives in `examples/anthropic_minimal/main.py` —
it's the smallest example that uses the convention end-to-end, and its
helpers (`_format_tokens` and `_print_token_usage`) are copy-paste-ready for
any new example.

### When to print

Print the token usage block at the end of `main()`, after the primary
output (summary, article, decision, whatever the example produces). Cost
follows result, not the other way around. The reader's first interaction
with the example output should be the work product; the cost data is the
operational answer to "what did this cost me?" that comes after.

For multi-task examples like newsroom, print one consolidated block per
task or one summary block at the end with per-stage rollups across all
completed tasks. The newsroom example does the latter — a single global
"Token usage" block summarizing the run rather than five per-article
blocks. Pick whichever shape best matches the example's narrative arc.

### Why this is foundational

Without this convention, Quadro's "measure waste, not just monitor it"
framing is a marketing claim with screenshots to back it up. With this
convention, every example a reader touches reinforces the framing — they
see the cost number every time they run the code, not just when they
remember to open the UI. The substrate's value is invisible without it,
and visible with it. Treat the token-usage output as a load-bearing part
of every example's pedagogy, not a footnote.

## Testing Your Saga

Most saga tests do not need a real Board or a real LLM. Existing unit tests use
a fake `board_fn` and a `RuntimeContext`. Both `QuadroSagaRuntime` and
`RuntimeContext` are exported from `quadro.runtime_plugins`:

```python
import asyncio

from quadro import StageSpec
from quadro.runtime_plugins import QuadroSagaRuntime, RuntimeContext


def _fake_board_fn(store):
    def _fn(intent, payload):
        if intent == "board.put_data":
            store[payload["key"]] = payload["value"]
            return {"ok": True}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        if intent == "board.get_full_state":
            return {"tasks": []}
        raise AssertionError(intent)
    return _fn
```

Then run the saga directly:

```python
runtime = QuadroSagaRuntime()
spec = StageSpec(capability="x", saga=saga, failure_status="failed")
result = asyncio.run(runtime.run_stage(RuntimeContext(
    stage=spec,
    task={"task_id": "t1"},
    context={"payload": {"task": {"task_id": "t1"}}},
    board_fn=_fake_board_fn({}),
)))
```

Use fake reasoners for `reason` steps:

```python
class FakeReasoner:
    reasoner_id = "fake"

    async def reason(self, *, prompt, user_message, schema, token_reporter):
        return ReasonResult(output="ok", tokens_used=0, raw_text="ok")
```

Good unit tests assert:

- The final `result.output`.
- The final `result.terminal_reason`.
- Persisted saga state under `_saga:<task_id>`.
- Telemetry events for failure, retry, compensation, or parallel branch cases.
- Board updates recorded by the fake `board_fn`.

`tests/unit/test_saga_step_kinds.py` is the broad reference for gate, guard,
expect, evidence, and stamp patterns. `tests/unit/test_saga_modifiers.py` covers
retry and deadline. `tests/unit/test_saga_compensations.py` covers rollback.
`tests/unit/test_saga_parallel.py` covers all parallel joins.

## Production Checklist

Before shipping a saga-backed stage, review these points:

- Lifecycle: every `board.update_task` target is valid for the task profile.
- Ownership: either the saga owns the final task transition or the stage's
  `success_status` does, not both.
- Outputs: every step output can be serialized to JSON-compatible state.
- Idempotency: side-effecting deterministic steps and compensations can tolerate
  resume after partial progress.
- Compensation: every completed side effect that needs rollback has a matching
  `.compensate(...)`.
- Compensation failure: use `on_failure="halt"` only when continuing would make
  state worse.
- Retry: catch only transient exception types.
- Deadline: account for retry multiplication; three attempts with a 30-second
  deadline can spend roughly 90 seconds before backoff.
- Reasoners: every `via=` value is registered on the pipeline.
- Schemas: pydantic schemas used with LLM outputs are strict enough for the
  provider or validated client-side by the reasoner.
- Telemetry: failure paths emit the events your audit readers expect.
- Tests: include at least one successful run and one meaningful failure or
  rollback run.

## Common Mistakes

These are the mistakes most likely to appear when converting an existing worker
or workflow into a saga.

### Double Transitioning A Task

Do not set `success_status` on a stage if the saga's final deterministic step
already calls `board.update_task`. The first transition succeeds and the second
one becomes a lifecycle self-transition or an invalid transition.

Pick one owner:

- Saga-owned transition: final step writes the task status, stage omits
  `success_status`.
- Pipeline-owned transition: final step returns output only, stage declares
  `success_status`.

The newsroom example uses saga-owned transitions.

### Returning Non-Serializable State

Saga state is persisted after successful steps. A step output that cannot be
serialized will make resume and audit harder even if the in-memory run happens
to continue.

Prefer:

```python
return {"article_id": article_id, "slug": slug}
```

Avoid:

```python
return open(path)
```

If a pydantic model is useful inside one step, convert it to a dict before
returning unless a later step truly benefits from the model instance.

### Hiding Side Effects Inside Reason Steps

A reason step should call the model and return its output. Keep Board writes,
file writes, inventory changes, and external API mutations in deterministic
steps. That gives each side effect a name, a telemetry boundary, and an optional
compensation.

### Overusing Retry

Retry is for transient failures. It should not mask validation errors, schema
mismatches, bad lifecycle targets, or deterministic coding mistakes. Catch the
narrowest exception type that represents the transient condition.

### Compensation Without A Record

A compensation needs to know what the forward step did. Make the forward step
return a record precise enough to undo:

```python
return {"sku": sku, "qty": qty, "warehouse": "WH-MAIN"}
```

Then the compensation reads `ctx.step["reserve_inventory"]`. Do not force the
compensation to rediscover what happened by scanning unrelated Board state.

### Skipping The Token Usage Output In An Example

Every example that runs a reason step should print token usage at the end of
its `main()`. See [Surfacing Token Usage In Examples](#surfacing-token-usage-in-examples).
Skipping it makes Quadro's cost-visibility framing invisible at the surface
where it should be most visible — when the reader runs the code for the
first time. The reference helpers live in `examples/anthropic_minimal/main.py`
and copy directly into any new example.

## Reference Map

Start with these files:

- `examples/minimal/main.py`: smallest end-to-end saga-backed pipeline.
- `examples/minimal/openai_reasoner.py`: smallest concrete reasoner adapter.
- `examples/anthropic_minimal/main.py`: reference implementation of the
  token-usage-in-examples convention.
- `examples/ordering_minimal/main.py`: compensation rollback reference.
- `src/quadro/saga/builder.py`: builder methods and validation.
- `src/quadro/saga/reasoner.py`: protocol contract for LLM adapters.
- `src/quadro/runtime_plugins/saga.py`: runtime dispatch semantics.
- `tests/unit/test_saga_step_kinds.py`: step-kind behavior examples.
- `tests/unit/test_saga_modifiers.py`: retry and deadline tests.
- `tests/unit/test_saga_compensations.py`: compensation tests.
- `tests/unit/test_saga_parallel.py`: parallel branch tests.

Read `examples/minimal/` first. It is intentionally small enough to copy, run,
and then extend one step at a time.
