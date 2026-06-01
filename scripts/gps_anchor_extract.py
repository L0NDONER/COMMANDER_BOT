#!/usr/bin/env python3
import csv
import math
from pathlib import Path
from statistics import median
from collections import defaultdict

# 5 km/h in m/s — speed at or below this is treated as "on foot / committed
# to the pocket".  Phone GPS loggers emit speed in m/s.
WALK_SPEED_MS = 5 / 3.6   # 1.389 m/s

# ------------------------------------------------------------
# Haversine distance (metres)
# ------------------------------------------------------------
def hav(a, b):
    R = 6371000.0
    p = math.radians(b[0] - a[0])
    q = math.radians(b[1] - a[1])
    x = (math.sin(p/2)**2 +
         math.cos(math.radians(a[0])) *
         math.cos(math.radians(b[0])) *
         math.sin(q/2)**2)
    return 2 * R * math.asin(math.sqrt(x))

# ------------------------------------------------------------
# Load raw GPS CSV: timestamp, lat, lon, accuracy, speed, bearing
# ------------------------------------------------------------
def load_gps(path):
    out = []
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            lat   = float(row["lat"])
            lon   = float(row["lon"])
            t     = float(row["timestamp"])
            speed = float(row.get("speed") or 0)
            st    = row.get("street", "").strip()
            out.append((lat, lon, t, speed, st))
    out.sort(key=lambda x: x[2])
    return out

# ------------------------------------------------------------
# Segment into passes near a target centroid
# ------------------------------------------------------------
def extract_passes(gps, centroid, radius_m=80, gap_s=60):
    passes = []
    curr = []
    for lat, lon, t, speed, st in gps:
        d = hav((lat, lon), centroid)
        if d < radius_m:
            if curr and t - curr[-1][2] > gap_s:
                passes.append(curr)
                curr = []
            curr.append((lat, lon, t, speed))
        else:
            if curr:
                passes.append(curr)
                curr = []
    if curr:
        passes.append(curr)
    return passes

# ------------------------------------------------------------
# Directional vectoring: first point in each pass where the
# courier has slowed to walking pace — the moment of commitment
# to the residential pocket.  Falls back to the slowest point
# if no tick reaches walk threshold.
# ------------------------------------------------------------
def entry_points(passes):
    pts = []
    for p in passes:
        slow = next(
            ((lat, lon, t) for lat, lon, t, speed in p
             if speed <= WALK_SPEED_MS),
            None,
        )
        if slow is None:
            # No tick slow enough — take the minimum-speed tick as fallback.
            slow = min(p, key=lambda x: x[3])
            slow = (slow[0], slow[1], slow[2])
        pts.append(slow)
    return pts

# ------------------------------------------------------------
# Clustering: group points within radius metres, running centroid
# ------------------------------------------------------------
def _dist(a, b):
    return hav((a["lat"], a["lon"]), (b["lat"], b["lon"]))


def cluster_points(points, thresh=25):
    clusters = []
    for lat, lon, t in points:
        pt = {"lat": lat, "lon": lon}
        found = False
        for c in clusters:
            if _dist(pt, c) < thresh:
                c["points"].append(pt)
                c["count"] += 1
                c["lat"] = (c["lat"] * (c["count"] - 1) + lat) / c["count"]
                c["lon"] = (c["lon"] * (c["count"] - 1) + lon) / c["count"]
                found = True
                break
        if not found:
            clusters.append({"lat": lat, "lon": lon, "points": [pt], "count": 1})
    return clusters

# ------------------------------------------------------------
# Compute anchor = median of main cluster
# ------------------------------------------------------------
def anchor_from_clusters(clusters, min_pts=4):
    if not clusters:
        return None
    clusters.sort(key=lambda c: c["count"], reverse=True)
    main = clusters[0]
    if main["count"] < min_pts:
        return None
    lats = [p["lat"] for p in main["points"]]
    lons = [p["lon"] for p in main["points"]]
    return (median(lats), median(lons))

# ------------------------------------------------------------
# Main: build MOUTH_COORDS for each close
# ------------------------------------------------------------
def build_anchor_map(gps_path, close_centroids):
    gps = load_gps(gps_path)
    anchors = {}
    for close, centroid in close_centroids.items():
        passes = extract_passes(gps, centroid)
        pts = entry_points(passes)
        clusters = cluster_points(pts)
        anchor = anchor_from_clusters(clusters)
        if anchor:
            anchors[close] = anchor
    return anchors

# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("gps_csv", help="raw GPS trace CSV")
    ap.add_argument("centroids_json", help="JSON: {close: [lat, lon]}")
    ap.add_argument("-o", "--out", default="mouth_coords.json")
    args = ap.parse_args()

    close_centroids = {
        k: tuple(v)
        for k, v in json.load(open(args.centroids_json)).items()
    }

    anchors = build_anchor_map(args.gps_csv, close_centroids)
    with open(args.out, "w") as f:
        json.dump(anchors, f, indent=2)

    print(f"wrote {len(anchors)} anchors → {args.out}")
