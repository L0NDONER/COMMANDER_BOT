#!/usr/bin/env python3
"""
velocity_lab — does a latency "time tax" carry any decision signal? Real data.

The operator under test (as proposed, verbatim intent):

    profit      = item_data['median'] - item_data['cost']
    latency_tax = node_latency * 0.05
    score       = profit - latency_tax

The premise behind a latency tax is a distributed acquisition race: many nodes
with different latencies, the slow one loses the item before it can buy. That
premise does not hold here (see [[project_velocity_filter]]):

  * One node. The live system is a single container (commander-leader). There is
    no fleet, so `node_latency` is not a per-candidate property — it's just the
    eBay/Vinted API round-trip, roughly constant across query variants.
  * The product is shelf-side triage: the operator is physically holding the
    item in the shop. Nothing sells "before you get there".
  * Units are unanchored. profit is GBP (£10-40 on these); latency*0.05 is
    GBP-per-what? ms → tax swamps profit; seconds → tax vanishes.

So the only way this term can DO anything in the real engine is by re-ranking
the consensus fan-out: each query variant returns a median price (the comp), the
engine picks a winner, and the tax could pull the winner toward a faster variant.
cost is the same physical item across variants, so profit-ranking == median-
ranking; the tax is the entire delta. That makes the test crisp:

    Does `median - latency*K` ever pick a DIFFERENT variant than `median` alone,
    and is that different variant BETTER (higher comp coverage — the one signal
    [[project_flat_landscape]] showed is real)?

We grab real eBay coverage+price+latency for Nike Air Jordan 1 query variants,
real Vinted resale for context, then sweep K and watch the winner.

Kill criterion: "SIGNAL" only if, at some defensible K, the tax flips the winner
to a variant with >= the profit-winner's coverage. If flips only ever happen at
an absurd K (tax >> profit) or always degrade coverage, the term is noise → do
NOT add it to the engine.

Standalone. Reuses fossil_lab's eBay token/search path; does NOT import the live
consensus engine. Makes real network calls (no disk cache — latency is the point).

Run:
    python3 scripts/velocity_lab.py
    python3 scripts/velocity_lab.py --samples 5 --ks 0 0.05 0.5 5 50
"""

import argparse
import statistics as stats
import time
from dataclasses import dataclass
from typing import List, Optional

from fossil_lab import _ebay_token, _title_matches, EBAY_CONDITION_IDS, EBAY_LIMIT

BASE = "nike air jordan 1"
# realistic consensus-style variants for one photo of a Jordan 1
VARIANTS = [
    "nike air jordan 1",
    "nike air jordan 1 high",
    "nike air jordan 1 low",
    "nike air jordan 1 mid",
    "nike air jordan 1 og",
    "nike air jordan 1 retro",
    "nike air jordan 1 mens used",
    "nike air jordan 1 chicago",
]
LATENCY_SAMPLES = 3
KS = [0.0, 0.05, 0.5, 5.0, 50.0]   # the 0.05 in the middle is the proposed coeff


@dataclass
class VariantResult:
    query: str
    coverage: int
    median_price: Optional[float]
    latency_ms: float          # median over samples


def _fetch_once(token: str, query: str):
    """One real eBay Browse call. Returns (latency_ms, prices, coverage)."""
    import requests
    t0 = time.perf_counter()
    res = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"q": query, "limit": EBAY_LIMIT,
                "filter": f"conditionIds:{{{EBAY_CONDITION_IDS}}},price:[5..500],priceCurrency:GBP"},
        timeout=30,
    )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    res.raise_for_status()
    items = res.json().get("itemSummaries", [])
    prices = []
    for i in items:
        if "price" not in i:
            continue
        if not _title_matches(i.get("title", ""), query):
            continue
        if (i.get("itemLocation", {}).get("country", "GB") or "GB") != "GB":
            continue
        try:
            prices.append(float(i["price"]["value"]))
        except (KeyError, ValueError, TypeError):
            continue
    return latency_ms, prices, len(prices)


def measure(token: str, query: str, samples: int) -> VariantResult:
    lats, last_prices, last_cov = [], [], 0
    for _ in range(samples):
        lat, prices, cov = _fetch_once(token, query)
        lats.append(lat)
        last_prices, last_cov = prices, cov
    median_price = stats.median(last_prices) if last_prices else None
    return VariantResult(query, last_cov, median_price, stats.median(lats))


def velocity_score(median_price: Optional[float], cost: float,
                   latency_ms: float, k: float) -> float:
    if median_price is None:
        return float("-inf")
    return (median_price - cost) - latency_ms * k


def try_vinted_median(query: str) -> Optional[float]:
    """Real Vinted resale median, for context. Needs the Pi SOCKS proxy; if it
    isn't reachable locally we just skip it (eBay carries the experiment)."""
    try:
        import asyncio
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from services.ebay.vinted_fetcher import search_vinted
        prices = asyncio.run(search_vinted(query))
        return stats.median(prices) if prices else None
    except Exception as e:
        print(f"  (Vinted skipped: {type(e).__name__}: {e})")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=LATENCY_SAMPLES)
    ap.add_argument("--ks", type=float, nargs="+", default=KS)
    args = ap.parse_args()

    print(f"\nvelocity_lab | real eBay {BASE!r} variants | latency samples={args.samples}")
    print("operator under test: score = (median - cost) - latency_ms*K\n")

    token = _ebay_token()
    results: List[VariantResult] = []
    for q in VARIANTS:
        r = measure(token, q, args.samples)
        results.append(r)
        mp = f"£{r.median_price:.0f}" if r.median_price is not None else "  -- "
        print(f"  {q:<32} cov={r.coverage:>2}  median={mp:>6}  latency={r.latency_ms:>6.0f}ms")

    # context: Vinted resale for the base query
    print("\nVinted resale (context):")
    v = try_vinted_median(BASE)
    if v is not None:
        print(f"  Vinted median for {BASE!r}: £{v:.0f}")

    # cost is the same physical pair across all variants → use a plausible buy cost.
    # any constant works: it shifts profit uniformly and cannot change the ranking.
    priced = [r for r in results if r.median_price is not None]
    cost = stats.median([r.median_price for r in priced]) * 0.5 if priced else 0.0
    print(f"\nassumed buy cost (constant across variants): £{cost:.0f}")

    # latency stats — is it even variant-dependent, or just constant API jitter?
    lat_vals = [r.latency_ms for r in priced]
    print(f"latency across variants: min={min(lat_vals):.0f}  max={max(lat_vals):.0f}  "
          f"spread={max(lat_vals)-min(lat_vals):.0f}ms  "
          f"cv={stats.pstdev(lat_vals)/stats.mean(lat_vals):.2f}")

    profit_winner = max(priced, key=lambda r: r.median_price)
    print(f"\nprofit-only winner: {profit_winner.query!r} "
          f"(median £{profit_winner.median_price:.0f}, cov {profit_winner.coverage}, "
          f"{profit_winner.latency_ms:.0f}ms)\n")

    print(f"{'K':>7} | {'winner':<32} | {'median':>7} | {'cov':>3} | {'flipped?':>8} | better cov?")
    print("-" * 86)
    for k in args.ks:
        win = max(priced, key=lambda r: velocity_score(r.median_price, cost, r.latency_ms, k))
        flipped = win.query != profit_winner.query
        better = ("same" if not flipped
                  else ("yes" if win.coverage >= profit_winner.coverage else "WORSE"))
        print(f"{k:>7.2f} | {win.query:<32} | £{win.median_price:>5.0f} | {win.coverage:>3} | "
              f"{'YES' if flipped else 'no':>8} | {better}")

    print("\nRead: a flip at K=0.05 (the proposed coeff) toward >= coverage would be")
    print("SIGNAL. A flip only at huge K, or flips that drop coverage, mean the tax")
    print("is just subtracting noise from a price the engine already ranks correctly.")


if __name__ == "__main__":
    main()
