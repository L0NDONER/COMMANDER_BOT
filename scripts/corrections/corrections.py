"""Learned door-correction table.

UK free geocoders (Nominatim, OSM Overpass) collapse every address inside
a postcode to the road/centroid. This module learns the actual van-stop
endpoint per stable key (address string or postcode+house) from the tail
of past breadcrumb tracks and writes it as a correction that overrides
the geocoder going forward.

Lookup precedence is:
  corrections table  >  geocoder result  >  postcode centroid
so calling code only needs to pass the table through; absence is a no-op.
"""
import json
import math
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------- #
#  Geo helpers
# --------------------------------------------------------------------- #

def haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6371000.0
    p = math.radians(lat2 - lat1)
    q = math.radians(lon2 - lon1)
    x = (math.sin(p / 2.0) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(q / 2.0) ** 2)
    return 2.0 * R * math.asin(math.sqrt(x))


def mean_point(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    if not points:
        raise ValueError("mean_point() called with empty list")
    sx = 0.0
    sy = 0.0
    for lat, lon in points:
        sx += lat
        sy += lon
    n = float(len(points))
    return sx / n, sy / n


# --------------------------------------------------------------------- #
#  Data structures
# --------------------------------------------------------------------- #

class CorrectionEntry:
    def __init__(self, lat: float, lon: float, count: int = 1,
                 last_used: Optional[float] = None):
        self.lat = lat
        self.lon = lon
        self.count = count
        self.last_used = last_used

    def to_dict(self) -> Dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "count": self.count,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "CorrectionEntry":
        return cls(
            lat=d["lat"],
            lon=d["lon"],
            count=d.get("count", 1),
            last_used=d.get("last_used"),
        )


class CorrectionTable:
    """Keyed by a stable identifier for a stop (full address string, or
    postcode + house number)."""

    def __init__(self, path: str):
        self.path = path
        self.entries: Dict[str, CorrectionEntry] = {}
        self._load()

    # -------- persistence --------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = {}
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.entries = {k: CorrectionEntry.from_dict(v)
                        for k, v in raw.items()}

    def save(self) -> None:
        raw = {k: v.to_dict() for k, v in self.entries.items()}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -------- lookup / update --------

    def get(self, key: str) -> Optional[Tuple[float, float]]:
        entry = self.entries.get(key)
        if not entry:
            return None
        return entry.lat, entry.lon

    def update(self, key: str, lat: float, lon: float,
               timestamp: Optional[float] = None) -> None:
        entry = self.entries.get(key)
        if entry is None:
            self.entries[key] = CorrectionEntry(
                lat=lat, lon=lon, count=1, last_used=timestamp)
        else:
            # Running average over n observations.
            n = float(entry.count)
            entry.lat = (entry.lat * n + lat) / (n + 1.0)
            entry.lon = (entry.lon * n + lon) / (n + 1.0)
            entry.count += 1
            entry.last_used = timestamp


# --------------------------------------------------------------------- #
#  Learning corrections from breadcrumb tails
# --------------------------------------------------------------------- #

def extract_stop_endpoint(breadcrumbs: List[Dict],
                          tail_seconds: float = 60.0
                          ) -> Optional[Tuple[float, float]]:
    """Mean position of the last `tail_seconds` of breadcrumbs at a stop.
    Each breadcrumb is {"lat", "lon", "t"} (t in seconds since epoch or
    route start). Falls back to the last single point if the tail window
    captures nothing."""
    if not breadcrumbs:
        return None
    t_last = breadcrumbs[-1]["t"]
    cutoff = t_last - tail_seconds
    tail_points: List[Tuple[float, float]] = [
        (b["lat"], b["lon"]) for b in breadcrumbs if b["t"] >= cutoff
    ]
    if not tail_points:
        tail_points.append(
            (breadcrumbs[-1]["lat"], breadcrumbs[-1]["lon"]))
    return mean_point(tail_points)


def learn_corrections_from_routes(
    table: CorrectionTable,
    routes: List[Dict],
    geocoder_fn,
    min_visits: int = 2,
    min_offset_m: float = 40.0,
    max_cluster_radius_m: float = 30.0,
) -> None:
    """Walk every (route, stop) pair, harvest endpoint clusters per stable
    key, and write a correction when the cluster (a) holds together within
    `max_cluster_radius_m` and (b) sits more than `min_offset_m` from the
    geocoder hit. Single visits are ignored (`min_visits`)."""
    endpoints: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for route in routes:
        for stop in route.get("stops", []):
            key = stop.get("key")
            if not key:
                continue
            bc = stop.get("breadcrumbs") or []
            endpoint = extract_stop_endpoint(bc)
            if endpoint is None:
                continue
            endpoints[key].append(endpoint)

    for key, pts in endpoints.items():
        if len(pts) < min_visits:
            continue
        centre = mean_point(pts)
        max_r = max(haversine(centre, p) for p in pts)
        if max_r > max_cluster_radius_m:
            continue  # too spread out — not a stable entrance
        geo = geocoder_fn(key)
        offset = min_offset_m + 1.0 if geo is None else haversine(centre, geo)
        if offset < min_offset_m:
            continue  # geocoder already close enough
        table.update(key, centre[0], centre[1], timestamp=None)


# --------------------------------------------------------------------- #
#  Applying corrections
# --------------------------------------------------------------------- #

def resolve_location(key: str,
                     table: CorrectionTable,
                     geocoder_fn) -> Optional[Tuple[float, float]]:
    """Best location for a stable key: corrected entrance if learned,
    else geocoder fallback."""
    corrected = table.get(key)
    if corrected is not None:
        return corrected
    return geocoder_fn(key)


# --------------------------------------------------------------------- #
#  Example wiring
# --------------------------------------------------------------------- #

def _example_geocoder_fn(key: str) -> Optional[Tuple[float, float]]:
    return None


def main():
    table = CorrectionTable(path="corrections.json")
    routes: List[Dict] = []
    learn_corrections_from_routes(
        table=table, routes=routes,
        geocoder_fn=_example_geocoder_fn,
    )
    table.save()
    key = "Cemetery Gardens, Dereham NR19 1AD"
    loc = resolve_location(key, table, _example_geocoder_fn)
    print("Resolved location for", key, "->", loc)


if __name__ == "__main__":
    main()
