"""Context layer — the tinted lens.

Route-level preconditions (solar phase, weather band, day-of-week, courier
identity, vehicle type) live HERE, never in the vote panel. See
[[variant-design-rules]] rule 5 and [[engine-stays-small]] corollary
(both updated 2026-06-01).

The clean function returns a multiplicative weight in [0, 1] that the
caller folds into its per-step weight chain (next to `KIND_WEIGHTS` in
courier_multivariant.lens). Returning 1.0 is a no-op — the current
default until a real precondition is wired with kill-criteria.

Adding a real gate? Stay symmetric: the gate must be able to return both
<1 (down-weight) and =1 (full weight) on real data. A gate that only
ever down-weights is a thumb on the scale; one that only ever returns 1
is dead code."""
from typing import Optional


def route_weight(route_solar: Optional[dict] = None,
                 route_weather: Optional[dict] = None) -> float:
    """Multiplicative weight for a whole route, derived from route-level
    preconditions. Stub: returns 1.0 (no gating). To wire a real gate:
    multiply in (visibility × weather_band) terms here. Keep them
    documented and A/B testable — the un-gated lag-game must stay
    runnable so any improvement can be attributed to the gate, not
    silently absorbed."""
    return 1.0
