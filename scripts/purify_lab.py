#!/usr/bin/env python3
"""
purify_lab — does suffix "purification" beat plain best-of-K? (sandbox)

Tests the user's purification idea: track each modifier's pit-rate (coverage==0)
across photos, permanently ban any that pit >= Z_MAX of the time after N_MIN
samples. Question: does a purified universe beat a plain (full-universe) best-of-K
on mean coverage AND pit-rate — WITHOUT banning a category-vital suffix?

Mirrors the LIVE action space (unlike sniper_lab's pairs):
  - variants = base anchor (always present) + K single modifiers
  - K = 3  (live _MAX_VARIANTS=4 = anchor + 3)
  - modifiers = the live generic vocab (the $-tokens expand to item keywords
    that are usually already in the base, so the purifiable space is the
    generics).

Category-spanning bases so gender pits actually surface. Two workloads:
  - realistic: clothing-heavy (matches the live logs)
  - balanced:  uniform across categories (adversarial — exposes global bans)

Kill-criterion: LIFT only if purified beats plain on coverage AND pit-rate by a
real margin, and bans no category-vital suffix. Prior: it won't.

Reuses fossil_lab's eBay path + disk cache. Standalone; touches nothing live.
"""

import random
import statistics as stats
from collections import defaultdict
from typing import Dict, List, Tuple

from fossil_lab import _prefetch_coverage

# base query -> category
BASES = {
    "hackett rugby shirt":      "mens_clothing",
    "barbour wax jacket":       "mens_clothing",
    "joules floral dress":      "womens_clothing",
    "radley leather handbag":   "womens_clothing",
    "lord of the rings hardback": "book",
    "le creuset casserole":     "homeware",
    "nintendo switch game":     "electronics",
    "lego star wars set":       "toy",
}

MODIFIERS = ["vintage", "rare", "boxed", "genuine", "bundle",
             "job lot", "mens", "womens", "new", "used"]
CATEGORY_VITAL = {"mens", "womens"}     # banning these = recall disaster on clothing

K = 3
RUNS = 4000
SEED = 42

# purification params (verbatim from the proposed snippet)
N_MIN = 20
Z_MAX = 0.50
PURGE_EVERY = 500


def shot_coverage(base: str, picks: List[str], cov: Dict[str, int]) -> Tuple[int, List[Tuple[str, int]]]:
    """best-of-K: max coverage over [anchor] + picks. Returns (best, per-pick covs)."""
    anchor = cov.get(f"{base}||", 0)
    pick_covs = [(m, cov.get(f"{base}||{m}", 0)) for m in picks]
    best = max([anchor] + [c for _, c in pick_covs])
    return best, pick_covs


def make_workload(rng: random.Random, mix: str) -> List[str]:
    bases = list(BASES)
    if mix == "realistic":           # clothing 4x weight
        weights = [4 if "clothing" in BASES[b] else 1 for b in bases]
    else:                            # balanced / uniform
        weights = [1] * len(bases)
    return rng.choices(bases, weights=weights, k=RUNS)


def run_plain(workload: List[str], cov: Dict[str, int], seed: int) -> dict:
    rng = random.Random(seed)
    best_covs = []
    for base in workload:
        picks = rng.sample(MODIFIERS, K)
        best, _ = shot_coverage(base, picks, cov)
        best_covs.append(best)
    return {"mean": stats.mean(best_covs),
            "pit": sum(b == 0 for b in best_covs) / len(best_covs),
            "banned": []}


def run_purified(workload: List[str], cov: Dict[str, int], seed: int) -> dict:
    rng = random.Random(seed)
    suffstats = {s: {"total": 0, "zeros": 0} for s in MODIFIERS}
    clean = set(MODIFIERS)
    banned_order = []

    def purify():
        for s in list(clean):
            st = suffstats[s]
            if st["total"] < N_MIN:
                continue
            if st["zeros"] / st["total"] >= Z_MAX:
                clean.discard(s)
                banned_order.append(s)

    best_covs = []
    for i, base in enumerate(workload, 1):
        pool = list(clean)
        picks = rng.sample(pool, min(K, len(pool)))
        best, pick_covs = shot_coverage(base, picks, cov)
        best_covs.append(best)
        for m, c in pick_covs:
            suffstats[m]["total"] += 1
            if c == 0:
                suffstats[m]["zeros"] += 1
        if i % PURGE_EVERY == 0:
            purify()
    return {"mean": stats.mean(best_covs),
            "pit": sum(b == 0 for b in best_covs) / len(best_covs),
            "banned": banned_order}


def main() -> None:
    suffixes = [""] + MODIFIERS            # "" = bare anchor
    cov = _prefetch_coverage(list(BASES), suffixes)

    print(f"\npurify_lab | K={K} runs={RUNS} | N_MIN={N_MIN} Z_MAX={Z_MAX} PURGE_EVERY={PURGE_EVERY}")
    print(f"bases={len(BASES)} ({sorted(set(BASES.values()))})\n")

    # per-modifier pit-rate across the full base set (uniform) — the structure purify sees
    print("per-modifier pit-rate across all bases (uniform):")
    for m in MODIFIERS:
        zeros = sum(cov.get(f"{b}||{m}", 0) == 0 for b in BASES)
        rate = zeros / len(BASES)
        vital = "  [category-vital]" if m in CATEGORY_VITAL else ""
        flag = " <-- would ban" if rate >= Z_MAX else ""
        print(f"   {m:<10} {rate:.2f}{flag}{vital}")
    anchor_pit = sum(cov.get(f"{b}||", 0) == 0 for b in BASES) / len(BASES)
    print(f"\n   anchor (bare) pit-rate: {anchor_pit:.2f}  <- floors the shot pit-rate regardless of suffixes\n")

    for mix in ("realistic", "balanced"):
        wl = make_workload(random.Random(SEED), mix)
        plain = run_plain(wl, cov, SEED)
        pure = run_purified(wl, cov, SEED)
        print(f"[{mix}] workload (clothing share="
              f"{sum('clothing' in BASES[b] for b in wl)/len(wl):.0%})")
        print(f"   {'arm':<10} {'mean cov':>9} {'pit%':>7}   banned")
        print(f"   {'plain':<10} {plain['mean']:>9.2f} {plain['pit']*100:>6.1f}%   -")
        print(f"   {'purified':<10} {pure['mean']:>9.2f} {pure['pit']*100:>6.1f}%   {pure['banned'] or '-'}")
        d_cov = pure["mean"] - plain["mean"]
        d_pit = (pure["pit"] - plain["pit"]) * 100
        vital_banned = [b for b in pure["banned"] if b in CATEGORY_VITAL]
        win = d_cov > 0.1 and d_pit < -0.1 and not vital_banned
        print(f"   Δcov={d_cov:+.2f}  Δpit={d_pit:+.1f}pp  vital_banned={vital_banned or 'none'}"
              f"  -> {'WIN' if win else 'no win'}\n")

    print("Read: anchor-floored pit-rate + mostly-live single modifiers leave purify")
    print("nothing to gain; on balanced load it bans category-vital suffixes for no lift.")


if __name__ == "__main__":
    main()
