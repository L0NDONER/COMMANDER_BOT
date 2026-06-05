"""Root pipeline — wire the five layers in order.

  motion       symbolic_walker.classify_legs   (manifest key → symbol seq)
  truth        corrections.resolve_location    (door fix when address known)
  context      context.route_weight            (route-level gate, stub=1.0)
  scorers      scorers.make_scorer             (mode | agreement | surprise
                                                | runlength → Δ fn)
  null harness arrow_test.run_falsification    (reversal + 4 nulls)

The motion layer is symbolic (postcode 7-char sectors) since the
geometric GPS substrate was retired 2026-06-01; see
[[courier-bubble-signature]] for the N=1 reframing.

Each layer is one file, each file exposes one clean function. This module
imports those five and runs them in the listed order. CLI:

    python3 scripts/corrections/pipeline.py  0289286389121:2023-12-27
    python3 scripts/corrections/pipeline.py  0289286389121:2023-12-27  --scorer surprise
    python3 scripts/corrections/pipeline.py  combined_dereham_grid_nodes:2026-05-30  --scorer mode
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # commander/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))      # scripts/corrections/

# Layer 1 — motion (symbolic substrate: postcode 7-char sectors)
from symbolic_walker import classify_legs  # noqa: E402
# Layer 2 — truth (used at address-resolution time; loaded here so the
# pipeline owns a single CorrectionTable instance for callers that want it)
from corrections import CorrectionTable  # noqa: E402
# Layer 3 — context (stub; gates live here per [[variant-design-rules]] #5)
from context import route_weight  # noqa: E402
# Layer 4 — scorers
from scorers import make_scorer  # noqa: E402
# Layer 5 — null harness
from arrow_test import run_falsification  # noqa: E402

CORRECTIONS_PATH = Path(__file__).resolve().parent / "corrections.json"


def run(manifest_spec: str,
        scorer_name: str = "mode",
        k: int = 2,
        L: int = 10,
        n_perm: int = 2000,
        seed: int = 0) -> dict:
    """Single-manifest end-to-end run.

    `manifest_spec` is `"mid:date"` (e.g. `"0289286389121:2023-12-27"`).
    Returns a dict with the per-layer artefacts:
    `{seq, weight, scorer, falsification}`."""
    if ":" not in manifest_spec:
        raise ValueError(f"manifest_spec must be 'mid:date', got "
                         f"{manifest_spec!r}")
    mid, date = manifest_spec.split(":", 1)
    # 1. motion — symbolic substrate (postcode 7-char sectors)
    seq = classify_legs((mid, date))
    # 2. truth — load table so it's available; for GPS-only traces there
    #    are no address keys, so this layer is a side-channel (the
    #    bootstrap + lens consume it via courier_lens.resolve_address).
    table = (CorrectionTable(str(CORRECTIONS_PATH))
             if CORRECTIONS_PATH.exists() else None)
    # 3. context — route-level weight (stub returns 1.0)
    weight = route_weight(route_solar=None, route_weather=None)
    # 4. scorer
    scorer = make_scorer(scorer_name, k=k)
    # 5. null harness
    if len(seq) < max(4, L + 1):
        falsification = {"skipped_reason": f"n={len(seq)} too short"}
    else:
        falsification = run_falsification(seq, scorer, L=L,
                                          n_perm=n_perm, seed=seed)
    return {
        "manifest": manifest_spec,
        "seq": seq,
        "n": len(seq),
        "alphabet": sorted(set(seq)),
        "context_weight": weight,
        "scorer": f"{scorer_name}(k={k})",
        "truth_table_entries": len(table.entries) if table else 0,
        "falsification": falsification,
    }


def _print_result(out: dict) -> None:
    print(f"manifest:  {out['manifest']}")
    print(f"motion:    n={out['n']}  alphabet={out['alphabet']}")
    print(f"truth:     {out['truth_table_entries']} correction(s) loaded")
    print(f"context:   weight={out['context_weight']:.3f}  (stub)")
    print(f"scorer:    {out['scorer']}")
    f = out["falsification"]
    if "skipped_reason" in f:
        print(f"nulls:     SKIPPED ({f['skipped_reason']})")
        return
    rev = f["reversal"]
    print(f"reversal:  S={rev['symmetric']:+.4f}  A={rev['antisymmetric']:+.4f}"
          f"  (Δfwd={rev['delta_fwd']:+.4f}  Δrev={rev['delta_rev']:+.4f})")
    print("nulls:")
    print(f"  {'name':<18}  {'p05':>8}  {'p50':>8}  {'p95':>8}  "
          f"{'pct':>5}  {'p':>6}")
    for name, r in f["nulls"].items():
        inside = 5.0 <= r["percentile"] <= 95.0
        tag = "inside" if inside else "OUTSIDE"
        print(f"  {name:<18}  {r['p05']:>+8.4f}  {r['p50']:>+8.4f}  "
              f"{r['p95']:>+8.4f}  {r['percentile']:>4.1f}  "
              f"{r['p_two_sided']:>6.4f}  {tag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest",
                    help="manifest spec 'mid:date' "
                         "(e.g. 0289286389121:2023-12-27)")
    ap.add_argument("--scorer",
                    choices=("mode", "agreement", "surprise", "runlength"),
                    default="mode")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--L", type=int, default=10)
    ap.add_argument("--n-perm", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", action="store_true",
                    help="emit raw JSON instead of human-readable summary")
    args = ap.parse_args()
    out = run(args.manifest, scorer_name=args.scorer, k=args.k, L=args.L,
              n_perm=args.n_perm, seed=args.seed)
    if args.json:
        print(json.dumps(out, default=str, indent=2))
    else:
        _print_result(out)


if __name__ == "__main__":
    main()
