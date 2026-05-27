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

    # Within-photo fixed-effects regression of log(median) on latency.
    # Demeaning per photo removes the £5-book vs £200-hifi level AND scale, so
    # we isolate pure within-fan-out covariation and use every vote, not just
    # the two extremes. log() tames the right-skew of price ratios.
    dl_all: List[float] = []      # within-photo demeaned latency
    dy_all: List[float] = []      # within-photo demeaned log(median)
    spans: List[float] = []
    photos = 0
    for g in groups.values():
        pairs = [(r["latency_ms"], r["median"]) for r in g
                 if isinstance(r.get("median"), (int, float)) and r["median"] > 0]
        lats = [p[0] for p in pairs]
        if len(pairs) < 2 or max(lats) == min(lats):
            continue
        lbar = _mean(lats)
        ys = [math.log(m) for _, m in pairs]
        ybar = _mean(ys)
        for (L, _), y in zip(pairs, ys):
            dl_all.append(L - lbar)
            dy_all.append(y - ybar)
        spans.append(max(lats) - min(lats))
        photos += 1

    n = len(dl_all)
    print(f"records={len(recs)}  live(non-cached)={len(live)}  cached={cached}  "
          f"photos used={photos}  pooled votes={n}")
    if photos < 8:
        print("not enough multi-variant fan-outs yet — let more photos flow, "
              "then re-run. (need >= ~8 to say anything.)")
        return

    sxx = sum(x * x for x in dl_all)
    sxy = sum(x * y for x, y in zip(dl_all, dy_all))
    if sxx == 0:
        print("no within-photo latency spread — cannot regress.")
        return
    beta = sxy / sxx                                   # d(log price) per ms
    sse = sum((y - beta * x) ** 2 for x, y in zip(dl_all, dy_all))
    df = n - photos - 1                                # photos means + 1 slope
    se = math.sqrt((sse / df) / sxx) if df > 0 else float("inf")
    t = beta / se if se not in (0.0, float("inf")) else 0.0

    per100 = math.exp(beta * 100) - 1                  # % price per +100ms
    gap = math.exp(beta * _mean(spans)) - 1            # % across mean per-photo span
    print(f"slope: {per100:+.1%} price per +100ms latency  (t {t:+.1f}, df {df})")
    print(f"implied slow-vs-fast gap over mean per-photo latency span: {gap:+.1%}")
    if abs(t) < 2:
        print("VERDICT: flat — no within-photo link between latency and price. The "
              "shared timeout is not biasing the verdict; the minimal engine is validated.")
    else:
        direction = "higher" if beta > 0 else "lower"
        print(f"VERDICT: biased — slower variants price systematically {direction} "
              f"(within photo, t {t:+.1f}). The timeout skews the median-of-medians; "
              f"consider a more generous CONSENSUS_TIMEOUT_SECONDS or down-weighting "
              f"late variants.")


if __name__ == "__main__":
    analyse(_parse(sys.stdin))
