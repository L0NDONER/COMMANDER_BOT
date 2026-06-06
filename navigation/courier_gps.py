"""Courier GPS trace consumer — the real manifold the van moves through.

Postcode centroids and address pins are a sparse, hop-by-hop projection
of what the phone actually captures every second:

    True position    (lat, lon, ±3-5m outdoors)
    Heading          (the direction the van is actually pointing, degrees)
    Velocity vector  (speed m/s + heading combine to a 2D velocity)
    Timestamp        (seconds since epoch, sub-second resolution)
    Accuracy         (horizontal accuracy in metres — quality gate)

Derived from a sequence of these ticks:
    Micro-geometry   (which side of road, which driveway, which cul-de-sac)
    Flow continuity  (smoothness of motion — second-derivative of position)

This module owns the schema + loader + per-stop matching. The lens
variants live in scripts/courier_multivariant_lens.py — they read GPS
context via this module's helpers and return 0 (neutral, silent) when
no trace is available, so they're harmless until upstream starts
emitting traces.

Trace location: scripts/gps_traces/{manifest_id}_{date}.json
Format: {"ticks": [{"ts": float, "lat": float, "lon": float,
                    "heading_deg": float, "speed_mps": float,
                    "accuracy_m": float}, ...]}
"""
import json
import math
from dataclasses import dataclass
from pathlib import Path

# Forward-declared so InferredStop can reference _haversine_m via lookup.

GPS_TRACES_DIR = Path(__file__).parent / "gps_traces"


@dataclass(frozen=True)
class GPSTick:
    ts: float
    lat: float
    lon: float
    heading_deg: float
    speed_mps: float
    accuracy_m: float


@dataclass
class GPSTrace:
    manifest_id: str
    date: str
    ticks: list[GPSTick]

    def is_empty(self) -> bool:
        return not self.ticks

    def window_around(self, ts: float, half_secs: float = 30.0
                      ) -> list[GPSTick]:
        """Ticks within ±half_secs of ts. Returns [] if none."""
        return [t for t in self.ticks if abs(t.ts - ts) <= half_secs]

    def ticks_near(self, lat: float, lon: float,
                   radius_m: float = 50.0) -> list[GPSTick]:
        """Ticks whose position is within `radius_m` metres of (lat, lon).
        Used to grab the trace window while the courier was *at* a stop,
        since breadcrumbs don't carry per-stop timestamps."""
        if not self.ticks:
            return []
        return [t for t in self.ticks
                if _haversine_m(lat, lon, t.lat, t.lon) <= radius_m]


def _haversine_m(lat1: float, lon1: float,
                 lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 6_371_000.0 * 2 * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class InferredStop:
    """A dwell cluster from the GPS trace: the lens treats this as a
    'stop' when no breadcrumb route is available. The `kind` field
    distinguishes delivery_stop (the courier worked here) from
    traffic_queue / walk_pause / hesitation_stop — all kept rather
    than filtered, so downstream can pick what to do with each."""
    label: str
    lat: float
    lon: float
    start_ts: float
    end_ts: float
    n_ticks: int
    kind: str = "delivery_stop"
    # Diagnostic features so callers can inspect why a stop got its kind.
    radius_m: float = 0.0
    linearity: float = 0.0
    walking_frac: float = 0.0
    heading_sd_deg: float = 0.0


def _circular_stdev_deg(degrees: list[float]) -> float:
    """Circular standard deviation in degrees (handles 359° vs 1° wrap)."""
    if not degrees:
        return 0.0
    rads = [math.radians(d) for d in degrees]
    sin_mean = sum(math.sin(r) for r in rads) / len(rads)
    cos_mean = sum(math.cos(r) for r in rads) / len(rads)
    R = math.sqrt(sin_mean ** 2 + cos_mean ** 2)
    if R >= 1.0:
        return 0.0
    if R <= 1e-9:
        return 180.0  # uniform → max circular spread
    return math.degrees(math.sqrt(-2 * math.log(R)))


def _classify_cluster(cluster: list[GPSTick]) -> dict:
    """Compute classification features + assign a kind label.

    Kinds (in classification order):
      traffic_queue   — linear motion at low speed (van crept along road)
      walk_pause      — brief stop with walking-pace ticks dominant
      delivery_stop   — walking-pace ticks dominant for >=30s (van→door→van)
      hesitation_stop — jittery heading, not walking, medium duration
    Falls back to delivery_stop when no rule fires.
    """
    n = len(cluster)
    centroid_lat = sum(t.lat for t in cluster) / n
    centroid_lon = sum(t.lon for t in cluster) / n
    radius_m = max(_haversine_m(centroid_lat, centroid_lon, t.lat, t.lon)
                   for t in cluster)
    net_disp_m = _haversine_m(cluster[0].lat, cluster[0].lon,
                              cluster[-1].lat, cluster[-1].lon)
    linearity = net_disp_m / max(radius_m, 1.0)
    duration = cluster[-1].ts - cluster[0].ts
    speeds = [t.speed_mps for t in cluster]
    median_speed = sorted(speeds)[n // 2]
    walking_frac = sum(1 for s in speeds if 0.5 <= s <= 2.0) / n
    heading_sd = _circular_stdev_deg([t.heading_deg for t in cluster])

    # Traffic queue first: tight-heading low-speed motion is unambiguous
    # regardless of duration (long jams are still queues). Linearity
    # alone was too generous — a bubble walk down a street naturally has
    # linearity > 1.2 because consecutive drops are roughly in one
    # direction. The dispositive 'van crawling in a line' signal is
    # heading_sd < 30°; bubble walks have heading_sd > 100° (compass
    # spinning around as the courier turns between doors).
    if median_speed < 3.0 and heading_sd < 30:
        kind = "traffic_queue"
    # Long-cluster short-circuit: a cluster longer than 120s is almost
    # certainly a walk-bubble visit (multi-drop bubble = 3-7 min). The
    # walking_frac threshold is naturally low for bubbles because each
    # dwell-at-door dominates time over the brief walks between doors,
    # so applying the short-cluster rules misclassifies them. Only call
    # a long cluster a hesitation if the courier was truly motionless
    # throughout (median_speed < 0.1, no walking ticks at all).
    elif duration > 120:
        if (median_speed < 0.1 and walking_frac < 0.05
                and heading_sd > 60):
            kind = "hesitation_stop"
        else:
            kind = "delivery_stop"
    # Short-cluster rules below — apply to <=120s events where the
    # composition is dominated by a single event (one door, one
    # hesitation, one walk pause).
    elif (walking_frac > 0.3 and duration >= 30
          and heading_sd > 60 and median_speed > 0.3):
        kind = "delivery_stop"
    elif (walking_frac > 0.4 and duration < 30
          and heading_sd > 60 and median_speed > 0.3):
        kind = "walk_pause"
    # Hesitation: jittery heading + stationary van + meaningful duration.
    elif (heading_sd > 60 and duration >= 30 and median_speed < 0.5):
        kind = "hesitation_stop"
    else:
        kind = "delivery_stop"

    return {
        "kind": kind,
        "radius_m": radius_m,
        "linearity": linearity,
        "walking_frac": walking_frac,
        "heading_sd_deg": heading_sd,
        "centroid": (centroid_lat, centroid_lon),
    }


def infer_stops(trace: GPSTrace,
                slow_mps: float = 2.0,
                min_dwell_secs: float = 20.0,
                merge_radius_m: float = 60.0) -> list[InferredStop]:
    """Find stop candidates by clustering consecutive slow ticks.

    A run of ticks with speed < slow_mps that lasts >= min_dwell_secs
    becomes one inferred stop. Adjacent stop centroids within
    merge_radius_m get merged (same physical stop reached twice in the
    trace, or one stop with a brief speed blip in the middle).

    Traffic queues won't trigger a stop because they don't last long
    enough; door dwells (35-60s in real synth) easily qualify.
    """
    if not trace.ticks:
        return []
    # Step 1: walk the trace, group consecutive slow ticks.
    raw: list[list[GPSTick]] = []
    current: list[GPSTick] = []
    for t in trace.ticks:
        if t.speed_mps < slow_mps:
            current.append(t)
        else:
            if current:
                raw.append(current)
                current = []
    if current:
        raw.append(current)

    # Step 2: keep only clusters whose timespan meets min_dwell_secs,
    # classify each, and stash diagnostic features.
    stops: list[InferredStop] = []
    idx = 0
    for cluster in raw:
        if not cluster:
            continue
        span = cluster[-1].ts - cluster[0].ts
        if span < min_dwell_secs:
            continue
        feats = _classify_cluster(cluster)
        idx += 1
        stops.append(InferredStop(
            label=f"GPS-{idx:03d}",
            lat=feats["centroid"][0],
            lon=feats["centroid"][1],
            start_ts=cluster[0].ts, end_ts=cluster[-1].ts,
            n_ticks=len(cluster),
            kind=feats["kind"],
            radius_m=feats["radius_m"],
            linearity=feats["linearity"],
            walking_frac=feats["walking_frac"],
            heading_sd_deg=feats["heading_sd_deg"],
        ))

    # Step 3: merge adjacent stops within merge_radius_m. Only merge if
    # both have the same kind (a delivery_stop next to a traffic_queue
    # shouldn't get fused into one ambiguous blob).
    merged: list[InferredStop] = []
    for s in stops:
        if (merged and merged[-1].kind == s.kind
                and _haversine_m(merged[-1].lat, merged[-1].lon,
                                 s.lat, s.lon) <= merge_radius_m):
            prev = merged[-1]
            n = prev.n_ticks + s.n_ticks
            merged[-1] = InferredStop(
                label=prev.label, kind=prev.kind,
                lat=(prev.lat * prev.n_ticks + s.lat * s.n_ticks) / n,
                lon=(prev.lon * prev.n_ticks + s.lon * s.n_ticks) / n,
                start_ts=prev.start_ts, end_ts=s.end_ts, n_ticks=n,
                # Diagnostic features for the merged stop are inherited
                # from the longer-running side rather than recomputed.
                radius_m=max(prev.radius_m, s.radius_m),
                linearity=(prev.linearity if prev.n_ticks >= s.n_ticks
                           else s.linearity),
                walking_frac=(prev.walking_frac * prev.n_ticks
                              + s.walking_frac * s.n_ticks) / n,
                heading_sd_deg=max(prev.heading_sd_deg, s.heading_sd_deg),
            )
        else:
            merged.append(s)
    # Renumber after merge so labels are sequential.
    return [InferredStop(label=f"GPS-{i + 1:03d}",
                         kind=s.kind,
                         lat=s.lat, lon=s.lon,
                         start_ts=s.start_ts, end_ts=s.end_ts,
                         n_ticks=s.n_ticks,
                         radius_m=s.radius_m, linearity=s.linearity,
                         walking_frac=s.walking_frac,
                         heading_sd_deg=s.heading_sd_deg)
            for i, s in enumerate(merged)]


def nearest_postcode(lat: float, lon: float,
                     centers: dict[str, tuple[float, float]],
                     max_dist_m: float = 200.0) -> str | None:
    """Return the postcode whose centre is closest to (lat, lon), within
    max_dist_m. None if nothing close enough — caller falls back to the
    inferred label."""
    best, best_d = None, float("inf")
    for pc, (la, lo) in centers.items():
        d = _haversine_m(lat, lon, la, lo)
        if d < best_d:
            best, best_d = pc, d
    return best if best_d <= max_dist_m else None


def discover_trace_manifests() -> list[tuple[str, str]]:
    """Return (manifest_id, date) for every trace file in GPS_TRACES_DIR."""
    out: list[tuple[str, str]] = []
    if not GPS_TRACES_DIR.exists():
        return out
    for path in GPS_TRACES_DIR.glob("*.json"):
        stem = path.stem  # "<manifest_id>_<date>"
        # date is the trailing YYYY-MM-DD (10 chars), manifest_id is the rest.
        if len(stem) <= 11 or stem[-11] != "_":
            continue
        out.append((stem[:-11], stem[-10:]))
    return out


def load_trace(manifest_id: str, date: str) -> GPSTrace:
    """Read trace from disk; return empty GPSTrace if file missing."""
    path = GPS_TRACES_DIR / f"{manifest_id}_{date}.json"
    if not path.exists():
        return GPSTrace(manifest_id, date, [])
    with path.open() as f:
        d = json.load(f)
    ticks = [GPSTick(**t) for t in d.get("ticks", [])]
    return GPSTrace(manifest_id, date, ticks)


def heading_diff_deg(h1: float, h2: float) -> float:
    """Smallest angular difference between two GPS headings, [0, 180]."""
    d = abs(h2 - h1) % 360.0
    return d if d <= 180.0 else 360.0 - d


def median_heading_change(ticks: list[GPSTick]) -> float | None:
    """Median |Δheading| across a window. None if too few ticks."""
    if len(ticks) < 2:
        return None
    diffs = [heading_diff_deg(ticks[i].heading_deg, ticks[i - 1].heading_deg)
             for i in range(1, len(ticks))]
    diffs.sort()
    return diffs[len(diffs) // 2]


def speed_band(mps: float) -> str:
    """Coarse band: walk (<2 m/s), creep (2-5), drive (>5)."""
    if mps < 2.0:
        return "walk"
    if mps < 5.0:
        return "creep"
    return "drive"


def speed_variance(ticks: list[GPSTick]) -> float | None:
    """Standard deviation of speed across a window. None if too few."""
    if len(ticks) < 2:
        return None
    speeds = [t.speed_mps for t in ticks]
    mean = sum(speeds) / len(speeds)
    var = sum((s - mean) ** 2 for s in speeds) / len(speeds)
    return math.sqrt(var)


# ---------------------------------------------------------------------------
# Van360 — local-frame obstacle sensing, U-turn and throat geometry
# ---------------------------------------------------------------------------

@dataclass
class Vec2:
    x: float
    y: float


@dataclass
class Arc:
    center: Vec2
    radius: float
    start_angle: float
    sweep: float


def latlon_to_vec2(lat: float, lon: float,
                   ref_lat: float, ref_lon: float) -> Vec2:
    """Public wrapper: (lat, lon) → local metres from ref point."""
    return _latlon_to_xy(ref_lat, ref_lon, lat, lon)


def _latlon_to_xy(ref_lat: float, ref_lon: float,
                  lat: float, lon: float) -> Vec2:
    """Equirectangular projection: (lat, lon) → local metres from ref point."""
    x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * 6_371_000.0
    y = math.radians(lat - ref_lat) * 6_371_000.0
    return Vec2(x, y)


def _vec2_distance(a: Vec2, b: Vec2) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def _intersects_arc(point: Vec2, size: float, arc: Arc) -> bool:
    d = _vec2_distance(point, arc.center)
    if not (arc.radius - size <= d <= arc.radius + size):
        return False
    angle = math.atan2(point.y - arc.center.y, point.x - arc.center.x)
    delta = (angle - arc.start_angle) % (2 * math.pi)
    return delta <= arc.sweep


@dataclass
class Van360:
    position: Vec2
    heading: float          # radians
    radius: float = 4.0     # sensing bubble (metres)
    turn_radius: float = 6.0

    def sense(self, world) -> list:
        return [obj for obj in world.objects
                if _vec2_distance(self.position, obj.position) <= self.radius]

    def clearance_arc(self, direction: str,
                      sweep: float = math.radians(45)) -> Arc:
        sign = +1 if direction == 'left' else -1
        cx = self.position.x + sign * self.turn_radius * math.sin(self.heading)
        cy = self.position.y - sign * self.turn_radius * math.cos(self.heading)
        start_angle = math.atan2(self.position.y - cy, self.position.x - cx)
        return Arc(center=Vec2(cx, cy), radius=self.turn_radius,
                   start_angle=start_angle, sweep=sweep)

    def arc_is_clear(self, arc: Arc, world) -> bool:
        return not any(_intersects_arc(obj.position, obj.size, arc)
                       for obj in world.objects)

    def can_turn(self, world, direction: str) -> bool:
        return self.arc_is_clear(self.clearance_arc(direction), world)

    def can_uturn(self, world) -> str | None:
        """Returns 'left', 'right', or None if no U-turn is geometrically possible."""
        for direction in ('left', 'right'):
            if self.arc_is_clear(
                    self.clearance_arc(direction, sweep=math.pi), world):
                return direction
        return None

    def throat_probe(self, world, steps: int = 6,
                     step_size: float = 2.0) -> int | None:
        """
        Step forward along heading; return the step index where U-turn
        capability is lost, or None if the van can always turn around.
        A non-None result means the entry is a throat at depth
        (result * step_size) metres in.
        """
        probe = Van360(
            position=Vec2(self.position.x, self.position.y),
            heading=self.heading,
            radius=self.radius,
            turn_radius=self.turn_radius,
        )
        for i in range(steps):
            probe.position = Vec2(
                probe.position.x + step_size * math.cos(probe.heading),
                probe.position.y + step_size * math.sin(probe.heading),
            )
            if probe.can_uturn(world) is None:
                return i
        return None


def van_from_tick(tick: GPSTick, ref_lat: float, ref_lon: float,
                  radius: float = 4.0, turn_radius: float = 6.0) -> Van360:
    """Project a GPSTick into local metres and return a Van360.

    ref_lat/ref_lon is the anchor/mouth point of the close — used as the
    local coordinate origin so Van360 geometry stays in metres.
    """
    pos = _latlon_to_xy(ref_lat, ref_lon, tick.lat, tick.lon)
    return Van360(position=pos, heading=math.radians(tick.heading_deg),
                  radius=radius, turn_radius=turn_radius)
