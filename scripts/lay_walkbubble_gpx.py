"""Walk-bubble GPX synth — the model from [[project_courier_postcode_walkbubble]].

Real Dereham routes aren't independent drops on a graph; they're a handful
of postcode anchors where the van parks and the courier walks 4-7 drops
within ~50m of the anchor before driving on. Most of the time is walking
+ dwelling; driving is the short connector between bubbles.

Architecture:
  - Pick N anchor postcodes (real NR19 centres) by max-spread greedy.
  - Order anchors by nearest-neighbour tour from a chosen depot.
  - Distribute total drops across anchors (roughly even split).
  - Per anchor: place drops at ±DROP_JITTER_M; emit walk + dwell at each.
  - Between anchors: emit_drive at van speed.

Reports total drive distance + walk distance + duration so the synth
can be calibrated against a real shift's mileage and clock.

Run:  python3 scripts/lay_walkbubble_gpx.py --n-drops 67 --n-anchors 12
      python3 scripts/lay_walkbubble_gpx.py --n-drops 67 -m walkbubble -d 2026-05-30
"""
import argparse
import glob
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from courier_gps import _haversine_m  # noqa: E402
from lay_synthetic_gpx import write_gpx  # noqa: E402
from lay_a47_detour_gpx import (  # noqa: E402
    emit_drive, emit_walk, emit_dwell, TRAFFIC_EVENT_PROB,
)

POSTCODES_DIR = Path(__file__).parent / "postcodes"
# Calibrated 2026-05-31 against real courier scan-to-finish ~21.9s/drop.
# That's the whole per-drop transaction (scan parcel → walk to door →
# deliver → press finish), so DWELL is just the at-door portion and the
# inter-drop walk does the rest. Old values (DWELL=50, JITTER=30) gave
# ~87s/drop — ~4× too long for an efficient courier in a tight bubble.
DROP_JITTER_M = 10.0       # drops within this radius of the anchor (~6-12m spacing)
INTER_DROP_WALK_M = 12.0   # representative walk between adjacent drops
WALK_PRE_SECS = 8.0        # van → first drop (parked at the cluster edge)
WALK_POST_SECS = 8.0       # last drop → van
# Calibrated 2026-05-31 to match real courier rate ~21.9 drops/hr in the
# door-1 → door-last window (67 drops in 3.06h). DWELL is per-drop
# time-on-task absorbing wait-for-answer, scan, sign, occasional
# no-answer-card paperwork, and brief chat — not just literal at-door.
DWELL_SECS = 80.0
SORT_AT_VAN_SECS = 60.0    # real sort + paperwork between bubbles
MEAL_BREAK_SECS = 1800.0   # 30-min break inserted at the route midpoint
ROUTE_START_TS = datetime(
    2026, 5, 30, 8, 30, 0, tzinfo=timezone.utc).timestamp()


def load_dereham_centres() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for path in glob.glob(str(POSTCODES_DIR / "NR19_*.json")):
        with open(path) as f:
            d = json.load(f)
        pc, c = d.get("postcode"), d.get("coords")
        if pc and c and len(c) == 2:
            out[pc] = (c[0], c[1])
    return out


def pick_anchors(centres: dict[str, tuple[float, float]],
                 n: int, seed: int) -> list[tuple[str, tuple[float, float]]]:
    """Greedy max-spread: start at a seeded postcode, then keep picking
    whichever remaining postcode is furthest from the nearest already-picked
    anchor. Spreads anchors across the Dereham urban area instead of
    clustering them in one corner."""
    rng = random.Random(seed)
    items = list(centres.items())
    rng.shuffle(items)
    picked = [items[0]]
    while len(picked) < n and len(picked) < len(items):
        best_idx, best_min_d = -1, -1.0
        for i, (pc, c) in enumerate(items):
            if (pc, c) in picked:
                continue
            min_d = min(_haversine_m(c[0], c[1], pc2_c[0], pc2_c[1])
                        for _, pc2_c in picked)
            if min_d > best_min_d:
                best_min_d, best_idx = min_d, i
        picked.append(items[best_idx])
    return picked


def nn_tour(anchors: list[tuple[str, tuple[float, float]]]
            ) -> list[tuple[str, tuple[float, float]]]:
    """Nearest-neighbour visit order starting from the first anchor."""
    remaining = list(anchors)
    out = [remaining.pop(0)]
    while remaining:
        cur = out[-1][1]
        idx = min(range(len(remaining)),
                  key=lambda i: _haversine_m(cur[0], cur[1],
                                             remaining[i][1][0],
                                             remaining[i][1][1]))
        out.append(remaining.pop(idx))
    return out


def distribute_drops(total: int, n_anchors: int) -> list[int]:
    """Spread total drops across anchors as evenly as possible."""
    base = total // n_anchors
    extra = total - base * n_anchors
    return [base + (1 if i < extra else 0) for i in range(n_anchors)]


def place_drops_around(anchor: tuple[float, float], n: int,
                       rng: random.Random) -> list[tuple[float, float]]:
    R = 6_371_000.0
    drops = []
    for _ in range(n):
        bearing = rng.uniform(0, 2 * math.pi)
        radius = abs(rng.gauss(0, DROP_JITTER_M))
        lat_off = radius * math.cos(bearing) / R
        lon_off = (radius * math.sin(bearing)
                   / (R * math.cos(math.radians(anchor[0]))))
        drops.append((anchor[0] + math.degrees(lat_off),
                      anchor[1] + math.degrees(lon_off)))
    return drops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-drops", type=int, default=67)
    ap.add_argument("--n-anchors", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/walkbubble.gpx"))
    ap.add_argument("-m", "--manifest-id", default="walkbubble_demo")
    ap.add_argument("-d", "--date", default="2026-05-30")
    args = ap.parse_args()

    centres = load_dereham_centres()
    if len(centres) < args.n_anchors:
        raise SystemExit(f"only {len(centres)} NR19 centres available")
    rng = random.Random(args.seed)
    # Reversed 2026-05-31 to match real courier choice: hit the outlier
    # bubble FIRST (when fresh, van full) instead of leaving it as a
    # tired final 7+km drive. Greedy NN strands outliers at the end;
    # walking the tour backwards gets you home through the dense
    # central cluster.
    anchors = list(reversed(
        nn_tour(pick_anchors(centres, args.n_anchors, args.seed))))
    drops_per = distribute_drops(args.n_drops, args.n_anchors)

    # Realise all drops up front so we can report walk distance.
    bubble_drops: list[list[tuple[float, float]]] = []
    for (_, anchor_coord), n in zip(anchors, drops_per):
        bubble_drops.append(place_drops_around(anchor_coord, n, rng))

    # Emit trace.
    ticks: list[dict] = []
    ts = ROUTE_START_TS
    total_drive_m = 0.0
    total_walk_m = 0.0
    prev_anchor = None

    meal_break_index = args.n_anchors // 2  # midpoint
    for idx, ((pc, anchor_coord), drops) in enumerate(
            zip(anchors, bubble_drops)):
        if prev_anchor is not None:
            leg_m = _haversine_m(prev_anchor[0], prev_anchor[1],
                                 anchor_coord[0], anchor_coord[1])
            total_drive_m += leg_m
            ts = emit_drive(prev_anchor, anchor_coord, ticks, ts, rng,
                            TRAFFIC_EVENT_PROB)
            ts = emit_dwell(anchor_coord, SORT_AT_VAN_SECS, ticks, ts, rng)
        # Meal break before this bubble (once per shift, at midpoint).
        if idx == meal_break_index:
            ts = emit_dwell(anchor_coord, MEAL_BREAK_SECS, ticks, ts, rng)
        # Walk from van (anchor) to first drop.
        if drops:
            ts = emit_walk(drops[0], WALK_PRE_SECS, ticks, ts, rng)
            total_walk_m += _haversine_m(anchor_coord[0], anchor_coord[1],
                                         drops[0][0], drops[0][1])
            for i, drop in enumerate(drops):
                ts = emit_dwell(drop, DWELL_SECS, ticks, ts, rng)
                if i + 1 < len(drops):
                    nxt = drops[i + 1]
                    inter_m = _haversine_m(drop[0], drop[1], nxt[0], nxt[1])
                    total_walk_m += inter_m
                    # Walk to next drop — duration scales with distance.
                    walk_secs = max(8.0, inter_m / 1.2)
                    ts = emit_walk(nxt, walk_secs, ticks, ts, rng)
            # Walk from last drop back to van.
            total_walk_m += _haversine_m(drops[-1][0], drops[-1][1],
                                         anchor_coord[0], anchor_coord[1])
            ts = emit_walk(anchor_coord, WALK_POST_SECS, ticks, ts, rng)
        prev_anchor = anchor_coord

    write_gpx(ticks, args.out, args.manifest_id, args.date)
    span_h = (ticks[-1]["ts"] - ticks[0]["ts"]) / 3600.0
    print(f"wrote {args.out}")
    print(f"  anchors: {args.n_anchors} (drops per anchor: {drops_per})")
    print(f"  total drops: {args.n_drops}")
    print(f"  trkpts: {len(ticks)}")
    print(f"  duration: {span_h:.2f}h ({span_h * 60:.0f} min)")
    print(f"  drive distance: {total_drive_m / 1000:.1f} km")
    print(f"  walk distance:  {total_walk_m / 1000:.2f} km")
    print("  anchors in visit order:")
    for (pc, _), n in zip(anchors, drops_per):
        print(f"    {pc}  ({n} drops)")


if __name__ == "__main__":
    main()
