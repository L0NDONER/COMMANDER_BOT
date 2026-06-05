"""Street-level topology tags for the Dereham manifest.

Curated by hand from local courier knowledge. Name-suffix heuristics
("Close", "Court") are unreliable — see Highfield *Road* (dead-end)
and "Court"-suffixed streets that flow through.

ANCHOR_MAP keys are street names matched as case- and
punctuation-insensitive substrings of each manifest line. Variants
like "Oak Apple Drive" ↔ "Oakapple Drive" and "De Narde Road" ↔
"De-narde Road" resolve via the same normaliser.

Tag semantics — what each tag does to the *score* at that cluster:
  TYPE_THROUGH — raw angular cost passes through.
  TYPE_CLOSE   — masked to 0. Mouth-anchor: vehicle parks at the
                 entrance, walks the close, leaves toward the next
                 node. The 180° flip is structural, unavoidable, and
                 not something reordering can fix.
  TYPE_HYBRID  — capped at 1.0 (the 90° equivalent). Mid-point park:
                 drive part-way in, walk both halves, drive out — a
                 three-point turn at most, not a U-turn.

The mask only changes the *score*, not the *route*. Until
MOUTH_COORDS is populated from GPS traces (per [[courier-gps-ingest]]),
every cluster still resolves to its postcode centroid.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

TYPE_THROUGH = "TYPE_THROUGH"
TYPE_CLOSE = "TYPE_CLOSE"
TYPE_HYBRID = "TYPE_HYBRID"


ANCHOR_MAP: dict[str, str] = {
    "Boton Drive":      TYPE_THROUGH,
    "Acorn Way":        TYPE_THROUGH,
    "Armstrong Drive":  TYPE_HYBRID,
    "Bulwer Road":      TYPE_THROUGH,
    "Warner Avenue":    TYPE_THROUGH,
    "Wollaston Drive":  TYPE_HYBRID,
    "Stigands Gate":    TYPE_CLOSE,
    "Oak Apple Drive":  TYPE_HYBRID,
    "Oakwood Road":     TYPE_HYBRID,
    "Oakwood Close":    TYPE_CLOSE,
    "De Narde Road":    TYPE_THROUGH,
    "Townshend Road":   TYPE_THROUGH,
    "Raynham Ride":     TYPE_THROUGH,
    "Chase Court":      TYPE_CLOSE,
    "Magpie Court":     TYPE_CLOSE,
    "Sheddick Court":   TYPE_CLOSE,
    "Windsor Park":     TYPE_THROUGH,
    "September House":        TYPE_CLOSE,
    "Rabbit Foot Barn":       TYPE_CLOSE,
    "Dillington Hall":        TYPE_CLOSE,
    "Glebe Cottage":          TYPE_CLOSE,
    "Gingerbread Cottages":   TYPE_CLOSE,
    "Quebec Hall Bungalows":  TYPE_CLOSE,
    "Stanton Close":    TYPE_CLOSE,
    "St Hilda Close":   TYPE_CLOSE,
    "St Hilda Road":    TYPE_CLOSE,
    "William O'Callaghan Place": TYPE_CLOSE,
    "Links View":       TYPE_HYBRID,
    "Heath Road":       TYPE_THROUGH,
    "Keats Close":      TYPE_CLOSE,
}


# Mouth coordinate semantics (TYPE_CLOSE and TYPE_HYBRID streets):
#
#   The mouth is the LAST POINT ON THE THROUGH-ROAD before the street
#   becomes a residential pocket — not any house in the pocket, and
#   not the cul-de-sac head.
#
#   Correct placement forces the Greedy_Angle_Aware sequencer to treat
#   everything beyond the mouth as a single high-cost block, entered
#   and exited once. Placing the mouth too deep (e.g. at the turning
#   head) leaks interior geometry back into the cost function and
#   defeats the mask.
#
#   Without a GPS trace, pick the coord manually: stand at the point
#   where a driver on the through road would commit to the dead-end.
#   That is the mouth.
#
#   Populated from GPS traces via scripts/gps_anchor_extract.py.
#   Falls back to postcode centroid when absent.
MOUTH_COORDS: dict[str, tuple[float, float]] = {}

_mouth_path = Path(__file__).parent / "mouth_coords.json"
if _mouth_path.exists():
    try:
        MOUTH_COORDS = json.load(open(_mouth_path))
    except Exception:
        MOUTH_COORDS = {}


LIGHTING_HIGH   = "HIGH"
LIGHTING_MEDIUM = "MEDIUM"
LIGHTING_LOW    = "LOW"

# Street-lighting level per route zone.
# HIGH   — well-lit urban streets; no pace adjustment needed.
# MEDIUM — partial lighting; caution on foot after dark.
# LOW    — unlit rural; requires a buffer after sunset (almanac.is_dark).
# Zones not listed default to MEDIUM.
ZONE_LIGHTING: dict[str, str] = {
    "dereham_centre":    LIGHTING_HIGH,
    "north_dereham_a":   LIGHTING_HIGH,
    "quebec_hall":       LIGHTING_LOW,    # cul-de-sac, pitch black
    "windsor_park":      LIGHTING_LOW,    # through-street, pitch black
    "gressenhall_depot": LIGHTING_MEDIUM,
}


def lighting(zone: str) -> str:
    """Return the lighting level for a route zone. Defaults to MEDIUM."""
    return ZONE_LIGHTING.get(zone, LIGHTING_MEDIUM)


# Road-level risk weights.
# Keys are snake_case road names (spaces → underscores, lower-cased).
# Applied to the raw angular cost in calculate_segment_cost — a weight
# of 3.0 means the sequencer treats a U-turn on that road as 3× as
# costly as a U-turn on a baseline road.
ROAD_RISK: dict[str, float] = {
    "sandy_lane":    3.0,   # narrow, ungritted, high-penalty
    "neatherd_road": 1.0,   # primary gritted route — baseline
    "quebec_road":   1.0,   # primary gritted route — baseline
}

# Normalisation for ROAD_RISK keys: lower-case, spaces/hyphens → underscores.
def _risk_normalise(s: str) -> str:
    import re as _re
    return _re.sub(r"[\s\-\.]+", "_", s.lower()).strip("_")

_RISK_MAP = {_risk_normalise(k): v for k, v in ROAD_RISK.items()}


# Throat constraints — flat time penalties (seconds) for egress difficulty.
# Applied when LEAVING a zone: time lost waiting for a gap, difficult sight-
# lines, awkward pull-out onto a busier road.  Additive, not multiplicative.
# Keys are snake_case zone/road names (same normalisation as ROAD_RISK).
# Road status constants.
BLOCKED           = "BLOCKED"           # road unavailable — infinite cost
PRIMARY_ARTERIAL  = "PRIMARY_ARTERIAL"  # main gritted/managed route

# Live detour map — overrides the default cost for a named road or corridor.
# Keys use the same snake_case normalisation as ROAD_RISK / THROAT_CONSTRAINTS.
# Updated at planning time when closures or conditions are known.
DETOUR_MAP: dict[str, str] = {
    "sandy_lane_exit":               BLOCKED,
    "quebec_to_townsend_corridor":   PRIMARY_ARTERIAL,
}

_DETOUR_NORM = {_risk_normalise(k): v for k, v in DETOUR_MAP.items()}

# Base transit time (seconds) assumed for a PRIMARY_ARTERIAL leg when no
# distance is available — caller should prefer distance/speed where possible.
_BASE_TRANSIT_SECS = 60.0
_ARTERIAL_QUEUE_SECS = 30.0   # intersection queue allowance on arterials


def road_cost_modifier(road_name: str,
                       base_secs: float = _BASE_TRANSIT_SECS,
                       dt: datetime.datetime | None = None) -> float:
    """Return the effective cost (seconds) for a named road segment.

    BLOCKED          → float('inf')  — removes the road from the graph.
    PRIMARY_ARTERIAL → base_secs + intersection_delay + _ARTERIAL_QUEUE_SECS.
    Unlisted         → base_secs (no override).

    Prefix-matches against DETOUR_MAP keys so 'sandy_lane' resolves
    'sandy_lane_exit' etc."""
    key = _risk_normalise(road_name)
    status = _DETOUR_NORM.get(key)
    if status is None:
        for k, v in _DETOUR_NORM.items():
            if k.startswith(key) or key.startswith(k):
                status = v
                break
    if status is None:
        return base_secs
    if status == BLOCKED:
        return float("inf")
    if status == PRIMARY_ARTERIAL:
        return base_secs + intersection_delay(road_name, dt) + _ARTERIAL_QUEUE_SECS
    return base_secs


THROAT_CONSTRAINTS: dict[str, dict] = {
    "windsor_park_entrance": {"penalty": 120},  # 120s egress difficulty
    "quebec_hall_drive":     {"penalty": 180},  # hard pull-out onto Quebec Rd
    "sandy_lane_exit":       {"penalty":  90},
}

_THROAT_MAP = {_risk_normalise(k): v for k, v in THROAT_CONSTRAINTS.items()}


def throat_penalty(zone_name: str) -> float:
    """Return the egress time penalty (seconds) for a zone, or 0.0.
    Matches exact keys first, then falls back to prefix match so that
    'windsor_park' resolves 'windsor_park_entrance' etc."""
    key = _risk_normalise(zone_name)
    if key in _THROAT_MAP:
        return float(_THROAT_MAP[key]["penalty"])
    for constraint_key, entry in _THROAT_MAP.items():
        if constraint_key.startswith(key):
            return float(entry["penalty"])
    return 0.0


INTERSECTION_CONSTRAINTS: dict[str, dict] = {
    "sandy_lane_dereham_rd": {
        "pedestrian_crossing": True,
        "base_delay": 45,
        "peak_hours": [["08:00", "09:00"], ["15:00", "16:00"], ["20:00", "21:00"]],
    },
}

_INTERSECTION_MAP = {_risk_normalise(k): v
                     for k, v in INTERSECTION_CONSTRAINTS.items()}


def intersection_delay(zone_name: str,
                       dt: datetime.datetime | None = None) -> float:
    """Return the intersection delay (seconds) for a zone at a given time.
    Applies base_delay when within a peak_hours window; 0.0 otherwise.
    Prefix-matches zone names against constraint keys (same as throat_penalty)."""
    import zoneinfo as _zi
    tz = _zi.ZoneInfo("Europe/London")
    if dt is None:
        dt = datetime.datetime.now(tz=tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    local_time = dt.astimezone(tz).time()

    key = _risk_normalise(zone_name)
    entry = _INTERSECTION_MAP.get(key)
    if entry is None:
        for k, v in _INTERSECTION_MAP.items():
            if k.startswith(key) or key.startswith(k):
                entry = v
                break
    if entry is None:
        return 0.0
    if _time_in_window(local_time, entry["peak_hours"]):
        return float(entry["base_delay"])
    return 0.0


def calculate_segment_cost(segment_name: str, base_angle_cost: float) -> float:
    """Apply the road risk factor to a raw angular cost.
    segment_name should be the road name (any casing/spacing — normalised
    internally). Returns base_angle_cost unchanged for unlisted roads."""
    risk = _RISK_MAP.get(_risk_normalise(segment_name), 1.0)
    return base_angle_cost * risk


def detour_key(addresses: list[str]) -> str | None:
    """Return the first DETOUR_MAP key whose road-name component appears
    in any address line. Qualifier suffixes (_exit, _entrance, _drive,
    _corridor) are stripped before matching so 'sandy_lane_exit' matches
    an address containing 'sandy_lane'."""
    _QUALIFIERS = {"exit", "entrance", "drive", "corridor", "rd", "road"}

    def _road_stem(key: str) -> str:
        parts = key.split("_")
        while parts and parts[-1] in _QUALIFIERS:
            parts.pop()
        return "_".join(parts)

    for line in addresses:
        n = _risk_normalise(line)
        for key in _DETOUR_NORM:
            stem = _road_stem(key)
            if stem and stem in n:
                return key
    return None


def risk_key(addresses: list[str]) -> str | None:
    """Return the first road name from `addresses` that has an entry in
    ROAD_RISK, or None if none match. Used by greedy_angle to look up
    the risk weight for a cluster without re-parsing addresses."""
    for line in addresses:
        n = _risk_normalise(line)
        for key in _RISK_MAP:
            if key in n:
                return key
    return None


PROFILES: dict[str, dict] = {
    "DEREHAM_URBAN": {
        "apply_school_multiplier":    True,
        "use_throat_penalties":       True,
        "enforce_peak_hour_avoidance": True,
    },
    "RURAL_COLLECTIVE": {
        "apply_school_multiplier":    False,
        "use_throat_penalties":       False,
        "enforce_peak_hour_avoidance": False,
    },
}

DEFAULT_PROFILE = "DEREHAM_URBAN"


def get_profile(name: str | None = None) -> dict:
    """Return the named profile dict, falling back to DEFAULT_PROFILE."""
    return PROFILES.get(name or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE])


def apply_profile_penalties(
        zone_name: str,
        latlon: tuple[float, float],
        dt: datetime.datetime | None = None,
        profile_name: str | None = None) -> float:
    """Return total additive time penalty (seconds) for a zone under the
    active profile.  Callers convert to equivalent metres as needed."""
    profile = get_profile(profile_name)
    total = 0.0
    if profile["use_throat_penalties"]:
        total += throat_penalty(zone_name)
    if profile["enforce_peak_hour_avoidance"]:
        total += intersection_delay(zone_name, dt)
    return total


def apply_profile_multiplier(
        zone_name: str,
        latlon: tuple[float, float],
        is_dark: bool = False,
        dt: datetime.datetime | None = None,
        profile_name: str | None = None) -> float:
    """Return combined multiplicative cost factor for a zone under the
    active profile."""
    profile = get_profile(profile_name)
    m = 1.0
    if profile["apply_school_multiplier"]:
        m *= school_multiplier(latlon, dt)
    return m


SCHOOL_ZONES: list[dict] = [
    {
        "name": "Northgate High School",
        "polygon": [
            (52.68194, 0.94442),
            (52.68163, 0.94498),
            (52.68122, 0.94533),
            (52.68096, 0.94508),
            (52.68128, 0.94447),
        ],
        "active": [("08:00", "09:15"), ("14:45", "16:15")],
        "school_multiplier": 1.35,
        "low_light_multiplier": 1.15,
    },
]


def _point_in_polygon(lat: float, lon: float,
                      polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test. polygon is [(lat, lon), ...]."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _time_in_window(t: datetime.time, windows: list[tuple[str, str]]) -> bool:
    for start_s, end_s in windows:
        start = datetime.time.fromisoformat(start_s)
        end   = datetime.time.fromisoformat(end_s)
        if start <= t <= end:
            return True
    return False


def school_multiplier(latlon: tuple[float, float],
                      dt: datetime.datetime | None = None) -> float:
    """Return the highest applicable school-zone multiplier for a position
    at the given datetime (defaults to now, Europe/London).
    Returns 1.0 when outside all zones or outside all active windows."""
    import zoneinfo as _zi
    tz = _zi.ZoneInfo("Europe/London")
    if dt is None:
        dt = datetime.datetime.now(tz=tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    local_time = dt.astimezone(tz).time()
    lat, lon = latlon
    mult = 1.0
    for zone in SCHOOL_ZONES:
        if not _point_in_polygon(lat, lon, zone["polygon"]):
            continue
        if not _time_in_window(local_time, zone["active"]):
            continue
        m = zone["school_multiplier"]
        mult = max(mult, m)
    return mult


def _normalise(s: str) -> str:
    return re.sub(r"[\s\-\.]+", "", s.lower())


_NORMALISED_MAP = {_normalise(k): v for k, v in ANCHOR_MAP.items()}


def classify(addresses: list[str]) -> str:
    """Return the topology tag for a cluster given the address
    lines that belong to it. TYPE_THROUGH is the default."""
    for line in addresses:
        n = _normalise(line)
        for needle, tag in _NORMALISED_MAP.items():
            if needle in n:
                return tag
    return TYPE_THROUGH


def mask_cost(raw_cost: float, tag: str) -> float:
    """Apply the structural mask. Raw cost is the centroid-based
    angular cost (1 - cos θ). The masked cost reflects what
    reordering could plausibly fix."""
    if tag == TYPE_CLOSE:
        return 0.0
    if tag == TYPE_HYBRID:
        return min(raw_cost, 1.0)
    return raw_cost


def cluster_anchor(postcode: str,
                   centroid: tuple[float, float]
                   ) -> tuple[float, float]:
    """Park-point coord if known, else the postcode centroid."""
    return MOUTH_COORDS.get(postcode, centroid)
