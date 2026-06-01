#!/usr/bin/env python3
"""
solve_maze.py

Ask the system for the fastest way to solve the maze.

The "maze" = your behavioural graph:
- Nodes: StopID (opaque, no postcode/address assumptions)
- Edges: observed transitions with learned time/cost
- Context: solar / weather / traffic lenses, combined into a route weight

This file:
- Defines a minimal BehaviouralGraph
- Defines a ContextRegistry (lenses push here)
- Defines a NonPostcodeRouter with a `solve_maze` method
- Shows an example of asking for the fastest way through today's stops

The Solar/Weather/Traffic dataclasses below are the consumer-side shapes
the registry expects — duck-typed compatible with the producer-side
classes in solar_phase.py / weather.py / traffic.py (same `light_factor`
/ `penalty` fields). The registry doesn't care about the type names.

Per [[variant-design-rules]] rule 5 and [[engine-stays-small]] corollary,
context lenses live in the weight layer here (ContextRegistry.route_weight),
never in a vote panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta


# ──────────────────────────────────────────────────────────────────────────────
# Core domain objects
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StopID:
    """
    Opaque identifier for a delivery location.
    No address, no postcode assumptions.
    """
    value: str


@dataclass
class EdgeStats:
    """
    Aggregated stats for a transition A -> B.
    All times are in seconds.
    """
    count: int = 0
    total_time: float = 0.0
    total_cost: float = 0.0

    @property
    def mean_time(self) -> float:
        return self.total_time / self.count if self.count > 0 else 0.0

    @property
    def mean_cost(self) -> float:
        return self.total_cost / self.count if self.count > 0 else 0.0

    def update(self, travel_time_s: float, cost: float) -> None:
        self.count += 1
        self.total_time += travel_time_s
        self.total_cost += cost


# Floor on the divide-at-query-time weight so a zero-weight context can't
# produce infinite cost. 0.05 caps amplification at ×20; lower the floor
# only if you genuinely want a single bad night to anchor the routing.
WEIGHT_FLOOR = 0.05


@dataclass
class BehaviouralGraph:
    """
    Map-free behavioural graph:
    - nodes: StopID
    - edges: StopID -> StopID with EdgeStats
    """
    edges: Dict[StopID, Dict[StopID, EdgeStats]] = field(default_factory=dict)

    def ensure_node(self, stop: StopID) -> None:
        if stop not in self.edges:
            self.edges[stop] = {}

    def update_transition(self, src: StopID, dst: StopID,
                          travel_time_s: float, cost: float) -> None:
        self.ensure_node(src)
        self.ensure_node(dst)
        if dst not in self.edges[src]:
            self.edges[src][dst] = EdgeStats()
        self.edges[src][dst].update(travel_time_s, cost)

    def get_neighbors(self, stop: StopID) -> Dict[StopID, EdgeStats]:
        return self.edges.get(stop, {})


# ──────────────────────────────────────────────────────────────────────────────
# Context lenses and registry
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SolarVariant:
    phase: str
    light_factor: float  # [0,1]
    meta: Dict[str, Any]


@dataclass
class WeatherVariant:
    intensity: float      # [0,1]
    penalty: float        # [0,1]
    meta: Dict[str, Any]


@dataclass
class TrafficVariant:
    pressure: float       # [0,1]
    penalty: float        # [0,1]
    meta: Dict[str, Any]


class ContextRegistry:
    """
    Shared buffer where lenses push their shaped values.
    The core only ever reads from here.
    """
    def __init__(self) -> None:
        self.lenses: Dict[str, Any] = {
            "solar": None,
            "weather": None,
            "traffic": None,
        }

    def update_lens(self, lens_name: str, data: Any) -> None:
        self.lenses[lens_name] = data

    def get(self, lens_name: str) -> Any:
        return self.lenses.get(lens_name)

    def route_weight(self) -> float:
        """
        Combine lenses into a single [0,1] weight.
        Default: if a lens is missing, treat it as neutral (1.0).
        """
        solar: Optional[SolarVariant] = self.get("solar")
        weather: Optional[WeatherVariant] = self.get("weather")
        traffic: Optional[TrafficVariant] = self.get("traffic")

        visibility = solar.light_factor if solar is not None else 1.0
        weather_penalty = 1.0 - (weather.penalty if weather is not None else 0.0)
        traffic_penalty = 1.0 - (traffic.penalty if traffic is not None else 0.0)

        weight = visibility * weather_penalty * traffic_penalty
        return max(0.0, min(weight, 1.0))


# ──────────────────────────────────────────────────────────────────────────────
# Route observation and learning
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ObservedTransition:
    """
    One observed transition between two stops on a completed route.
    """
    src: StopID
    dst: StopID
    start_time: datetime
    end_time: datetime


@dataclass
class RouteObservation:
    """
    A completed route, expressed as a sequence of observed transitions.
    """
    transitions: List[ObservedTransition]


class BehaviouralLearner:
    """
    Learns the behavioural graph from completed routes.
    """
    def __init__(self, graph: BehaviouralGraph,
                 context_registry: ContextRegistry) -> None:
        self.graph = graph
        self.context_registry = context_registry

    def ingest_route(self, route: RouteObservation) -> None:
        """
        Update the behavioural graph with a completed route.
        Stores pure travel time — the current route_weight is applied at
        query time in NonPostcodeRouter, not folded into the stored cost.
        This keeps stats.mean_time interpretable as actual mean travel
        time across all observations, regardless of the historical
        context they were recorded under.
        """
        for tr in route.transitions:
            travel_time_s = (tr.end_time - tr.start_time).total_seconds()
            self.graph.update_transition(tr.src, tr.dst, travel_time_s,
                                         travel_time_s)


# ──────────────────────────────────────────────────────────────────────────────
# Non-postcode router: "fastest way to solve the maze"
# ──────────────────────────────────────────────────────────────────────────────

class NonPostcodeRouter:
    """
    Simple heuristic router that:
    - starts from a depot
    - repeatedly chooses the next stop based on lowest mean_cost edge
    - falls back to arbitrary choice for unknown stops

    The "maze" is the behavioural graph.
    The "fastest way to solve it" is the lowest-cost traversal through
    today's targets.
    """
    def __init__(self, graph: BehaviouralGraph,
                 context_registry: ContextRegistry) -> None:
        self.graph = graph
        self.context_registry = context_registry

    def solve_maze(self, depot: StopID, targets: List[StopID]) -> List[StopID]:
        """
        Public entry point: ask for the fastest way to solve today's maze.
        Returns an ordered list of stops starting at depot and visiting
        all targets.
        """
        return self._plan_route(depot, targets)

    def _plan_route(self, depot: StopID, targets: List[StopID]) -> List[StopID]:
        remaining: Set[StopID] = set(targets)
        order: List[StopID] = []
        current = depot

        while remaining:
            next_stop = self._choose_next(current, remaining)
            order.append(next_stop)
            remaining.remove(next_stop)
            current = next_stop

        return order

    def _choose_next(self, current: StopID, candidates: Set[StopID]) -> StopID:
        """
        Choose the next stop among candidates.

        Expected cost is computed at query time from the stored mean
        travel time and the current route weight:
            expected_cost = stats.mean_time / max(current_weight, WEIGHT_FLOOR)
        Harsh conditions (low weight) amplify the cost; clear conditions
        (weight ≈ 1) leave it equal to mean_time. The floor caps the
        amplification so a zero-weight context can't produce infinities.

        Falls back to an arbitrary candidate when no edge stats exist.
        """
        neighbors = self.graph.get_neighbors(current)
        weight = self.context_registry.route_weight()
        effective_weight = max(weight, WEIGHT_FLOOR)
        best_stop: Optional[StopID] = None
        best_cost: float = float("inf")

        for c in candidates:
            stats = neighbors.get(c)
            if stats is not None and stats.mean_time > 0:
                expected_cost = stats.mean_time / effective_weight
                if expected_cost < best_cost:
                    best_cost = expected_cost
                    best_stop = c

        if best_stop is not None:
            return best_stop

        # No known edges to any candidate: fall back to arbitrary choice
        return next(iter(candidates))


# ──────────────────────────────────────────────────────────────────────────────
# Example wiring: asking for the fastest way to solve today's maze
# ──────────────────────────────────────────────────────────────────────────────

def example_usage() -> None:
    """
    This is just a placeholder to show how you'd ask:
        "What's the fastest way to solve today's maze?"
    """
    # Core structures
    graph = BehaviouralGraph()
    registry = ContextRegistry()

    # Example: lenses push their shaped values
    registry.update_lens("solar",
                         SolarVariant(phase="day", light_factor=1.0, meta={}))
    registry.update_lens("weather",
                         WeatherVariant(intensity=0.3, penalty=0.3, meta={}))
    registry.update_lens("traffic",
                         TrafficVariant(pressure=0.5, penalty=0.5, meta={}))

    learner = BehaviouralLearner(graph, registry)
    router = NonPostcodeRouter(graph, registry)

    # Dummy stops
    depot = StopID("DEPOT")
    a = StopID("A")
    b = StopID("B")
    c = StopID("C")

    # Dummy observed route: DEPOT -> A -> B -> C
    now = datetime.utcnow()
    route_obs = RouteObservation(
        transitions=[
            ObservedTransition(depot, a, now, now + timedelta(minutes=5)),
            ObservedTransition(a, b, now + timedelta(minutes=5),
                               now + timedelta(minutes=10)),
            ObservedTransition(b, c, now + timedelta(minutes=10),
                               now + timedelta(minutes=15)),
        ]
    )

    # Learn from the observed route
    learner.ingest_route(route_obs)

    # Ask: "fastest way to solve today's maze" for targets [A, B, C]
    planned = router.solve_maze(depot, [a, b, c])
    print("Fastest maze solution (order):", [s.value for s in planned])


if __name__ == "__main__":
    example_usage()
