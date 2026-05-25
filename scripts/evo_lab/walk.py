"""Phase 1b — incremental accepted-walk vs k-NN respawn vs random.

Tests a multi-step mutation operator against the cheap alternatives, to settle
whether the genotype/embedding apparatus earns its keep over a lookup table.

Operators (each answers "where does a dead token's replacement land?"):

  random      uniform random token. The live engine's current behaviour. Floor.
  knn         uniform from the dead token's top-m nearest neighbours. The
              one-line lookup that captures "semantic neighbour" with NO
              genotype, NO walk. If the walk can't beat this, the apparatus is
              unjustified.
  walk        prev-anchored accepted walk: from the current token, propose
              v_cur + ε, snap to nearest token; accept iff cos(prev, cand) ≥ τ
              (a local hop), else resample; re-anchor at the landed token and
              repeat up to K steps. Stalls (stops) if no local hop is found.

The walk's only possible edge: reaching tokens FAR from start (low cos to
start) via a chain of locally-semantic hops — tokens both random (too far) and
knn-top-m (too near) miss. So the decisive numbers are:

  %novel      fraction of walk endpoints outside knn top-m  (≈0 ⇒ walk ≡ knn)
  novel_cos   mean cos(start, endpoint) among those novel endpoints, vs the
              ~random baseline (≈baseline ⇒ the far reach is just noise)
  stall%      walks that stopped early (τ too strict / σ wrong)

    python walk.py --vocab rich
    python walk.py --vocab rich --tau 0.5 --K 12 --sigma 0.4
"""

from __future__ import annotations

import argparse

import numpy as np

from embeddings import build_or_load_E, neighbours
from vocab import VOCABS


def run_walks(E, starts, K, sigma, tau, samples, rng, max_resample=25):
    """Batched prev-anchored walks. Returns end token, accepted steps, and
    min hop-cosine for every (start, sample)."""
    d = E.shape[1]
    cur = np.repeat(starts, samples)            # current token per walk
    origin = cur.copy()
    steps = np.zeros(cur.shape, dtype=int)
    min_hop = np.ones(cur.shape)
    alive = np.ones(cur.shape, dtype=bool)
    for _ in range(K):
        pending = alive.copy()
        for _ in range(max_resample):
            idx = np.where(pending)[0]
            if idx.size == 0:
                break
            cand_vec = E[cur[idx]] + rng.normal(0.0, sigma, (idx.size, d))
            cand = np.argmax(cand_vec @ E.T, axis=1)
            hop = np.einsum("ij,ij->i", E[cand], E[cur[idx]])
            accept = (cand != cur[idx]) & (hop >= tau)
            acc = idx[accept]
            cur[acc] = cand[accept]
            min_hop[acc] = np.minimum(min_hop[acc], hop[accept])
            steps[acc] += 1
            pending[acc] = False                # this step resolved for accepts
        alive &= ~pending                       # no hop found this step ⇒ stall
    return origin, cur, steps, min_hop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="sentence-transformers",
                    choices=["sentence-transformers", "random"])
    ap.add_argument("--vocab", default="rich", choices=list(VOCABS))
    ap.add_argument("--K", type=int, default=10)
    ap.add_argument("--sigma", type=float, default=0.35)
    ap.add_argument("--tau", type=float, default=0.45)
    ap.add_argument("--m", type=int, default=5)
    ap.add_argument("--samples", type=int, default=400)
    args = ap.parse_args()

    tokens, E = build_or_load_E(VOCABS[args.vocab], backend=args.backend,
                                name=args.vocab)
    n = len(tokens)
    nbr = neighbours(E)
    sim = E @ E.T
    baseline = float(sim[~np.eye(n, dtype=bool)].mean())
    nn1 = float(sim[np.arange(n), nbr[:, 0]].mean())    # mean top-1 nbr cosine
    print(f"backend={args.backend} vocab={args.vocab} |V|={n} d={E.shape[1]}")
    print(f"K={args.K} sigma={args.sigma} tau={args.tau} m={args.m} "
          f"samples={args.samples}")
    print(f"random-pair cosine baseline={baseline:.2f}  "
          f"mean top-1 neighbour cosine={nn1:.2f}  (calibrate τ between these)")

    rng = np.random.default_rng(0)
    starts = np.arange(n)
    topm = nbr[:, :args.m]                                # (n, m) knn sets

    # --- walk ---
    origin, end, steps, min_hop = run_walks(
        E, starts, args.K, args.sigma, args.tau, args.samples, rng)
    end_cos = np.einsum("ij,ij->i", E[end], E[origin])
    in_knn = np.array([end[i] in set(topm[origin[i]]) for i in range(len(end))])
    moved = steps > 0
    novel = moved & ~in_knn
    stall = ~moved

    # --- knn baseline ---
    knn_pick = topm[np.repeat(starts, args.samples),
                    rng.integers(0, args.m, size=n * args.samples)]
    knn_cos = np.einsum("ij,ij->i", E[knn_pick], E[np.repeat(starts, args.samples)])

    print(f"\n{'operator':>10} {'meanCos(start,end)':>18} {'%novel(vs knn)':>15} "
          f"{'novel_cos':>10} {'stall%':>7} {'meanSteps':>10}")
    print("-" * 76)
    print(f"{'random':>10} {baseline:18.2f} {'100':>15} {baseline:10.2f} "
          f"{'-':>7} {'-':>10}")
    print(f"{'knn top'+str(args.m):>10} {knn_cos.mean():18.2f} {'0':>15} "
          f"{'-':>10} {'-':>7} {'1':>10}")
    novel_cos = end_cos[novel].mean() if novel.any() else float("nan")
    print(f"{'walk':>10} {end_cos[moved].mean():18.2f} "
          f"{100*novel.sum()/max(moved.sum(),1):15.0f} {novel_cos:10.2f} "
          f"{100*stall.mean():7.0f} {steps[moved].mean():10.1f}")
    print(f"\nwalk min hop-cosine (path coherence, should be ≥τ={args.tau}): "
          f"{min_hop[moved].min():.2f}–{np.median(min_hop[moved]):.2f} median")

    # The m-independent test: where do walk endpoints sit in the start token's
    # neighbour ranking? If they're mostly low-rank, a knn(top-m) with that m
    # reproduces the walk trivially — no apparatus needed.
    rank = np.empty((n, n), dtype=int)
    rank[np.repeat(np.arange(n), n), nbr.ravel()] = np.tile(np.arange(n), n)
    end_rank = rank[origin[moved], end[moved]]
    print("\nwalk endpoint neighbour-rank (could knn(top-m) just grab these?):")
    print(f"  median rank {int(np.median(end_rank))}, "
          f"90th pct {int(np.percentile(end_rank, 90))}")
    for mm in (5, 10, 15, 20):
        print(f"  knn(top-{mm}) covers {100*(end_rank < mm).mean():.0f}% "
              "of walk endpoints")

    # The decisive baseline: a closed-form sampler of the same soft-semantic
    # diffusion the walk produces — token ∝ exp(cos(start,·)/T), one line from
    # the cosine row. If some T reproduces the walk's (meanCos, rank-coverage),
    # the walk is just a Monte-Carlo sampler of it and the apparatus is moot.
    walk_mean = end_cos[moved].mean()
    cover15 = (end_rank < 15).mean()
    sim_self = sim.copy()
    np.fill_diagonal(sim_self, -np.inf)
    print("\ncosine-softmax respawn (one-liner, no walk): token ∝ exp(cos/T)")
    sm = []
    for T in (0.08, 0.10, 0.12, 0.15, 0.20):
        logits = sim_self / T
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        picks = np.concatenate([rng.choice(n, args.samples, p=p[s])
                                for s in starts])
        o2 = np.repeat(starts, args.samples)
        sc = np.einsum("ij,ij->i", E[picks], E[o2]).mean()
        c15 = (rank[o2, picks] < 15).mean()
        sm.append((T, sc, c15))
        print(f"  T={T}: meanCos={sc:.2f}  cover15={100*c15:.0f}%")
    nearest = min(sm, key=lambda r: abs(r[1] - walk_mean))

    print("\n--- verdict ---")
    if stall.mean() > 0.5:
        print(f"INCONCLUSIVE: {100*stall.mean():.0f}% of walks stalled — retune "
              "τ/σ before reading the rest.")
    elif abs(nearest[2] - cover15) <= 0.10:
        print(f"walk ≡ cosine-softmax: at T={nearest[0]} the one-line softmax "
              f"matches the walk's meanCos ({walk_mean:.2f}≈{nearest[1]:.2f}) "
              f"AND rank-coverage ({100*cover15:.0f}%≈{100*nearest[2]:.0f}%). "
              "The walk is a Monte-Carlo sampler of a closed-form distribution "
              f"— with {100*stall.mean():.0f}% stalls as overhead. Apparatus "
              "unjustified: use softmax respawn off the precomputed cosine row.")
    else:
        print(f"walk differs from softmax (cover15 {100*cover15:.0f}% vs "
              f"{100*nearest[2]:.0f}% at matched meanCos) — likely manifold "
              "connectivity (τ-chains can't jump disconnected clusters). Assess "
              "whether that distinction has any product value before believing "
              "it; it comes with a {:.0f}% stall tax.".format(100*stall.mean()))


if __name__ == "__main__":
    main()
