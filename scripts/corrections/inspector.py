"""Browse, diff, and sanity-check the learned door corrections.

Default view: each entry, with delta vs geocoder cache and delta vs the
postcode centroid. Sorted by |delta-vs-cache| descending — the wrong-city
geocoder hits float to the top.

    python3 scripts/corrections/inspector.py               # full table
    python3 scripts/corrections/inspector.py --key BECCLES # substring filter
    python3 scripts/corrections/inspector.py --min-delta 40
    python3 scripts/corrections/inspector.py --suspect-only

A correction is flagged 'suspect' when it lies more than --suspect-radius
metres from its postcode centroid — usually the symptom of an inferred
stop being paired to the wrong postcode in the bootstrap.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # commander/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))      # scripts/corrections/

from corrections import haversine  # noqa: E402
from courier_lens import load_address_cache, load_coords  # noqa: E402

CORRECTIONS_PATH = Path(__file__).resolve().parent / "corrections.json"


def _postcode_of(key: str) -> str:
    return key.rsplit(", ", 1)[-1] if ", " in key else ""


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[int(q * (len(sorted_vals) - 1))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="",
                    help="Case-insensitive substring filter on the key.")
    ap.add_argument("--min-delta", type=float, default=0.0,
                    help="Only show entries whose |delta vs cache| ≥ this.")
    ap.add_argument("--suspect-radius", type=float, default=300.0,
                    help="A correction farther than this from its postcode "
                         "centroid is flagged 'suspect' (default 300m).")
    ap.add_argument("--suspect-only", action="store_true",
                    help="Only show suspect rows.")
    ap.add_argument("--no-cache", action="store_true",
                    help="Only show entries with no geocoder cache hit.")
    args = ap.parse_args()

    if not CORRECTIONS_PATH.exists():
        print(f"no corrections file at {CORRECTIONS_PATH}")
        return
    with open(CORRECTIONS_PATH) as f:
        table = json.load(f)
    cache = load_address_cache()
    centers = load_coords()

    rows = []
    for key, e in table.items():
        fix = (e["lat"], e["lon"])
        geo = cache.get(key)
        delta = haversine(fix, geo) if geo else float("inf")
        pc = _postcode_of(key)
        pc_centre = centers.get(pc)
        pc_delta = haversine(fix, pc_centre) if pc_centre else float("inf")
        suspect = pc_delta > args.suspect_radius
        rows.append({
            "key": key, "fix": fix, "geo": geo, "delta": delta,
            "pc_delta": pc_delta, "count": e["count"],
            "suspect": suspect,
        })

    needle = args.key.lower()
    if needle:
        rows = [r for r in rows if needle in r["key"].lower()]
    if args.suspect_only:
        rows = [r for r in rows if r["suspect"]]
    if args.no_cache:
        rows = [r for r in rows if r["geo"] is None]
    if args.min_delta > 0:
        rows = [r for r in rows
                if (r["delta"] if r["delta"] != float("inf")
                    else args.min_delta) >= args.min_delta]
    rows.sort(key=lambda r: r["delta"], reverse=True)

    # ---- summary line ------------------------------------------------ #
    n = len(rows)
    n_cache = sum(1 for r in rows if r["geo"] is not None)
    n_nocache = n - n_cache
    n_suspect = sum(1 for r in rows if r["suspect"])
    finite_deltas = sorted(r["delta"] for r in rows
                            if r["delta"] != float("inf"))
    if finite_deltas:
        med = _percentile(finite_deltas, 0.5)
        p95 = _percentile(finite_deltas, 0.95)
    else:
        med = p95 = float("nan")
    print(f"corrections: {n}  "
          f"(cache-matched: {n_cache}, cache-gap fills: {n_nocache}, "
          f"suspect: {n_suspect})")
    print(f"  delta-vs-cache median {med:.1f}m  p95 {p95:.1f}m  "
          f"(suspect radius {args.suspect_radius:.0f}m vs postcode centre)")

    # ---- table ------------------------------------------------------- #
    print(f"\n  {'flag':<5} {'Δcache(m)':>10} {'Δpc(m)':>8} {'n':>3}  key")
    for r in rows:
        # !CITY only fires when there's a real cache hit that's wildly off.
        if r["geo"] is not None and r["delta"] > 1000:
            flag = "!CITY"
        elif r["suspect"]:
            flag = "!SUS"
        elif r["geo"] is None:
            flag = "+NEW"
        else:
            flag = "    "
        d = "  (new)" if r["geo"] is None else f"{r['delta']:>10.1f}"
        pcd = (f"{r['pc_delta']:>8.1f}"
               if r["pc_delta"] != float("inf") else "      —")
        print(f"  {flag:<5} {d}  {pcd}  {r['count']:>3}  {r['key']}")


if __name__ == "__main__":
    main()
