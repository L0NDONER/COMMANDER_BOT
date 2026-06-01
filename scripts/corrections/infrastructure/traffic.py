#!/usr/bin/env python3
"""Route-level traffic classifier — context-layer helper.

Returns a TrafficVariant with `pressure` and `penalty`, both in [0, 1].
`penalty` is the canonical multiplier for a down-weight: 0.0 = free
flow (observed speed matches expected), 1.0 = gridlock (observed = 0).

The pressure is derived from the speed ratio: ratio =
observed_speed / expected_speed, clamped to [0, 1]; pressure = 1 - ratio.
Third sibling to solar_phase.py and weather.py — same architectural
slot, same composition rule.

Per [[variant-design-rules]] rule 5 and [[engine-stays-small]] corollary,
this output goes into the WEIGHT layer (via `context.route_weight`),
never into the vote panel.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class TrafficSample:
    time: datetime
    expected_speed_mps: float
    observed_speed_mps: float


@dataclass
class TrafficVariant:
    pressure: float
    penalty: float
    meta: Dict[str, Any]


def nearest_traffic(samples: List[TrafficSample],
                    t: datetime) -> TrafficSample | None:
    if not samples:
        return None
    return min(samples, key=lambda s: abs((s.time - t).total_seconds()))


def compute_traffic_variant(route,
                            traffic_samples: List[TrafficSample]
                            ) -> TrafficVariant:
    if not route.segments:
        return TrafficVariant(0.0, 0.0, {"reason": "empty_route"})

    t0 = route.segments[0].start_time
    t1 = route.segments[-1].end_time
    mid = t0 + (t1 - t0) / 2

    sample = nearest_traffic(traffic_samples, mid)
    if sample is None or sample.expected_speed_mps <= 0:
        return TrafficVariant(0.0, 0.0, {"reason": "no_traffic_data"})

    ratio = sample.observed_speed_mps / sample.expected_speed_mps
    ratio = max(0.0, min(ratio, 1.0))

    pressure = 1.0 - ratio
    penalty = pressure

    meta = {
        "expected_speed_mps": sample.expected_speed_mps,
        "observed_speed_mps": sample.observed_speed_mps,
        "speed_ratio": ratio,
    }

    return TrafficVariant(float(pressure), float(penalty), meta)
