# Quadro examples

Examples are grouped by **what you need installed to run them**. Pick a folder,
read its `README.md`, copy any `.env.example` to `.env`, and run `main.py`.

| Folder | What you need | Highlights |
|---|---|---|
| [`core/`](core/) | Just Quadro | Deterministic demos, no API keys, no extra deps |
| [`microsoft_agent_framework/`](microsoft_agent_framework/) | Quadro + [`agent-framework`] + OpenAI-compatible endpoint | LLM-backed pipelines via `MafPipeline` |
| [`langchain/`](langchain/) | Quadro + LangChain + OpenAI-compatible endpoint | LLM-backed pipelines via `LangChainPipeline` |

[`agent-framework`]: https://pypi.org/project/agent-framework/

## `core/` — runs with nothing but Quadro

Pure-Python workers, no API keys, no network calls. Good first contact.

| Example | What it teaches |
|---|---|
| [`core/newsroom_cooperation/`](core/newsroom_cooperation/) | Research / write / review pipeline, Chief policy chaining tasks |
| [`core/ordering_system/`](core/ordering_system/) | Custom lifecycle profile, board-held warehouse inventory, `board_fn` from workers |
| [`core/crm_sponsor/`](core/crm_sponsor/) | External authority (mocked CRM ticket) driving runtime lifetime via a Sponsor — the continuity story |

```bash
python examples/core/newsroom_cooperation/main.py
python examples/core/ordering_system/main.py
python examples/core/crm_sponsor/main.py
```

## `microsoft_agent_framework/` — LLM-backed via MAF

Needs `pip install -r examples/microsoft_agent_framework/requirements.txt` and
an OpenAI-compatible endpoint (`OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`OPENAI_MODEL_ID`). Works against OpenAI, Ollama, vLLM, SGLang, LiteLLM, or
any `/v1/chat/completions` server.

| Example | What it teaches |
|---|---|
| [`microsoft_agent_framework/ordering_system/`](microsoft_agent_framework/ordering_system/) | High-throughput LLM order fulfilment, Chief under sustained load |
| [`microsoft_agent_framework/newsroom/`](microsoft_agent_framework/newsroom/) | 9-stage pipeline with PubMed research and a revision loop; Chief sleeps between long-running LLM stages |
| [`microsoft_agent_framework/llm_token_budget/`](microsoft_agent_framework/llm_token_budget/) | `LlmTokenBudgetSponsor` wired via MAF `token_reporter`; two runs, two termination paths (drain vs. stop) |

## `langchain/` — LLM-backed via LangChain

Needs `pip install -r examples/langchain/requirements.txt` and an
OpenAI-compatible endpoint (`OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`OPENAI_MODEL_ID`). Works against OpenAI, Ollama, vLLM, SGLang, LiteLLM,
or any `/v1/chat/completions` server — the adapter builds on
`langchain-openai`'s `ChatOpenAI`.

| Example | What it teaches |
|---|---|
| [`langchain/llm_token_budget/`](langchain/llm_token_budget/) | `LlmTokenBudgetSponsor` wired via the LangChain adapter's `token_reporter` hook; two runs, two termination paths (drain vs. stop) |

## Running conventions

All examples currently bootstrap `quadro` from the source tree via a
`sys.path.insert(...)` line at the top of `main.py`. If you have done
`pip install -e .` from the repo root, that line is a no-op — `quadro` is
already importable.

Generated runtime artefacts (databases, `output/` directories) are
`.gitignore`d. Curated reference output lives in each example's
`output_sample/` folder and is intentionally committed.
