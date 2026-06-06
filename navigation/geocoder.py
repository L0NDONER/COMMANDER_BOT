#!/usr/bin/env python3
"""
geocoder.py — address-level geocoding for the courier sim.

Three-level pipeline:
  1. Postcode centroid (from postcode JSON)
  2. Address-level refinement — stable hash offset ~15-25m inside cell
  3. Unit-level refinement — flats/barns/cottages get an extra 5-10m nudge
  4. Vec2 projection into local metre frame

Usage:
    from geocoder import geocode_address, geocode_postcode

    vec = geocode_address("12 Magpie Court", "NR19 2FG", ref_lat, ref_lon)
"""
import hashlib
from pathlib import Path
from typing import Optional

from courier_gps import Vec2, latlon_to_vec2

POSTCODES_DIR = Path(__file__).parent / "postcodes"

_UNIT_TOKENS = frozenset([
    "flat", "apt", "apartment", "unit",
    "barn", "cottage", "annex", "annexe",
])


# ---------------------------------------------------------------------------
# Level 0 — postcode centroid
# ---------------------------------------------------------------------------

def _normalise_pc(postcode: str) -> str:
    s = postcode.replace(" ", "").upper()
    return f"{s[:-3]} {s[-3:]}" if len(s) >= 5 else s


def geocode_postcode(postcode: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) centroid for postcode, or None if not found."""
    fn = POSTCODES_DIR / f"{_normalise_pc(postcode).replace(' ', '_')}.json"
    if not fn.exists():
        return None
    import json
    d = json.load(open(fn))
    coords = d.get("coords")
    if not coords or not coords[0]:
        return None
    return float(coords[0]), float(coords[1])


# ---------------------------------------------------------------------------
# Level 1 — address-level refinement (~15-25m)
# ---------------------------------------------------------------------------

def refine_with_address(lat: float, lon: float,
                        address: str) -> tuple[float, float]:
    """Stable deterministic offset from address string. ~15-25m range."""
    h = int(hashlib.md5(address.lower().encode()).hexdigest(), 16)
    dx = ((h % 1000) / 1000.0 - 0.5) * 0.00025
    dy = (((h // 1000) % 1000) / 1000.0 - 0.5) * 0.00025
    return lat + dy, lon + dx


# ---------------------------------------------------------------------------
# Level 2 — unit-level refinement (5-10m for flats/barns/cottages)
# ---------------------------------------------------------------------------

def refine_unit(lat: float, lon: float,
                address: str) -> tuple[float, float]:
    """Extra nudge for sub-address units. No-op if address has no unit marker."""
    if not any(t in address.lower() for t in _UNIT_TOKENS):
        return lat, lon
    h = int(hashlib.sha1(address.encode()).hexdigest(), 16)
    dx = ((h % 1000) / 1000.0 - 0.5) * 0.0001
    dy = (((h // 1000) % 1000) / 1000.0 - 0.5) * 0.0001
    return lat + dy, lon + dx


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def geocode_address(address: str, postcode: str,
                    ref_lat: float, ref_lon: float) -> Optional[dict]:
    """
    Returns:
        {"address", "postcode", "lat", "lon", "vec2": Vec2} or None.
    """
    latlon = geocode_postcode(postcode)
    if latlon is None:
        return None

    lat, lon = latlon
    lat, lon = refine_with_address(lat, lon, address)
    lat, lon = refine_unit(lat, lon, address)
    vec = latlon_to_vec2(lat, lon, ref_lat, ref_lon)

    return {
        "address": address,
        "postcode": postcode,
        "lat": lat,
        "lon": lon,
        "vec2": vec,
    }
