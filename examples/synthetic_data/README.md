# Synthetic Data Generation Example

The industry-shaped demo for Quadro's Estimator: generate two types of LLM
training data (SQuAD-style QA pairs and multi-hop reasoning chains) from real
Wikipedia passages, with cost projection before committing to the full run.

This is also the canonical example showing Quadro is useful for production
data-generation pipelines, not just orchestration. The output JSONL files
follow community-standard schemas and are immediately loadable as training
datasets.

## Why This Example Exists

The minimal `examples/estimator/` example demonstrates the Estimator's
mechanics against a homogeneous translation queue. This example demonstrates
the Estimator solving a real industry use case with realistic input variance:

- Wikipedia article lengths vary by 10x or more (200 to 3,000 words), driving
  real input-size heterogeneity that exercises the estimator's variance
  reporting.
- Two distinct sagas with materially different per-task token costs reveal the
  per-saga cost breakdown the Costs tab is designed to surface.
- Output JSONL files are real artifacts in formats practitioners recognize,
  demonstrating that Quadro is a credible substrate for synthetic data
  pipelines.

## Run It

### Estimate-Only (Default - Costs About $0.20)

```bash
pip install -r examples/synthetic_data/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python examples/synthetic_data/main.py
```

The estimator scans the loaded passages, runs a small cost-bounded sample, and
prints a projection report distinguishing per-saga costs. No outputs are
persisted in this mode.

### Generate-All (Runs The Full Pipeline - Costs About $2-5)

```bash
python examples/synthetic_data/main.py --generate-all --passages 20
```

Runs the full pipeline. With `--passages 20` (20 QA tasks + 10 reasoning tasks
= 30 total), the run takes 4-8 minutes and costs roughly $2-5. Outputs:

- `examples/synthetic_data/output/squad_format.jsonl`
- `examples/synthetic_data/output/reasoning_format.jsonl`

Default `--passages 50` produces a larger run (50 QA + 25 reasoning = 75 total
tasks) at proportionally higher cost.

### Inspect The Run In The Board UI

```bash
python -m quadro.ui synthetic_data.db --open
```

The Costs tab shows the per-saga breakdown (`qa_generate` vs.
`reasoning_generate`) with dollar amounts, demonstrating heterogeneous-pipeline
cost visibility.

## Sample Run

A representative run of the example produces output approximately like the
block below. Numbers vary across runs because the dry-run sampler picks
different passages each invocation; the **shape** of the output is what to
study.

```text
$ python examples/synthetic_data/main.py
Loading wikitext-103-raw-v1 from HuggingFace (cached after first run; ~500MB on first download)...
Loaded 50 passages (word count range: 435 - 2,931)
Built queue: 50 QA tasks + 25 reasoning tasks
=== Estimator dry run ===
Pass 1 (input collection): 75 tasks scanned in 0.0s
Pass 2 (sampling): 8 tasks executed (cost: $0.23)

Sample distribution chosen by input-size span:
  Smallest input:  8,119 chars
  Largest input:   54,761 chars
  Middle samples:  6 across the distribution

=== Projection for 75 tasks ===
Total tokens:  ~322K
  Range (95% CI):  172K - 473K
  Per-stage breakdown (mean):
    reasoning_generate     227K  (70.3%)
    qa_generate         96K  (29.7%)
  Stdev/task: 2.3K (CoV 0.53)

Total dollars: ~$2.13
  Range (95% CI):  $1.13 - $3.12

Variance warning: HIGH
   Coefficient of variation: 0.53 (>0.30 threshold)
   Recommendation: run additional samples for a tighter estimate.

Pricing source: configured at runtime startup
Verify current rates at https://anthropic.com/pricing
Sample run cost: $0.23 (already spent; included in your billing)

=== Scale projection for 5,000 passages ===
Projected workload: 5,000 QA tasks + 2,500 reasoning tasks = 7,500 total tasks
This scales the sampled WikiText workload shape; it does not load more rows.
Confidence interval includes parameter-uncertainty contribution from the 8-sample calibration; expect wider intervals when extrapolating far beyond sample size.
Total tokens:  ~32.2M
  Range (95% CI):  17.9M - 46.5M
Total dollars: ~$212.57
  Range (95% CI):  $118.10 - $307.03

Estimate complete. Run with --generate-all to execute the full pipeline.
```

A few things to notice in this output, since each one demonstrates a distinct
piece of the Estimator's design.

The **per-stage breakdown** shows that the reasoning saga is roughly 2x more
expensive per task than the QA saga (190K vs 100K tokens for the same number
of tasks). This is the kind of signal that lets a developer make informed
decisions about which sagas to scale up first.

The **variance warning** fires loudly because Wikipedia article lengths vary
widely after the length filter. A coefficient of variation above 0.30 means
the per-task cost is genuinely heterogeneous, and the estimator says so rather
than producing a deceptively tight projection.

The **scale projection's confidence interval** is wide on purpose. Projecting
from 8 samples to 7,500 tasks compounds two sources of uncertainty: the
per-task variability (which the sample reveals) and the parameter-estimate
uncertainty (because 8 samples is a small basis for estimating the population
mean). The interval is wider than a naive standard-error-of-the-sum
calculation would give, which is the honest answer for small-sample
extrapolation. Real cost on 7,500 tasks may land anywhere in the $118-$288
range with 95% confidence; halving that uncertainty requires roughly 4x more
samples (`max_samples=32` or higher).

If your run produces tighter or wider intervals, that's the math working as
designed against your particular sample. The CoV is the dial: low CoV means
your inputs are homogeneous and the projection is reliable; high CoV means
the inputs are heterogeneous and you should sample more before trusting the
total.

## Output Schemas

### `squad_format.jsonl` - SQuAD v1.1 Format

Stanford Question Answering Dataset (SQuAD) is the canonical extractive
question-answering training format, used by major language model evaluation
suites since 2016.

Each line is a JSON object:

```json
{
  "id": "wt103_00001_0",
  "title": "Passage Title",
  "context": "The full passage text from which the question is answerable...",
  "question": "What is the question?",
  "answers": {
    "text": ["the exact answer span"],
    "answer_start": [123]
  }
}
```

The `answer_start` field is a character offset within `context`. The `answers`
field is an object with parallel `text` and `answer_start` arrays (allowing
multiple correct answers in evaluation contexts; this example always emits
exactly one answer per pair).

Reference: https://rajpurkar.github.io/SQuAD-explorer/

### `reasoning_format.jsonl` - Alpaca-Style With Chain-Of-Thought

The Alpaca instruction-tuning format from Stanford CRFM, extended with a
`reasoning` field for chain-of-thought traces. This shape is used by recent
reasoning-model bootstrapping work.

Each line is a JSON object:

```json
{
  "id": "wt103_00001_wt103_00002",
  "instruction": "What is the multi-hop question?",
  "input": "=== Passage 1: Title 1 ===\n...\n\n=== Passage 2: Title 2 ===\n...",
  "reasoning": "Step 1: From passage 1, ...\nStep 2: From passage 2, ...\nStep 3: Combining these facts...",
  "output": "The final answer."
}
```

Reference: https://github.com/tatsu-lab/stanford_alpaca

## Loading The Output

```python
from datasets import load_dataset

qa_dataset = load_dataset(
    "json",
    data_files="examples/synthetic_data/output/squad_format.jsonl",
    split="train",
)
print(qa_dataset[0])

reasoning_dataset = load_dataset(
    "json",
    data_files="examples/synthetic_data/output/reasoning_format.jsonl",
    split="train",
)
print(reasoning_dataset[0])
```

The output is drop-in compatible with HuggingFace's `datasets` library. To
push to the HuggingFace Hub:

```python
qa_dataset.push_to_hub("your-username/your-dataset-name")
```

## Cost Expectations

Approximate per-task costs at Claude Sonnet 4.6 pricing:

- `qa_generate`: about 3K-10K tokens/task, or $0.02-0.08/task.
- `reasoning_generate`: about 12K-40K tokens/task, or $0.10-0.30/task.

The reasoning saga is more expensive because input is two concatenated
passages instead of one, and output includes a chain-of-thought trace alongside
the question and answer.

The estimator's per-stage breakdown surfaces this asymmetry directly.

## How This Differs From The Minimal Estimator Example

The `examples/estimator/` folder has a smaller, faster demonstration:

- One saga (translation) instead of two.
- Synthetic short articles instead of real Wikipedia passages.
- 50 tasks at low input variance.
- No output persistence.
- Runs in well under a minute.

Use the minimal example to learn how the Estimator API works. Use this example
to see what the Estimator looks like against a realistic production workload.

## Dataset Note

This example loads `wikitext-103-raw-v1` from HuggingFace's
`Salesforce/wikitext` dataset. The train split contains roughly 28,000
articles after length filtering, so realistic passage counts (50, 500, even
5000) are achievable without hitting fallback warnings. The first run
downloads about 500MB to `~/.cache/huggingface/`; subsequent runs use the
cache. Setting `HF_DATASETS_CACHE` overrides the cache location.

An earlier iteration of this example used `wikitext-2-raw-v1` test split (~30
articles after filtering), which was small enough that requesting 5,000
passages would silently fall back to running 33, then *extrapolate*
mathematically to the requested count. That extrapolation produced
deceptively tight confidence intervals on a small-sample base. The switch to
WikiText-103 train means the example projects against real workload depth
rather than scaling math, and the Estimator's CI math (corrected to compound
parameter uncertainty when extrapolating beyond sample size) tells the honest
story when sampling is genuinely thin.
