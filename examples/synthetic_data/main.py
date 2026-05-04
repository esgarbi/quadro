"""
Synthetic training data generation with cost projection.

Demonstrates Quadro's Estimator against a production-shaped heterogeneous
workload: generate two types of LLM training data (SQuAD-style QA pairs and
multi-hop reasoning chains) from real Wikipedia passages loaded from
HuggingFace.

Estimate-only mode (default):
    pip install -r examples/synthetic_data/requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/synthetic_data/main.py

Generate-all mode (runs the full pipeline; uses real API budget):
    python examples/synthetic_data/main.py --generate-all --passages 5

Outputs:
    examples/synthetic_data/output/squad_format.jsonl
    examples/synthetic_data/output/reasoning_format.jsonl

Output files follow community-standard schemas (SQuAD v1.1 for QA pairs,
Alpaca-style with chain-of-thought for reasoning chains). Loadable via
HuggingFace datasets:
    from datasets import load_dataset
    ds = load_dataset("json", data_files="path/to/file.jsonl")
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience only
    load_dotenv = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quadro import Estimator, LifecycleBuilder, Pipeline, QuadroRuntime, Saga  # noqa: E402
from quadro.board.backends import SqliteBoardBackend  # noqa: E402
from quadro.sponsor import GoalSponsor  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_PASSAGE_COUNT = 50
MIN_WORDS = 200
MAX_WORDS = 3000

# WikiText-103 train split contains ~28k articles (~500MB on first download,
# cached locally afterwards). The earlier shipped example used wikitext-2's
# test split which only contains ~30 articles matching the length filter,
# making any "5,000 passage" demo extrapolate from a tiny sample. WikiText-103
# train gives realistic dataset depth with the same format and license.
WIKITEXT_DATASET = "Salesforce/wikitext"
WIKITEXT_CONFIG = "wikitext-103-raw-v1"
WIKITEXT_SPLIT = "train"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DB_PATH = "synthetic_data.db"

if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parents[1] / "anthropic_minimal" / ".env")


def _ensure_example_deps() -> None:
    """Verify HuggingFace dependencies that are local to this example."""
    missing: list[str] = []
    try:
        import datasets  # noqa: F401
    except ImportError:
        missing.append("datasets")
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        missing.append("huggingface_hub")

    if missing:
        print("ERROR: example-local dependencies missing: " + ", ".join(missing))
        print()
        print("Install with:")
        print("  pip install -r examples/synthetic_data/requirements.txt")
        sys.exit(1)


def _ensure_anthropic_adapter() -> Any:
    try:
        from quadro_anthropic import AnthropicReasoner
    except ImportError:
        print("ERROR: quadro_anthropic adapter is not installed.")
        print()
        print("Install with:")
        print("  pip install 'quadro[anthropic]'")
        sys.exit(1)
    return AnthropicReasoner


class QAPair(BaseModel):
    question: str = Field(description="The question text")
    answer: str = Field(description="The exact answer span from the passage")
    answer_start: int = Field(
        description="Character offset of answer span within the passage"
    )


class QAGenerationOutput(BaseModel):
    qa_pairs: list[QAPair] = Field(description="3 question-answer pairs")


class ReasoningChainOutput(BaseModel):
    instruction: str = Field(description="The multi-hop question")
    input: str = Field(description="The concatenated passage text")
    reasoning: str = Field(description="Step-by-step chain-of-thought trace")
    output: str = Field(description="The final answer")


QA_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "qa_pending")
    .phase("qa_pending", "qa_done")
    .build()
)

REASONING_PROFILE = (
    LifecycleBuilder()
    .phase("UNASSIGNED", "reasoning_pending")
    .phase("reasoning_pending", "reasoning_done")
    .build()
)


def _qa_extract(ctx: Any) -> dict[str, str]:
    notes = ctx.task.get("notes") or []
    return {
        "passage": notes[0] if notes else "",
        "passage_id": ctx.task.get("passage_id", "unknown"),
        "title": ctx.task.get("title", "Untitled"),
    }


def _is_qa_task(ctx: Any) -> bool:
    return ctx.task.get("task_type") == "qa_pair"


def _qa_skip(ctx: Any) -> dict[str, bool]:
    return {
        "skipped": True,
        "task_type": str(ctx.task.get("task_type") or "unknown"),
    }


def _warn_if_answer_offset_mismatch(
    *,
    passage_id: str,
    pair_index: int,
    passage: str,
    pair: QAPair,
) -> None:
    start = pair.answer_start
    end = start + len(pair.answer)
    if start < 0 or end > len(passage) or passage[start:end] != pair.answer:
        actual = passage.find(pair.answer)
        hint = f" first occurrence is {actual}" if actual >= 0 else " span not found"
        print(
            "WARNING: answer_start mismatch for "
            f"{passage_id} pair {pair_index}: got {start};{hint}. "
            "Persisting anyway per SQuAD soft-validation policy."
        )


def _is_estimator_task(task: dict[str, Any]) -> bool:
    task_id = str(task.get("task_id") or "")
    return task_id.startswith(("dry-run-", "sample-"))


def _qa_persist(ctx: Any) -> dict[str, int]:
    """Append the QA pairs to the SQuAD-format JSONL output file."""
    board_fn = ctx.task["_board_fn"]
    extracted = ctx.step["qa_extract"]
    if _is_estimator_task(ctx.task):
        board_fn(
            "board.update_task",
            {
                "task_id": ctx.task["task_id"],
                "to_status": "qa_done",
                "output": json.dumps({"pair_count": 0}, ensure_ascii=False),
            },
        )
        return {"persisted": 0}

    result: QAGenerationOutput = ctx.step["qa_generate"]

    output_file = OUTPUT_DIR / "squad_format.jsonl"
    with output_file.open("a", encoding="utf-8") as f:
        for i, pair in enumerate(result.qa_pairs):
            _warn_if_answer_offset_mismatch(
                passage_id=extracted["passage_id"],
                pair_index=i,
                passage=extracted["passage"],
                pair=pair,
            )
            squad_entry = {
                "id": f"{extracted['passage_id']}_{i}",
                "title": extracted["title"],
                "context": extracted["passage"],
                "question": pair.question,
                "answers": {
                    "text": [pair.answer],
                    "answer_start": [pair.answer_start],
                },
            }
            f.write(json.dumps(squad_entry, ensure_ascii=False) + "\n")

    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "qa_done",
            "output": json.dumps(
                {"pair_count": len(result.qa_pairs)}, ensure_ascii=False
            ),
        },
    )
    return {"persisted": len(result.qa_pairs)}


qa_saga = (
    Saga("qa_generate")
    .gate(
        "route_qa_task",
        when=_is_qa_task,
        on_true="qa_extract",
        on_false="qa_skip",
    )
    .deterministic("qa_extract", _qa_extract)
    .reason(
        "qa_generate",
        prompt=PROMPTS_DIR / "qa_generation.md",
        user_message=lambda ctx: ctx.step["qa_extract"]["passage"],
        schema=QAGenerationOutput,
    )
    .deterministic("qa_persist", _qa_persist)
    .deterministic("qa_skip", _qa_skip)
    .build()
)


def _reasoning_extract(ctx: Any) -> dict[str, str]:
    notes = ctx.task.get("notes") or []
    return {
        "passages": notes[0] if notes else "",
        "passage_id": ctx.task.get("passage_id", "unknown"),
    }


def _is_reasoning_task(ctx: Any) -> bool:
    return ctx.task.get("task_type") == "reasoning_chain"


def _reasoning_skip(ctx: Any) -> dict[str, bool | str]:
    return {
        "skipped": True,
        "task_type": str(ctx.task.get("task_type") or "unknown"),
    }


def _reasoning_persist(ctx: Any) -> dict[str, bool]:
    """Append the reasoning chain to the Alpaca-format JSONL output file."""
    board_fn = ctx.task["_board_fn"]
    extracted = ctx.step["reasoning_extract"]
    if _is_estimator_task(ctx.task):
        board_fn(
            "board.update_task",
            {
                "task_id": ctx.task["task_id"],
                "to_status": "reasoning_done",
                "output": json.dumps({"persisted": False}, ensure_ascii=False),
            },
        )
        return {"persisted": False}

    result: ReasoningChainOutput = ctx.step["reasoning_generate"]

    output_file = OUTPUT_DIR / "reasoning_format.jsonl"
    with output_file.open("a", encoding="utf-8") as f:
        alpaca_entry = {
            "id": extracted["passage_id"],
            "instruction": result.instruction,
            "input": result.input,
            "reasoning": result.reasoning,
            "output": result.output,
        }
        f.write(json.dumps(alpaca_entry, ensure_ascii=False) + "\n")

    board_fn(
        "board.update_task",
        {
            "task_id": ctx.task["task_id"],
            "to_status": "reasoning_done",
            "output": json.dumps({"persisted": True}, ensure_ascii=False),
        },
    )
    return {"persisted": True}


reasoning_saga = (
    Saga("reasoning_generate")
    .gate(
        "route_reasoning_task",
        when=_is_reasoning_task,
        on_true="reasoning_extract",
        on_false="reasoning_skip",
    )
    .deterministic("reasoning_extract", _reasoning_extract)
    .reason(
        "reasoning_generate",
        prompt=PROMPTS_DIR / "reasoning_chain.md",
        user_message=lambda ctx: ctx.step["reasoning_extract"]["passages"],
        schema=ReasoningChainOutput,
    )
    .deterministic("reasoning_persist", _reasoning_persist)
    .deterministic("reasoning_skip", _reasoning_skip)
    .build()
)


def _load_wikitext_passages(count: int, seed: int = 42) -> list[dict[str, str]]:
    """Load filtered WikiText passages with deterministic sampling.

    Uses the wikitext-103 train split (~28k articles, ~500MB cached on first
    download) so realistic passage counts are achievable. Earlier iterations
    used wikitext-2 test (~30 articles) which forced "5,000 passage" demos
    into a fallback that scaled estimates from a tiny sample.

    For very small ``count`` values, streams the dataset and stops as soon as
    enough length-filtered passages are collected. For larger ``count``
    values, loads the full split into memory because the deterministic seed
    needs to shuffle a complete passage list before slicing.
    """
    from datasets import load_dataset

    print(
        f"Loading {WIKITEXT_CONFIG} from HuggingFace "
        "(cached after first run; ~500MB on first download)..."
    )

    passages: list[dict[str, str]] = []
    current_text: list[str] = []
    current_title: str | None = None
    counter = 0
    seen_first_heading = False

    dataset = load_dataset(WIKITEXT_DATASET, WIKITEXT_CONFIG, split=WIKITEXT_SPLIT)

    for row in dataset:
        line = str(row["text"]).strip()
        is_top_level_heading = _is_top_level_wikitext_heading(line)
        if is_top_level_heading:
            if current_text and current_title:
                counter = _append_passage_if_usable(
                    passages=passages,
                    counter=counter,
                    title=current_title,
                    lines=current_text,
                )
            current_title = _wikitext_heading_title(line)
            current_text = []
            seen_first_heading = True
        elif line and seen_first_heading:
            current_text.append(line)

    if current_text and current_title:
        _append_passage_if_usable(
            passages=passages,
            counter=counter,
            title=current_title,
            lines=current_text,
        )

    rng = random.Random(seed)
    rng.shuffle(passages)

    if count > len(passages):
        print(
            f"WARNING: requested {count} passages but only {len(passages)} "
            "match the length filter; using all available."
        )
        count = len(passages)

    selected = passages[:count]
    if not selected:
        print(
            "ERROR: no WikiText passages matched the length filter. "
            "Check the heading heuristic or adjust MIN_WORDS/MAX_WORDS."
        )
        sys.exit(1)

    word_counts = [len(p["text"].split()) for p in selected]
    print(
        f"Loaded {len(selected)} passages "
        f"(word count range: {min(word_counts):,} - {max(word_counts):,})"
    )
    return selected


def _is_top_level_wikitext_heading(line: str) -> bool:
    """Detect WikiText top-level article headings like '= Title ='."""
    return line.startswith("= ") and line.endswith(" =") and not line.startswith("= =")


def _wikitext_heading_title(line: str) -> str:
    return line.removeprefix("= ").removesuffix(" =").strip()


def _append_passage_if_usable(
    *,
    passages: list[dict[str, str]],
    counter: int,
    title: str,
    lines: list[str],
) -> int:
    text = "\n".join(lines).strip()
    word_count = len(text.split())
    if MIN_WORDS <= word_count <= MAX_WORDS:
        passages.append(
            {
                "passage_id": f"wt103_{counter:05d}",
                "title": title,
                "text": text,
            }
        )
        return counter + 1
    return counter


def _build_queue(passages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Build 1 QA task per passage and 1 reasoning task per passage pair."""
    queue: list[dict[str, Any]] = []

    for passage in passages:
        queue.append(
            {
                "task_type": "qa_pair",
                "label": f"QA: {passage['title'][:60]}",
                "notes": [passage["text"]],
                "passage_id": passage["passage_id"],
                "title": passage["title"],
            }
        )

    for i in range(0, len(passages) - 1, 2):
        p1 = passages[i]
        p2 = passages[i + 1]
        combined = (
            f"=== Passage 1: {p1['title']} ===\n{p1['text']}\n\n"
            f"=== Passage 2: {p2['title']} ===\n{p2['text']}"
        )
        queue.append(
            {
                "task_type": "reasoning_chain",
                "label": f"Reasoning: {p1['title'][:30]} + {p2['title'][:30]}",
                "notes": [combined],
                "passage_id": f"{p1['passage_id']}_{p2['passage_id']}",
            }
        )

    return queue


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _training_task_count_for_passages(passage_count: int) -> int:
    return passage_count + (passage_count // 2)


def _format_tokens(n: int) -> str:
    if n < 1000:
        return f"{n:,}"
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}M"


def _print_scale_projection(estimator: Estimator, passage_count: int) -> None:
    task_count = _training_task_count_for_passages(passage_count)
    projection = estimator.project(n_tasks=task_count)

    print()
    print(f"=== Scale projection for {passage_count:,} passages ===")
    print(
        f"Projected workload: {passage_count:,} QA tasks + "
        f"{passage_count // 2:,} reasoning tasks = {task_count:,} total tasks"
    )
    print(
        "This scales the sampled WikiText workload shape; "
        "it does not load more rows."
    )
    print(
        "Confidence interval includes parameter-uncertainty contribution "
        f"from the {projection.samples_used}-sample calibration; expect "
        "wider intervals when extrapolating far beyond sample size."
    )
    print(f"Total tokens:  ~{_format_tokens(projection.total_tokens)}")
    print(
        f"  Range ({projection.confidence:.0%} CI):  "
        f"{_format_tokens(projection.total_tokens_low)} - "
        f"{_format_tokens(projection.total_tokens_high)}"
    )
    if projection.total_dollars is not None:
        print(f"Total dollars: ~${projection.total_dollars:,.2f}")
        print(
            f"  Range ({projection.confidence:.0%} CI):  "
            f"${projection.total_dollars_low or 0:,.2f} - "
            f"${projection.total_dollars_high or 0:,.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic training data generation with Quadro Estimator"
    )
    parser.add_argument(
        "--passages",
        type=int,
        default=DEFAULT_PASSAGE_COUNT,
        help=f"Number of Wikipedia passages to load (default: {DEFAULT_PASSAGE_COUNT})",
    )
    parser.add_argument(
        "--generate-all",
        action="store_true",
        help="After projection, run the full pipeline and persist outputs",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for deterministic passage sampling (default: 42)",
    )
    parser.add_argument(
        "--scale-passages",
        type=int,
        default=5000,
        help=(
            "Also print an approximate scale projection for this many passages "
            "(default: 5000)"
        ),
    )
    args = parser.parse_args()

    _ensure_example_deps()

    if args.passages < 1:
        print("ERROR: --passages must be at least 1.")
        sys.exit(1)
    if args.scale_passages < 1:
        print("ERROR: --scale-passages must be at least 1.")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.generate_all:
        for filename in ("squad_format.jsonl", "reasoning_format.jsonl"):
            target = OUTPUT_DIR / filename
            if target.exists():
                target.unlink()

    passages = _load_wikitext_passages(args.passages, seed=args.seed)
    queue = _build_queue(passages)
    qa_task_count = sum(1 for task in queue if task["task_type"] == "qa_pair")
    reasoning_task_count = sum(
        1 for task in queue if task["task_type"] == "reasoning_chain"
    )
    print(
        f"Built queue: {qa_task_count} QA tasks + "
        f"{reasoning_task_count} reasoning tasks"
    )

    model = os.environ.get("ANTHROPIC_MODEL_ID", DEFAULT_MODEL)
    AnthropicReasoner = _ensure_anthropic_adapter()
    runtime = (
        QuadroRuntime(SqliteBoardBackend(DB_PATH))
        .with_profiles(
            profile_resolver={
                "qa_pair": "qa_pair",
                "reasoning_chain": "reasoning_chain",
            },
            custom_profiles={
                "qa_pair": QA_PROFILE,
                "reasoning_chain": REASONING_PROFILE,
            },
        )
        .with_pricing(
            {
                model: {
                    "input": 3.0,
                    "output": 15.0,
                    "io_ratio": 0.30,
                }
            },
            verify_url="https://anthropic.com/pricing",
        )
    )
    board = runtime.board

    def client_factory() -> Any:
        from anthropic import Anthropic

        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    pipeline = (
        Pipeline(board)
        .reasoner(AnthropicReasoner(client_factory=client_factory, model=model))
        .workers(1)
        .stage("qa_generate", saga=qa_saga, active_status="qa_pending")
        .stage(
            "reasoning_generate",
            saga=reasoning_saga,
            active_status="reasoning_pending",
        )
    )

    estimator = Estimator.from_dry_run(pipeline=pipeline, queue=queue)
    print(estimator.format())
    _print_scale_projection(estimator, args.scale_passages)

    if not args.generate_all:
        print()
        print("Estimate complete. Run with --generate-all to execute the full pipeline.")
        return

    print()
    print(f"User confirmed --generate-all. Running {len(queue)} tasks...")
    print()

    for task in queue:
        runtime.client.post_task(
            task["task_type"],
            task["label"],
            notes=task["notes"],
            passage_id=task["passage_id"],
            title=task.get("title", ""),
        )

    built = pipeline.build()
    runtime.sponsor(
        GoalSponsor(
            lambda state: sum(
                1
                for task in state.get("tasks", [])
                if task.get("status") in ("qa_done", "reasoning_done")
            )
            >= len(queue)
        )
    ).poll_every(1.0).run(built)

    print()
    print("=== Generation complete ===")
    qa_file = OUTPUT_DIR / "squad_format.jsonl"
    reasoning_file = OUTPUT_DIR / "reasoning_format.jsonl"
    qa_count = sum(1 for _ in qa_file.open(encoding="utf-8")) if qa_file.exists() else 0
    reasoning_count = (
        sum(1 for _ in reasoning_file.open(encoding="utf-8"))
        if reasoning_file.exists()
        else 0
    )
    print(f"QA pairs:           {qa_count} examples -> {_relative_path(qa_file)}")
    print(
        "Reasoning chains:   "
        f"{reasoning_count} examples -> {_relative_path(reasoning_file)}"
    )
    print()
    print("Output files follow community-standard schemas:")
    print("- squad_format.jsonl: SQuAD v1.1 format (Stanford QA Dataset)")
    print("- reasoning_format.jsonl: Alpaca-style with chain-of-thought reasoning")
    print()
    print("Validate with HuggingFace datasets:")
    print("  from datasets import load_dataset")
    print(
        '  qa = load_dataset("json", '
        'data_files="examples/synthetic_data/output/squad_format.jsonl")'
    )
    print(
        '  reasoning = load_dataset("json", '
        'data_files="examples/synthetic_data/output/reasoning_format.jsonl")'
    )


if __name__ == "__main__":
    main()
