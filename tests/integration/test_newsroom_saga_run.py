"""
Integration test: the four-saga newsroom produces a published article.

Uses a real QuadroBoard (in-memory SQLite) and a fake reasoner. No
network access, no LLM key, no MAF runtime involved. The test drives
each stage's saga via ``QuadroSagaRuntime.run_stage`` directly,
bypassing chief/worker wiring — the point is to prove that the four
sagas compose and produce the expected on-disk article JSON through
the saga runtime alone. The MAF reasoner is covered separately by
the manual newsroom run with a real OpenAI key.

This is the structural bridge between the saga step-kind unit tests
and the manual end-to-end acceptance criterion.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# ── sys.path plumbing so the newsroom's own modules import cleanly ────────────

NEWSROOM_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "newsroom"
)
if str(NEWSROOM_DIR) not in sys.path:
    sys.path.insert(0, str(NEWSROOM_DIR))

# dotenv is used at module-top of sagas.py via `dotenv.load_dotenv()`; skip
# rather than fail if a developer runs this test without the dep.
pytest.importorskip("dotenv")

from quadro import LifecycleBuilder, QuadroRuntime                           # noqa: E402
from quadro.board.backends import SqliteBoardBackend                         # noqa: E402
from quadro.pipeline import StageSpec                                        # noqa: E402
from quadro.runtime_plugins.base import RuntimeContext                       # noqa: E402
from quadro.runtime_plugins.saga import QuadroSagaRuntime                    # noqa: E402
from quadro.saga.reasoner import ReasonResult                                # noqa: E402


# ── Fake reasoner ─────────────────────────────────────────────────────────────


class _FakeReasoner:
    """Structurally matches the Reasoner Protocol. Returns canned
    pydantic instances in FIFO order."""

    reasoner_id = "fake"

    def __init__(self) -> None:
        self.queue: list[tuple[object, int]] = []

    async def reason(self, *, prompt, user_message, schema, token_reporter):
        if not self.queue:
            raise AssertionError(
                f"FakeReasoner exhausted; next requested prompt="
                f"{prompt[:40]!r}, user_message={str(user_message)[:40]!r}"
            )
        output, tokens = self.queue.pop(0)
        if token_reporter is not None and tokens > 0:
            try:
                token_reporter(tokens)
            except Exception:
                pass
        return ReasonResult(output=output, tokens_used=tokens, raw_text=str(output))


def _article_lifecycle():
    return (
        LifecycleBuilder()
        .phase("UNASSIGNED", "ideating")
        .phase("ideating", "idea_ready")
        .phase("idea_ready", "researching")
        .phase("researching", "research_ready")
        .phase("research_ready", "writing")
        .phase("writing", "draft_ready")
        .phase("draft_ready", "reviewing")
        .phase("reviewing", "published")
        .revision("reviewing", "idea_ready")
        .build()
    )


def test_four_saga_newsroom_publishes_article(tmp_path, monkeypatch) -> None:
    """Drive the four sagas in order on a single task and verify the
    published .md and .json files appear with the expected shape."""
    from schemas import (                                                    # noqa: E402
        ApprovedOutput,
        ArticleBrief,
        Headline,
        ResearchStrategy,
    )
    import sagas                                                             # noqa: E402

    # Isolate the on-disk artefact directory to this test's tmp_path.
    monkeypatch.setattr(sagas, "ARTICLES_DIR", tmp_path)

    # Short-circuit the PubMed fetch so the test is offline and
    # deterministic. Returns the dict shape that the saga's dedupe step
    # expects.
    canned = [
        {
            "pmid": "12345",
            "title": "A canned citation",
            "authors": "Smith J",
            "year": 2023,
            "journal": "Example Journal",
            "abstract": "",
        }
    ]
    monkeypatch.setattr(sagas, "_pubmed_search", lambda *a, **kw: list(canned))

    # Real in-memory SQLite board with the article lifecycle registered.
    backend = SqliteBoardBackend()
    runtime = QuadroRuntime(backend).with_profiles(
        profile_resolver={"article": "article"},
        custom_profiles={"article": _article_lifecycle()},
    )
    client = runtime.client
    task = client.post_task("article", "health and wellbeing")
    task_id = task["task_id"]

    # Prime the reasoner with one canned response per reason() call the
    # four sagas will make, in the order the saga runtime invokes them.
    headline = Headline(headline="Why Walking Is Fabulous")
    brief = ArticleBrief(
        title="Why Walking Is Fabulous",
        primary_category="wellbeing",
        keywords=["walking", "cardio", "health"],
        lead="A short lead on walking.",
        thesis="Walking improves health.",
        sections=["Intro", "Evidence", "Practice", "Conclusion"],
        writer="Alice Reviewer",
        writer_rationale="Alice covers movement science.",
        abstract="An abstract on walking as a longevity intervention.",
        research_keywords=["walking benefits", "cardio health", "activity aging"],
    )
    strategy = ResearchStrategy(
        core_concepts=[
            {
                "concept": "walking",
                "scientific_terms": ["walking[MeSH]"],
                "consumer_terms": ["walking"],
            }
        ],
        pubmed_queries=["walking benefits"],
        gap_angle="",
        suggested_filters={
            "date_range": "",
            "study_types": [],
            "exclude_terms": [],
        },
    )
    article_md = (
        "# Why Walking Is Fabulous\n\n"
        "Walking is among the simplest, most accessible forms of exercise.\n"
    )
    decision = ApprovedOutput(approved=True, reason="clear and well-evidenced")

    reasoner = _FakeReasoner()
    reasoner.queue = [
        (headline, 50),      # ideation / propose_headline
        (brief, 200),        # ideation / flesh_out_brief
        (strategy, 100),     # research / plan_strategy
        (article_md, 500),   # writing / draft_article (no schema)
        (decision, 40),      # review  / editorial_decision
    ]

    saga_runtime = QuadroSagaRuntime()
    saga_runtime.register_reasoner(reasoner)

    def board_fn(intent, payload):
        return client.request(intent, payload)

    async def _run(stage_name: str, saga_obj, active_status: str):
        # Transition the task to this stage's active_status, then run
        # the saga against the freshly-read task state.
        current = client.get_task(task_id)
        if current["status"] != active_status:
            client.update_task(task_id, active_status)
        task_dict = client.get_task(task_id)
        spec = StageSpec(
            capability=stage_name,
            saga=saga_obj,
            active_status=active_status,
        )
        ctx = RuntimeContext(
            stage=spec,
            task=dict(task_dict),
            context={"payload": {"task": task_dict}},
            board_fn=board_fn,
        )
        return await saga_runtime.run_stage(ctx)

    # Mirror the pipeline's flow: ideation → research → writing → review.
    # Each saga's last step performs its own board.update_task, so after
    # the saga returns the task is already in the next "ready" phase.
    asyncio.run(_run("ideation", sagas.ideation_saga, "ideating"))
    asyncio.run(_run("research", sagas.research_saga, "researching"))
    asyncio.run(_run("writing", sagas.writing_saga, "writing"))
    asyncio.run(_run("review", sagas.review_saga, "reviewing"))

    # Final state: the task reached `published` and both output files
    # are present with the expected top-level keys.
    final = client.get_task(task_id)
    assert final["status"] == "published", f"task ended at {final['status']!r}"

    md_files = list(tmp_path.glob("*.md"))
    json_files = list(tmp_path.glob("*.json"))
    assert md_files, "expected at least one published .md file"
    assert json_files, "expected at least one published .json file"

    flight = json.loads(json_files[0].read_text())
    # The writing step produced markdown, the review step wrote both
    # files. The flight-plan JSON should reference the brief, research,
    # and the reviewer's decision.
    assert "brief" in flight
    assert "research" in flight
    assert "review_decision" in flight
    assert "article_md" in flight
    # tokens.by_stage is the observation-#2 closeout: every stage that
    # ran a reason() call should have a non-zero entry here.
    by_stage = flight.get("tokens", {}).get("by_stage", {})
    for stage in ("ideation", "research", "writing", "review"):
        assert by_stage.get(stage, 0) > 0, (
            f"tokens.by_stage[{stage!r}] = {by_stage.get(stage)!r}; "
            f"expected non-zero after the saga runtime's per-stage "
            f"token attribution wiring (milestone B observation #2 "
            f"closeout)."
        )
