# Anthropic Minimal Example

The smallest Quadro example using Claude as the reasoner. It demonstrates the
substrate's framework-neutral plug-in story: Quadro orchestrates the saga,
Claude answers the reasoning step, and the standard token records and Costs UI
work without any additional integration.

## Run It

From the repo root:

```sh
pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL_ID=claude-sonnet-4-6  # optional
python examples/anthropic_minimal/main.py
```

If `ANTHROPIC_MODEL_ID` is unset, the example uses `claude-sonnet-4-6`. The
adapter package itself still defaults to `claude-3-5-sonnet-latest` per the
milestone brief, but Anthropic currently rejects that alias for live API calls.

## What It Does

1. Creates a Board with a custom lifecycle (`UNASSIGNED -> pending -> summarized`).
2. Posts one task with an example article in the notes.
3. Runs a saga that extracts the article text, asks Claude to summarize it with
   a Pydantic schema enforcing the output shape, and persists the summary back
   to the Board.
4. Exits when the task reaches `summarized`.

## What To Look At Next

Open the Board UI for the run:

```sh
python -m quadro.ui anthropic_minimal.db --open
```

The task drawer shows the Token Usage section with the `summarize` step listed,
`claude` as the reasoner, and the token count from the call. The Costs tab shows
the per-stage token attribution. The per-reasoner section is hidden for this
single-reasoner example, which is expected behavior.

## How To Adapt

Swap the `Summary` schema for your own Pydantic model, change the prompt, or
change the article. To use Claude alongside another reasoner, register both
reasoners on the `Pipeline` builder and use `via=` per-step routing on saga
`.reason()` calls.
