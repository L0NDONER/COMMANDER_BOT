#!/usr/bin/env python3
"""
arb_lab — measure the TRUE eBay<->Vinted arbitrage per logged sale, no guessing.

Replaces the armchair scalar (DEFAULT_VINTED_DISCOUNT = 0.72) with measurement:
each row is a real Vinted sale (ground truth, [[project_vinted_data]]) joined to a
live eBay GB used comp. Delta_arb = vinted_sold / ebay_comp — what you actually
realised vs what eBay comps said the thing was worth.

Honest scope: this is n=1 per category. Four points across four categories cannot
establish a per-category coefficient — a single broad-query comp vs one specific
sale is noisy. It tells us the SHAPE (is the realised ratio near 0.72? does it
swing wildly?), which is the precondition for a lens, not the lens itself.

Standalone, real eBay calls. Reuses fossil_lab's token/search path.
"""

import statistics as stats
from dataclasses import dataclass
from typing import Optional

from fossil_lab import _ebay_token, _title_matches, EBAY_CONDITION_IDS, EBAY_LIMIT


@dataclass
class Sale:
    desc: str
    sold: float
    category: str
    ebay_query: str   # how we look up the comp


SALES = [
    Sale("Levi's 501 W36 L30",     13.50, "Denim",      "levis 501 jeans"),
    Sale("Puma Arsenal Jersey",    17.99, "Sportswear", "puma arsenal shirt"),
    Sale("Ringspun Ringer Tee",    10.99, "Streetwear", "ringspun t shirt"),
    Sale("Crew Clothing Polo",     10.00, "Casual",     "crew clothing polo"),
]


def ebay_comp(token: str, query: str):
    """Return (median, coverage) for GB used comps, or (None, 0)."""
    import requests
    res = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"q": query, "limit": EBAY_LIMIT,
                "filter": f"conditionIds:{{{EBAY_CONDITION_IDS}}},price:[3..500],priceCurrency:GBP"},
        timeout=30,
    )
    res.raise_for_status()
    prices = []
    for i in res.json().get("itemSummaries", []):
        if "price" not in i or not _title_matches(i.get("title", ""), query):
            continue
        if (i.get("itemLocation", {}).get("country", "GB") or "GB") != "GB":
            continue
        try:
            prices.append(float(i["price"]["value"]))
        except (KeyError, ValueError, TypeError):
            continue
    return (stats.median(prices) if prices else None), len(prices)


def main() -> None:
    token = _ebay_token()
    print(f"\n{'Description':<22} | {'Price':>6} | {'Category':<11} | {'eBay':>6} | {'cov':>3} | {'Δarb':>5}")
    print("-" * 70)
    ratios = []
    for s in SALES:
        comp, cov = ebay_comp(token, s.ebay_query)
        if comp:
            r = s.sold / comp
            ratios.append((s.category, r))
            cs, rs = f"£{comp:.0f}", f"{r:.2f}"
        else:
            cs, rs = " -- ", " -- "
        print(f"{s.desc:<22} | £{s.sold:>4.0f} | {s.category:<11} | {cs:>6} | {cov:>3} | {rs:>5}")

    print(f"\n(comp query used: " + "; ".join(f"{s.category}={s.ebay_query!r}" for s in SALES) + ")")
    if len(ratios) >= 2:
        vals = [r for _, r in ratios]
        print(f"\nrealised ratios: {', '.join(f'{c}={r:.2f}' for c, r in ratios)}")
        print(f"mean={stats.mean(vals):.2f}  spread={max(vals)-min(vals):.2f}  "
              f"vs armchair scalar 0.72")
        print("\nRead: if these cluster near one value, 0.72-ish is fine and there's no")
        print("lens. If they swing hard by category, the scalar is mispricing whole")
        print("categories — but you need many sales/category to trust the per-category #.")


if __name__ == "__main__":
    main()
