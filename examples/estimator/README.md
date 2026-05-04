# Estimator Example

This example demonstrates `Estimator.from_dry_run` against a translation-shaped
saga. It scans a 50-task queue, samples representative tasks with the real
Anthropic API, and prints token and dollar projections with variance.

## Run

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python examples/estimator/main.py
```

The dry-run sample is bounded by `max_sample_cost_dollars=1.0` and usually costs
well under that for the provided queue. The example writes pricing to
`anthropic_minimal.db`, so after a run you can inspect the Costs tab:

```bash
python -m quadro.ui anthropic_minimal.db --open
```

Use `--run-all` to execute the full 50-task queue after the projection and
compare actual costs with the estimate.
