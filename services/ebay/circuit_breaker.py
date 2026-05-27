"""Circuit breaker for a polite, flaky vote source — lives off the hot path.

`CircuitBreaker.wrap(fetch_vote)` returns a drop-in fetch_vote of the same
shape. After `threshold` consecutive timeouts the breaker trips OPEN: further
calls short-circuit to None for `cooldown` seconds instead of making the whole
fan-out wait on a source we already know isn't answering. Once the cooldown
elapses the breaker goes half-open and lets a single probe through; a clean
return closes it, another timeout re-arms it.

It trips on asyncio.CancelledError only — that is the fan-out deadline firing,
i.e. the "knock that wasn't answered". A graceful None (no comps found) leaves
the breaker closed: the source answered, it just had no data. (Vinted swallows
its own HTTP errors into None upstream, so the timeout is the only hard-failure
signal that reaches this layer.)

Generic — knows nothing about Vinted or eBay; mirrors scout_diag.instrument().
One breaker instance per source, held at module scope so state persists across
fan-outs — that cross-photo memory is the whole point.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, Optional

LOGGER = logging.getLogger(__name__)

VoteFetcher = Callable[[str, str, int], Awaitable[Optional[Dict]]]


class CircuitBreaker:
    def __init__(self, name: str = "?", threshold: int = 3, cooldown: float = 60.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """True while tripped and still inside the cooldown; False once the
        cooldown elapses (half-open — a probe is allowed through)."""
        if self.opened_at is None:
            return False
        return (time.monotonic() - self.opened_at) < self.cooldown

    def _record_success(self) -> None:
        if self.opened_at is not None:
            LOGGER.info("circuit CLOSED for %s", self.name)
        self.failures = 0
        self.opened_at = None

    def _record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            was_closed = self.opened_at is None
            self.opened_at = time.monotonic()      # (re)arm the cooldown
            if was_closed:
                LOGGER.warning(
                    "circuit OPEN for %s after %d timeouts; skipping for %.0fs",
                    self.name, self.failures, self.cooldown,
                )

    def wrap(self, fetch_vote: VoteFetcher) -> VoteFetcher:
        async def voter(query: str, condition: str, index: int = 0) -> Optional[Dict]:
            if self.is_open:
                return None                         # don't wait on a dead source
            try:
                result = await fetch_vote(query, condition, index)
            except asyncio.CancelledError:
                self._record_failure()
                raise                               # never swallow cancellation
            self._record_success()
            return result

        return voter
