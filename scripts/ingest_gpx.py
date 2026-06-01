"""Real GPS trace ingest — convert a phone-side GPX file into the JSON
schema scripts/courier_gps.py consumes.

The phone-side workflow:
  1. Run any Android GPS logger that exports GPX (GPS Logger by Mendhak
     is the recommended open-source option; OsmAnd, Strava, Garmin all
     work too). Configure it for ~1 Hz sampling and 'track in background'.
  2. After a delivery shift, export the GPX file (typically saved to
     /storage/emulated/0/GPSLogger/ on Android, share via Telegram or
     Google Drive to the dev box).
  3. Run: python3 scripts/ingest_gpx.py path/to/trace.gpx \\
              --manifest-id <id> --date YYYY-MM-DD
  4. Multivariant lens picks it up automatically on the next run.

Schema mapping:
  <trkpt lat lon>   → lat, lon
  <time>            → ts (seconds since epoch)
  <course>          → heading_deg  (else derived from consecutive positions)
  <speed>           → speed_mps    (else derived from Δposition/Δt)
  <hdop>            → accuracy_m   (multiplied by ~5m base GPS error)

Drops ticks whose accuracy_m exceeds --min-accuracy-m (default 50m =
worse than landmark-tier). Decimates by --decimate (default 1 = no thin)
to handle high-frequency loggers without blowing up the JSON.

Run:  python3 scripts/ingest_gpx.py trace.gpx --manifest-id X --date 2026-05-30
      python3 scripts/ingest_gpx.py trace.gpx -m X -d 2026-05-30 --decimate 5
"""
import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from courier_gps import GPS_TRACES_DIR, _haversine_m  # noqa: E402

DEFAULT_BASE_ACCURACY_M = 5.0  # multiplier for hdop → metres
FALLBACK_ACCURACY_M = 5.0       # used when no hdop tag present
GPX_NS = "{http://www.topografix.com/GPX/1/1}"
GPX_NS_10 = "{http://www.topografix.com/GPX/1/0}"


def _ns_tag(tag: str, root_tag: str) -> str:
    """Pick the right GPX namespace based on what the file declares."""
    if root_tag.startswith(GPX_NS):
        return f"{GPX_NS}{tag}"
    if root_tag.startswith(GPX_NS_10):
        return f"{GPX_NS_10}{tag}"
    return tag  # no namespace, use bare tag


def _parse_time(s: str) -> float:
    """ISO 8601 → seconds since epoch. Handles 'Z' and offset forms."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()


def _bearing_deg(lat1: float, lon1: float,
                 lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def parse_gpx(path: Path) -> list[dict]:
    """Return raw tick dicts from a GPX file. Heading/speed are filled
    from <course>/<speed> when present, else derived from consecutive
    positions in a second pass."""
    tree = ET.parse(path)
    root = tree.getroot()
    pt = _ns_tag("trkpt", root.tag)
    time_tag = _ns_tag("time", root.tag)
    course_tag = _ns_tag("course", root.tag)
    speed_tag = _ns_tag("speed", root.tag)
    hdop_tag = _ns_tag("hdop", root.tag)
    raw: list[dict] = []
    for tp in root.iter(pt):
        try:
            lat = float(tp.attrib["lat"])
            lon = float(tp.attrib["lon"])
        except (KeyError, ValueError):
            continue
        t_el = tp.find(time_tag)
        if t_el is None or not t_el.text:
            continue
        try:
            ts = _parse_time(t_el.text.strip())
        except ValueError:
            continue
        course = tp.find(course_tag)
        speed = tp.find(speed_tag)
        hdop = tp.find(hdop_tag)
        raw.append({
            "ts": ts, "lat": lat, "lon": lon,
            "heading_deg": float(course.text) if course is not None
                                                 and course.text else None,
            "speed_mps": float(speed.text) if speed is not None
                                               and speed.text else None,
            "accuracy_m": (float(hdop.text) * DEFAULT_BASE_ACCURACY_M
                           if hdop is not None and hdop.text
                           else FALLBACK_ACCURACY_M),
        })
    return raw


def fill_derived(raw: list[dict]) -> list[dict]:
    """Compute heading_deg / speed_mps from consecutive positions when
    the GPX didn't provide them. First tick gets the second tick's
    derived values back-propagated so it isn't a (0, 0) outlier."""
    for i in range(1, len(raw)):
        prev, cur = raw[i - 1], raw[i]
        dt = cur["ts"] - prev["ts"]
        if dt <= 0:
            continue
        d_m = _haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        derived_speed = d_m / dt
        derived_heading = (_bearing_deg(prev["lat"], prev["lon"],
                                        cur["lat"], cur["lon"])
                           if d_m > 0.5 else prev.get("_last_heading", 0.0))
        if cur["speed_mps"] is None:
            cur["speed_mps"] = derived_speed
        if cur["heading_deg"] is None:
            cur["heading_deg"] = derived_heading
        cur["_last_heading"] = cur["heading_deg"]
    # First tick: copy from tick 2 if we have one.
    if len(raw) >= 2:
        if raw[0]["heading_deg"] is None:
            raw[0]["heading_deg"] = raw[1]["heading_deg"]
        if raw[0]["speed_mps"] is None:
            raw[0]["speed_mps"] = raw[1]["speed_mps"]
    elif len(raw) == 1:
        raw[0].setdefault("heading_deg", 0.0)
        raw[0].setdefault("speed_mps", 0.0)
    for t in raw:
        t.pop("_last_heading", None)
    return raw


def filter_and_decimate(ticks: list[dict], min_accuracy_m: float,
                        decimate: int) -> list[dict]:
    kept = [t for t in ticks if t["accuracy_m"] <= min_accuracy_m]
    if decimate > 1:
        kept = kept[::decimate]
    return kept


def to_schema(ticks: list[dict]) -> list[dict]:
    return [{
        "ts": round(t["ts"], 3),
        "lat": round(t["lat"], 7),
        "lon": round(t["lon"], 7),
        "heading_deg": round(t["heading_deg"] % 360.0, 1),
        "speed_mps": round(max(0.0, t["speed_mps"]), 2),
        "accuracy_m": round(max(0.5, t["accuracy_m"]), 1),
    } for t in ticks]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx_path", type=Path)
    ap.add_argument("-m", "--manifest-id", required=True)
    ap.add_argument("-d", "--date", required=True,
                    help="YYYY-MM-DD — must match the breadcrumb date so "
                         "the lens picks up this trace for that route")
    ap.add_argument("--min-accuracy-m", type=float, default=50.0,
                    help="drop ticks worse than this (default 50m = "
                         "landmark tier of three-tier geo-locking)")
    ap.add_argument("--decimate", type=int, default=1,
                    help="keep every Nth tick (1 = no decimation)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + summarise without writing")
    args = ap.parse_args()

    if not args.gpx_path.exists():
        raise SystemExit(f"file not found: {args.gpx_path}")

    raw = parse_gpx(args.gpx_path)
    print(f"parsed {len(raw)} <trkpt> elements")
    if not raw:
        return
    fill_derived(raw)
    kept = filter_and_decimate(raw, args.min_accuracy_m, args.decimate)
    print(f"after filter (≤{args.min_accuracy_m}m) + decimate "
          f"(every {args.decimate}): {len(kept)} ticks")
    if not kept:
        print("(nothing to write — all ticks filtered out)")
        return

    span_secs = kept[-1]["ts"] - kept[0]["ts"]
    speeds = [t["speed_mps"] for t in kept]
    accs = [t["accuracy_m"] for t in kept]
    print(f"span: {span_secs / 60:.1f} min "
          f"({datetime.fromtimestamp(kept[0]['ts']).isoformat()} → "
          f"{datetime.fromtimestamp(kept[-1]['ts']).isoformat()})")
    print(f"speed: min {min(speeds):.2f}  median "
          f"{sorted(speeds)[len(speeds)//2]:.2f}  max {max(speeds):.2f} m/s")
    print(f"accuracy: min {min(accs):.1f}  median "
          f"{sorted(accs)[len(accs)//2]:.1f}  max {max(accs):.1f} m")

    if args.dry_run:
        print("(--dry-run: not writing)")
        return

    GPS_TRACES_DIR.mkdir(parents=True, exist_ok=True)
    out = GPS_TRACES_DIR / f"{args.manifest_id}_{args.date}.json"
    if out.exists():
        print(f"WARNING: overwriting existing {out.name}")
    payload = {"ticks": to_schema(kept)}
    with out.open("w") as f:
        json.dump(payload, f)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
