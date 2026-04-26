"""
OrderProducer — configurable order emission for the ordering system example.

Designed for observing Chief sleep patterns under different load conditions.

TWO MODES:

1. Single profile — emit orders at a fixed rate:
       producer = OrderProducer(bc, profile="steady")

2. Choreography — cycle through (profile, duration_seconds) steps automatically:
       producer = OrderProducer(bc, choreography=[
           ("steady",  60),   # emit orders for 1 minute
           ("idle",   120),   # pause for 2 minutes
           ("burst",   30),   # burst for 30 seconds
           ("idle",    90),   # pause for 90 seconds
       ])
   The sequence repeats from the beginning when it reaches the end.
   Useful for capturing distinct sleep pattern changes hands-free.

PROFILES:
    burst    Rapid batches, short pauses — Chief wakes frequently
    steady   Regular small batches — even cadence (default)
    slow     Occasional orders, long pauses — Chief sleeps most of the time
    wave     Bursts and silence alternating — visible wave in sleep sparkline
    drought  Very long pauses, rare orders — tests stale task detection
    idle     No orders at all — Chief stays asleep; baseline sleep study

Switch profile at runtime (single-profile mode only):
    producer.set_profile("drought")
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Literal

from quadro.board.client import BoardClient

from data import CUSTOMER_DETAILS, PRODUCT_CATALOG

logger = logging.getLogger(__name__)

Profile = Literal["burst", "steady", "slow", "wave", "drought", "idle"]

# ── Emission profiles ──────────────────────────────────────────────────────────

_PROFILES: dict[str, dict] = {
    "burst": {
        "batch_sizes": ([3, 4, 5, 6, 8], [40, 30, 15, 10, 5]),
        "delay_mean": 5.0,
        "delay_min": 2.0,
        "delay_max": 12.0,
        "dropout_prob": 0.0,
        "description": "Rapid batches — Chief wakes frequently, short sleep intervals",
    },
    "steady": {
        "batch_sizes": ([1, 2, 3], [60, 30, 10]),
        "delay_mean": 15.0,
        "delay_min": 8.0,
        "delay_max": 25.0,
        "dropout_prob": 0.05,
        "description": "Regular small batches — even cadence, predictable sleep pattern",
    },
    "slow": {
        "batch_sizes": ([1, 2], [85, 15]),
        "delay_mean": 45.0,
        "delay_min": 20.0,
        "delay_max": 90.0,
        "dropout_prob": 0.15,
        "description": "Occasional orders — long sleep gaps, few Chief wakes",
    },
    "wave": {
        "batch_sizes": ([4, 5, 6], [40, 35, 25]),
        "delay_mean": 8.0,
        "delay_min": 3.0,
        "delay_max": 60.0,
        "dropout_prob": 0.30,
        "description": "Bursts and silence alternating — visible wave in sleep sparkline",
    },
    "drought": {
        "batch_sizes": ([1], [100]),
        "delay_mean": 120.0,
        "delay_min": 60.0,
        "delay_max": 240.0,
        "dropout_prob": 0.50,
        "description": "Very long pauses — Chief barely wakes; tests stale task detection",
    },
    "idle": {
        "batch_sizes": ([0], [100]),
        "delay_mean": 60.0,
        "delay_min": 60.0,
        "delay_max": 60.0,
        "dropout_prob": 1.0,
        "description": "No orders — board stays empty, Chief stays asleep",
    },
}

# Type alias for a choreography step: (profile_name, duration_in_seconds)
ChoreographyStep = tuple[Profile, float]


class OrderProducer:
    """
    Background thread that emits orders onto the board at a configurable rate.

    Single-profile mode:
        producer = OrderProducer(bc, profile="steady")

    Choreography mode (cycles through steps automatically):
        producer = OrderProducer(bc, choreography=[
            ("steady",  60),   # work for 1 minute
            ("idle",   120),   # pause for 2 minutes
        ])
    """

    def __init__(
        self,
        board_client: BoardClient,
        profile: Profile = "steady",
        choreography: list[ChoreographyStep] | None = None,
    ) -> None:
        self._bc = board_client
        self._lock = threading.Lock()
        self.running = True
        self._emission_count = 0
        self._order_count = 0

        # Choreography mode takes precedence over single profile
        self._choreography = choreography
        self._choreo_index = 0
        self._choreo_step_started: float = time.monotonic()

        if choreography:
            self._profile_name = choreography[0][0]
            logger.info(
                "OrderProducer starting — choreography mode (%d steps): %s",
                len(choreography),
                " → ".join(f"{p}({d}s)" for p, d in choreography),
            )
        else:
            self._profile_name: Profile = profile
            logger.info(
                "OrderProducer starting — profile=%r (%s)",
                profile,
                _PROFILES[profile]["description"],
            )

        self._thread = threading.Thread(
            target=self._production_loop,
            daemon=True,
            name="order-producer",
        )
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_profile(self, profile: Profile) -> None:
        """
        Switch emission profile at runtime (single-profile mode only).
        Takes effect on the next emission cycle.
        Has no effect in choreography mode.
        """
        if self._choreography:
            logger.warning("OrderProducer: set_profile() ignored in choreography mode")
            return
        if profile not in _PROFILES:
            raise ValueError(
                f"Unknown profile {profile!r}. Choose from: {list(_PROFILES)}"
            )
        with self._lock:
            old = self._profile_name
            self._profile_name = profile
        logger.info(
            "OrderProducer profile: %r → %r (%s)",
            old,
            profile,
            _PROFILES[profile]["description"],
        )

    def stop(self) -> None:
        self.running = False
        logger.info(
            "OrderProducer stopped — %d batches, %d orders total",
            self._emission_count,
            self._order_count,
        )

    @property
    def stats(self) -> dict:
        with self._lock:
            info = {
                "profile": self._profile_name,
                "emissions": self._emission_count,
                "orders": self._order_count,
            }
            if self._choreography:
                step_elapsed = time.monotonic() - self._choreo_step_started
                _, step_duration = self._choreography[self._choreo_index]
                info["choreo_step"] = self._choreo_index
                info["step_remaining_s"] = max(0.0, step_duration - step_elapsed)
            return info

    # ── Internal ───────────────────────────────────────────────────────────────

    def _advance_choreography(self) -> None:
        """Check if the current choreography step has expired and advance if so."""
        if not self._choreography:
            return
        now = time.monotonic()
        _, step_duration = self._choreography[self._choreo_index]
        if (now - self._choreo_step_started) >= step_duration:
            self._choreo_index = (self._choreo_index + 1) % len(self._choreography)
            new_profile, new_duration = self._choreography[self._choreo_index]
            with self._lock:
                old = self._profile_name
                self._profile_name = new_profile
            self._choreo_step_started = now
            logger.info(
                "Choreography step %d/%d: %r → %r (will run for %.0fs)",
                self._choreo_index,
                len(self._choreography),
                old,
                new_profile,
                new_duration,
            )

    def _current_profile(self) -> dict:
        with self._lock:
            return _PROFILES[self._profile_name]

    def _production_loop(self) -> None:
        while self.running:
            self._advance_choreography()

            profile = self._current_profile()

            # Dropout: simulate silence / dead periods
            if random.random() < profile["dropout_prob"]:
                time.sleep(min(profile["delay_min"], 5.0))
                continue

            # Batch size
            sizes, weights = profile["batch_sizes"]
            n = random.choices(sizes, weights=weights)[0]

            if n > 0:
                orders = [self._generate_order() for _ in range(n)]
                try:
                    self._bc.put_data("orders_in_queue", orders)
                    self._emission_count += 1
                    self._order_count += n
                    logger.debug(
                        "OrderProducer: emitted %d order(s) [profile=%s, total=%d]",
                        n,
                        self._profile_name,
                        self._order_count,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OrderProducer: board write failed: %s", exc)

            # Wait — exponential distribution clamped to [min, max]
            # In choreography mode, cap the sleep so step transitions are responsive
            raw_delay = random.expovariate(1.0 / profile["delay_mean"])
            wait = max(profile["delay_min"], min(profile["delay_max"], raw_delay))
            if self._choreography:
                # Don't sleep past the current step boundary
                _, step_duration = self._choreography[self._choreo_index]
                remaining = step_duration - (
                    time.monotonic() - self._choreo_step_started
                )
                wait = min(wait, max(1.0, remaining))
            time.sleep(wait)

    def _generate_order(self) -> dict:
        sku = random.choice(list(PRODUCT_CATALOG.keys()))
        product = PRODUCT_CATALOG[sku]
        quantity = (
            random.randint(1, 10) if random.random() > 0.01 else random.randint(20, 50)
        )
        customer = random.choice(CUSTOMER_DETAILS)
        return {
            "customer_name": customer["name"],
            "product_name": product["name"],
            "brand": product["brand"],
            "category": product["category"],
            "quantity": quantity,
            "price": product["price"],
            "sku": sku,
            "address": customer["address"],
        }
