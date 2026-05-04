# Quadro examples

Each example lives at `examples/<purpose>/` and demonstrates one facet of
Quadro. The integration choice is a configuration detail; the example's
identity is its teaching purpose.

## newsroom (`examples/newsroom/`)

Declarative authoring at scale. Four sagas drive an end-to-end
article-publishing pipeline through the MAF adapter (`quadro_maf`): ideation,
research, writing, and review. Requires `pip install quadro[maf]` and an
`OPENAI_API_KEY`.

## ordering (`examples/ordering/`)

LLM-backed order fulfillment through MAF. The example demonstrates a governed
multi-stage pipeline with validation, inventory checks, procurement, logistics,
and a board-held warehouse state. Requires `pip install quadro[maf]` and an
`OPENAI_API_KEY`.

## ordering_minimal (`examples/ordering_minimal/`)

The same compensation rollback pattern without an LLM framework. Quadro's
deterministic chief drives a saga that accepts, reserves inventory, ships, and
rolls back side effects on injected failure. Requires only `pip install quadro`.

## token_budget (`examples/token_budget/`)

The sponsor system with live LLM metering. Demonstrates
`LlmTokenBudgetSponsor` with soft drain and hard stop paths through the
LangChain adapter (`quadro_langchain`). Requires `pip install quadro[langchain]`
and an `OPENAI_API_KEY`.

## crm_sponsor (`examples/crm_sponsor/`)

The sponsor system, framework-neutral version. A mocked CRM ticket governs
runtime lifetime through continue, drain, and stop decisions. Requires only
`pip install quadro`.

## cooperation (`examples/cooperation/`)

Minimal cooperation example using Quadro's built-in lifecycle profiles and
pure Python workers. Requires only `pip install quadro`.

## minimal (`examples/minimal/`)

The substrate plug-in story. A bare-OpenAI-SDK reasoner adapter is co-located
with a tiny saga, proving any LLM SDK can plug in through the `Reasoner`
protocol without MAF or LangChain. Requires `pip install quadro openai` and an
`OPENAI_API_KEY`.

## anthropic_minimal (`examples/anthropic_minimal/`)

The smallest example using Claude as the reasoner. Posts one task, runs a saga
that asks Claude to summarise an article with a Pydantic-enforced output
schema, and exits when the task reaches `summarized`. The reference
implementation of the token-usage-in-output convention (helpers
`_format_tokens` and `_print_token_usage` are designed to copy-paste into any
new example). Requires `pip install quadro[anthropic]` and an
`ANTHROPIC_API_KEY`.

## estimator (`examples/estimator/`)

The minimal demonstration of `Estimator.from_dry_run`. Scans a 50-task
translation queue, samples representative tasks under a `$1.00` cap, and
prints a token-and-dollar projection with variance reporting. Shorter and
faster than `synthetic_data/`; use this one to learn how the Estimator API
works. Requires `pip install quadro[anthropic]` and an `ANTHROPIC_API_KEY`.

## synthetic_data (`examples/synthetic_data/`)

The industry-shaped demonstration of the Estimator. Loads Wikipedia passages
from HuggingFace and runs them through two distinct sagas — SQuAD-style
extractive QA and Alpaca-style multi-hop reasoning chains. Surfaces per-saga
cost asymmetry, projects against full-scale workloads with confidence
intervals that widen when extrapolating beyond the sample, and outputs JSONL
files in formats directly loadable by the HuggingFace `datasets` library.
Requires `pip install quadro[anthropic]`, an `ANTHROPIC_API_KEY`, and the
example-local extras in `examples/synthetic_data/requirements.txt`.

## workflow_stage_minimal (`examples/workflow_stage_minimal/`)

The native-stage path with MAF: `stage(workflow=...)` instead of
`stage(saga=...)`. Requires `pip install quadro[maf]`.

## supervisor_stage_minimal (`examples/supervisor_stage_minimal/`)

The native-stage path with LangChain: `stage(supervisor=...)`. Symmetric to
`workflow_stage_minimal/`, but for LangGraph/LangChain. Requires
`pip install quadro[langchain]`.

## Running conventions

Examples bootstrap `quadro` from the source tree via a `sys.path.insert(...)`
line at the top of `main.py`. If you have done `pip install -e .` from the repo
root, that line is a no-op.

Generated runtime artefacts (databases, `output/` directories) are
`.gitignore`d. Curated reference output lives in each example's
`output_sample/` folder and is intentionally committed.
