"""Null harness for route sequencers.

Compares three sequencers on the same manifest:
  * radial   — distance-from-depot sort (the [[outside-in]] control).
  * random   — N random permutations of the cluster pool.
  * greedy   — angle-aware greedy from [[greedy-angle]].

Reports total km and total angular cost for each, with the random
distribution summarised by median + IQR. Both metrics matter:
angular cost alone can be gamed by long detours.

Kill criteria (default, override with --kill-cost / --kill-km):
  greedy median must beat radial by ≥30% on angular cost AND not
  lose more than 10% on total km vs radial. Anything else is
  refuted at this rate / α / β / normalize combo.

Usage (via master CLI):
    python3 scripts/corrections/dispatch/dispatch.py null \\
        --depot 'PE32 2NQ' --home 'NR20 4AW' --n 1000 \\
        --alpha 1 --beta 1 --normalize --pin-tail < manifest.txt
"""
import argparse
import json
import math
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import topology
from greedy_angle import TRAFFIC_PROFILES, _traffic_scale  # noqa: E402

ONS_PATH = Path(__file__).resolve().parents[2] / "ons_nr_postcodes.json"
LAT0 = 52.7
PC_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\s?\d?[A-Z]{0,2}\b")
EXTRA_CENTROIDS = {"PE32 2NQ": (52.704136, 0.8259)}


def to_m(latlon):
    lat, lon = latlon
    return (lat * 111320.0,
            lon * 111320.0 * math.cos(math.radians(LAT0)))


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


def load_centroids():
    c = {k: tuple(v) for k, v in json.load(open(ONS_PATH)).items()}
    c.update(EXTRA_CENTROIDS)
    return c


def parse_manifest(text):
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
    drops = Counter(pc for _, pc in manifest)
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
    return clusters


def score(sequence, depot, home):
    """Return (km, raw_C, masked_C, uturns_raw, uturns_masked).
    Cluster shape is (name, n, centroid, tag)."""
    pts_m = [to_m(depot)] + [to_m(c[2]) for c in sequence] + [to_m(home)]
    coords = [depot] + [c[2] for c in sequence] + [home]
    total_km = sum(hav(coords[i], coords[i + 1]) for i in range(len(coords) - 1)) / 1000.0
    raw_c = 0.0
    masked_c = 0.0
    u_raw = 0
    u_mask = 0
    for i in range(1, len(pts_m) - 1):
        a_raw = angular_cost(pts_m[i - 1], pts_m[i], pts_m[i + 1])
        tag = sequence[i - 1][3]
        a_mask = topology.mask_cost(a_raw, tag)
        raw_c += a_raw
        masked_c += a_mask
        if a_raw > 1.5:
            u_raw += 1
        if a_mask > 1.5:
            u_mask += 1
    return total_km, raw_c, masked_c, u_raw, u_mask


def radial_sort(pool, depot):
    return sorted(pool, key=lambda c: hav(depot, c[2]))


def greedy(pool, depot, alpha, beta, normalize, traffic_profile=None):
    remaining = list(range(len(pool)))
    pos_m = to_m(depot)
    prev_m = None
    order = []
    while remaining:
        scores = []
        for i in remaining:
            cm = to_m(pool[i][2])
            d = math.hypot(cm[0] - pos_m[0], cm[1] - pos_m[1])
            d *= _traffic_scale(pool[i][2], traffic_profile)
            a = angular_cost(prev_m, pos_m, cm) if prev_m else 0.0
            scores.append((i, d, a, cm))
        if normalize and len(scores) > 1:
            ds = [s[1] for s in scores]
            as_ = [s[2] for s in scores]
            d_lo, d_hi = min(ds), max(ds)
            a_lo, a_hi = min(as_), max(as_)
            d_range = d_hi - d_lo or 1.0
            a_range = a_hi - a_lo or 1.0
            ranked = [
                (i, alpha * (d - d_lo) / d_range + beta * (a - a_lo) / a_range, cm)
                for i, d, a, cm in scores
            ]
        else:
            ranked = [(i, alpha * d + beta * a, cm) for i, d, a, cm in scores]
        ranked.sort(key=lambda r: r[1])
        chosen_i, _, cm = ranked[0]
        order.append(chosen_i)
        remaining.remove(chosen_i)
        prev_m = pos_m
        pos_m = cm
    return [pool[i] for i in order]


def summarise(samples, label):
    med = statistics.median(samples)
    q1 = statistics.quantiles(samples, n=4)[0]
    q3 = statistics.quantiles(samples, n=4)[2]
    lo = min(samples)
    hi = max(samples)
    return {"label": label, "median": med, "q1": q1, "q3": q3,
            "min": lo, "max": hi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depot", default="PE32 2NQ")
    ap.add_argument("--home", default="NR20 4AW")
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--beta", type=float, default=1.0)
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("--pin-tail", action="store_true")
    ap.add_argument("--n", type=int, default=1000,
                    help="random permutations")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--kill-cost", type=float, default=0.30,
                    help="min fractional improvement in angular cost vs radial")
    ap.add_argument("--kill-km", type=float, default=0.10,
                    help="max fractional regression in km vs radial")
    ap.add_argument("--traffic-profile", default=None,
                    choices=list(TRAFFIC_PROFILES),
                    help="scale arterial edge costs during greedy sequencing")
    args = ap.parse_args()

    random.seed(args.seed)
    centers = load_centroids()
    manifest = parse_manifest(sys.stdin.read())
    if not manifest:
        sys.exit("no postcodes parsed from stdin")
    clusters = build_clusters(manifest, centers)

    depot = centers[args.depot]
    home = centers[args.home]

    if args.pin_tail:
        pool = [c for c in clusters if not c[0].startswith("NR19 2EU·")]
        tail = [c for c in clusters if c[0] == "NR19 2EU·Northgate"]
        tail += [c for c in clusters if c[0] == "NR19 2EU·Allotment"]
    else:
        pool, tail = clusters, []

    # --- radial baseline ---
    radial_seq = radial_sort(pool, depot) + tail
    rad_km, rad_raw, rad_mask, rad_u_raw, rad_u_mask = score(radial_seq, depot, home)

    # --- random null ---
    km_s, raw_s, mask_s, u_raw_s, u_mask_s = [], [], [], [], []
    for _ in range(args.n):
        perm = pool[:]
        random.shuffle(perm)
        seq = perm + tail
        km, raw, mask, ur, um = score(seq, depot, home)
        km_s.append(km); raw_s.append(raw); mask_s.append(mask)
        u_raw_s.append(ur); u_mask_s.append(um)
    rnd_km   = summarise(km_s,   "random km")
    rnd_raw  = summarise(raw_s,  "random raw C")
    rnd_mask = summarise(mask_s, "random masked C")
    rnd_u    = summarise(u_mask_s, "random masked U")

    # --- greedy candidate ---
    traffic_profile = TRAFFIC_PROFILES.get(args.traffic_profile)
    g_seq = greedy(pool, depot, args.alpha, args.beta, args.normalize,
                   traffic_profile) + tail
    g_km, g_raw, g_mask, g_u_raw, g_u_mask = score(g_seq, depot, home)

    # --- report ---
    n_corners = len(g_seq)
    print(f"depot {args.depot} → home {args.home}   "
          f"clusters: {len(g_seq)}   "
          f"alpha={args.alpha} beta={args.beta} "
          f"normalize={args.normalize} pin_tail={args.pin_tail}")
    print(f"random null: n={args.n}, seed={args.seed}")
    print()
    print(f"  {'sequencer':<10}  {'km':>7}  "
          f"{'raw_C':>7}  {'mask_C':>7}  {'mean_mask':>9}  "
          f"{'U_raw':>6}  {'U_mask':>7}")
    print(f"  {'radial':<10}  {rad_km:>7.2f}  "
          f"{rad_raw:>7.2f}  {rad_mask:>7.2f}  "
          f"{rad_mask / n_corners:>9.2f}  "
          f"{rad_u_raw:>6}  {rad_u_mask:>7}")
    print(f"  {'random med':<10}  {rnd_km['median']:>7.2f}  "
          f"{rnd_raw['median']:>7.2f}  {rnd_mask['median']:>7.2f}  "
          f"{rnd_mask['median'] / n_corners:>9.2f}  "
          f"{statistics.median(u_raw_s):>6.1f}  "
          f"{statistics.median(u_mask_s):>7.1f}")
    print(f"  {'random IQR':<10}  "
          f"[{rnd_km['q1']:>4.1f},{rnd_km['q3']:>5.1f}]  "
          f"[{rnd_raw['q1']:>4.1f},{rnd_raw['q3']:>5.1f}]  "
          f"[{rnd_mask['q1']:>4.1f},{rnd_mask['q3']:>5.1f}]")
    print(f"  {'greedy':<10}  {g_km:>7.2f}  "
          f"{g_raw:>7.2f}  {g_mask:>7.2f}  "
          f"{g_mask / n_corners:>9.2f}  "
          f"{g_u_raw:>6}  {g_u_mask:>7}")
    print()

    # --- kill check uses masked cost (the fair comparison) ---
    cost_drop = (rad_mask - g_mask) / rad_mask if rad_mask else 0
    km_regress = (g_km - rad_km) / rad_km if rad_km else 0
    pass_cost = cost_drop >= args.kill_cost
    pass_km = km_regress <= args.kill_km
    verdict = "PASS" if (pass_cost and pass_km) else "REFUTED"
    print(f"greedy vs radial (masked cost):")
    print(f"  masked C  : {rad_mask:.2f} → {g_mask:.2f}   "
          f"Δ = {-cost_drop * 100:+.1f}%   "
          f"(kill if < -{args.kill_cost * 100:.0f}%)  "
          f"{'OK' if pass_cost else 'FAIL'}")
    print(f"  total km  : {rad_km:.2f} → {g_km:.2f}   "
          f"Δ = {km_regress * 100:+.1f}%   "
          f"(kill if > +{args.kill_km * 100:.0f}%)  "
          f"{'OK' if pass_km else 'FAIL'}")
    print(f"  verdict: {verdict}")

    # exit code mirrors verdict so this works as a CI gate
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
