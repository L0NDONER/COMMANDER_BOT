"""Vinted session-warmup primitive — two-phase client architecture.

PHASE 1 (identity): a single GET to the Vinted homepage establishes the
session cookies the catalog API expects on subsequent calls. No variant
runs until identity is established.

PHASE 2 (steady-state, machine-time): variants fan out through the warmed
session, paced by a configurable strategy (default: random jitter) and
capped by a concurrency limit. Each request carries a Referer back to the
homepage so the session looks coherent.

The "open the door, then send friends through" pattern — the script keeps
the demo at the bottom (`main()`), but the building blocks
(`establish_identity`, `run_variants`, `Pacer`) are reusable from other
workflows.

Run as a demo:  python3 scripts/vinted_session_warmup.py
"""
import asyncio
import random
import time
from typing import Callable, Iterable

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HOME = "https://www.vinted.co.uk/"
DEFAULT_SEARCH_TERMS = ["nike", "zara", "uniqlo", "carhartt", "north face"]
DEFAULT_CONCURRENCY = 3
IDENTITY_SETTLE_SECONDS = 2.0  # gap between Phase 1 and Phase 2


class Pacer:
    """Pacing-strategy interface. `wait()` is awaited before each variant
    request; `observe()` (optional, default no-op) is called with each
    response's status/error so adaptive policies can update internal state.
    Duck-typed — any object with these two methods works."""
    async def wait(self) -> None:
        raise NotImplementedError

    def observe(self, status: int | None, error: str | None) -> None:
        pass


class JitterPacer(Pacer):
    """Uniform random delay in [lo, hi] seconds. Defensible default — no
    burst detection, just polite irregularity. Ignores observations."""
    def __init__(self, lo: float = 1.0, hi: float = 3.0):
        self.lo = lo
        self.hi = hi

    async def wait(self) -> None:
        await asyncio.sleep(random.uniform(self.lo, self.hi))


class AdaptivePacer(Pacer):
    """AIMD-style adaptive backoff. Starts at `base_delay`. On each observed
    failure (timeout/client error/throttling status) the delay is multiplied
    by `backoff_mult` (capped at `max_delay`). After `recovery_after`
    consecutive clean successes the delay is divided by `recovery_div`
    (floored at `min_delay`). A small ±15% jitter is layered on top of the
    current delay so even the adaptive cadence stays irregular.

    Aggressive backoff + gentle recovery is the standard TCP-derived shape.
    Tune `backoff_mult` ↑ if the server is sensitive, `recovery_div` ↑ if
    you want it to forget heat faster."""
    BACKOFF_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

    def __init__(self, base_delay: float = 1.5, min_delay: float = 0.5,
                 max_delay: float = 30.0, backoff_mult: float = 2.0,
                 recovery_div: float = 1.2, recovery_after: int = 3,
                 verbose: bool = True):
        self.current_delay = base_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.backoff_mult = backoff_mult
        self.recovery_div = recovery_div
        self.recovery_after = recovery_after
        self.verbose = verbose
        self._consecutive_ok = 0

    async def wait(self) -> None:
        await asyncio.sleep(self.current_delay * random.uniform(0.85, 1.15))

    def observe(self, status: int | None, error: str | None) -> None:
        prev = self.current_delay
        bad = error is not None or (status is not None
                                    and status in self.BACKOFF_STATUSES)
        if bad:
            self._consecutive_ok = 0
            self.current_delay = min(self.current_delay * self.backoff_mult,
                                     self.max_delay)
        elif status is not None and 200 <= status < 400:
            self._consecutive_ok += 1
            if self._consecutive_ok >= self.recovery_after:
                self.current_delay = max(self.current_delay / self.recovery_div,
                                         self.min_delay)
                self._consecutive_ok = 0
        if self.verbose and self.current_delay != prev:
            reason = error or (f"HTTP {status}" if status else "?")
            direction = "↑ backoff" if self.current_delay > prev else "↓ recover"
            print(f"[pacer] {direction}: {prev:.2f}s → "
                  f"{self.current_delay:.2f}s ({reason})")


async def establish_identity(session: aiohttp.ClientSession) -> bool:
    """PHASE 1. One GET to the homepage so the cookie jar picks up whatever
    the server sets for legitimate visitors. Returns True if any cookies
    were captured (signal of a successful warmup)."""
    t0 = time.perf_counter()
    async with session.get(HOME) as r:
        await r.read()
        dt = time.perf_counter() - t0
        print(f"[identity] {r.status} in {dt:.2f}s")
    cookies = sorted(c.key for c in session.cookie_jar)
    print(f"[identity] cookies: {cookies or '(none)'}")
    return bool(cookies)


def _catalog_url(term: str) -> str:
    return f"https://www.vinted.co.uk/catalog?search_text={term}"


# .invalid is a reserved TLD that DNS will refuse to resolve, so a request
# to this host raises aiohttp.ClientError quickly — exactly the failure
# signal an adaptive pacer is meant to react to. Used only when the demo
# is explicitly asked to inject failures via --demo-failures.
_FAILURE_LABEL_PREFIX = "__fail__"
_FAILURE_URL = "https://this-host-does-not-resolve.invalid/"


def _demo_build_url(term: str) -> str:
    if term.startswith(_FAILURE_LABEL_PREFIX):
        return _FAILURE_URL
    return _catalog_url(term)


async def _fetch_one(session, sem, pacer, label, url, referer):
    async with sem:
        await pacer.wait()
        t0 = time.perf_counter()
        try:
            async with session.get(url, headers={"Referer": referer},
                                   timeout=15) as r:
                body = await r.read()
                result = {"label": label, "status": r.status,
                          "size": len(body),
                          "elapsed": time.perf_counter() - t0, "error": None}
        except asyncio.TimeoutError:
            result = {"label": label, "status": None, "size": 0,
                      "elapsed": time.perf_counter() - t0,
                      "error": "TimeoutError"}
        except aiohttp.ClientError as e:
            # str(e) gives a one-line human message ("Cannot connect to host
            # … [Name or service not known]"); repr would dump the whole
            # ConnectionKey. Prefix with the class name for triage.
            result = {"label": label, "status": None, "size": 0,
                      "elapsed": time.perf_counter() - t0,
                      "error": f"{type(e).__name__}: {e}"}
    pacer.observe(result["status"], result["error"])
    return result


async def run_variants(
    session: aiohttp.ClientSession,
    variants: Iterable[str],
    pacer: Pacer,
    concurrency: int = DEFAULT_CONCURRENCY,
    build_url: Callable[[str], str] = _catalog_url,
    referer: str = HOME,
) -> list[dict]:
    """PHASE 2. Each variant becomes one request, paced by `pacer.wait()`
    and capped by an asyncio.Semaphore. Returns per-variant dicts:
    {label, status, size, elapsed, error}.

    The session must already be primed by `establish_identity()` — variants
    inherit its cookies, UA, and (by default) a Referer back to the homepage
    so the session reads as coherent."""
    sem = asyncio.Semaphore(concurrency)
    variants = list(variants)
    return await asyncio.gather(*(
        _fetch_one(session, sem, pacer, v, build_url(v), referer)
        for v in variants
    ))


async def main(variants: Iterable[str] | None = None,
               concurrency: int = DEFAULT_CONCURRENCY,
               pacer: Pacer | None = None,
               demo_failures: int = 0) -> None:
    variants = list(variants or DEFAULT_SEARCH_TERMS)
    pacer = pacer or JitterPacer()

    # Prepend N bogus variants that will fail DNS resolution. Placing them
    # first means they enter the semaphore early, drive the pacer into
    # backoff, and the real variants then run at the elevated cadence —
    # so the visual story is "failures → backoff → recovery on success".
    if demo_failures > 0:
        bogus = [f"{_FAILURE_LABEL_PREFIX}{i}" for i in range(demo_failures)]
        variants = bogus + variants
    build_url = _demo_build_url if demo_failures > 0 else _catalog_url

    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
        print("--- phase 1: establishing identity ---")
        if not await establish_identity(session):
            print("[!] no cookies baked — server may be blocking from this IP")
        await asyncio.sleep(IDENTITY_SETTLE_SECONDS)

        print(f"\n--- phase 2: {len(variants)} variants "
              f"at concurrency {concurrency}"
              f"{f' (with {demo_failures} injected failure(s))' if demo_failures else ''} ---")
        results = await run_variants(session, variants, pacer, concurrency,
                                     build_url=build_url)

    print("\n--- results ---")
    ok = 0
    for r in results:
        if r["error"]:
            print(f"  {r['label']:<12} FAIL {r['elapsed']:.2f}s  {r['error']}")
        else:
            tag = "OK " if r["status"] == 200 else "BAD"
            print(f"  {r['label']:<12} {tag} {r['status']} "
                  f"{r['size']/1024:>6.1f}KB {r['elapsed']:.2f}s")
            if r["status"] == 200:
                ok += 1
    print(f"\n{ok}/{len(results)} succeeded")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pacer", choices=["jitter", "adaptive"], default="jitter",
                    help="pacing strategy (default: jitter)")
    ap.add_argument("--demo-failures", type=int, default=0, metavar="N",
                    help="inject N variants pointing at an unresolvable host "
                         "so the adaptive pacer's backoff is visible")
    args = ap.parse_args()
    chosen = AdaptivePacer() if args.pacer == "adaptive" else JitterPacer()
    asyncio.run(main(pacer=chosen, demo_failures=args.demo_failures))
