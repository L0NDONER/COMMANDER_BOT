"""Scenario GPX: A47 closed weekend, Dereham→Norwich all detoured via
Swanton Morley. Lays down a phone-style GPX with explicit walking phases
at each stop, stop-start traffic events on long legs, and a forced
waypoint through Swanton Morley for any Dereham↔Norwich leg.

Why this exists: the existing synth was clean drive→dwell→drive. Real
days have road closures, traffic queuing, and the courier walking
between van and doors. This shape exercises gps_flow_continuity (jerky
speed) and gps_heading_change (frequent turns on detour roundabouts)
in ways the clean synth doesn't.

Scenario hard-coded:
  - 5 Dereham stops (real NR19 postcode centres from scripts/postcodes/)
  - 4 Norwich stops (approximate central Norwich coords)
  - Swanton Morley as forced detour waypoint (52.7235, 0.9919)
  - A47-closed detour legs get 5× traffic event probability

Output: /tmp/a47_detour.gpx by default, then run through ingest_gpx.py:
  python3 scripts/ingest_gpx.py /tmp/a47_detour.gpx \\
    -m a47_detour_demo -d 2026-06-06
"""
import argparse
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from courier_gps import _haversine_m  # noqa: E402
from lay_synthetic_gpx import (  # noqa: E402
    _bearing, _step_along, _jitter, _hdop, _to_iso, write_gpx,
    WALK_MPS, DRIVE_MPS, COURSE_NOISE_DEG, TICK_JITTER_SECS,
    GAP_PROB, GAP_SECS_MIN, GAP_SECS_MAX,
)

SWANTON_MORLEY = (52.7235, 0.9919)
DEREHAM_BBOX_LAT = (52.65, 52.71)
DEREHAM_BBOX_LON = (0.90, 0.97)
NORWICH_BBOX_LAT = (52.60, 52.68)
NORWICH_BBOX_LON = (1.20, 1.34)

WALK_PRE_SECS = 25.0     # van → door
DWELL_SECS = 35.0        # at the door / signing
WALK_POST_SECS = 20.0    # door → van
TRAFFIC_EVENT_PROB = 0.01     # per drive tick on normal legs
DETOUR_TRAFFIC_PROB = 0.05    # 5× on the A47 detour
TRAFFIC_SLOW_MPS = 1.5
TRAFFIC_HOLD_SECS_MIN = 8.0
TRAFFIC_HOLD_SECS_MAX = 25.0
ROUTE_START_TS = datetime(2026, 6, 6, 8, 30, 0, tzinfo=timezone.utc).timestamp()

DEREHAM_STOPS = [
    (52.682382, 0.937374, "NR19 1TD"),   # Becclesgate
    (52.682407, 0.940676, "NR19 2AX"),   # Market Place
    (52.683890, 0.944340, "NR19 2AG"),   # Kings Road
    (52.659588, 0.990752, "NR19 1DE"),   # Peters Way (eastern Dereham)
    (52.673000, 0.952000, "NR19 1AA"),   # Norwich Road end
]

NORWICH_STOPS = [
    (52.628000, 1.293000, "NR1 3DH"),    # Castle area
    (52.633000, 1.298000, "NR3 1RY"),    # Magdalen Street
    (52.625000, 1.275000, "NR2 2DR"),    # West Norwich
    (52.640000, 1.290000, "NR3 4AB"),    # North inner
]


def _in_bbox(pt, lat_range, lon_range) -> bool:
    return (lat_range[0] <= pt[0] <= lat_range[1]
            and lon_range[0] <= pt[1] <= lon_range[1])


def _needs_detour(a: tuple[float, float],
                  b: tuple[float, float]) -> bool:
    """True iff this leg crosses the Dereham↔Norwich line and so would
    have used the A47 — meaning under closure it routes via Swanton."""
    a_dereham = _in_bbox(a, DEREHAM_BBOX_LAT, DEREHAM_BBOX_LON)
    a_norwich = _in_bbox(a, NORWICH_BBOX_LAT, NORWICH_BBOX_LON)
    b_dereham = _in_bbox(b, DEREHAM_BBOX_LAT, DEREHAM_BBOX_LON)
    b_norwich = _in_bbox(b, NORWICH_BBOX_LAT, NORWICH_BBOX_LON)
    return (a_dereham and b_norwich) or (a_norwich and b_dereham)


def _push(ticks, ts, lat, lon, bearing, speed, rng):
    if rng.random() < GAP_PROB:
        return  # signal loss; this tick goes missing
    jit_lat, jit_lon = _jitter(lat, lon, rng)
    ticks.append({
        "ts": ts,
        "lat": jit_lat,
        "lon": jit_lon,
        "course": (bearing + rng.gauss(0, COURSE_NOISE_DEG)) % 360.0,
        "speed": max(0.0, speed + rng.gauss(0, 0.3)),
        "hdop": _hdop(rng),
    })


def _next_ts(ts: float, rng: random.Random) -> float:
    if rng.random() < GAP_PROB:
        return ts + rng.uniform(GAP_SECS_MIN, GAP_SECS_MAX)
    return ts + 1.0 + rng.gauss(0, TICK_JITTER_SECS)


def emit_walk(point, secs, ticks, ts, rng):
    """Walking at ~1.2 m/s in a small radius around `point`."""
    end_ts = ts + secs
    while ts < end_ts:
        bearing = rng.uniform(0, 360)
        lat, lon = _step_along(point[0], point[1], bearing,
                               rng.gauss(0, 2.0))
        speed = max(0.0, rng.gauss(WALK_MPS, 0.3))
        _push(ticks, ts, lat, lon, bearing, speed, rng)
        ts = _next_ts(ts, rng)
    return ts


def emit_dwell(point, secs, ticks, ts, rng):
    """Stationary at door (speed near 0). Speed model tightened
    2026-05-31: was gauss(0.2, 0.2) which bled into the walking band
    (0.5-2 m/s) ~10% of the time and mis-tagged courier-in-van
    hesitations as delivery_stops. Now gauss(0.0, 0.05) keeps stationary
    ticks well under the 0.5 m/s walking floor."""
    end_ts = ts + secs
    while ts < end_ts:
        bearing = rng.uniform(0, 360)
        lat, lon = _step_along(point[0], point[1], bearing,
                               rng.gauss(0, 0.8))
        _push(ticks, ts, lat, lon, bearing,
              max(0.0, rng.gauss(0.0, 0.05)), rng)
        ts = _next_ts(ts, rng)
    return ts


def emit_drive(start, end, ticks, ts, rng, traffic_prob):
    """Drive start → end at DRIVE_MPS with deceleration near the end
    and stochastic traffic events along the way."""
    total_m = _haversine_m(start[0], start[1], end[0], end[1])
    if total_m < 1.0:
        return ts
    bearing = _bearing(start[0], start[1], end[0], end[1])
    cur_lat, cur_lon = start
    travelled = 0.0
    DECEL_R = 60.0
    while travelled < total_m:
        remaining = total_m - travelled
        if rng.random() < traffic_prob and remaining > DECEL_R:
            # Traffic event: slow to TRAFFIC_SLOW_MPS for a chunk of time.
            hold_secs = rng.uniform(TRAFFIC_HOLD_SECS_MIN,
                                    TRAFFIC_HOLD_SECS_MAX)
            t_end = ts + hold_secs
            while ts < t_end and travelled < total_m:
                _push(ticks, ts, cur_lat, cur_lon, bearing,
                      max(0.0, rng.gauss(TRAFFIC_SLOW_MPS, 0.5)), rng)
                step_m = TRAFFIC_SLOW_MPS * 1.0
                cur_lat, cur_lon = _step_along(cur_lat, cur_lon,
                                               bearing, step_m)
                travelled += step_m
                ts = _next_ts(ts, rng)
            continue
        if remaining <= DECEL_R:
            speed = WALK_MPS + (DRIVE_MPS - WALK_MPS) * (remaining / DECEL_R)
        else:
            speed = DRIVE_MPS + rng.gauss(0, 0.8)
        speed = max(0.3, speed)
        step_m = min(speed * 1.0, remaining)
        cur_lat, cur_lon = _step_along(cur_lat, cur_lon, bearing, step_m)
        travelled += step_m
        _push(ticks, ts, cur_lat, cur_lon, bearing, speed, rng)
        ts = _next_ts(ts, rng)
    return ts


def emit_leg_maybe_detour(a, b, ticks, ts, rng):
    """If the leg would have used the A47 (Dereham↔Norwich), route it
    via Swanton Morley with elevated traffic. Otherwise drive direct."""
    if _needs_detour(a, b):
        ts = emit_drive(a, SWANTON_MORLEY, ticks, ts, rng,
                        DETOUR_TRAFFIC_PROB)
        ts = emit_drive(SWANTON_MORLEY, b, ticks, ts, rng,
                        DETOUR_TRAFFIC_PROB)
    else:
        ts = emit_drive(a, b, ticks, ts, rng, TRAFFIC_EVENT_PROB)
    return ts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/a47_detour.gpx"))
    args = ap.parse_args()
    rng = random.Random(args.seed)

    stops = [(s[0], s[1]) for s in DEREHAM_STOPS + NORWICH_STOPS]
    ticks: list[dict] = []
    ts = ROUTE_START_TS

    # Start: walking phase from depot to van.
    ts = emit_walk(stops[0], WALK_PRE_SECS, ticks, ts, rng)

    for i, stop in enumerate(stops):
        if i > 0:
            ts = emit_leg_maybe_detour(stops[i - 1], stop, ticks, ts, rng)
        # Walk from where van parked to the door.
        ts = emit_walk(stop, WALK_PRE_SECS, ticks, ts, rng)
        # Dwell at the door.
        ts = emit_dwell(stop, DWELL_SECS, ticks, ts, rng)
        # Walk back to van.
        ts = emit_walk(stop, WALK_POST_SECS, ticks, ts, rng)

    write_gpx(ticks, args.out, "a47_detour_demo", "2026-06-06")
    span_min = (ticks[-1]["ts"] - ticks[0]["ts"]) / 60.0
    detour_legs = sum(1 for i in range(1, len(stops))
                      if _needs_detour(stops[i - 1], stops[i]))
    print(f"wrote {args.out}: {len(stops)} stops → {len(ticks)} trkpts, "
          f"{span_min:.1f} min")
    print(f"  detour legs (forced via Swanton Morley): {detour_legs}")


if __name__ == "__main__":
    main()
