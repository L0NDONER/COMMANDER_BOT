#!/usr/bin/env python3
"""
osm_obstacles.py — fetch OSM obstacle geometry for each postcode and write
it into the 'landmarks' field of the postcode JSON.

Obstacle types pulled from Overpass:
  bollard        barrier=bollard nodes               size 0.3m
  building       building=* way nodes (corners)      size 1.5m
  kerb_node      highway way nodes (road edge)       size 0.4m
  narrow_mouth   highway=service way (tight entry)   size 1.0m
  turning_circle highway=turning_circle node         size 0.5m

Run:
  python3 scripts/osm_obstacles.py [--postcode NR19 1AD] [--radius 80] [--dry-run]

Without --postcode, processes all postcode JSONs in scripts/postcodes/.
Rate-limited to 1 request/second to avoid hammering Overpass.
Idempotent: re-running overwrites landmarks with fresh data.
"""
import argparse
import json
import math
import time
import urllib.parse
from pathlib import Path

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
POSTCODES_DIR = Path(__file__).parent / "postcodes"
RATE_LIMIT_S  = 2.0   # seconds between requests

# OSM tags → (landmark_type, size_m)
BARRIER_TAGS = {
    "bollard":     ("bollard",  0.3),
    "gate":        ("gate",     0.5),
    "block":       ("bollard",  0.4),
}
HIGHWAY_NODES = {
    "turning_circle": ("turning_circle", 0.5),
    "passing_place":  ("passing_place",  0.5),
}


def _bbox(lat: float, lon: float, radius_m: float) -> str:
    """Return Overpass bbox string: south,west,north,east."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"


def _build_query(bbox: str) -> str:
    return f"""
[out:json][timeout:25];
(
  node["barrier"~"bollard|gate|block"]({bbox});
  node["highway"~"turning_circle|passing_place"]({bbox});
  way["building"]({bbox});
  way["highway"~"service|residential|unclassified"]({bbox});
);
out body;
>;
out skel qt;
""".strip()


def fetch_overpass(lat: float, lon: float, radius_m: float) -> dict:
    bbox  = _bbox(lat, lon, radius_m)
    query = _build_query(bbox)
    for attempt in range(4):
        try:
            resp = requests.get(OVERPASS_URL, params={"data": query},
                                headers={"User-Agent": "commander-sim/1.0"},
                                timeout=35)
            if resp.status_code == 429:
                wait = 10 * (2 ** attempt)
                print(f"    rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < 3:
                time.sleep(5)
                continue
            raise
    resp.raise_for_status()
    return resp.json()


def _node_index(elements: list) -> dict:
    """id → {lat, lon} for all nodes."""
    return {e["id"]: (e["lat"], e["lon"])
            for e in elements if e["type"] == "node"}


def extract_obstacles(data: dict, ref_lat: float, ref_lon: float,
                      radius_m: float) -> list[dict]:
    """
    Parse Overpass response into a flat list of obstacle dicts:
      {type, lat, lon, size, dx_m, dy_m}
    dx_m/dy_m are local-frame offsets from the postcode centroid.
    """
    elements  = data.get("elements", [])
    nodes     = _node_index(elements)
    obstacles = []

    def proj(lat, lon):
        x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * 6_371_000.0
        y = math.radians(lat - ref_lat) * 6_371_000.0
        return round(x, 2), round(y, 2)

    def dist(lat, lon):
        dlat = math.radians(lat - ref_lat)
        dlon = math.radians(lon - ref_lon)
        a = (math.sin(dlat/2)**2
             + math.cos(math.radians(ref_lat)) * math.cos(math.radians(lat))
             * math.sin(dlon/2)**2)
        return 6_371_000.0 * 2 * math.asin(math.sqrt(a))

    seen = set()

    for e in elements:
        if e["type"] == "node":
            tags = e.get("tags", {})
            barrier = tags.get("barrier", "")
            hway    = tags.get("highway", "")
            ob_type = size = None

            if barrier in BARRIER_TAGS:
                ob_type, size = BARRIER_TAGS[barrier]
            elif hway in HIGHWAY_NODES:
                ob_type, size = HIGHWAY_NODES[hway]

            if ob_type and dist(e["lat"], e["lon"]) <= radius_m:
                key = (round(e["lat"], 6), round(e["lon"], 6))
                if key not in seen:
                    seen.add(key)
                    dx, dy = proj(e["lat"], e["lon"])
                    obstacles.append({
                        "type": ob_type,
                        "lat": e["lat"], "lon": e["lon"],
                        "size": size, "dx_m": dx, "dy_m": dy,
                    })

        elif e["type"] == "way":
            tags     = e.get("tags", {})
            building = tags.get("building", "")
            hway     = tags.get("highway", "")

            if building:
                # Sample every other corner node — enough for wall detection
                way_nodes = e.get("nodes", [])
                for nid in way_nodes[::2]:
                    if nid not in nodes:
                        continue
                    nlat, nlon = nodes[nid]
                    if dist(nlat, nlon) > radius_m:
                        continue
                    key = (round(nlat, 6), round(nlon, 6))
                    if key not in seen:
                        seen.add(key)
                        dx, dy = proj(nlat, nlon)
                        obstacles.append({
                            "type": "building",
                            "lat": nlat, "lon": nlon,
                            "size": 1.5, "dx_m": dx, "dy_m": dy,
                        })

            elif hway in ("service", "residential", "unclassified"):
                # Tag narrow entry nodes (service roads ≤ 3m wide)
                width = float(tags.get("width", tags.get("est_width", "0")) or 0)
                if hway == "service" or (width > 0 and width <= 4.0):
                    ob_type = "narrow_mouth" if hway == "service" else "kerb_node"
                    sz      = 1.0 if hway == "service" else 0.4
                    for nid in e.get("nodes", []):
                        if nid not in nodes:
                            continue
                        nlat, nlon = nodes[nid]
                        if dist(nlat, nlon) > radius_m:
                            continue
                        key = (round(nlat, 6), round(nlon, 6))
                        if key not in seen:
                            seen.add(key)
                            dx, dy = proj(nlat, nlon)
                            obstacles.append({
                                "type": ob_type,
                                "lat": nlat, "lon": nlon,
                                "size": sz, "dx_m": dx, "dy_m": dy,
                            })

    return obstacles


def process_postcode(pc_path: Path, radius_m: float, dry_run: bool) -> int:
    d = json.load(open(pc_path))
    coords = d.get("coords")
    if not coords or not coords[0]:
        return 0

    lat, lon = coords
    try:
        data = fetch_overpass(lat, lon, radius_m)
    except Exception as exc:
        print(f"  SKIP {d['postcode']}: {exc}")
        return 0

    obstacles = extract_obstacles(data, lat, lon, radius_m)

    summary = {}
    for ob in obstacles:
        summary[ob["type"]] = summary.get(ob["type"], 0) + 1
    print(f"  {d['postcode']:12}  {len(obstacles):3} obstacles  {summary}")

    if not dry_run:
        d["landmarks"] = obstacles
        with open(pc_path, "w") as f:
            json.dump(d, f, indent=2)

    return len(obstacles)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--postcode", help="single postcode to process (e.g. 'NR19 1AD')")
    ap.add_argument("--radius",   type=float, default=80.0,
                    help="search radius in metres (default 80)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="fetch and print but do not write JSON")
    args = ap.parse_args()

    if args.postcode:
        fn = args.postcode.replace(" ", "_") + ".json"
        paths = [POSTCODES_DIR / fn]
    else:
        paths = sorted(POSTCODES_DIR.glob("*.json"))

    print(f"Processing {len(paths)} postcode(s), radius={args.radius}m"
          + (" [DRY RUN]" if args.dry_run else ""))

    total = 0
    for i, path in enumerate(paths):
        if i > 0:
            time.sleep(RATE_LIMIT_S)
        total += process_postcode(path, args.radius, args.dry_run)

    print(f"\nDone. {total} obstacles written across {len(paths)} postcodes.")


if __name__ == "__main__":
    main()
