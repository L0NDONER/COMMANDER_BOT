"""Lay down a phone-style GPX file for a manifest's route. The output
looks like what an Android GPS logger (e.g. GPS Logger by Mendhak) would
write after a delivery shift — useful for shaking out the ingest path
without waiting on a real phone trace.

Adds realistic-ish phone noise on top of the clean kinematic model from
lay_synthetic_gps.py:
  - position jitter ~ N(0, JITTER_M) on every tick
  - HDOP usually low (0.8-1.5), occasional spike (5-10) → ingester filters
  - tick interval ~ 1 Hz with ±100ms jitter (loggers aren't periodic)
  - course = movement bearing + N(0, 5°) compass noise
  - occasional signal-loss gaps (5-15 s) at random points

Output: GPX 1.1 with the trkpt/time/course/speed/hdop fields the ingest
script expects. Written to --out (defaults to /tmp/synthetic_<mid>_<date>.gpx).

Run:  python3 scripts/lay_synthetic_gpx.py -m <manifest_id> -d <date>
      python3 scripts/lay_synthetic_gpx.py -m X -d Y --out trace.gpx
"""
import argparse
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from courier_gps import _haversine_m  # noqa: E402

WALK_MPS = 1.2
DRIVE_MPS = 9.0
DECEL_RADIUS_M = 60.0
STOP_SECS = 45.0
JITTER_M = 3.0
COURSE_NOISE_DEG = 5.0
TICK_JITTER_SECS = 0.1
HDOP_SPIKE_PROB = 0.03
GAP_PROB = 0.002  # per tick
GAP_SECS_MIN = 5.0
GAP_SECS_MAX = 15.0
EPOCH_START = datetime(2026, 5, 30, 9, 0, 0, tzinfo=timezone.utc).timestamp()


def _bearing(lat1: float, lon1: float,
             lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _step_along(lat: float, lon: float, bearing_deg: float,
                metres: float) -> tuple[float, float]:
    R = 6_371_000.0
    ang = metres / R
    br = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    phi2 = math.asin(math.sin(phi1) * math.cos(ang)
                     + math.cos(phi1) * math.sin(ang) * math.cos(br))
    lam2 = lam1 + math.atan2(
        math.sin(br) * math.sin(ang) * math.cos(phi1),
        math.cos(ang) - math.sin(phi1) * math.sin(phi2),
    )
    return (math.degrees(phi2), math.degrees(lam2))


def _jitter(lat: float, lon: float, rng: random.Random) -> tuple[float, float]:
    dx = rng.gauss(0, JITTER_M)
    dy = rng.gauss(0, JITTER_M)
    # Convert metre offsets to degree offsets (rough, fine at small scales).
    dlat = dx / 111_111.0
    dlon = dy / (111_111.0 * math.cos(math.radians(lat)))
    return (lat + dlat, lon + dlon)


def _hdop(rng: random.Random) -> float:
    if rng.random() < HDOP_SPIKE_PROB:
        return rng.uniform(5.0, 10.0)
    return rng.uniform(0.8, 1.5)


def _emit_tick(lat: float, lon: float, bearing: float, speed: float,
               ts: float, rng: random.Random) -> dict | None:
    """Apply phone-style noise to a true position. Returns None for the
    rare 'signal loss' case (random gap)."""
    if rng.random() < GAP_PROB:
        return None
    jit_lat, jit_lon = _jitter(lat, lon, rng)
    course = (bearing + rng.gauss(0, COURSE_NOISE_DEG)) % 360.0
    spd = max(0.0, speed + rng.gauss(0, 0.3))
    return {
        "ts": ts,
        "lat": jit_lat,
        "lon": jit_lon,
        "course": course,
        "speed": spd,
        "hdop": _hdop(rng),
    }


def _emit_dwell(point: tuple[float, float], ticks: list, ts: float,
                rng: random.Random) -> float:
    """Hang out at `point` for STOP_SECS at walking pace, with jitter and
    occasional signal gaps."""
    end_ts = ts + STOP_SECS
    while ts < end_ts:
        bearing = rng.uniform(0, 360)
        # Walking jitter — small movements around the point.
        lat, lon = _step_along(point[0], point[1], bearing,
                               rng.gauss(0, 1.5))
        tick = _emit_tick(lat, lon, bearing, max(0.0, rng.gauss(0.6, 0.4)),
                          ts, rng)
        if tick is not None:
            ticks.append(tick)
        # Inter-tick interval ~ 1 Hz with jitter; occasionally a gap.
        if rng.random() < GAP_PROB:
            ts += rng.uniform(GAP_SECS_MIN, GAP_SECS_MAX)
        else:
            ts += 1.0 + rng.gauss(0, TICK_JITTER_SECS)
    return ts


def _emit_leg(start: tuple[float, float], end: tuple[float, float],
              ticks: list, ts: float, rng: random.Random) -> float:
    """Drive start → end with deceleration near the destination."""
    total_m = _haversine_m(start[0], start[1], end[0], end[1])
    if total_m < 1.0:
        return ts
    bearing = _bearing(start[0], start[1], end[0], end[1])
    cur_lat, cur_lon = start
    travelled = 0.0
    while travelled < total_m:
        remaining = total_m - travelled
        if remaining <= DECEL_RADIUS_M:
            speed = WALK_MPS + (DRIVE_MPS - WALK_MPS) * (remaining
                                                         / DECEL_RADIUS_M)
        else:
            speed = DRIVE_MPS + rng.gauss(0, 0.8)
        speed = max(0.3, speed)
        # Advance one tick of true motion.
        step_m = min(speed * 1.0, remaining)
        cur_lat, cur_lon = _step_along(cur_lat, cur_lon, bearing, step_m)
        travelled += step_m
        tick = _emit_tick(cur_lat, cur_lon, bearing, speed, ts, rng)
        if tick is not None:
            ticks.append(tick)
        if rng.random() < GAP_PROB:
            ts += rng.uniform(GAP_SECS_MIN, GAP_SECS_MAX)
        else:
            ts += 1.0 + rng.gauss(0, TICK_JITTER_SECS)
    return ts


def _to_iso(ts: float) -> str:
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{int((ts % 1) * 1000):03d}Z")


def write_gpx(ticks: list[dict], out_path: Path,
              manifest_id: str, date: str) -> None:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="commander-synth-gpx" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <metadata><name>{escape(manifest_id)} {escape(date)}</name>'
        f'<time>{_to_iso(ticks[0]["ts"])}</time></metadata>',
        '  <trk>',
        f'    <name>{escape(manifest_id)}</name>',
        '    <trkseg>',
    ]
    for t in ticks:
        parts.append(
            f'      <trkpt lat="{t["lat"]:.7f}" lon="{t["lon"]:.7f}">'
            f'<time>{_to_iso(t["ts"])}</time>'
            f'<course>{t["course"]:.1f}</course>'
            f'<speed>{t["speed"]:.2f}</speed>'
            f'<hdop>{t["hdop"]:.2f}</hdop></trkpt>'
        )
    parts += ['    </trkseg>', '  </trk>', '</gpx>', '']
    out_path.write_text("\n".join(parts))


def main() -> None:
    from courier_lens import (  # noqa: E402 — lazy: only needed for standalone run
        load_address_cache, load_breadcrumb_meta, load_coords, load_edges,
        reconstruct_route, resolve_address,
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--manifest-id", required=True)
    ap.add_argument("-d", "--date", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    centers = load_coords()
    addr_cache = load_address_cache()
    meta_by_manifest = load_breadcrumb_meta()
    edges_by_manifest = load_edges()

    key = (args.manifest_id, args.date)
    if key not in edges_by_manifest:
        raise SystemExit(
            f"no breadcrumbs found for manifest_id={args.manifest_id!r} "
            f"date={args.date!r}. Available: "
            f"{sorted(edges_by_manifest)}")
    route = reconstruct_route(edges_by_manifest[key])
    meta = meta_by_manifest.get(key, {})
    points: list[tuple[float, float]] = []
    for pc in route:
        rec = meta.get(pc, {})
        preferred = rec.get("entry") if not points else rec.get("exit")
        pt = resolve_address(pc, preferred, addr_cache) or centers.get(pc)
        if pt:
            points.append(pt)
    if len(points) < 2:
        raise SystemExit(f"too few resolvable points ({len(points)})")

    rng = random.Random(args.seed)
    ticks: list[dict] = []
    ts = EPOCH_START
    ts = _emit_dwell(points[0], ticks, ts, rng)
    for a, b in zip(points, points[1:]):
        ts = _emit_leg(a, b, ticks, ts, rng)
        ts = _emit_dwell(b, ticks, ts, rng)

    out = args.out or Path("/tmp") / f"synthetic_{args.manifest_id}_{args.date}.gpx"
    write_gpx(ticks, out, args.manifest_id, args.date)
    print(f"wrote {out}: {len(points)} stops → {len(ticks)} trkpts "
          f"({(ticks[-1]['ts'] - ticks[0]['ts']) / 60:.1f} min)")


if __name__ == "__main__":
    main()
