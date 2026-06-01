#!/usr/bin/env python3
"""Route-level solar phase classifier — context-layer helper.

Returns a SolarPhaseVariant with `phase` (night / dawn / day / dusk) and
`light_factor` in [0, 1]. The light_factor is the canonical multiplier
the context layer is meant to consume: 1.0 = full daylight, 0.0 = night.
Dawn/dusk interpolate linearly across a ±45-minute twilight window.

Per [[variant-design-rules]] rule 5 and [[engine-stays-small]] corollary,
this module's output goes into the WEIGHT layer (via
`context.route_weight`), never into the vote panel. Wiring it as a vote
would inject route-level autocorrelation that collapses the null
distribution.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict


@dataclass
class SolarPhaseVariant:
    phase: str
    light_factor: float
    meta: Dict[str, Any]


def classify_phase(t: datetime, sunrise: datetime,
                   sunset: datetime) -> SolarPhaseVariant:
    dawn_offset = timedelta(minutes=45)
    dusk_offset = timedelta(minutes=45)

    dawn_start = sunrise - dawn_offset
    dusk_end = sunset + dusk_offset

    if t < dawn_start or t >= dusk_end:
        phase = "night"
        light = 0.0
    elif dawn_start <= t < sunrise:
        phase = "dawn"
        total = (sunrise - dawn_start).total_seconds()
        done = (t - dawn_start).total_seconds()
        light = done / total if total > 0 else 0.5
    elif sunrise <= t < sunset:
        phase = "day"
        light = 1.0
    else:
        phase = "dusk"
        total = (dusk_end - sunset).total_seconds()
        done = (t - sunset).total_seconds()
        light = 1.0 - (done / total if total > 0 else 0.5)

    meta = {
        "sunrise": sunrise.isoformat(),
        "sunset": sunset.isoformat(),
        "timestamp": t.isoformat(),
    }

    return SolarPhaseVariant(phase, float(light), meta)


def compute_solar_phase_variant(route, sunrise: datetime,
                                sunset: datetime) -> SolarPhaseVariant:
    if not route.segments:
        return SolarPhaseVariant("unknown", 1.0, {"reason": "empty_route"})

    t0 = route.segments[0].start_time
    t1 = route.segments[-1].end_time
    mid = t0 + (t1 - t0) / 2

    return classify_phase(mid, sunrise, sunset)
