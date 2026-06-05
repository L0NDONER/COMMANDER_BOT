"""Run the user's walker (pause/walk/turn/drive classifier) on every
gps_traces/*.json file and feed each resulting symbol sequence into
arrow_test.run_test. 2 warm (drop first 2 steps), 1 hot (lag k=1)."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # commander/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/

from courier_lens import lag_game  # noqa: E402
from arrow_test import (  # noqa: E402
    reversal_decompose, permutation_null, block_permutation_null,
    iid_null, markov1_null,
)
from courier_gps import GPSTrace, GPSTick, infer_stops  # noqa: E402

WARMUP = 2
LAG = 2  # k=1 is structurally symmetric (past_hit == future_hit by construction)
BUBBLE_L = 10
N_PERM = 1000


def haversine(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6371000
    p = math.radians(lat2 - lat1)
    q = math.radians(lon2 - lon1)
    x = (math.sin(p / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(q / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def heading(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def classify_step(dist_m, dh_deg):
    """Leg-scale bins (7-way). The 5-bin version collapsed the long-route
    specimen 0289286389121 to ~75% large_turn because real urban
    courier legs cluster between 60° and 180° heading delta. Splitting
    large_turn into gentle / sharp / u_turn (cuts at 100° and 145°)
    rescues alphabet entropy on dh-rich routes; promoted 2026-06-01.
      pause        : <30m              (essentially co-located stops)
      walk         : 30–300m           (postcode-internal hops)
      small_turn   : ≥300m, dh<25°     (sustained heading, slight drift)
      medium_turn  : ≥300m, 25°≤dh<60° (gentle re-orientation)
      gentle_large : ≥300m, 60°≤dh<100° (broad sweep)
      sharp_large  : ≥300m, 100°≤dh<145° (sharp turn)
      u_turn       : ≥300m, dh≥145°    (near-reversal)"""
    if dist_m < 30:
        return "pause"
    if dist_m < 300:
        return "walk"
    if dh_deg < 25:
        return "small_turn"
    if dh_deg < 60:
        return "medium_turn"
    if dh_deg < 100:
        return "gentle_large"
    if dh_deg < 145:
        return "sharp_large"
    return "u_turn"


def _wrap_dh(dh):
    """Clamp |Δheading| to [0, 180]. Without this, 270° really meant a
    90° turn the other way and bin-fired as 'turn' incorrectly."""
    dh = dh % 360
    return dh if dh <= 180 else 360 - dh


def run_game(breadcrumbs):
    out = []
    for i in range(1, len(breadcrumbs)):
        a = breadcrumbs[i - 1]
        b = breadcrumbs[i]
        pa = (a["lat"], a["lon"])
        pb = (b["lat"], b["lon"])
        dist = haversine(pa, pb)
        hd_a = heading(pa, pb)
        if i >= 2:
            pc = (breadcrumbs[i - 2]["lat"], breadcrumbs[i - 2]["lon"])
            hd_prev = heading(pc, pa)
            dh = _wrap_dh(hd_a - hd_prev)
        else:
            dh = 0
        out.append({
            "i": i,
            "dist_m": round(dist, 2),
            "heading": round(hd_a, 1),
            "dh": round(dh, 1),
            "kind": classify_step(dist, dh),
        })
    return out


# Symbol encoding for arrow_test: 4 categories collapse to a small integer
# alphabet so lag_game's mode comparison works cleanly.
KIND_TO_SYM = {"pause": 0, "walk": 1,
               "small_turn": 2, "medium_turn": 3,
               "gentle_large": 4, "sharp_large": 5, "u_turn": 6}


def scorer(seq):
    rows = lag_game(seq, LAG)
    r = next(row for row in rows if row["k"] == LAG)
    return r["future_hit"] - r["past_hit"]


# Canonical motion-layer function: GPS-trace path → symbol sequence.
# Pipeline imports exactly this one entry point.
def classify_legs(trace_path) -> list[int]:
    """Read a gps_traces/*.json file, infer stops, run the leg-scale
    walker on the stop centroids, return the integer symbol sequence
    after the standard `WARMUP` trim. Used as the motion-layer step in
    `pipeline.run`."""
    with open(trace_path) as f:
        data = json.load(f)
    raw_ticks = data["ticks"] if isinstance(data, dict) else data
    gps_ticks = [GPSTick(ts=t.get("ts", t.get("t")),
                         lat=t["lat"], lon=t["lon"],
                         heading_deg=t.get("heading_deg", 0.0),
                         speed_mps=t.get("speed_mps", 0.0),
                         accuracy_m=t.get("accuracy_m", 0.0))
                 for t in raw_ticks]
    trace = GPSTrace("", "", gps_ticks)
    stops = infer_stops(trace)
    breadcrumbs = [{"lat": s.lat, "lon": s.lon, "t": s.start_ts}
                   for s in stops]
    if len(breadcrumbs) < 3:
        return []
    result = run_game(breadcrumbs)
    return [KIND_TO_SYM[r["kind"]] for r in result][WARMUP:]


def main():
    traces_dir = Path(__file__).resolve().parents[1] / "gps_traces"
    files = sorted(traces_dir.glob("*.json"))
    print(f"found {len(files)} trace(s)  "
          f"warmup={WARMUP}  lag k={LAG}  block L={BUBBLE_L}  "
          f"(downsampled to one breadcrumb per inferred stop)")
    for path in files:
        with open(path) as f:
            data = json.load(f)
        ticks = data["ticks"] if isinstance(data, dict) else data
        # Downsample to per-leg granularity: one breadcrumb per inferred
        # stop centroid. This is the scale the courier actually navigates
        # at — tick-density saturates 'walk', stop-density doesn't.
        gps_ticks = [GPSTick(ts=t.get("ts", t.get("t")),
                             lat=t["lat"], lon=t["lon"],
                             heading_deg=t.get("heading_deg", 0.0),
                             speed_mps=t.get("speed_mps", 0.0),
                             accuracy_m=t.get("accuracy_m", 0.0))
                     for t in ticks]
        trace = GPSTrace("", "", gps_ticks)
        stops = infer_stops(trace)
        breadcrumbs = [{"lat": s.lat, "lon": s.lon, "t": s.start_ts}
                       for s in stops]
        if len(breadcrumbs) < 4:
            print(f"\n########  {path.stem}   n_ticks={len(ticks)}   "
                  f"n_stops={len(stops)}  ########")
            print("  too few stops to walk — skipping")
            continue
        result = run_game(breadcrumbs)
        seq = [KIND_TO_SYM[r["kind"]] for r in result]
        seq = seq[WARMUP:]
        kinds = {k: sum(1 for r in result if r["kind"] == k)
                 for k in ("pause", "walk", "small_turn", "medium_turn",
                           "gentle_large", "sharp_large", "u_turn")}
        print(f"\n########  {path.stem}   n_ticks={len(ticks)}   "
              f"n_stops={len(stops)}   n_legs={len(seq)}  ########")
        print(f"  leg-kind mix (pre-warmup): {kinds}")
        if len(seq) < max(4, BUBBLE_L + 1):
            print("  too short for arrow_test — skipping")
            continue
        rev = reversal_decompose(seq, scorer)
        A, S = rev["antisymmetric"], rev["symmetric"]
        contam = (abs(S) / abs(A)) if abs(A) > 1e-9 else (
            float("inf") if abs(S) > 1e-9 else 0.0)
        clean = abs(S) <= 0.25 * max(abs(A), 1e-9)
        print(f"  Δ fwd={rev['delta_fwd']:+.4f}  Δ rev={rev['delta_rev']:+.4f}"
              f"   S={S:+.4f}  A={A:+.4f}  "
              f"|S|/|A|={contam:.0%} {'CLEAN' if clean else 'CONTAM'}")
        if abs(A) < 1e-9:
            print("  A is structurally zero — alphabet too saturated; "
                  "skipping nulls")
            continue
        # Four nulls, hardest-to-easiest. The Markov-1 fit is the strongest
        # filter: if A survives it, the asymmetry is beyond first-order
        # autocorrelation. Block-perm is the dispatcher control. Full-perm
        # checks against symbol-frequency luck. IID is the loose floor.
        results = {
            "IID-marginal": iid_null(seq, scorer, "antisymmetric",
                                     N_PERM, seed=0),
            "full-perm": permutation_null(seq, scorer, "antisymmetric",
                                          N_PERM, seed=0),
            "block-perm L=" + str(BUBBLE_L): block_permutation_null(
                seq, scorer, BUBBLE_L, "antisymmetric", N_PERM, seed=0),
            "Markov-1": markov1_null(seq, scorer, "antisymmetric",
                                     N_PERM, seed=0),
        }
        print(f"  {'null':<22}  {'p05':>7}  {'p50':>7}  {'p95':>7}  "
              f"{'pct':>5}  {'p':>7}  verdict")
        for name, r in results.items():
            inside = 5.0 <= r["percentile"] <= 95.0
            tag = "inside" if inside else "OUTSIDE"
            print(f"  {name:<22}  {r['p05']:>+7.4f}  {r['p50']:>+7.4f}  "
                  f"{r['p95']:>+7.4f}  {r['percentile']:>4.1f}  "
                  f"{r['p_two_sided']:>7.4f}  {tag}")


if __name__ == "__main__":
    main()
