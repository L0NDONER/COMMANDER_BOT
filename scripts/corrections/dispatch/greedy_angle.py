"""Greedy angle-aware route sequencer.

Picks the next cluster at each step by minimising

    cost = alpha * d_norm + beta * a_norm

where d_norm is straight-line distance from current position to
candidate and a_norm is the angular cost of the turn (1 - cos θ).
With --normalize, both terms are min-max scaled over remaining
candidates at each step, so the dominant term flips automatically
when local geometry compresses one of them — no regime classifier.

Companion to [[outside-in]] (the radial-sort control) and
[[lag-game-null-framework]] (validated externally via
`route_null.py`).

Usage (via master CLI):
    python3 scripts/corrections/dispatch/dispatch.py plan --pin-tail < manifest.txt
    python3 scripts/corrections/dispatch/dispatch.py plan \\
        --depot 'PE32 2NQ' --home 'NR20 4AW' --alpha 1 --beta 1 \\
        --normalize --pin-tail < manifest.txt

--pin-tail holds the NR19 2EU cluster out of the greedy pool and
appends "Northgate" (non-allotment doors) then "Allotment" (toad
hall / allotment-keyword doors) as the final two stops, matching
the courier's hard end-of-round constraint.

Input format: any whitespace-separated postcodes / addresses; the
postcode at the end of each line groups the line into a cluster.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import topology

ONS_PATH = Path(__file__).resolve().parents[2] / "ons_nr_postcodes.json"

# Built-in traffic profiles.  Each zone dict has lat/lon bounds and a
# d_scale multiplier applied to any leg whose candidate endpoint falls
# inside the zone — inflating that leg's distance cost to reflect real
# slowing through a congested corridor.
TRAFFIC_PROFILES: dict[str, dict] = {
    "weekend": {
        "label": "A47 weekend diversion — south Dereham ×1.5",
        "zones": [
            {"lat_max": 52.680, "lon_min": 0.920, "lon_max": 0.970,
             "d_scale": 1.5},
        ],
    },
    "a47-closed": {
        "label": "A47 full closure — south Dereham ×2.0",
        "zones": [
            {"lat_max": 52.680, "lon_min": 0.920, "lon_max": 0.970,
             "d_scale": 2.0},
        ],
    },
    "weekend_a47": {
        "label": "A47 weekend closure — south Dereham ×1.8 (weekend + diversion combined)",
        "zones": [
            {"lat_max": 52.680, "lon_min": 0.920, "lon_max": 0.970,
             "d_scale": 1.8},
        ],
    },
}


def get_dwell_multiplier(zone_name: str, is_dark: bool) -> float:
    """Scale the effective cost of working at a zone in the dark.
    LOW-lit zones take ~25% longer — torch, careful footing, slower
    address reads. Inflates d so the sequencer front-loads them."""
    if is_dark and topology.ZONE_LIGHTING.get(zone_name) == topology.LIGHTING_LOW:
        return 1.25
    return 1.0


def get_transit_multiplier(zone_name: str, is_dark: bool) -> float:
    """Scale the transit cost through an unlit zone after dark.
    Unlit through-streets (LOW lighting) impose a 10% safety buffer
    on drive speed — applies even when just passing through, not only
    when working there."""
    if is_dark and topology.ZONE_LIGHTING.get(zone_name) == topology.LIGHTING_LOW:
        return 1.10
    return 1.0


def _traffic_scale(latlon: tuple[float, float],
                   profile: dict | None) -> float:
    """Return the d_scale multiplier for a candidate position given the
    active traffic profile.  Returns 1.0 when no profile is active or
    when the candidate falls outside all congested zones."""
    if profile is None:
        return 1.0
    lat, lon = latlon
    for z in profile["zones"]:
        if (lat <= z.get("lat_max", 90.0)
                and lat >= z.get("lat_min", -90.0)
                and lon >= z["lon_min"]
                and lon <= z["lon_max"]):
            return z["d_scale"]
    return 1.0
LAT0 = 52.7
PC_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\s?\d?[A-Z]{0,2}\b")

# PE32 2NQ depot — not in the NR-only ONS file; hard-coded from postcodes.io.
EXTRA_CENTROIDS = {"PE32 2NQ": (52.704136, 0.8259)}


def to_m(latlon):
    """Equirectangular projection to local metres at LAT0."""
    lat, lon = latlon
    return (lat * 111320.0,
            lon * 111320.0 * math.cos(math.radians(LAT0)))


def load_centroids():
    centers = {k: tuple(v) for k, v in json.load(open(ONS_PATH)).items()}
    centers.update(EXTRA_CENTROIDS)
    return centers


def parse_manifest(text):
    """Return [(line, postcode)] keeping address strings so the
    Allotment sub-cluster can be split out by keyword."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = PC_RE.findall(line.upper())
        if m:
            out.append((line, m[-1]))
    return out


def is_allotment(addr):
    a = addr.lower()
    return "allotment" in a or "toad hall" in a


def build_clusters(manifest, centers):
    """Return ordered list of (cluster_id, n_drops, (lat, lon), tag)
    and a warning list for postcodes missing from `centers`. Tag is
    derived from address lines via `topology.classify`."""
    drops = Counter(pc for _, pc in manifest)
    missing = sorted({pc for pc in drops if pc not in centers})

    # group addresses by (display key) so we can classify per cluster
    addr_by_key = defaultdict(list)
    for line, pc in manifest:
        if pc == "NR19 2EU":
            key = "NR19 2EU·Allotment" if is_allotment(line) else "NR19 2EU·Northgate"
        else:
            key = pc
        addr_by_key[key].append(line)

    allot_n = sum(1 for a, pc in manifest if pc == "NR19 2EU" and is_allotment(a))
    other_n = sum(1 for a, pc in manifest if pc == "NR19 2EU" and not is_allotment(a))

    clusters = []
    for pc in dict.fromkeys(pc for _, pc in manifest):
        if pc == "NR19 2EU":
            if other_n:
                tag = topology.classify(addr_by_key["NR19 2EU·Northgate"])
                clusters.append(("NR19 2EU·Northgate", other_n,
                                 topology.cluster_anchor(pc, centers[pc]), tag))
            if allot_n:
                tag = topology.classify(addr_by_key["NR19 2EU·Allotment"])
                clusters.append(("NR19 2EU·Allotment", allot_n,
                                 topology.cluster_anchor(pc, centers[pc]), tag))
        elif pc in centers:
            tag = topology.classify(addr_by_key[pc])
            clusters.append((pc, drops[pc],
                             topology.cluster_anchor(pc, centers[pc]), tag))

    # Road name per cluster for ROAD_RISK lookup in greedy_sequence.
    risks: dict[str, str] = {}   # cluster_name → snake_case road key
    detours: dict[str, str] = {} # cluster_name → detour status
    for name, _, _, _ in clusters:
        addrs = addr_by_key.get(name, [])
        rk = topology.risk_key(addrs)
        if rk:
            risks[name] = rk
        dk = topology.detour_key(addrs)
        if dk:
            detours[name] = topology._DETOUR_NORM[dk]

    return clusters, missing, risks, detours


def hav(a, b):
    R = 6371000.0
    p = math.radians(b[0] - a[0])
    q = math.radians(b[1] - a[1])
    x = (math.sin(p / 2) ** 2
         + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0]))
         * math.sin(q / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def angular_cost(prev_m, curr_m, next_m):
    v1 = (curr_m[0] - prev_m[0], curr_m[1] - prev_m[1])
    v2 = (next_m[0] - curr_m[0], next_m[1] - curr_m[1])
    m1 = math.hypot(*v1)
    m2 = math.hypot(*v2)
    if m1 == 0 or m2 == 0:
        return 0.0
    cos_t = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
    cos_t = max(-1.0, min(1.0, cos_t))
    return 1 - cos_t


def greedy_sequence(depot, clusters, alpha, beta, normalize,
                    traffic_profile=None, is_dark: bool = False,
                    cluster_risks: dict | None = None,
                    cluster_detours: dict | None = None,
                    route_profile: str | None = None):
    """Greedy angle-aware sort. Returns ordered list of cluster
    indices into `clusters`."""
    remaining = list(range(len(clusters)))
    pos_m = to_m(depot)
    prev_m = None
    order = []
    while remaining:
        scores = []
        # gather raw d / a for normalisation pass
        for i in remaining:
            cand_m = to_m(clusters[i][2])
            d = math.hypot(cand_m[0] - pos_m[0], cand_m[1] - pos_m[1])
            d *= _traffic_scale(clusters[i][2], traffic_profile)
            d *= get_dwell_multiplier(clusters[i][0], is_dark)
            d *= get_transit_multiplier(clusters[i][0], is_dark)
            d *= topology.apply_profile_multiplier(
                clusters[i][0], clusters[i][2], is_dark,
                profile_name=route_profile)
            d += topology.apply_profile_penalties(
                clusters[i][0], clusters[i][2],
                profile_name=route_profile) * 9.0
            if cluster_detours:
                status = cluster_detours.get(clusters[i][0])
                if status == topology.BLOCKED:
                    d = float("inf")
                elif status == topology.PRIMARY_ARTERIAL:
                    d += (topology._ARTERIAL_QUEUE_SECS
                          + topology.intersection_delay(clusters[i][0])) * 9.0
            a = angular_cost(prev_m, pos_m, cand_m) if prev_m else 0.0
            if cluster_risks:
                road = cluster_risks.get(clusters[i][0])
                if road:
                    a = topology.calculate_segment_cost(road, a)
            scores.append((i, d, a, cand_m))

        if normalize and len(scores) > 1:
            fin_ds = [s[1] for s in scores if s[1] != float("inf")]
            as_ = [s[2] for s in scores]
            d_lo = min(fin_ds) if fin_ds else 0.0
            d_hi = max(fin_ds) if fin_ds else 1.0
            a_lo, a_hi = min(as_), max(as_)
            d_range = d_hi - d_lo or 1.0
            a_range = a_hi - a_lo or 1.0
            ranked = [
                (i, 2.0 + beta * (a - a_lo) / a_range, cand_m)
                if d == float("inf") else
                (i, alpha * (d - d_lo) / d_range
                    + beta * (a - a_lo) / a_range, cand_m)
                for i, d, a, cand_m in scores
            ]
        else:
            ranked = [
                (i, alpha * d + beta * a, cand_m)
                for i, d, a, cand_m in scores
            ]
        ranked.sort(key=lambda r: r[1])
        chosen_i, _, chosen_m = ranked[0]
        order.append(chosen_i)
        remaining.remove(chosen_i)
        prev_m = pos_m
        pos_m = chosen_m
    return order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depot", default="PE32 2NQ")
    ap.add_argument("--home", default="NR20 4AW")
    ap.add_argument("--alpha", type=float, default=1.0,
                    help="distance weight")
    ap.add_argument("--beta", type=float, default=1.0,
                    help="angle weight")
    ap.add_argument("--normalize", action="store_true",
                    help="min-max scale d and a at each step")
    ap.add_argument("--pin-tail", action="store_true",
                    help="force NR19 2EU Northgate then Allotment as final stops")
    ap.add_argument("--rate", type=float, default=21.6,
                    help="drops per hour for time budget")
    ap.add_argument("--traffic-profile", default=None,
                    choices=list(TRAFFIC_PROFILES),
                    help="scale arterial edge costs to model congestion "
                         f"({', '.join(TRAFFIC_PROFILES)})")
    ap.add_argument("--profile", default=topology.DEFAULT_PROFILE,
                    choices=list(topology.PROFILES),
                    help=f"route behaviour profile (default: {topology.DEFAULT_PROFILE})")
    args = ap.parse_args()

    centers = load_centroids()
    if args.depot not in centers:
        sys.exit(f"depot {args.depot!r} has no centroid (add to EXTRA_CENTROIDS)")
    if args.home not in centers:
        sys.exit(f"home {args.home!r} not in ONS data")

    manifest = parse_manifest(sys.stdin.read())
    if not manifest:
        sys.exit("no postcodes parsed from stdin")
    clusters, missing, cluster_risks, cluster_detours = build_clusters(manifest, centers)
    if missing:
        print(f"# warning: dropped postcodes not in centroids: {missing}",
              file=sys.stderr)

    if args.pin_tail:
        pool = [c for c in clusters if not c[0].startswith("NR19 2EU·")]
        tail = [c for c in clusters if c[0] == "NR19 2EU·Northgate"]
        tail += [c for c in clusters if c[0] == "NR19 2EU·Allotment"]
    else:
        pool, tail = clusters, []

    depot_pt = centers[args.depot]
    home_pt = centers[args.home]

    traffic_profile = TRAFFIC_PROFILES.get(args.traffic_profile)
    if traffic_profile:
        print(f"# traffic profile: {traffic_profile['label']}", file=sys.stderr)

    try:
        from almanac import is_dark as _is_dark
        dark = _is_dark()
    except ImportError:
        dark = False
    if dark:
        print("# almanac: is_dark=True — LOW-lit zones scaled ×1.25", file=sys.stderr)

    print(f"# profile: {args.profile}", file=sys.stderr)
    order = greedy_sequence(depot_pt, pool, args.alpha, args.beta,
                            args.normalize, traffic_profile, dark,
                            cluster_risks, cluster_detours, args.profile)
    sequence = [pool[i] for i in order] + tail

    # report
    knob = 60.0 / args.rate
    total_drops = sum(n for _, n, _, _ in sequence)
    pts_m = [to_m(depot_pt)] + [to_m(pt) for _, _, pt, _ in sequence] + [to_m(home_pt)]

    total_km = 0.0
    total_raw = 0.0
    total_masked = 0.0
    rows = []
    cum_drops = 0
    centroids = [depot_pt] + [c[2] for c in sequence]
    # pts_m[0]=depot, pts_m[i+1]=cluster_i, pts_m[-1]=home.
    # turn at cluster_i is centered on pts_m[i+1].
    for i, (name, n, pt, tag) in enumerate(sequence):
        seg_km = hav(centroids[i], pt) / 1000.0
        total_km += seg_km
        a_raw = angular_cost(pts_m[i], pts_m[i + 1], pts_m[i + 2])
        a_mask = topology.mask_cost(a_raw, tag)
        total_raw += a_raw
        total_masked += a_mask
        cum_drops += n
        rows.append((i + 1, name, n, seg_km, a_raw, a_mask, tag, cum_drops))

    last_pt = sequence[-1][2]
    closing_km = hav(last_pt, home_pt) / 1000.0
    total_km += closing_km

    tag_glyph = {topology.TYPE_THROUGH: " ",
                 topology.TYPE_CLOSE:   "▲",
                 topology.TYPE_HYBRID:  "◆"}
    print(f"depot {args.depot} → home {args.home}")
    print(f"clusters: {len(sequence)}  drops: {total_drops}  "
          f"alpha={args.alpha} beta={args.beta} "
          f"normalize={args.normalize} pin_tail={args.pin_tail}")
    print()
    print(f"  {'#':>3} t {'cluster':<22} {'drops':>5} {'leg_km':>6} "
          f"{'C_raw':>5} {'C_mask':>6} {'cum':>5} {'cum_min':>7}")
    for i, name, n, km, a_raw, a_mask, tag, cum in rows:
        g = tag_glyph.get(tag, " ")
        flag = "  U" if a_raw > 1.5 else ("  b" if a_raw > 1.2 else "")
        print(f"  {i:>3} {g} {name:<22} {n:>5} {km:>5.2f}  "
              f"{a_raw:>5.2f} {a_mask:>6.2f} {cum:>5} {cum * knob:>6.1f}m{flag}")
    print()
    print(f"legend: ▲ TYPE_CLOSE (masked to 0)   ◆ TYPE_HYBRID (capped at 1.0)")
    print(f"closing leg → home: {closing_km:.2f} km")
    print(f"total km (depot→…→home): {total_km:.2f}")
    n_corners = max(1, len(rows))
    print(f"total angular cost: raw {total_raw:.2f} (mean {total_raw / n_corners:.2f}) "
          f" →  masked {total_masked:.2f} (mean {total_masked / n_corners:.2f})")
    u_raw = sum(1 for r in rows if r[4] > 1.5)
    u_mask = sum(1 for r in rows if r[5] > 1.5)
    print(f"U-turns (C>1.5):  raw {u_raw}  →  masked {u_mask}  "
          f"/ {n_corners} interior corners")


if __name__ == "__main__":
    main()
