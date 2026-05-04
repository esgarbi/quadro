"""
ArticleProducer — feeds article tasks into the newsroom board.

Separates task *creation* from task *routing*. The producer posts articles;
the chief routes them through ideation → research → writing → review → published.
The chief never needs to create articles — it only advances what's already there.

Simple API:

    # Uniform batches: 2 articles every 5 minutes until 10 are posted
    ArticleProducer(bc, target=10, batch_size=2, batch_interval_minutes=5.0)

    # Explicit choreography: list of (batch_size, wait_minutes_before_posting)
    ArticleProducer(bc, target=8, choreography=[
        (2, 0.0),    # post 2 immediately
        (2, 5.0),    # post 2 more after 5 min
        (2, 10.0),   # post 2 more after 10 min
        (2, 5.0),    # final 2 after 5 min
    ])
"""

from __future__ import annotations

import logging
import random
import threading
import time

from quadro.board.client import BoardClient

logger = logging.getLogger(__name__)

_TOPIC_HINTS = [
    "gut microbiome and mental health",
    "sleep quality and cognitive performance",
    "intermittent fasting mechanisms",
    "cold exposure and metabolism",
    "heart rate variability and stress",
    "circadian rhythm and longevity",
    "inflammation and chronic disease",
    "strength training and aging",
    "mindfulness and cortisol regulation",
    "omega-3 and brain health",
    "hydration and cognitive function",
    "breathing techniques and anxiety",
    "vitamin D and immune function",
    "blue light and sleep disruption",
    "exercise and neuroplasticity",
]


class ArticleProducer:
    """
    Background thread that posts article tasks onto the newsroom board.

    Decouples article creation from routing. The chief sees UNASSIGNED tasks
    and routes them through the pipeline — it never has to decide what to create.

    Args:
        board_client:           BoardClient for posting tasks.
        target:                 Hard cap on total articles posted.
        batch_size:             Articles per batch (used when choreography is None).
        batch_interval_minutes: Minutes between batches (used when choreography is None).
        choreography:           Explicit list of (batch_size, wait_minutes) steps.
                                When provided, batch_size and batch_interval_minutes
                                are ignored. The producer walks the list once and stops.
    """

    def __init__(
        self,
        board_client: BoardClient,
        target: int,
        batch_size: int = random.randint(3, 6),
        batch_interval_minutes: float = random.uniform(0.5, 1.5),
        choreography: list[tuple[int, float]] | None = None,
    ) -> None:
        self._bc = board_client
        self._target = target
        self._lock = threading.Lock()
        self._posted = 0
        self.running = True

        if choreography is not None:
            self._steps = list(choreography)
        else:
            count = target
            steps: list[tuple[int, float]] = []
            while count > 0:
                n = min(batch_size, count)
                wait = 0.0 if not steps else batch_interval_minutes
                steps.append((n, wait))
                count -= n
            self._steps = steps

        total_planned = sum(s for s, _ in self._steps)
        logger.info(
            "ArticleProducer starting — target=%d  steps=%d  planned=%d",
            target,
            len(self._steps),
            min(total_planned, target),
        )

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="article-producer"
        )
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self.running = False
        logger.info(
            "ArticleProducer stopped — posted %d/%d articles",
            self._posted,
            self._target,
        )

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "posted": self._posted,
                "target": self._target,
                "done": self._posted >= self._target,
            }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _post_article(self) -> str | None:
        """Post one article task. Returns task_id or None on failure."""
        topic = random.choice(_TOPIC_HINTS)
        try:
            task = self._bc.post_task("article", topic)
            return task["task_id"]
        except Exception as exc:
            logger.warning("ArticleProducer: post_task failed: %s", exc)
            return None

    def _loop(self) -> None:
        for batch_size, wait_minutes in self._steps:
            if not self.running:
                break

            if wait_minutes > 0:
                deadline = time.monotonic() + (wait_minutes * 60)
                while self.running and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
                if not self.running:
                    break

            with self._lock:
                remaining = self._target - self._posted
            n = min(batch_size, remaining)
            if n <= 0:
                break

            posted_this_batch = 0
            for _ in range(n):
                task_id = self._post_article()
                if task_id:
                    posted_this_batch += 1
                    with self._lock:
                        self._posted += 1
                    logger.debug(
                        "ArticleProducer: posted %s [total=%d/%d]",
                        task_id[:8],
                        self._posted,
                        self._target,
                    )

        logger.info(
            "ArticleProducer: finished — posted %d/%d articles",
            self._posted,
            self._target,
        )
        self.running = False
