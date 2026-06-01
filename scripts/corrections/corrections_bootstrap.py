"""Bootstrap scripts/corrections/corrections.json from the breadcrumb-paired GPS traces.

For each manifest with both breadcrumb meta and a paired GPS trace:
  1. Reconstruct the postcode route from edges.
  2. Run courier_gps.infer_stops on the trace to get dwell centroids.
  3. For each postcode in the route, find the nearest inferred stop within
     a tolerance (postcode-to-stop pairing radius).
  4. Take the tail-60s endpoint of that stop's tick window.
  5. Write the endpoint as a correction keyed by `{NORM_ADDR}, {POSTCODE}`
     for both the entry and exit addresses bound to that postcode.

Single-visit data → corrections.update() builds count=1 entries; future
re-runs on additional traces will refine via running-average.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # commander/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))      # scripts/corrections/

from corrections import CorrectionTable, extract_stop_endpoint, haversine  # noqa: E402
from courier_gps import (  # noqa: E402
    discover_trace_manifests, infer_stops, load_trace,
)
from courier_lens import (  # noqa: E402
    _norm_addr, load_breadcrumb_meta, load_coords, load_edges,
    reconstruct_route,
)

CORRECTIONS_PATH = Path(__file__).resolve().parent / "corrections.json"
PAIRING_RADIUS_M = 150.0  # postcode centroid → nearest inferred stop
TAIL_SECONDS = 60.0


def nearest_stop(stops, centre, radius_m):
    """Return (stop, distance) of the closest inferred stop within radius."""
    best = None
    best_d = float("inf")
    for s in stops:
        d = haversine(centre, (s.lat, s.lon))
        if d < best_d and d <= radius_m:
            best_d = d
            best = s
    return (best, best_d) if best else (None, None)


def stop_breadcrumbs(trace, stop, tail_seconds):
    """Ticks belonging to the stop's dwell window, formatted for
    extract_stop_endpoint."""
    ticks = [t for t in trace.ticks
             if stop.start_ts <= t.ts <= stop.end_ts]
    return [{"lat": t.lat, "lon": t.lon, "t": t.ts} for t in ticks]


def bootstrap() -> dict:
    """Run the bootstrap; return summary stats for printing."""
    edges_by = load_edges()
    meta_by = load_breadcrumb_meta()
    centers = load_coords()
    trace_keys = set(discover_trace_manifests())
    table = CorrectionTable(str(CORRECTIONS_PATH))

    eligible = sorted(set(edges_by.keys()) & trace_keys)
    summary = {
        "manifests": 0, "postcodes_seen": 0, "postcodes_paired": 0,
        "addresses_written": 0, "skipped_no_centre": 0,
        "skipped_no_stop": 0, "skipped_no_addr": 0,
    }

    for key in eligible:
        mid, date = key
        route = reconstruct_route(edges_by[key])
        if len(route) < 1:
            continue
        meta = meta_by.get(key, {})
        trace = load_trace(mid, date)
        stops = infer_stops(trace)
        if not stops:
            continue
        summary["manifests"] += 1
        print(f"\n=== {mid}  {date}  "
              f"route={len(route)}pcs  inferred_stops={len(stops)} ===")

        # Track which inferred stop got paired to which postcode-in-route
        # ordinal so a postcode appearing twice doesn't always grab the
        # same dwell.
        used_stop_idx: set[int] = set()

        for pc in route:
            summary["postcodes_seen"] += 1
            centre = centers.get(pc)
            if centre is None:
                summary["skipped_no_centre"] += 1
                continue
            # Pick the nearest still-unused inferred stop within radius.
            best, best_d, best_idx = None, float("inf"), -1
            for i, s in enumerate(stops):
                if i in used_stop_idx:
                    continue
                d = haversine(centre, (s.lat, s.lon))
                if d < best_d and d <= PAIRING_RADIUS_M:
                    best, best_d, best_idx = s, d, i
            if best is None:
                summary["skipped_no_stop"] += 1
                continue
            used_stop_idx.add(best_idx)
            summary["postcodes_paired"] += 1

            bc = stop_breadcrumbs(trace, best, TAIL_SECONDS)
            endpoint = extract_stop_endpoint(bc, TAIL_SECONDS)
            if endpoint is None:
                continue

            rec = meta.get(pc, {})
            for field in ("entry", "exit"):
                addr = rec.get(field)
                if not addr:
                    continue
                k = f"{_norm_addr(addr)}, {pc}"
                table.update(k, endpoint[0], endpoint[1],
                             timestamp=best.start_ts)
                summary["addresses_written"] += 1
            if not rec.get("entry") and not rec.get("exit"):
                summary["skipped_no_addr"] += 1

    table.save()
    return summary


def main() -> None:
    s = bootstrap()
    print(f"\n--- bootstrap summary ---")
    for k, v in s.items():
        print(f"  {k:<22}  {v}")
    print(f"  corrections file: {CORRECTIONS_PATH}")


if __name__ == "__main__":
    main()
