"""Scenario GPX: Gressenhall depot → work into Dereham → exit Quebec Road
→ Windsor Park → September House → Highfields → Allotment Gardens.

Models a full rural shift at configurable drops/hr with live traffic events.
Drops are distributed across named anchor zones in route order; zones marked
APPROX need coord verification if used for door-level calibration.

Run:
    python3 scripts/lay_gressenhall_route_gpx.py
    python3 scripts/lay_gressenhall_route_gpx.py --n-drops 101 --drops-per-hour 21.6
    python3 scripts/lay_gressenhall_route_gpx.py --n-drops 101 --drops-per-hour 21.6 --seed 7
"""
import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from courier_gps import _haversine_m  # noqa: E402
from lay_synthetic_gpx import (  # noqa: E402
    write_gpx, DRIVE_MPS,
    _bearing, _step_along, _jitter, _hdop,
    GAP_PROB, GAP_SECS_MIN, GAP_SECS_MAX, TICK_JITTER_SECS, WALK_MPS,
)
from lay_a47_detour_gpx import emit_walk, emit_dwell  # noqa: E402
from lay_a47_detour_gpx import (  # noqa: E402
    TRAFFIC_SLOW_MPS, TRAFFIC_HOLD_SECS_MIN, TRAFFIC_HOLD_SECS_MAX,
)
from lay_walkbubble_gpx import (  # noqa: E402
    place_drops_around, WALK_PRE_SECS, WALK_POST_SECS,
    SORT_AT_VAN_SECS,
)
sys.path.insert(0, str(Path(__file__).resolve().parent / "corrections" / "dispatch"))
import topology  # noqa: E402
from almanac import (  # noqa: E402
    get_weather_multiplier, get_temp_dwell_multiplier,
    CLEAR, HEAVY_FOG, HEAVY_SNOW,
)

def _next_ts(ts: float, rng: random.Random) -> float:
    if rng.random() < GAP_PROB:
        return ts + rng.uniform(GAP_SECS_MIN, GAP_SECS_MAX)
    return ts + 1.0 + rng.gauss(0, TICK_JITTER_SECS)


def _push(ticks, ts, lat, lon, bearing, speed, rng):
    if rng.random() < GAP_PROB:
        return
    jit_lat, jit_lon = _jitter(lat, lon, rng)
    ticks.append({
        "ts": ts, "lat": jit_lat, "lon": jit_lon,
        "course": (bearing + rng.gauss(0, 2.0)) % 360.0,
        "speed": max(0.0, speed + rng.gauss(0, 0.3)),
        "hdop": _hdop(rng),
    })


def emit_drive(start, end, ticks, ts, rng, traffic_prob,
               weather_mult: float = 1.0):
    """Drive start→end. weather_mult > 1.0 reduces effective speed
    (e.g. 1.40 for heavy fog → 40% more time on each leg)."""
    total_m = _haversine_m(start[0], start[1], end[0], end[1])
    if total_m < 1.0:
        return ts
    bearing = _bearing(start[0], start[1], end[0], end[1])
    cur_lat, cur_lon = start
    travelled = 0.0
    DECEL_R = 60.0
    effective_mps = DRIVE_MPS / weather_mult
    while travelled < total_m:
        remaining = total_m - travelled
        if rng.random() < traffic_prob and remaining > DECEL_R:
            hold_secs = rng.uniform(TRAFFIC_HOLD_SECS_MIN, TRAFFIC_HOLD_SECS_MAX)
            t_end = ts + hold_secs
            while ts < t_end and travelled < total_m:
                _push(ticks, ts, cur_lat, cur_lon, bearing,
                      max(0.0, rng.gauss(TRAFFIC_SLOW_MPS, 0.5)), rng)
                step_m = TRAFFIC_SLOW_MPS * 1.0
                cur_lat, cur_lon = _step_along(cur_lat, cur_lon, bearing, step_m)
                travelled += step_m
                ts = _next_ts(ts, rng)
            continue
        if remaining <= DECEL_R:
            speed = WALK_MPS + (effective_mps - WALK_MPS) * (remaining / DECEL_R)
        else:
            speed = effective_mps + rng.gauss(0, 0.8)
        speed = max(0.3, speed)
        step_m = min(speed * 1.0, remaining)
        cur_lat, cur_lon = _step_along(cur_lat, cur_lon, bearing, step_m)
        travelled += step_m
        _push(ticks, ts, cur_lat, cur_lon, bearing, speed, rng)
        ts = _next_ts(ts, rng)
    return ts


# 5× default (0.01) — represents real A-road and town-centre congestion.
LIVE_TRAFFIC_PROB = 0.05
# A47 weekend diversion: diverted traffic bleeds onto Dereham town roads.
# Applied on any leg where the destination zone is in south Dereham
# (lat < A47_LAT_THRESHOLD).
A47_DIVERSION_PROB = 0.10
A47_LAT_THRESHOLD  = 52.680

DEFAULT_START = datetime(2026, 6, 1, 8, 30, 0, tzinfo=timezone.utc)

# Route anchors in visit order.
# coords: real NR19/NR20 postcode centroids unless marked APPROX.
# weight: relative drop allocation (0 = transit-only, no drops).
ROUTE_ZONES: list[tuple[str, tuple[float, float], int]] = [
    # ── Gressenhall depot ──────────────────────────────────────────────────
    ("gressenhall_depot",   (52.6957, 0.9552),   0),   # NR20 4BA — transit
    # ── Work into north Dereham (NR20→NR19 corridor) ───────────────────────
    ("north_dereham_a",     (52.6934, 0.9399),  10),   # NR19 2SU
    ("north_dereham_b",     (52.6912, 0.9409),  12),   # NR19 2HG
    ("north_dereham_c",     (52.6907, 0.9437),  10),   # NR19 2HA
    # ── Dereham centre ─────────────────────────────────────────────────────
    ("dereham_centre",      (52.6826, 0.9404),  15),   # NR19 2AX
    ("dereham_south",       (52.6754, 0.9413),   8),   # NR19 1AL area
    # ── Sandy Lane / Dereham Rd intersection — transit + crossing penalty ──
    ("sandy_lane_dereham_rd", (52.6683, 0.9355),  0),  # APPROX — transit only
    # ── Exit via Quebec Road (S→N) ─────────────────────────────────────────
    ("quebec_sheddick",     (52.6857, 0.9374),   8),   # NR19 2DT
    ("quebec_hall",         (52.6926, 0.9371),   6),   # NR19 2QY
    # ── Windsor Park → September House → Highfields → finish ───────────────
    ("windsor_park",        (52.6814, 0.9460),  12),   # NR19 2XB area — APPROX
    ("september_house",     (52.6862, 0.9452),   8),   # NR19 2HH area — APPROX
    ("highfields",          (52.6864, 0.9476),   8),   # NR19 2HJ area — APPROX
    ("allotment_gardens",   (52.6818, 0.9473),   4),   # NR19 2UH area — APPROX finish
]

# Quebec Road collision — two candidate detours from NR19 2DT (Sheddick)
# to NR19 2QY (Quebec Hall). Sim scores each by effective distance
# (metres × snow multiplier) and picks the lower-cost route.
QUEBEC_COLLISION_POINT = (52.6891, 0.9373)   # midway on Quebec Rd

DETOUR_CANDIDATES = [
    {
        "name": "Dillington Road",
        "waypoints": [
            (52.6891, 0.9328),   # ~300m west — rural lane parallel to Quebec Rd
            (52.6926, 0.9328),   # north along lane to Quebec Hall level
        ],
        "gritted": False,
        "desc": "western rural lane, ungritted",
    },
    {
        "name": "Neatherd Road / Northgate",
        "waypoints": [
            (52.6857, 0.9404),   # east onto gritted town road (NR19 2AX area)
            (52.6920, 0.9399),   # north via NR19 2SR (gritted bus corridor)
        ],
        "gritted": True,
        "desc": "eastern town loop, gritted bus route",
    },
]

COLLISION_HESITATION_SECS = 45.0   # stop, read situation, check phone

# Zones on gritted bus routes — Norfolk Highways grits arterial roads
# first; residential pockets stay ungritted until a second pass (if at all).
GRITTED_ZONES = {
    "gressenhall_depot",
    "north_dereham_a",
    "north_dereham_b",
    "north_dereham_c",
    "dereham_centre",
    "dereham_south",
    "quebec_sheddick",
}

assert sum(w for _, _, w in ROUTE_ZONES) == 101, \
    "zone weights must sum to 101 (the default n-drops)"


def distribute_drops(total: int,
                     zones: list[tuple[str, tuple[float, float], int]]
                     ) -> list[int]:
    """Scale zone weights to total, preserving relative proportions."""
    weights = [w for _, _, w in zones]
    total_weight = sum(weights)
    if total_weight == 0:
        return [0] * len(zones)
    base = [total * w // total_weight for w in weights]
    remainder = total - sum(base)
    # Distribute leftover to largest fractional zones.
    fractions = [(total * w / total_weight - base[i], i)
                 for i, w in enumerate(weights)]
    fractions.sort(reverse=True)
    for _, i in fractions[:remainder]:
        base[i] += 1
    return base


def score_detour(candidate: dict, start: tuple, end: tuple,
                 weather_cond: str, gritted_bus: bool) -> float:
    """Effective distance (metres × snow mult) for a detour candidate."""
    pts = [start] + candidate["waypoints"] + [end]
    raw_m = sum(_haversine_m(pts[i][0], pts[i][1],
                             pts[i+1][0], pts[i+1][1])
                for i in range(len(pts) - 1))
    gritted = candidate["gritted"] and gritted_bus
    mult = get_weather_multiplier(weather_cond, gritted)
    return raw_m * mult


def pick_detour(start: tuple, end: tuple, weather_cond: str,
                gritted_bus: bool) -> dict:
    return min(DETOUR_CANDIDATES,
               key=lambda c: score_detour(c, start, end,
                                          weather_cond, gritted_bus))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-drops", type=int, default=101)
    ap.add_argument("--drops-per-hour", type=float, default=21.6,
                    help="calibrate dwell to this rate (drive adds on top)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/gressenhall_route.gpx"))
    ap.add_argument("-m", "--manifest-id", default="gressenhall_route")
    ap.add_argument("-d", "--date", default=None,
                    help="YYYY-MM-DD for GPX metadata (default: derived from --start)")
    ap.add_argument("--start", default=None,
                    help="ISO datetime for shift start, e.g. 2025-12-14T18:00:00 "
                         "(assumed UTC; default 2026-06-01T08:30:00Z)")
    ap.add_argument("--a47-diversion", action="store_true",
                    help="elevate traffic prob on south-Dereham legs (A47 closed)")
    ap.add_argument("--weather", default=CLEAR,
                    choices=[CLEAR, HEAVY_FOG, "LIGHT_FOG", "HEAVY_RAIN",
                             HEAVY_SNOW],
                    help="weather condition (default: CLEAR)")
    ap.add_argument("--temp", type=float, default=10.0,
                    help="air temperature °C — affects dwell (icy paths below 0)")
    ap.add_argument("--gritted-bus-routes", action="store_true",
                    help="arterial/bus-route legs gritted — halves snow penalty "
                         "on GRITTED_ZONES legs")
    ap.add_argument("--collision-quebec", action="store_true",
                    help="collision blocks Quebec Road before Quebec Hall — "
                         "sim scores detour candidates and picks lowest cost")
    ap.add_argument("--profile", default=topology.DEFAULT_PROFILE,
                    choices=list(topology.PROFILES),
                    help=f"route behaviour profile (default: {topology.DEFAULT_PROFILE})")
    args = ap.parse_args()

    if args.start:
        start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    else:
        start_dt = DEFAULT_START
    start_ts = start_dt.timestamp()
    date_str = args.date or start_dt.strftime("%Y-%m-%d")

    temp_dwell = get_temp_dwell_multiplier(args.temp)
    dwell_secs = max(5.0, (3600.0 / args.drops_per_hour
                     - WALK_PRE_SECS - WALK_POST_SECS) * temp_dwell)

    rng = random.Random(args.seed)
    drops_per_zone = distribute_drops(args.n_drops, ROUTE_ZONES)

    # Pre-place all drops.
    bubble_drops: list[list[tuple[float, float]]] = []
    for (_, anchor, _), n in zip(ROUTE_ZONES, drops_per_zone):
        bubble_drops.append(place_drops_around(anchor, n, rng) if n else [])

    ticks: list[dict] = []
    ts = start_ts
    total_drive_m = 0.0
    total_walk_m = 0.0
    prev_anchor: tuple[float, float] | None = None
    chosen_detour: dict | None = None
    zone_arrivals: list[tuple[str, float]] = []   # (zone_name, ts_utc)

    import zoneinfo as _zi
    _LOCAL_TZ = _zi.ZoneInfo("Europe/London")

    prev_zone: str | None = None
    for (zone_name, anchor, _), drops in zip(ROUTE_ZONES, bubble_drops):
        if prev_anchor is not None:
            leg_m = _haversine_m(prev_anchor[0], prev_anchor[1],
                                 anchor[0], anchor[1])
            total_drive_m += leg_m
            t_prob = (A47_DIVERSION_PROB
                      if args.a47_diversion and anchor[0] < A47_LAT_THRESHOLD
                      else LIVE_TRAFFIC_PROB)
            is_gritted = args.gritted_bus_routes and zone_name in GRITTED_ZONES
            weather_mult = get_weather_multiplier(args.weather, is_gritted)

            # Quebec Road collision: intercept sheddick→hall leg
            if (args.collision_quebec
                    and prev_zone == "quebec_sheddick"
                    and zone_name == "quebec_hall"):
                chosen_detour = pick_detour(prev_anchor, anchor,
                                            args.weather, args.gritted_bus_routes)
                # hesitation at collision point
                ts = emit_drive(prev_anchor, QUEBEC_COLLISION_POINT,
                                ticks, ts, rng, t_prob, weather_mult)
                ts = emit_dwell(QUEBEC_COLLISION_POINT,
                                COLLISION_HESITATION_SECS, ticks, ts, rng)
                # drive the chosen detour
                det_pts = [QUEBEC_COLLISION_POINT] + chosen_detour["waypoints"] + [anchor]
                det_gritted = chosen_detour["gritted"] and args.gritted_bus_routes
                det_mult = get_weather_multiplier(args.weather, det_gritted)
                for j in range(len(det_pts) - 1):
                    leg_m = _haversine_m(det_pts[j][0], det_pts[j][1],
                                        det_pts[j+1][0], det_pts[j+1][1])
                    total_drive_m += leg_m
                    ts = emit_drive(det_pts[j], det_pts[j+1],
                                    ticks, ts, rng, t_prob, det_mult)
            else:
                ts = emit_drive(prev_anchor, anchor, ticks, ts, rng, t_prob,
                                weather_mult)
            if drops:
                ts = emit_dwell(anchor, SORT_AT_VAN_SECS, ticks, ts, rng)

        zone_arrivals.append((zone_name, ts))

        if drops:
            ts = emit_walk(drops[0], WALK_PRE_SECS, ticks, ts, rng)
            total_walk_m += _haversine_m(anchor[0], anchor[1],
                                         drops[0][0], drops[0][1])
            for i, drop in enumerate(drops):
                ts = emit_dwell(drop, dwell_secs, ticks, ts, rng)
                if i + 1 < len(drops):
                    nxt = drops[i + 1]
                    inter_m = _haversine_m(drop[0], drop[1], nxt[0], nxt[1])
                    total_walk_m += inter_m
                    ts = emit_walk(nxt, max(8.0, inter_m / 1.2), ticks, ts, rng)
            total_walk_m += _haversine_m(drops[-1][0], drops[-1][1],
                                         anchor[0], anchor[1])
            ts = emit_walk(anchor, WALK_POST_SECS, ticks, ts, rng)

        # Profile-gated penalties (throat + intersection).
        arr_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_LOCAL_TZ)
        total_penalty = topology.apply_profile_penalties(
            zone_name, anchor, arr_dt, args.profile)
        if total_penalty > 0:
            ts = emit_dwell(anchor, total_penalty, ticks, ts, rng)

        prev_anchor = anchor
        prev_zone = zone_name

    write_gpx(ticks, args.out, args.manifest_id, date_str)
    span_h = (ticks[-1]["ts"] - ticks[0]["ts"]) / 3600.0
    actual_rate = args.n_drops / span_h
    print(f"wrote {args.out}")
    print(f"  drops: {args.n_drops}  trkpts: {len(ticks)}")
    print(f"  duration: {span_h:.2f}h ({span_h * 60:.0f} min)")
    print(f"  rate: {actual_rate:.1f} drops/hr actual  "
          f"(target {args.drops_per_hour}, dwell={dwell_secs:.0f}s)")
    print(f"  drive: {total_drive_m / 1000:.1f} km  "
          f"walk: {total_walk_m / 1000:.2f} km")
    t_label = (f"live + A47 diversion (south-Dereham legs {A47_DIVERSION_PROB})"
               if args.a47_diversion else f"{LIVE_TRAFFIC_PROB} (live)")
    print(f"  start:   {start_dt.isoformat()}")
    gritted_label = " + bus routes gritted" if args.gritted_bus_routes else ""
    print(f"  weather: {args.weather}{gritted_label}  temp={args.temp}°C  "
          f"(dwell ×{temp_dwell:.2f})")
    print(f"  traffic: {t_label}")
    print(f"  profile: {args.profile}")
    profile_cfg = topology.get_profile(args.profile)
    if profile_cfg["use_throat_penalties"]:
        active_throats = [(z, topology.throat_penalty(z))
                          for z, _, _ in ROUTE_ZONES
                          if topology.throat_penalty(z) > 0]
        if active_throats:
            print("  throats: " + "  ".join(
                f"{z}={int(p)}s" for z, p in active_throats))
    if profile_cfg["enforce_peak_hour_avoidance"]:
        active_xings = [(z, topology.intersection_delay(z))
                        for z, _, _ in ROUTE_ZONES
                        if topology.intersection_delay(z) > 0]
        if active_xings:
            print("  crossings (peak): " + "  ".join(
                f"{z}={int(p)}s" for z, p in active_xings))
    if chosen_detour:
        scores = {c["name"]: score_detour(c,
                      ROUTE_ZONES[6][1], ROUTE_ZONES[7][1],
                      args.weather, args.gritted_bus_routes)
                  for c in DETOUR_CANDIDATES}
        print("  collision: Quebec Road blocked before Quebec Hall")
        for name, s in scores.items():
            flag = " ← CHOSEN" if name == chosen_detour["name"] else ""
            print(f"    {name:<30}  eff_m={s:.0f}{flag}")
        print(f"  detour: {chosen_detour['name']} ({chosen_detour['desc']})")
    import zoneinfo as _zi2
    _LTZ = _zi2.ZoneInfo("Europe/London")
    print("  zones:")
    for (name, _, _), n, (_, arr_ts) in zip(
            ROUTE_ZONES, drops_per_zone, zone_arrivals):
        arr = datetime.fromtimestamp(arr_ts, tz=timezone.utc).astimezone(_LTZ)
        school = " ▶SCHOOL" if (name == "allotment_gardens"
                                and topology.school_multiplier(
                                    next(a for z, a, _ in ROUTE_ZONES
                                         if z == name),
                                    arr) > 1.0) else ""
        print(f"    {name:<22}  {n:>3} drops  arrive {arr.strftime('%H:%M %Z')}{school}")


if __name__ == "__main__":
    main()
