#!/usr/bin/env python3
"""Route-level weather classifier — context-layer helper.

Returns a WeatherVariant with `intensity` and `penalty`, both in [0, 1].
`penalty` is the canonical multiplier for a down-weight: 0.0 = clear
conditions, 1.0 = severe weather (heavy rain, high wind, or low
visibility). intensity == penalty here; kept as separate fields so
downstream gating can distinguish "raw severity" from "applied weight".

Normalisation thresholds:
  rain:        10 mm/hr  → fully normalised
  wind:        15 m/s    → fully normalised
  visibility:  5000 m    → clear; below 5km starts to penalise

The maximum of the three normalised scores becomes the intensity — the
worst single factor dominates, matching how a courier experiences
weather (one severe factor wrecks the leg regardless of the others).

Per [[variant-design-rules]] rule 5 and [[engine-stays-small]] corollary,
this output goes into the WEIGHT layer (via `context.route_weight`),
never into the vote panel.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class WeatherSample:
    time: datetime
    rain_mm_per_hr: float
    wind_mps: float
    visibility_m: float


@dataclass
class WeatherVariant:
    intensity: float
    penalty: float
    meta: Dict[str, Any]


def nearest_weather(samples: List[WeatherSample],
                    t: datetime) -> WeatherSample | None:
    if not samples:
        return None
    return min(samples, key=lambda s: abs((s.time - t).total_seconds()))


def compute_weather_variant(route,
                            weather_samples: List[WeatherSample]
                            ) -> WeatherVariant:
    if not route.segments:
        return WeatherVariant(0.0, 0.0, {"reason": "empty_route"})

    t0 = route.segments[0].start_time
    t1 = route.segments[-1].end_time
    mid = t0 + (t1 - t0) / 2

    sample = nearest_weather(weather_samples, mid)
    if sample is None:
        return WeatherVariant(0.0, 0.0, {"reason": "no_weather_data"})

    rain_norm = min(sample.rain_mm_per_hr / 10.0, 1.0)
    wind_norm = min(sample.wind_mps / 15.0, 1.0)
    vis_norm = 1.0 - min(sample.visibility_m / 5000.0, 1.0)

    intensity = max(rain_norm, wind_norm, vis_norm)
    penalty = intensity

    meta = {
        "rain_mm_per_hr": sample.rain_mm_per_hr,
        "wind_mps": sample.wind_mps,
        "visibility_m": sample.visibility_m,
        "rain_norm": rain_norm,
        "wind_norm": wind_norm,
        "vis_norm": vis_norm,
    }

    return WeatherVariant(float(intensity), float(penalty), meta)
