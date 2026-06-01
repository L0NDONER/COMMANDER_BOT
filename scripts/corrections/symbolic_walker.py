"""Motion layer — symbolic substrate.

The geometric pipeline `trace → infer_stops → classify_legs` is replaced
by the manifest path `edges → reconstruct_route → prefix-sector symbols`.
The walk is now a discrete, noise-free, exactly-reversible sequence of
postcode-sector identifiers — no GPS, no coordinates, no binning.

Granularity: 7-char prefix (e.g. "NR19 2BC" → "NR19 2B") matches the
walk-bubble grain from the original (refuted) study — see
[[courier-bubble-signature]]. Per-route alphabet sits in the tens of
sectors, giving the lag-based scorers room to find structure without
collapsing to a near-constant series.

Canonical function: `classify_legs(manifest_key, prefix_len=7)` returns
the integer symbol sequence (sectors interned to ints in first-seen
order). The pipeline imports exactly this one entry point.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courier_lens import load_edges, reconstruct_route  # noqa: E402


def classify_legs(manifest_key: tuple[str, str],
                  prefix_len: int = 7) -> list[int]:
    """Read the breadcrumb manifest at `manifest_key = (mid, date)`,
    reconstruct the ordered postcode route, map each postcode to its
    `prefix_len`-char sector, intern sectors to integer IDs in
    first-seen order, and return the symbol sequence.

    No GPS. No coordinates. Reversal is `list(reversed(seq))` exactly."""
    edges_by = load_edges()
    edges = edges_by.get(manifest_key)
    if not edges:
        return []
    route = reconstruct_route(edges)
    sectors = [pc[:prefix_len] for pc in route]
    intern: dict[str, int] = {}
    out = []
    for s in sectors:
        if s not in intern:
            intern[s] = len(intern)
        out.append(intern[s])
    return out
