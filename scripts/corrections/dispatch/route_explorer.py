"""Stochastic route explorer — probabilistic graph walk over manifest clusters.

Where greedy_angle.py deterministically picks the lowest-cost next cluster,
route_explorer.py samples from a cost-weighted probability distribution.
This produces a family of plausible routes rather than one optimal sequence,
useful for:
  - Stress-testing the greedy solution against random variants
  - Discovering alternative sequences the greedy misses (local optima)
  - Generating training traces for learned sequencers

The graph is built from the same ONS centroids and manifest clusters as
greedy_angle.py. Every cluster is connected to every other (complete graph);
edge cost is travel time (distance / median_speed) scaled by the full
environment stack: weather, temperature, lighting, school zones, road risk.

RouteContext owns all time-varying state — current datetime, elapsed seconds,
is_dark flag — and applies the multiplier stack via almanac + topology.

Usage:
    python3 scripts/corrections/dispatch/route_explorer.py \\
        --n 5 --steps 20 < manifest.txt

    python3 scripts/corrections/dispatch/route_explorer.py \\
        --n 10 --steps 0 --weather HEAVY_SNOW --temp -2 \\
        --depot PE32\\ 2NQ --home NR20\\ 4AW < manifest.txt

--steps 0  walks every cluster exactly once (full-route mode).
--n        number of independent random walks to emit.
--best     also print the walk with the lowest total cost.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import topology
import almanac
from greedy_angle import get_transit_multiplier, get_dwell_multiplier  # noqa: E402

ONS_PATH = Path(__file__).resolve().parents[2] / "ons_nr_postcodes.json"
EXTRA_CENTROIDS = {"PE32 2NQ": (52.704136, 0.8259)}
PC_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\s?\d?[A-Z]{0,2}\b")
LAT0 = 52.7
MEDIAN_SPEED_MPS = 7.0   # typical inter-cluster van speed


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hav(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6_371_000.0
    p = math.radians(b[0] - a[0])
    q = math.radians(b[1] - a[1])
    x = (math.sin(p / 2) ** 2
         + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0]))
         * math.sin(q / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def softmax_neg(costs: dict) -> dict:
    """Lower cost → higher probability."""
    shift = max(costs.values())
    exp = {k: math.exp(-(v - shift)) for k, v in costs.items()}
    total = sum(exp.values())
    return {k: e / total for k, e in exp.items()}


def weighted_choice(probs: dict) -> str:
    keys = list(probs.keys())
    weights = [probs[k] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


# ── RouteContext ──────────────────────────────────────────────────────────────

class RouteContext:
    """Carries all time-varying environmental state for one walk.

    apply_modifiers returns the combined multiplier for an edge.
    advance_time updates elapsed seconds and rechecks is_dark.
    """

    def __init__(self, weather: str = almanac.CLEAR,
                 temp_c: float = 10.0,
                 start_dt: datetime.datetime | None = None,
                 traffic_profile: str | None = None,
                 route_profile: str | None = None):
        import zoneinfo
        self._tz = zoneinfo.ZoneInfo("Europe/London")
        self.weather  = weather
        self.temp_c   = temp_c
        self.elapsed  = 0.0
        self.start_dt = (start_dt or datetime.datetime.now(tz=self._tz))
        if self.start_dt.tzinfo is None:
            self.start_dt = self.start_dt.replace(tzinfo=self._tz)
        self.traffic_profile = traffic_profile
        self.route_profile   = route_profile
        self._update_dark()

    def _update_dark(self) -> None:
        self.is_dark = almanac.is_dark(self.current_dt)

    @property
    def current_dt(self) -> datetime.datetime:
        return self.start_dt + datetime.timedelta(seconds=self.elapsed)

    def apply_modifiers(self, from_node: str, to_node: str,
                        edge: dict) -> float:
        centroid = edge.get("centroid", (0.0, 0.0))
        m  = almanac.get_weather_multiplier(self.weather)
        m *= almanac.get_temp_dwell_multiplier(self.temp_c)
        m *= get_transit_multiplier(to_node, self.is_dark)
        m *= get_dwell_multiplier(to_node, self.is_dark)
        m *= topology.apply_profile_multiplier(
            to_node, centroid, self.is_dark,
            self.current_dt, self.route_profile)
        road = topology.ROAD_RISK.get(
            topology._risk_normalise(to_node), 1.0)
        m *= road
        return m

    def advance_time(self, cost_secs: float) -> None:
        self.elapsed += cost_secs
        self._update_dark()


# ── Graph construction ────────────────────────────────────────────────────────

def load_centroids() -> dict[str, tuple[float, float]]:
    c = {k: tuple(v) for k, v in json.load(open(ONS_PATH)).items()}
    c.update(EXTRA_CENTROIDS)
    return c


def parse_manifest(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = PC_RE.findall(line.upper())
        if m:
            out.append((line, m[-1]))
    return out


def build_graph(clusters: list[tuple], centers: dict) -> dict:
    """Complete graph: every cluster is connected to every other.
    Edge cost = haversine distance; median_speed is constant.
    centroid stored on each edge for school_multiplier lookup."""
    graph = {}
    for name, n, coord, tag in clusters:
        edges = {}
        for other_name, other_n, other_coord, other_tag in clusters:
            if other_name == name:
                continue
            dist = _hav(coord, other_coord)
            edges[other_name] = {
                "distance":     dist,
                "median_speed": MEDIAN_SPEED_MPS,
                "centroid":     other_coord,
                "tag":          other_tag,
            }
        graph[name] = {"coord": coord, "tag": tag, "n": n, "edges": edges}
    return graph


# ── Random walk ───────────────────────────────────────────────────────────────

def random_walk(graph: dict, start_node: str, steps: int,
                route_context: RouteContext,
                visited: set | None = None) -> tuple[list[str], float]:
    """Walk `steps` hops from start_node, sampling edges by cost.

    If steps == 0 visits every unvisited node exactly once (full-route mode).
    Returns (path, total_cost_seconds)."""
    if visited is None:
        visited = set()

    path = [start_node]
    current = start_node
    visited.add(start_node)
    total_cost = 0.0

    full_route = (steps == 0)
    remaining_steps = len(graph) - 1 if full_route else steps

    for _ in range(remaining_steps):
        neighbours = {
            v: e for v, e in graph[current]["edges"].items()
            if v not in visited
        }
        if not neighbours:
            break

        costs = {}
        for v, edge in neighbours.items():
            base = edge["distance"] / edge["median_speed"]
            env  = route_context.apply_modifiers(current, v, edge)
            # throat penalty applies when leaving current node
            raw = base * env + topology.throat_penalty(current)
            costs[v] = topology.road_cost_modifier(v, base_secs=raw)

        probs = softmax_neg(costs)
        next_node = weighted_choice(probs)

        path.append(next_node)
        visited.add(next_node)
        total_cost += costs[next_node]
        route_context.advance_time(costs[next_node])
        current = next_node

    return path, total_cost


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_path(path: list[str], graph: dict) -> dict:
    """Compute km, raw angular cost, and masked angular cost for a path."""
    total_km = 0.0
    raw_c = 0.0
    masked_c = 0.0
    coords = [graph[n]["coord"] for n in path]
    for i in range(len(coords) - 1):
        total_km += _hav(coords[i], coords[i + 1]) / 1000.0
    for i in range(1, len(coords) - 1):
        v1 = (coords[i][0] - coords[i-1][0], coords[i][1] - coords[i-1][1])
        v2 = (coords[i+1][0] - coords[i][0],  coords[i+1][1] - coords[i][1])
        m1 = math.hypot(*v1)
        m2 = math.hypot(*v2)
        if m1 == 0 or m2 == 0:
            continue
        cos_t = max(-1.0, min(1.0,
            (v1[0]*v2[0] + v1[1]*v2[1]) / (m1 * m2)))
        a_raw = 1.0 - cos_t
        raw_c += a_raw
        masked_c += topology.mask_cost(a_raw, graph[path[i]]["tag"])
    return {"km": round(total_km, 2), "raw_C": round(raw_c, 2),
            "masked_C": round(masked_c, 2)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depot",   default="PE32 2NQ")
    ap.add_argument("--home",    default="NR20 4AW")
    ap.add_argument("--n",       type=int,   default=5,
                    help="number of independent walks")
    ap.add_argument("--steps",   type=int,   default=0,
                    help="hops per walk (0 = full route, visit all clusters)")
    ap.add_argument("--seed",    type=int,   default=0)
    ap.add_argument("--weather", default=almanac.CLEAR,
                    choices=[almanac.CLEAR, almanac.HEAVY_FOG,
                             almanac.LIGHT_FOG, almanac.HEAVY_RAIN,
                             almanac.HEAVY_SNOW])
    ap.add_argument("--temp",    type=float, default=10.0,
                    help="air temperature °C")
    ap.add_argument("--start",   default=None,
                    help="ISO datetime for shift start (default: now)")
    ap.add_argument("--best",    action="store_true",
                    help="highlight the walk with lowest masked angular cost")
    args = ap.parse_args()

    random.seed(args.seed)
    centers = load_centroids()

    manifest = parse_manifest(sys.stdin.read())
    if not manifest:
        sys.exit("no postcodes parsed from stdin")

    drops = Counter(pc for _, pc in manifest)
    addr_by_key: dict[str, list[str]] = defaultdict(list)
    for line, pc in manifest:
        addr_by_key[pc].append(line)

    clusters = []
    for pc in dict.fromkeys(pc for _, pc in manifest):
        if pc in centers:
            tag = topology.classify(addr_by_key[pc])
            clusters.append((pc, drops[pc],
                             topology.cluster_anchor(pc, centers[pc]), tag))

    if not clusters:
        sys.exit("no clusters resolved against centroids")

    graph = build_graph(clusters, centers)

    start_dt = (datetime.datetime.fromisoformat(args.start)
                if args.start else None)

    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/London")

    print(f"depot {args.depot}  clusters: {len(clusters)}  "
          f"walks: {args.n}  steps: {args.steps or 'full'}")
    print(f"weather: {args.weather}  temp: {args.temp}°C  "
          f"seed: {args.seed}")
    print()

    results = []
    for i in range(args.n):
        ctx = RouteContext(
            weather=args.weather,
            temp_c=args.temp,
            start_dt=start_dt,
        )
        # start from a random cluster
        start_node = random.choice(list(graph.keys()))
        path, cost_secs = random_walk(
            graph, start_node, args.steps, ctx)
        sc = score_path(path, graph)
        sc["cost_secs"] = round(cost_secs)
        sc["start"] = start_node
        results.append((path, sc))

    results.sort(key=lambda r: r[1]["masked_C"])

    for idx, (path, sc) in enumerate(results):
        best_flag = " ★" if (args.best and idx == 0) else ""
        print(f"  walk {idx+1:>2}{best_flag}  "
              f"km={sc['km']:>6.2f}  "
              f"raw_C={sc['raw_C']:>5.2f}  "
              f"masked_C={sc['masked_C']:>5.2f}  "
              f"cost={sc['cost_secs']//60}m  "
              f"start={sc['start']}")
        print(f"         {' → '.join(path)}")
    print()
    if args.best:
        best_path, best_sc = results[0]
        print(f"best route (lowest masked_C = {best_sc['masked_C']}):")
        for step, node in enumerate(best_path):
            tag = graph[node]["tag"]
            glyph = {"TYPE_CLOSE": "▲", "TYPE_HYBRID": "◆"}.get(tag, " ")
            print(f"  {step+1:>3} {glyph} {node}")


if __name__ == "__main__":
    main()
