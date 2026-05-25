#!/usr/bin/env python3
"""
sniper_lab — stateless best-of-K on the flat plain. Real eBay signal.

The flat-landscape conclusion ([[project_flat_landscape]]) made measurable. No
evolution, no memory, no fossils. Each "shot" samples K query suffixes, fetches
real eBay comp coverage once per suffix, and takes the best. That's it — it's
the live consensus fan-out with the ALife scaffolding stripped off.

Full line of sight. K is fixed at 5 for the headline (population question is
settled), but the whole K curve is reported so 5 is shown, not assumed:

    single (K=1)  — do-nothing baseline: one random suffix.
    best-of-K     — what the sniper buys.
    oracle        — best suffix in the entire universe (the ceiling).

What we read off it:
    lift     = best-of-K mean coverage − single mean coverage  (pit-avoidance)
    %oracle  = how much of the ceiling best-of-K captures
    pit-rate = fraction of shots that land on a 0-coverage dud

Scoring = coverage only (the validated signal). Price is carried for diagnostics
but NOT scored: in resale a high comp price is upside, so the stub's
`-price*0.01` sign was backwards — left out until a real price objective exists.

Reuses fossil_lab's eBay token/search path and its disk cache, so if the cache
is warm this makes zero network calls. Standalone — does not touch the live
engine.

Run:
    python3 scripts/sniper_lab.py            # real eBay coverage (cached)
    python3 scripts/sniper_lab.py --runs 5000 --ks 1 2 3 5 8
"""

import argparse
import random
import statistics as stats
from dataclasses import dataclass
from typing import Dict, List

from fossil_lab import BASE_QUERIES, _prefetch_coverage, _suffix_universe

NUM_CANDIDATES = 5          # the headline K
RUNS           = 3000       # independent shots per (base, K)
RANDOM_SEED    = 42
KS             = [1, 2, 3, 5, 8]


@dataclass
class Candidate:
    literal: str
    coverage: int
    score: float


def best_of_k(suffixes: List[str], cov: Dict[str, int], base: str,
              k: int, rng: random.Random) -> Candidate:
    """One sniper shot: K random suffixes, score=coverage, take the best."""
    picks = [rng.choice(suffixes) for _ in range(k)]
    cands = [Candidate(s, cov.get(f"{base}||{s}", 0), float(cov.get(f"{base}||{s}", 0)))
             for s in picks]
    return max(cands, key=lambda c: c.score)


def analyse_base(base: str, suffixes: List[str], cov: Dict[str, int],
                 ks: List[int], runs: int, seed: int) -> Dict[int, dict]:
    oracle = max(cov.get(f"{base}||{s}", 0) for s in suffixes)
    out = {}
    for k in ks:
        rng = random.Random(seed)
        covs = [best_of_k(suffixes, cov, base, k, rng).coverage for _ in range(runs)]
        out[k] = {
            "mean": stats.mean(covs),
            "pit_rate": sum(1 for c in covs if c == 0) / runs,
            "pct_oracle": (stats.mean(covs) / oracle) if oracle else 0.0,
            "oracle": oracle,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--seed", type=int, default=RANDOM_SEED)
    ap.add_argument("--ks", type=int, nargs="+", default=KS)
    args = ap.parse_args()

    suffixes = _suffix_universe()
    cov = _prefetch_coverage(BASE_QUERIES, suffixes)   # disk-cached; warm = no calls

    print(f"\nsniper_lab | best-of-K on real eBay coverage | runs={args.runs}/base")
    print(f"universe={len(suffixes)} suffixes/base | headline K={NUM_CANDIDATES}\n")

    # accumulate across bases for an aggregate line
    agg = {k: {"mean": [], "pit_rate": [], "pct_oracle": []} for k in args.ks}

    for base in BASE_QUERIES:
        res = analyse_base(base, suffixes, cov, args.ks, args.runs, args.seed)
        oracle = res[args.ks[0]]["oracle"]
        print(f"{base!r}  (oracle coverage={oracle})")
        print(f"   {'K':>2} | {'mean cov':>8} | {'lift vs K=1':>11} | {'%oracle':>7} | {'pit%':>5}")
        print("   " + "-" * 48)
        single = res[args.ks[0]]["mean"]
        for k in args.ks:
            r = res[k]
            star = " *" if k == NUM_CANDIDATES else "  "
            print(f"   {k:>2}{star}| {r['mean']:>8.2f} | {r['mean']-single:>+11.2f} | "
                  f"{r['pct_oracle']*100:>6.0f}% | {r['pit_rate']*100:>4.1f}%")
            agg[k]["mean"].append(r["mean"])
            agg[k]["pit_rate"].append(r["pit_rate"])
            agg[k]["pct_oracle"].append(r["pct_oracle"])
        print()

    print("AGGREGATE (mean across bases)")
    print(f"   {'K':>2} | {'mean cov':>8} | {'lift vs K=1':>11} | {'%oracle':>7} | {'pit%':>5}")
    print("   " + "-" * 48)
    single = stats.mean(agg[args.ks[0]]["mean"])
    for k in args.ks:
        m = stats.mean(agg[k]["mean"])
        star = " *" if k == NUM_CANDIDATES else "  "
        print(f"   {k:>2}{star}| {m:>8.2f} | {m-single:>+11.2f} | "
              f"{stats.mean(agg[k]['pct_oracle'])*100:>6.0f}% | {stats.mean(agg[k]['pit_rate'])*100:>4.1f}%")
    print()
    print("Read: lift = pit-avoidance (no hills to climb, only holes to dodge).")
    print("Diminishing returns in the K column show whether K=5 is over/under-shot.")


if __name__ == "__main__":
    main()
