"""Outside-in router — sort an arbitrary list of postcodes by distance
from home (furthest first), the way the dispatcher would.

Usage:
    echo "NR19 2BB NR19 1AD NR19 2ET" | python3 scripts/corrections/outside_in.py
    python3 scripts/corrections/outside_in.py < manifest.txt
    python3 scripts/corrections/outside_in.py --home "NR19 1AA" < manifest.txt

Input format: any whitespace-separated postcodes. Lines, commas, or
spaces all OK. Unknown postcodes are reported and dropped.

This is the deterministic version of the dispatcher's routing
heuristic — paired with [[courier-bubble-signature]] it's the control
that demonstrates the "marginal arrow" was 100% the dispatcher's
outside-in ordering, 0% time-direction signal."""
import argparse
import json
import math
import re
import sys
from pathlib import Path

ONS_PATH = Path(__file__).resolve().parents[2] / "ons_nr_postcodes.json"
DEFAULT_HOME = "NR20 4AW"


def haversine(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6371000.0
    p = math.radians(lat2 - lat1)
    q = math.radians(lon2 - lon1)
    x = (math.sin(p / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(q / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def load_centers() -> dict:
    with open(ONS_PATH) as f:
        return {k: tuple(v) for k, v in json.load(f).items()}


def parse_postcodes(text: str) -> list[str]:
    """Pull out anything that looks like a UK postcode (loose pattern)."""
    return re.findall(r"\b[A-Z]{1,2}\d{1,2}\s?\d?[A-Z]{0,2}\b",
                      text.upper())


def outside_in(postcodes: list[str], home: str,
               centers: dict) -> list[tuple[str, float]]:
    """Return [(postcode, distance_m), …] sorted furthest-first.
    Unknown postcodes get distance = None and sort to the end."""
    home_pt = centers.get(home)
    if home_pt is None:
        raise SystemExit(f"home postcode {home!r} not in ONS data")
    rows = []
    for pc in dict.fromkeys(postcodes):  # de-dupe preserving order
        pt = centers.get(pc)
        d = haversine(home_pt, pt) if pt else None
        rows.append((pc, d))
    return sorted(rows, key=lambda r: (-1 if r[1] is None else r[1]),
                  reverse=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=DEFAULT_HOME)
    args = ap.parse_args()
    text = sys.stdin.read()
    postcodes = parse_postcodes(text)
    if not postcodes:
        print("no postcodes parsed from stdin", file=sys.stderr)
        return
    centers = load_centers()
    ordered = outside_in(postcodes, args.home, centers)
    print(f"home: {args.home}   stops: {len(ordered)}")
    print(f"  {'#':>3}  {'postcode':<10}  {'distance':>9}")
    for i, (pc, d) in enumerate(ordered, 1):
        dstr = f"{d/1000:>6.2f}km" if d is not None else "  unknown"
        print(f"  {i:>3}  {pc:<10}  {dstr}")


if __name__ == "__main__":
    main()
