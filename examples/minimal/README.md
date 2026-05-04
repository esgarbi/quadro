# Minimal Quadro Example

## What This Example Demonstrates

`examples/minimal/` is the smallest LLM-backed Quadro pipeline in the repo. It
uses the Quadro substrate plus a small bare-OpenAI-SDK reasoner adapter, without
`quadro_maf`, `quadro_langchain`, or any LLM framework.

The adapter lives in `openai_reasoner.py`. It implements the structural
`Reasoner` protocol from `quadro.saga.reasoner`: expose a `reasoner_id`, accept
`prompt`, `user_message`, optional `schema`, and optional `token_reporter`, then
return `ReasonResult(output=..., tokens_used=..., raw_text=...)`.

## Run It

From this directory:

```sh
pip install quadro openai python-dotenv
cp .env.example .env
export OPENAI_API_KEY=sk-...
python main.py
```

Or from the repo root:

```sh
OPENAI_API_KEY=sk-... python examples/minimal/main.py
```

The run seeds one in-memory task, lets the deterministic chief dispatch it to a
single saga stage, asks OpenAI one question, writes the answer back to the board,
and prints it.

## Swap To A Different Adapter

Copy `openai_reasoner.py` and replace the SDK call. The rest of the pipeline
does not care whether the implementation uses Anthropic, Google, an in-house
service, or another Python library, as long as it returns a `ReasonResult`.

```python
pipeline = (
    Pipeline(board)
    .reasoner(MyReasoner(client=my_client))
    .workers(1)
    .stage("summarize", saga=summarize_saga, active_status="summarizing")
    .build()
)
```

This example is the proof that Quadro is a substrate rather than a framework
wrapper. The larger newsroom and ordering examples use MAF or LangChain because
those are useful adapters; this example uses neither because the substrate does
not require them.
