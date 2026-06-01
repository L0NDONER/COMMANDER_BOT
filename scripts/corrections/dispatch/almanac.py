"""Environmental truth for the Dereham operating area.

Parallel to topology.py (ground truth for the map): almanac.py is the
ground truth for the physical environment the courier moves through —
light, dark, moon, season.

Requires: pip install astral

Public API
----------
conditions(date)   → full dict: sunrise / sunset / dawn / dusk /
                     civil twilight / moon phase + name / is_dark now
is_dark(dt=None)   → bool — is it dark right now (or at a given moment)?
moon_phase(date)   → float 0..28, astral convention (0 = new moon,
                                                      14 = full moon)
phase_name(phase)  → human label for a phase value

All datetimes returned are timezone-aware (Europe/London).
"""
from __future__ import annotations

import datetime
import zoneinfo
from functools import lru_cache

from astral import LocationInfo
from astral.sun import sun
from astral import moon

# ── Location ────────────────────────────────────────────────────────────────

DEREHAM = LocationInfo(
    name="Dereham",
    region="UK",
    timezone="Europe/London",
    latitude=52.68,
    longitude=0.94,
)

_TZ = zoneinfo.ZoneInfo("Europe/London")

# ── Moon ────────────────────────────────────────────────────────────────────

_PHASE_LABELS = [
    (1,  "new moon"),
    (6,  "waxing crescent"),
    (8,  "first quarter"),
    (13, "waxing gibbous"),
    (15, "full moon"),
    (20, "waning gibbous"),
    (22, "last quarter"),
    (27, "waning crescent"),
    (29, "new moon"),
]


def phase_name(phase: float) -> str:
    """Human label for an astral moon.phase() value (0..28)."""
    for threshold, label in _PHASE_LABELS:
        if phase < threshold:
            return label
    return "new moon"


def moon_phase(date: datetime.date) -> float:
    """Moon phase for `date` — astral convention: 0 = new, 14 = full."""
    return moon.phase(date)


# ── Sun / conditions ─────────────────────────────────────────────────────────

@lru_cache(maxsize=32)
def _sun(date: datetime.date) -> dict:
    return sun(DEREHAM.observer, date=date, tzinfo=_TZ)


def conditions(date: datetime.date | None = None) -> dict:
    """Return a snapshot of environmental conditions for `date`
    (defaults to today). is_dark is evaluated against the current
    wall-clock time, not the date itself."""
    if date is None:
        date = datetime.date.today()
    s = _sun(date)
    phase = moon_phase(date)
    now = datetime.datetime.now(tz=_TZ)
    dark = now < s["dawn"] or now > s["dusk"]
    return {
        "date":         date,
        "dawn":         s["dawn"],
        "sunrise":      s["sunrise"],
        "noon":         s["noon"],
        "sunset":       s["sunset"],
        "dusk":         s["dusk"],
        "is_dark":      dark,
        "moon_phase":   round(phase, 2),
        "moon_name":    phase_name(phase),
    }


def is_dark(dt: datetime.datetime | None = None) -> bool:
    """True if it is dark in Dereham at `dt` (defaults to now)."""
    if dt is None:
        dt = datetime.datetime.now(tz=_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    s = _sun(dt.date())
    return dt < s["dawn"] or dt > s["dusk"]


# ── Weather ─────────────────────────────────────────────────────────────────

CLEAR      = "CLEAR"
HEAVY_FOG  = "HEAVY_FOG"
LIGHT_FOG  = "LIGHT_FOG"
HEAVY_RAIN = "HEAVY_RAIN"
HEAVY_SNOW = "HEAVY_SNOW"


def get_weather_multiplier(weather_condition: str,
                           gritted: bool = False) -> float:
    """Transit time multiplier for weather conditions.
    Applied to drive legs — reduces effective speed.
    gritted=True applies to legs on gritted bus routes:
    snow penalty is halved (salt + grit restore partial traction)."""
    multipliers = {
        CLEAR:      1.0,
        LIGHT_FOG:  1.15,
        HEAVY_FOG:  1.40,
        HEAVY_RAIN: 1.20,
        HEAVY_SNOW: 1.60,
    }
    m = multipliers.get(weather_condition, 1.0)
    if gritted and m > 1.0:
        m = 1.0 + (m - 1.0) * 0.5   # gritting halves the penalty
    return m


def get_temp_dwell_multiplier(temp_c: float) -> float:
    """Dwell time multiplier from temperature.
    Below 0°C: icy paths, gloves slowing scan/sign, cautious footing.
    5% per degree below zero, capped at 1.25."""
    if temp_c >= 0.0:
        return 1.0
    return min(1.25, 1.0 + abs(temp_c) * 0.05)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dereham environmental conditions")
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    date = (datetime.date.fromisoformat(args.date)
            if args.date else datetime.date.today())
    c = conditions(date)

    fmt = "%H:%M %Z"
    print(f"Dereham  {c['date']}")
    print(f"  dawn    {c['dawn'].strftime(fmt)}")
    print(f"  sunrise {c['sunrise'].strftime(fmt)}")
    print(f"  noon    {c['noon'].strftime(fmt)}")
    print(f"  sunset  {c['sunset'].strftime(fmt)}")
    print(f"  dusk    {c['dusk'].strftime(fmt)}")
    print(f"  dark now: {c['is_dark']}")
    print(f"  moon    {c['moon_phase']:.1f}/28  ({c['moon_name']})")
