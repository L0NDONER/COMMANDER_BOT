"""Survivorship-bias probe for the consensus fan-out — lives off the hot path.

`instrument(fetch_vote)` returns a drop-in fetch_vote that times each variant
and logs one SCOUT_DIAG line per COMPLETED vote. Variants cancelled by the
shared timeout never reach the log line — the ones that do log are exactly the
survivors we want to study. One `event` id per instrument() call groups a single
fan-out, so the analyser can ask, *within one photo*, whether slower variants
price differently (i.e. whether the timeout biases the median-of-medians).

Generic timing wrapper — knows nothing about eBay; the engine stays pure.

Analyse a log capture:
    docker compose logs commander-leader | python3 -m services.ebay.scout_diag
    # or:  python3 -m services.ebay.scout_diag < some_logfile
"""
import json
import logging
import math
import sys
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

VoteFetcher = Callable[[str, str, int], Awaitable[Optional[Dict]]]


def instrument(fetch_vote: VoteFetcher) -> VoteFetcher:
    """Wrap a fetch_vote so each completed call emits a SCOUT_DIAG log line."""
    event = f"{int(time.time() * 1000):x}"
    src = getattr(fetch_vote, "__name__", "?")

    async def voter(query: str, condition: str, index: int = 0) -> Optional[Dict]:
        t0 = time.perf_counter()
        r = await fetch_vote(query, condition, index)
        median = r.get("median") if isinstance(r, dict) else None
        LOGGER.info("SCOUT_DIAG %s", json.dumps({
            "event": event, "src": src, "i": index, "q": query, "cond": condition,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "median": median,
        }))
        return r

    return voter


# ── analyser: read SCOUT_DIAG lines, test latency<->price within each photo ───
_CACHE_MS = 2.0   # below this, a "fetch" was a cache hit (no eBay latency signal)


def _parse(stream) -> List[dict]:
    recs = []
    for line in stream:
        k = line.find("SCOUT_DIAG ")
        if k == -1:
            continue
        try:
            recs.append(json.loads(line[k + len("SCOUT_DIAG "):]))
        except ValueError:
            pass
    return recs


def _mean(xs): return sum(xs) / len(xs)


def analyse(recs: List[dict]) -> None:
    live = [r for r in recs if r.get("median") is not None
            and r.get("latency_ms", 0) >= _CACHE_MS]
    cached = sum(1 for r in recs if 0 <= r.get("latency_ms", 0) < _CACHE_MS)

    # group live votes by (event, src) = one fan-out
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for r in live:
        groups.setdefault((r.get("event"), r.get("src")), []).append(r)

    gaps = []   # within-photo (slowest median - fastest median) / mean median
    for g in groups.values():
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda r: r["latency_ms"])
        lo, hi = g[0]["median"], g[-1]["median"]
        m = _mean([r["median"] for r in g])
        if m:
            gaps.append((hi - lo) / m)

    print(f"records={len(recs)}  live(non-cached)={len(live)}  cached={cached}  "
          f"usable fan-outs={len(gaps)}")
    if len(gaps) < 8:
        print("not enough multi-variant fan-outs yet — let more photos flow, "
              "then re-run. (need >= ~8 to say anything.)")
        return

    mean = _mean(gaps)
    sd = math.sqrt(_mean([(x - mean) ** 2 for x in gaps]))
    if sd == 0:
        t = 0.0 if abs(mean) < 1e-9 else float("inf")
    else:
        t = mean / (sd / math.sqrt(len(gaps)))
    print(f"slowest-vs-fastest variant price gap: mean {mean:+.1%}  (sd {sd:.1%}, t {t:+.1f})")
    if abs(t) < 2:
        print("VERDICT: flat — slow variants do NOT price differently. The shared "
              "timeout is not biasing the verdict; the minimal engine is validated.")
    else:
        direction = "higher" if mean > 0 else "lower"
        print(f"VERDICT: biased — slower variants price systematically {direction}. "
              f"The timeout amputates them, skewing the median-of-medians. Consider a "
              f"more generous CONSENSUS_TIMEOUT_SECONDS or down-weighting late variants.")


if __name__ == "__main__":
    analyse(_parse(sys.stdin))
