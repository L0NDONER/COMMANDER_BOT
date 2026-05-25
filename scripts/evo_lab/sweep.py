"""Phase 1 — σ / Voronoi sweep, global and per-cell.

For a grid of mutation scales, measure the two quantities the concept rests on:

  p           boundary-crossing rate: P(express(v+ε) ≠ express(v))
  locality    fraction of crossings landing in the source token's k nearest
              neighbours (semantic, not random)

plus the per-token spread of p (does one global σ work — G3).

Two modes for the noise scale:
  global   : every token perturbed with the same σ.
  per-cell : σ_i = multiplier × cell_scale_i, where cell_scale_i is the
             token's normalized nearest-neighbour distance. Big cells get big
             σ, small cells small σ — equalising crossing rates across the
             space. The G3-motivated lever.
  compare  : run both and print the decisive head-to-head at matched p.

Pure numpy + frozen E. No engine, no eBay, no EC2.

    python sweep.py                       # global, default grid
    python sweep.py --mode per-cell
    python sweep.py --mode compare
    python sweep.py --backend random --mode compare   # negative control
"""

from __future__ import annotations

import argparse

import numpy as np

from embeddings import build_or_load_E, neighbours
from vocab import SUFFIX_VOCAB

# Gate targets (starting guesses — argue with the data, see README).
P_BAND = (0.05, 0.15)
LOCALITY_TARGET = 0.60
K_NEIGHBOURS = 3
DEFAULT_GRID = [0.10, 0.15, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.40]


def cell_scales(E: np.ndarray) -> np.ndarray:
    """Per-token cell-size proxy: Euclidean distance to nearest neighbour
    (unit vecs), normalized to mean 1 so the multiplier is comparable to a
    global σ. A Voronoi boundary sits ~halfway to the nearest neighbour, so
    this tracks cell radius."""
    sim = E @ E.T
    np.fill_diagonal(sim, -np.inf)
    nn_cos = sim.max(axis=1)
    nn_dist = np.sqrt(np.maximum(0.0, 2.0 - 2.0 * nn_cos))
    return nn_dist / nn_dist.mean()


def sweep(E, grid, trials, k, scales, seed=0):
    """scales is a per-token multiplier array; σ_i(g) = g × scales[i]."""
    rng = np.random.default_rng(seed)
    topk = neighbours(E)[:, :k]
    n, d = E.shape
    rows = []
    for g in grid:
        crossed = np.zeros(n)
        local = np.zeros(n)
        for i in range(n):
            sig = g * scales[i]
            pert = E[i] + rng.normal(0.0, sig, size=(trials, d))
            who = np.argmax(pert @ E.T, axis=1)
            mask = who != i
            crossed[i] = mask.sum()
            if mask.any():
                local[i] = np.isin(who[mask], topk[i]).sum()
        total = crossed.sum()
        per_token_p = crossed / trials
        rows.append({
            "g": g,
            "p": total / (n * trials),
            "locality": (local.sum() / total) if total else float("nan"),
            "p_std": float(np.std(per_token_p)),
            "p_min": float(per_token_p.min()),
            "p_max": float(per_token_p.max()),
        })
    return rows


def _best_in_band(rows):
    in_band = [r for r in rows if P_BAND[0] <= r["p"] <= P_BAND[1]]
    return max(in_band, key=lambda r: r["locality"]) if in_band else None


def _table(rows, label):
    print(f"\n[{label}]  {'mult':>6} {'p':>7} {'local':>7} {'p_std':>7} "
          f"{'p_min':>6} {'p_max':>6}")
    print("-" * 52)
    for r in rows:
        loc = "  nan" if np.isnan(r["locality"]) else f"{r['locality']:.2f}"
        print(f"{'':>{len(label)+4}}{r['g']:6.3f} {r['p']:7.3f} {loc:>7} "
              f"{r['p_std']:7.3f} {r['p_min']:6.2f} {r['p_max']:6.2f}")


def report(rows, n_tokens, label="global"):
    _table(rows, label)
    print("\n--- gates ---")
    best = _best_in_band(rows)
    if best is None:
        print(f"G1 FAIL: no grid point lands p in {P_BAND}.")
        return
    chance = K_NEIGHBOURS / (n_tokens - 1)
    lift = best["locality"] / chance
    print(f"G1 PASS: mult={best['g']:.3f} gives p={best['p']:.3f} (in {P_BAND}).")
    print(f"G2: in-band locality {best['locality']:.2f} vs chance {chance:.2f} "
          f"(random respawn) = {lift:.1f}x lift.")
    if best["locality"] >= LOCALITY_TARGET:
        print(f"     STRONG: ≥{LOCALITY_TARGET} target. Clearly steerable.")
    elif lift >= 2.0:
        print("     REAL BUT PARTIAL: well above random, below strong target.")
    else:
        print("     DEAD: ~chance. Indistinguishable from random respawn.")
    print(f"G3: per-token p spans [{best['p_min']:.2f}, {best['p_max']:.2f}] "
          f"(std {best['p_std']:.3f}). Lower spread ⇒ a global σ suffices.")


def compare(g_rows, pc_rows, n_tokens):
    gb, pb = _best_in_band(g_rows), _best_in_band(pc_rows)
    chance = K_NEIGHBOURS / (n_tokens - 1)
    print("\n=== head-to-head (best in-band) ===")
    print(f"{'mode':>10} {'mult':>6} {'p':>7} {'local':>7} {'lift':>6} "
          f"{'p_std':>7}")
    for name, b in [("global", gb), ("per-cell", pb)]:
        if b is None:
            print(f"{name:>10}   no grid point in band")
            continue
        print(f"{name:>10} {b['g']:6.3f} {b['p']:7.3f} {b['locality']:7.2f} "
              f"{b['locality']/chance:6.1f} {b['p_std']:7.3f}")
    if gb is None or pb is None:
        return
    d_loc = pb["locality"] - gb["locality"]
    d_std = gb["p_std"] - pb["p_std"]
    print(f"\nΔlocality (per-cell − global): {d_loc:+.2f}")
    print(f"Δp_std (uniformity gain):      {d_std:+.3f}")
    print("--- verdict ---")
    if pb["locality"] >= LOCALITY_TARGET and gb["locality"] < LOCALITY_TARGET:
        print("Per-cell σ reaches the STRONG bar where global σ couldn't. "
              "The lever works → Phase 2 justified.")
    elif d_loc >= 0.05:
        print(f"Per-cell σ improves steerability ({d_loc:+.2f} locality) and "
              "evens out crossing rates, but stays partial. Lever helps, "
              "not decisive.")
    else:
        print("Per-cell σ gives no material locality gain. Global σ is the "
              "ceiling for this E/vocab — the cap is intrinsic, not a tuning "
              "miss. Decide if ~chance×lift drift beats random respawn's "
              "simplicity for a triage bot.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="sentence-transformers",
                    choices=["sentence-transformers", "random"])
    ap.add_argument("--mode", default="global",
                    choices=["global", "per-cell", "compare"])
    ap.add_argument("--trials", type=int, default=4000)
    ap.add_argument("--k", type=int, default=K_NEIGHBOURS)
    ap.add_argument("--sigmas", type=float, nargs="+", default=DEFAULT_GRID)
    args = ap.parse_args()

    tokens, E = build_or_load_E(SUFFIX_VOCAB, backend=args.backend)
    ones = np.ones(len(tokens))
    scales = cell_scales(E)
    print(f"backend={args.backend}  |V|={len(tokens)}  d={E.shape[1]}  "
          f"trials/token={args.trials}  k={args.k}  mode={args.mode}")

    if args.mode == "compare":
        g_rows = sweep(E, args.sigmas, args.trials, args.k, ones)
        pc_rows = sweep(E, args.sigmas, args.trials, args.k, scales)
        _table(g_rows, "global")
        _table(pc_rows, "per-cell")
        compare(g_rows, pc_rows, len(tokens))
    elif args.mode == "per-cell":
        report(sweep(E, args.sigmas, args.trials, args.k, scales),
               len(tokens), label="per-cell")
    else:
        report(sweep(E, args.sigmas, args.trials, args.k, ones),
               len(tokens), label="global")


if __name__ == "__main__":
    main()
