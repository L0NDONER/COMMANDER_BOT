#!/usr/bin/env python3
"""Pure helpers and constants for the eBay pricing pipeline.

The networking, Redis fan-out, and consensus dispatch that used to live here
were collapsed into services/ebay/scout_async.py. This module now holds only
the side-effect-free pieces that scout_async (and the test suite) import.
"""

import logging
import os
import re
import statistics
from typing import Dict, List

from services.ebay.brands import STRONG_BRANDS, SLOW_KEYWORDS

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

MARKETPLACE = "EBAY_GB"

# Vinted discount tiers (eBay median × tier = Vinted list price)
DEFAULT_VINTED_DISCOUNT = 0.72
STRONG_BRAND_DISCOUNT = 0.65
SLOW_KEYWORD_DISCOUNT = 0.40
FAST_SALE_MULTIPLIER = 0.88        # Vinted "Fast Sale" = list × this

# Consensus
CONSENSUS_TIMEOUT_SECONDS = 10

# kv_cache TTLs (seconds)
VISION_CACHE_TTL_SECONDS = 3600
STATS_CACHE_TTL_SECONDS = 3600

# Verdict thresholds (ROI %)
ROI_STRONG_BUY = 150
ROI_BUY = 80
ROI_MAYBE = 30
# Free-stock branch: ROI is mathematically infinite; display sentinel and
# judge on absolute return instead.
FREE_STOCK_MIN_SELL = 5.0
FREE_STOCK_ROI_SENTINEL = 999

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

CONDITION_FILTERS = {
    "new": "1000|1500",
    "used": "3000|4000|5000",
}

# Phrases in the user's caption that mean the item is unused.
# Kept strict — bare "new" matches too much ("new arrival", "newly listed").
NEW_CONDITION_PATTERNS = re.compile(
    r"\b(bnib|bnwt|bnwot|brand\s+new|new\s+in\s+box|still\s+in\s+box|"
    r"sealed|unworn|unused|never\s+worn|never\s+used|new\s+with\s+tags)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------------------

def _title_matches(title: str, query: str, min_tokens: int = 2) -> bool:
    title_lower = title.lower()
    tokens = query.lower().split()
    hits = sum(1 for t in tokens if t in title_lower)
    return hits >= min(min_tokens, len(tokens))


def analyse(items: List[dict], query: str = "") -> Dict:
    prices = []
    for i in items:
        if "price" not in i:
            continue
        title = i.get("title", "")
        if query and title and not _title_matches(title, query):
            continue
        prices.append(float(i["price"]["value"]))
    return {"median": statistics.median(prices)} if prices else {}


def detect_condition(caption: str) -> str:
    if caption and NEW_CONDITION_PATTERNS.search(caption):
        return "new"
    return "used"


def charm(p: float) -> str:
    """Charm price: floor at £0.99, otherwise round(p) - 0.01.

    Rounds (not truncates) so £20.75 → £20.99, not £19.99.
    """
    return f"£{max(0.99, round(p) - 0.01):.2f}"


def diversify_query(base: str, replica: str, condition: str = "used") -> str:
    cond_word = "new" if condition == "new" else "used"
    variants = [base, f"{base} {cond_word}", f"{base} mens", f"{base} womens", f"{base} vintage"]
    return variants[int(os.getenv("WORKER_INDEX", "0")) % len(variants)]


def compute_confidence(vals: List[float]) -> str:
    if not vals:
        return "LOW"
    avg = sum(vals) / len(vals)
    if avg == 0:
        return "LOW"
    ratio = (max(vals) - min(vals)) / avg
    if ratio < 0.20:
        return "HIGH"
    if ratio < 0.40:
        return "MEDIUM"
    return "LOW"


def choose_vinted_discount(query: str) -> float:
    ql = query.lower()
    if any(b in ql for b in STRONG_BRANDS):
        return STRONG_BRAND_DISCOUNT
    if any(k in ql for k in SLOW_KEYWORDS):
        return SLOW_KEYWORD_DISCOUNT
    return DEFAULT_VINTED_DISCOUNT


def generate_listing_draft(query: str, keywords: List[str]) -> Dict[str, str]:
    title = f"{query.title()} - Excellent Condition - {', '.join(keywords[:2])}"
    description = (
        f"{query.title()}\n\n"
        "Details:\n"
        "- Authentic item\n"
        f"- Keywords: {', '.join(keywords)}\n"
        "- Condition: Great pre-owned condition\n\n"
        "Fast shipping! Check my other items for bundle deals."
    )
    # Vinted search is a text search bar, not a hashtag browser. Space-stripped
    # tags (#StoneIsland) don't match a buyer typing "Stone Island" — keep the
    # spaces and use commas so each word is a separate search-indexable token.
    seo_tags = ", ".join(keywords)
    return {"title": title[:80], "description": description, "tags": seo_tags}


# ------------------------------------------------------------------------------
# Verdict math (imported by scout_async.evaluate_with_consensus_saas)
# ------------------------------------------------------------------------------

def _parse_buy_price(raw) -> float:
    """Strip non-digits and parse. Returns 1.0 (and warns) if unparseable."""
    try:
        digits = re.sub(r"[^\d.]", "", str(raw))
        if digits:
            return float(digits)
    except (ValueError, TypeError):
        pass
    LOGGER.warning("Could not parse buy price from %r — defaulting to £1.00", raw)
    return 1.0


def _verdict_from_roi(roi: float) -> str:
    if roi >= ROI_STRONG_BUY:
        return "STRONG BUY"
    if roi >= ROI_BUY:
        return "BUY"
    if roi >= ROI_MAYBE:
        return "MAYBE"
    return "PASS"


def _score(votes: List[Dict], base_query: str, clean_buy: float) -> Dict:
    medians = [v["median"] for v in votes]
    avg_median = statistics.median(medians)
    winner = min(votes, key=lambda v: abs(v["median"] - avg_median))
    LOGGER.info(
        "Consensus: %d votes, winner=%s query=%s",
        len(votes), winner["replica"], winner["query"],
    )

    discount = choose_vinted_discount(base_query)
    sell_price = avg_median * discount

    if clean_buy > 0:
        roi = (sell_price - clean_buy) / clean_buy * 100
        verdict = _verdict_from_roi(roi)
    else:
        roi = FREE_STOCK_ROI_SENTINEL
        verdict = "STRONG BUY" if sell_price >= FREE_STOCK_MIN_SELL else "PASS"

    return {
        "avg_median": avg_median,
        "sell_price": sell_price,
        "confidence": compute_confidence(medians),
        "winner": winner["replica"],
        "roi": roi,
        "verdict": verdict,
    }
