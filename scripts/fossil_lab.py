#!/usr/bin/env python3
"""
fossil_lab — contained A/B sandbox. NOT production, NOT the live engine.

One question only:

    Does a fossil-based respawn operator show measurable lift over pure
    random respawn, at the same population size N?

Mechanism under test: when a variant's accumulated energy crosses a threshold,
snapshot its literal into a bounded ring buffer ("fossils"). A fraction of
deaths respawn from a random fossil instead of a fresh random literal. That is
the only difference between the two arms. Everything else (N, steps, seeds,
scoring) is held identical, and the two arms are paired on the same seeds.

Two scoring backends
--------------------
--fake (default): score = hash(literal) noise. Zero deps, offline. Useful only
    as a plumbing check. Fossils trivially "win" structurally, so a flat result
    here is the *uninformative* outcome.

--real: score = real eBay comp coverage. A literal is a query SUFFIX; its
    fitness is the number of title-matched GB used-condition comps the Browse
    API returns for "{base} {suffix}" (price £5–500). This is what the live
    engine actually rewards — a suffix that produces a populated, on-topic
    result set. One base query == one "photo". The suffix universe (bare + 10
    modifiers + all pairs = 56) is prefetched ONCE per base and cached to disk,
    so the A/B itself makes zero network calls and is reproducible.

    Standalone: this does NOT import the live consensus engine. It reuses only
    the eBay app credentials and replicates the minimal token+search path.

Honest read of --real: coverage is deterministic per (base, suffix), so the
action space is finite and enumerable. Both arms therefore converge to "keep
the high-coverage suffixes"; fossils can only change transient dynamics, not the
endpoint. If --real shows no lift, that is the real answer to the question — and
it would match evo_lab's verdict that the ALife apparatus buys nothing a direct
best-of lookup can't.

Kill criterion: "LIFT" only if the paired mean-Δ 95% CI clears 0 AND fossil wins
≥60% of seeds. Otherwise the operator buys nothing → drop it.

Run:
    python3 scripts/fossil_lab.py                       # fake, offline
    python3 scripts/fossil_lab.py --real                # real eBay coverage
    python3 scripts/fossil_lab.py --real --metric best --runs 200
"""

import argparse
import json
import os
import random
import statistics as stats
from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Dict, List, Tuple


# -------------------------
# Config (defaults; overridable via argv)
# -------------------------

NS               = [3, 5, 8]
STEPS_PER_RUN    = 400
RUNS             = 100
RANDOM_SEED      = 42

FOSSIL_LIMIT     = 20
FOSSIL_THRESHOLD = 100.0
FOSSIL_RESPAWN_P = 0.15

# --- real backend ---
MODIFIERS = ["vintage", "rare", "boxed", "genuine", "bundle",
             "job lot", "mens", "womens", "new", "used"]
BASE_QUERIES = ["lego star wars set", "nintendo switch game", "north face jacket"]
CACHE_PATH = os.path.join(os.path.dirname(__file__), ".fossil_lab_cache.json")
EBAY_CONDITION_IDS = "3000|4000|5000"   # used, as in the live filter
EBAY_LIMIT = 15


# -------------------------
# A "world" = the action space + fitness for one run (bound to a seed).
# gen_literal(rng) -> str ; score(literal, rng) -> float
# -------------------------

@dataclass
class World:
    gen_literal: Callable[[random.Random], str]
    score: Callable[[str, random.Random], float]


# --- fake backend -------------------------------------------------

def _fake_literal(rng: random.Random) -> str:
    prefix = rng.choice(["$KW0", "$KW1", "$COND", "$LIT"])
    suffix = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(4))
    return f"{prefix}_{suffix}"


def _fake_score(literal: str, rng: random.Random) -> float:
    base = (hash(literal) % 1000) / 1000.0
    return max(0.0, min(1.0, base + rng.uniform(-0.2, 0.2))) * 6.0


def fake_world(seed: int) -> World:
    return World(gen_literal=_fake_literal, score=_fake_score)


# --- real backend (eBay comp coverage) ----------------------------

def _suffix_universe() -> List[str]:
    singles = [""] + MODIFIERS
    pairs = [f"{a} {b}" for a, b in combinations(MODIFIERS, 2)]
    return singles + pairs


def _ebay_token() -> str:
    import base64
    import sys
    import requests
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root for credentials.py
    from credentials import EBAY_APP_ID, EBAY_SECRET
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_SECRET}".encode()).decode()
    res = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def _title_matches(title: str, query: str, min_tokens: int = 2) -> bool:
    tl = title.lower()
    toks = query.lower().split()
    hits = sum(1 for t in toks if t in tl)
    return hits >= min(min_tokens, len(toks))


def _coverage(token: str, full_query: str) -> int:
    """Number of title-matched, GB, priced comps. Mirrors live analyse(), counts."""
    import requests
    res = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"q": full_query, "limit": EBAY_LIMIT,
                "filter": f"conditionIds:{{{EBAY_CONDITION_IDS}}},price:[5..500],priceCurrency:GBP"},
        timeout=30,
    )
    res.raise_for_status()
    items = res.json().get("itemSummaries", [])
    n = 0
    for i in items:
        if "price" not in i:
            continue
        if not _title_matches(i.get("title", ""), full_query):
            continue
        if (i.get("itemLocation", {}).get("country", "GB") or "GB") != "GB":
            continue
        n += 1
    return n


def _load_cache() -> Dict[str, int]:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _prefetch_coverage(bases: List[str], suffixes: List[str]) -> Dict[str, int]:
    """coverage[f'{base}||{suffix}'] = comp count. Disk-cached; only misses hit eBay."""
    cache = _load_cache()
    keys = [(b, s) for b in bases for s in suffixes]
    misses = [(b, s) for b, s in keys if f"{b}||{s}" not in cache]
    if misses:
        print(f"prefetch: {len(misses)} eBay calls ({len(keys) - len(misses)} cached)...")
        token = _ebay_token()
        for n, (b, s) in enumerate(misses, 1):
            q = b if not s else f"{b} {s}"
            try:
                cache[f"{b}||{s}"] = _coverage(token, q)
            except Exception as e:
                print(f"  !! {q!r}: {e}; treating as 0 coverage")
                cache[f"{b}||{s}"] = 0
            if n % 20 == 0:
                print(f"  {n}/{len(misses)}")
                with open(CACHE_PATH, "w") as f:
                    json.dump(cache, f)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    return cache


_REAL_STATE: Dict[str, object] = {}   # filled by build_real_backend()


def build_real_backend() -> None:
    suffixes = _suffix_universe()
    cov = _prefetch_coverage(BASE_QUERIES, suffixes)
    _REAL_STATE["suffixes"] = suffixes
    _REAL_STATE["cov"] = cov
    # diagnostic: show the real fitness landscape per base
    print("\nreal coverage landscape (top/bottom suffixes per base):")
    for b in BASE_QUERIES:
        ranked = sorted(suffixes, key=lambda s: cov.get(f"{b}||{s}", 0), reverse=True)
        top = ", ".join(f"{s or 'bare'}={cov[f'{b}||{s}']}" for s in ranked[:3])
        nonzero = sum(1 for s in suffixes if cov.get(f"{b}||{s}", 0) > 0)
        print(f"  {b!r}: top[{top}]  nonzero={nonzero}/{len(suffixes)}")
    print()


def real_world(seed: int) -> World:
    """One run == one 'photo' == one base query, cycled by seed."""
    base = BASE_QUERIES[seed % len(BASE_QUERIES)]
    suffixes: List[str] = _REAL_STATE["suffixes"]      # type: ignore
    cov: Dict[str, int] = _REAL_STATE["cov"]           # type: ignore

    def gen_literal(rng: random.Random) -> str:
        return rng.choice(suffixes)

    def score(literal: str, rng: random.Random) -> float:
        # real, deterministic coverage + tiny tie-break noise so equal-coverage
        # suffixes don't deadlock the loser selection.
        return cov.get(f"{base}||{literal}", 0) + rng.uniform(-0.05, 0.05)

    return World(gen_literal=gen_literal, score=score)


# -------------------------
# Core model
# -------------------------

@dataclass
class Variant:
    literal: str
    energy: float = 0.0


@dataclass
class Lab:
    rng: random.Random
    world: World
    fossils_enabled: bool
    n: int
    fossils: List[str] = field(default_factory=list)

    def random_respawn(self) -> Variant:
        return Variant(literal=self.world.gen_literal(self.rng))

    def fossil_respawn(self) -> Variant:
        if not self.fossils:
            return self.random_respawn()
        return Variant(literal=self.rng.choice(self.fossils))

    def maybe_leave_fossil(self, v: Variant) -> None:
        if v.energy >= FOSSIL_THRESHOLD:
            self.fossils.append(v.literal)
            if len(self.fossils) > FOSSIL_LIMIT:
                self.fossils.pop(0)

    def step(self, pool: List[Variant]) -> None:
        for v in pool:
            v.energy += self.world.score(v.literal, self.rng)
            if self.fossils_enabled:
                self.maybe_leave_fossil(v)
        idx = min(range(len(pool)), key=lambda i: pool[i].energy)
        if self.fossils_enabled and self.rng.random() < FOSSIL_RESPAWN_P:
            pool[idx] = self.fossil_respawn()
        else:
            pool[idx] = self.random_respawn()

    def run(self, steps: int) -> Dict[str, float]:
        pool = [self.random_respawn() for _ in range(self.n)]
        for _ in range(steps):
            self.step(pool)
        energies = [v.energy for v in pool]
        return {"mean": stats.mean(energies), "best": max(energies)}


def run_arm(seed: int, n: int, steps: int, world: World, fossils: bool) -> Dict[str, float]:
    # Each arm gets its own rng seeded identically. Enabling fossils changes how
    # much randomness is consumed, so streams diverge after the first fossil
    # respawn — arms are NOT lockstep. We control for seed luck by pairing per
    # seed over many seeds, not by assuming identical draws.
    lab = Lab(rng=random.Random(seed), world=world, fossils_enabled=fossils, n=n)
    return lab.run(steps)


# -------------------------
# Harness
# -------------------------

def paired_stats(deltas: List[float]) -> Dict[str, float]:
    m = stats.mean(deltas)
    stderr = (stats.stdev(deltas) / len(deltas) ** 0.5) if len(deltas) > 1 else 0.0
    return {"mean": m, "ci_half": 1.96 * stderr,
            "win_rate": sum(1 for d in deltas if d > 0) / len(deltas)}


def sweep(ns: List[int], steps: int, runs: int, base_seed: int,
          make_world: Callable[[int], World], metric: str, label: str) -> None:
    print(f"fossil_lab A/B [{label}] | metric={metric}  steps={steps}  runs={runs}")
    print(f"fossil: limit={FOSSIL_LIMIT} thresh={FOSSIL_THRESHOLD} p={FOSSIL_RESPAWN_P}")
    print()
    header = f"{'N':>3} | {'random':>9} | {'fossil':>9} | {'Δ mean':>9} | {'95% CI':>9} | {'win%':>5} | verdict"
    print(header)
    print("-" * len(header))
    for n in ns:
        rand_vals, foss_vals, deltas = [], [], []
        for i in range(runs):
            seed = base_seed + i
            world = make_world(seed)
            r = run_arm(seed, n, steps, world, fossils=False)[metric]
            f = run_arm(seed, n, steps, world, fossils=True)[metric]
            rand_vals.append(r); foss_vals.append(f); deltas.append(f - r)
        ps = paired_stats(deltas)
        lift = (ps["mean"] - ps["ci_half"]) > 0 and ps["win_rate"] >= 0.60
        print(f"{n:>3} | {stats.mean(rand_vals):>9.2f} | {stats.mean(foss_vals):>9.2f} | "
              f"{ps['mean']:>+9.3f} | {ps['ci_half']:>9.3f} | {ps['win_rate']*100:>4.0f}% | "
              f"{'LIFT' if lift else 'no lift'}")
    print()
    if label == "fake":
        print("Reminder: fake scoring — 'LIFT' would only mean mass concentrates on")
        print("high-hash literals. Run --real for the actual answer.")
    else:
        print("Coverage is deterministic per (base,suffix); a flat result means the")
        print("fossil operator adds nothing a best-of lookup wouldn't.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--real", action="store_true", help="use real eBay comp coverage as fitness")
    ap.add_argument("--steps", type=int, default=STEPS_PER_RUN)
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--seed", type=int, default=RANDOM_SEED)
    ap.add_argument("--metric", choices=["mean", "best"], default="mean")
    ap.add_argument("--ns", type=int, nargs="+", default=NS)
    args = ap.parse_args()

    if args.real:
        build_real_backend()
        sweep(args.ns, args.steps, args.runs, args.seed, real_world, args.metric, "real")
    else:
        sweep(args.ns, args.steps, args.runs, args.seed, fake_world, args.metric, "fake")


if __name__ == "__main__":
    main()
