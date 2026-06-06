#!/usr/bin/env python3
"""
route_optimiser.py — bubble-based route sequencer with throat awareness.

Pipeline:
  stops → make_bubbles → classify_throats → sequence_bubble → full_route

A bubble is a tight geographic cluster of stops (one residential close or
pocket). Throat classification runs Van360.throat_probe at each stop entry
heading. Sequencing puts throat stops last-in/first-out so the van never
has to U-turn inside one.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from courier_gps import Van360, Vec2, _latlon_to_xy
from geocoder import geocode_address

BUBBLE_RADIUS_M = 120.0   # stops within this radius form one bubble
VAN_TURN_RADIUS = 6.0
VAN_SENSE_RADIUS = 4.0
THROAT_STEP_M   = 2.0
THROAT_STEPS    = 6


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Stop:
    label: str
    position: Vec2
    postcode: str = ""
    address: str  = ""
    # set by classify_throats
    throat_depth: Optional[int] = None   # step index where U-turn is lost
    uturn_side: Optional[str]   = None   # 'left' | 'right' | None


@dataclass
class VanState:
    position: Vec2
    heading: float   # radians


@dataclass
class Bubble:
    stops: List[Stop] = field(default_factory=list)

    @property
    def centroid(self) -> Vec2:
        xs = [s.position.x for s in self.stops]
        ys = [s.position.y for s in self.stops]
        return Vec2(sum(xs) / len(xs), sum(ys) / len(ys))


@dataclass
class Segment:
    street: str
    stops: List[Stop]
    ascending: bool
    side: str = 'both'   # 'left' | 'right' | 'both'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dist(a: Vec2, b: Vec2) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def heading_from_to(a: Vec2, b: Vec2) -> float:
    """Heading in radians from a to b (math convention, 0 = east)."""
    return math.atan2(b.y - a.y, b.x - a.x)


def stops_from_postcodes(postcode_data: dict) -> List[Stop]:
    """
    Build Stop list from a {postcode: {...}} dict (loaded from JSON files).
    Uses the first known address as label; projects to local metres from the
    median centroid of all stops so Vec2 coordinates are comparable.
    """
    coords = [(d['coords'][0], d['coords'][1])
              for d in postcode_data.values()
              if d.get('coords') and d['coords'][0]]
    if not coords:
        return []
    ref_lat = sum(c[0] for c in coords) / len(coords)
    ref_lon = sum(c[1] for c in coords) / len(coords)

    stops = []
    for pc, d in postcode_data.items():
        if not (d.get('coords') and d['coords'][0]):
            continue
        addrs = d.get('known_addresses') or []
        addr  = addrs[0].get('building_street', '') if addrs else ''

        geo = geocode_address(addr, pc, ref_lat, ref_lon) if addr else None
        if geo:
            pos = geo['vec2']
        else:
            lat, lon = d['coords']
            pos = _latlon_to_xy(ref_lat, ref_lon, lat, lon)

        stops.append(Stop(label=pc, position=pos, postcode=pc, address=addr))
    return stops


# ---------------------------------------------------------------------------
# Step 1 — make_bubbles
# ---------------------------------------------------------------------------

def make_bubbles(stops: List[Stop]) -> List[Bubble]:
    """
    Greedy radius clustering: each unassigned stop starts a new bubble;
    remaining unassigned stops within BUBBLE_RADIUS_M join the nearest
    existing bubble centroid. Single-stop clusters are kept as-is.
    """
    assigned = [False] * len(stops)
    bubbles: List[Bubble] = []

    for i, stop in enumerate(stops):
        if assigned[i]:
            continue
        b = Bubble(stops=[stop])
        assigned[i] = True
        for j, other in enumerate(stops):
            if assigned[j]:
                continue
            if _dist(b.centroid, other.position) <= BUBBLE_RADIUS_M:
                b.stops.append(other)
                assigned[j] = True
        bubbles.append(b)

    return bubbles


# ---------------------------------------------------------------------------
# Step 2 — classify_throats
# ---------------------------------------------------------------------------

def classify_throats(bubble: Bubble, world, van_heading: float) -> None:
    """
    For each stop in the bubble, probe whether the van would lose U-turn
    capability when approaching from van_heading. Sets stop.throat_depth
    and stop.uturn_side in place.
    """
    for stop in bubble.stops:
        van = Van360(
            position=stop.position,
            heading=van_heading,
            radius=VAN_SENSE_RADIUS,
            turn_radius=VAN_TURN_RADIUS,
        )
        stop.uturn_side  = van.can_uturn(world)
        stop.throat_depth = van.throat_probe(
            world, steps=THROAT_STEPS, step_size=THROAT_STEP_M
        )


# ---------------------------------------------------------------------------
# Segment helpers — A-B-C linear road pattern
# ---------------------------------------------------------------------------

_FLAT_PREFIX = re.compile(r'^(?:flat|apt|apartment|unit)\s+\S+\s+', re.I)
_LEADING_NUM = re.compile(r'^(\d+)[a-z]?\s+(.+)', re.I)

# Through-roads score higher than cul-de-sacs for backbone selection.
_THROUGH_WORDS  = frozenset(['road','street','lane','avenue','hill','rise','walk','row'])
_CULDESAC_WORDS = frozenset(['close','court','place','yard','mews','fold','end'])


def _house_number(address: str) -> Optional[int]:
    a = _FLAT_PREFIX.sub('', address.strip())
    m = _LEADING_NUM.match(a)
    return int(m.group(1)) if m else None


def _street_name(address: str) -> Optional[str]:
    a = _FLAT_PREFIX.sub('', address.lower().strip())
    m = _LEADING_NUM.match(a)
    return m.group(2).strip() if m else None


def _road_type(street: str) -> int:
    """2 = through-road, 0 = cul-de-sac, 1 = unknown."""
    last = street.split()[-1] if street else ''
    if last in _THROUGH_WORDS:  return 2
    if last in _CULDESAC_WORDS: return 0
    return 1


def _group_by_street(stops: List[Stop]) -> Dict[str, List[Tuple[int, Stop]]]:
    """All streets with ≥1 numbered stop → {street: [(num, stop), ...]} ascending."""
    groups: Dict[str, List[Tuple[int, Stop]]] = defaultdict(list)
    for s in stops:
        st = _street_name(s.address)
        num = _house_number(s.address)
        if st and num is not None:
            groups[st].append((num, s))
    return {
        st: sorted(items, key=lambda x: x[0])
        for st, items in groups.items()
    }


def _sequence_abc(
    linear_stops: List[Stop],
    detour_stops: List[Stop],
    van: VanState,
) -> List[Stop]:
    """
    Three-segment pattern for a linear road with a side-street detour:
      A  ascending street stops up to the intercept
      B  detour (sequenced via sequence_bubble — throat data already set)
      C  remaining street stops continuing from intercept
    """
    if not detour_stops:
        return linear_stops

    # Intercept = linear stop closest to detour centroid
    cx = sum(s.position.x for s in detour_stops) / len(detour_stops)
    cy = sum(s.position.y for s in detour_stops) / len(detour_stops)
    centroid = Vec2(cx, cy)
    best_idx = min(range(len(linear_stops)),
                   key=lambda i: _dist(linear_stops[i].position, centroid))

    seg_a = linear_stops[:best_idx + 1]
    seg_c = linear_stops[best_idx + 1:]

    entry_van = VanState(
        position=seg_a[-1].position if seg_a else van.position,
        heading=van.heading,
    )
    seg_b = sequence_bubble(Bubble(stops=detour_stops), entry_van)

    return seg_a + seg_b + seg_c


# ---------------------------------------------------------------------------
# Step 3 — sequence_bubble
# ---------------------------------------------------------------------------

def sequence_bubble(bubble: Bubble, van: VanState) -> List[Stop]:
    """
    Order stops within a bubble.

    If the bubble contains ≥2 numbered stops on the same street, apply the
    A-B-C linear road pattern: ascending street pass → detour → continuation.
    Otherwise fall back to nearest-neighbour + throat-aware sequencing.
    """
    # A-B-C: detect linear run within bubble when stops span ≥2 named streets.
    runs = _group_by_street(bubble.stops)
    if len(runs) >= 2:
        # backbone = highest road type; tie-break = most stops
        street, run_items = max(runs.items(),
                                key=lambda x: (_road_type(x[0]), len(x[1])))
        linear = [s for _, s in run_items]
        linear_ids = {id(s) for s in linear}
        detour = [s for s in bubble.stops if id(s) not in linear_ids]
        return _sequence_abc(linear, detour, van)

    free:   List[Stop] = []
    throat: List[Stop] = []

    for s in bubble.stops:
        if s.throat_depth is not None or s.uturn_side is None:
            throat.append(s)
        else:
            free.append(s)

    # Nearest-neighbour for free stops
    ordered: List[Stop] = []
    pos = van.position
    remaining = list(free)
    while remaining:
        nxt = min(remaining, key=lambda s: _dist(pos, s.position))
        ordered.append(nxt)
        pos = nxt.position
        remaining.remove(nxt)

    # Throat stops: shallow-first so van walks in and backs out
    if throat:
        throat_sorted = sorted(
            throat,
            key=lambda s: (s.throat_depth if s.throat_depth is not None else 0)
        )
        # Insert throat run at the point where van is closest to mouth
        mouth = throat_sorted[0].position
        if not ordered:
            insert_at = 0
        else:
            dists = [_dist(ordered[i].position, mouth) for i in range(len(ordered))]
            insert_at = dists.index(min(dists)) + 1
        for i, s in enumerate(throat_sorted):
            ordered.insert(insert_at + i, s)

    return ordered


# ---------------------------------------------------------------------------
# Top-level optimiser
# ---------------------------------------------------------------------------

def optimise_route(stops: List[Stop],
                   world,
                   start_position: Vec2,
                   start_heading: float) -> List[Stop]:
    bubbles = make_bubbles(stops)

    full_route: List[Stop] = []
    van = VanState(position=start_position, heading=start_heading)

    for bubble in bubbles:
        classify_throats(bubble, world, van.heading)
        ordered = sequence_bubble(bubble, van)
        full_route.extend(ordered)
        if ordered:
            last = ordered[-1]
            van.heading  = heading_from_to(van.position, last.position)
            van.position = last.position

    return full_route
