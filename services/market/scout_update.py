#!/usr/bin/env python3
"""Pure helpers and constants for the market pricing pipeline.  # [ZWJheQ==]

The networking, Redis fan-out, and consensus dispatch that used to live here
were collapsed into services/market/scout_async.py. This module now holds only
the side-effect-free pieces that scout_async (and the test suite) import.
"""

import logging
import math
import os
import re
import statistics
from datetime import datetime, timezone
from typing import Dict, List

from services.market.brands import STRONG_BRANDS, SLOW_KEYWORDS

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

MARKETPLACE = "EBAY_GB"

# site discount tiers (market median × tier = site list price)  # [dmludGVk] [ZWJheQ==]
DEFAULT_SITE_DISCOUNT = 0.75
STRONG_BRAND_DISCOUNT = 0.80
SLOW_KEYWORD_DISCOUNT = 0.40
FAST_SALE_MULTIPLIER = 0.88        # site "Fast Sale" = list × this  # [dmludGVk]

# Consensus — tiered fan-out deadlines (passed into gather_votes per source).
# market is fast + reliable: keep the hot path snappy. site is slow + polite  # [ZWJheQ==] [dmludGVk]
# (behind the home-IP SOCKS tunnel): give it a generous window so the timeout
# stops truncating real comps and biasing the median-of-medians.
CONSENSUS_TIMEOUT_SECONDS = 10          # legacy default / shared fallback
MARKET_TIMEOUT_SECONDS = 8                # Tier 1 (fast)
SITE_TIMEOUT_SECONDS = 25             # Tier 2 (polite)

# Votes needed from the base-anchor bucket alone (variant_idx == 0) before we
# trust it without the suffix bucket. A majority of the 5-replica fan-out, not
# the same number as MIN_VOTES_FOR_CONSENSUS (consensus_engine.py) — that one
# is the overall floor across every bucket combined; this one gates whether
# the base bucket is trustworthy on its own. Keeping them separate means a
# single flaky base replica can't silently demote the verdict to the
# suffix-biased pooled path.
MIN_BUCKET_VOTES = 3

# kv_cache TTLs (seconds)
VISION_CACHE_TTL_SECONDS = 3600
STATS_CACHE_TTL_SECONDS = 3600

# Verdict thresholds (ROI %)
ROI_STRONG_BUY = 150
ROI_BUY = 80
ROI_MAYBE = 30
# Thin comp data should make the verdict more cautious without hiding the true
# ROI. We judge the verdict on a confidence-discounted ROI so a wildly
# profitable LOW item still rates STRONG, but a marginal one slips a tier.
CONFIDENCE_ROI_HAIRCUT = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5}
# Free-stock branch: ROI is mathematically infinite; display sentinel and
# judge on absolute return instead.
FREE_STOCK_MIN_SELL = 5.0
FREE_STOCK_ROI_SENTINEL = 999

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

FRESHNESS_HALFLIFE_DAYS = 15  # listing weight halves every N days (exp decay, no hard cutoff)

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


def _trust_score(pct: float) -> int:
    if pct == 100:
        return 3
    if pct >= 98:
        return 2
    if pct >= 95:
        return 1
    return 0


def _weighted_median(values: List[float], weights: List[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    total = sum(w for _, w in pairs)
    half = total / 2
    cumulative = 0.0
    for idx, (v, w) in enumerate(pairs):
        cumulative += w
        if cumulative > half:
            return v
        if abs(cumulative - half) < 1e-10 and idx + 1 < len(pairs):
            return (v + pairs[idx + 1][0]) / 2
    return pairs[-1][0]


def analyse(items: List[dict], query: str = "") -> Dict:
    prices: List[float] = []
    weights: List[float] = []
    trust_scores: List[int] = []
    now = datetime.now(timezone.utc)
    for i in items:
        if "price" not in i:
            continue
        title = i.get("title", "")
        if query and title and not _title_matches(title, query):
            continue
        country = i.get("itemLocation", {}).get("country", "")
        if country and country != "GB":
            continue
        created = i.get("itemCreationDate")
        age_days = 0.0
        if created:
            try:
                age_days = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 86400
            except ValueError:
                pass
        prices.append(float(i["price"]["value"]))
        weights.append(math.exp(-age_days / FRESHNESS_HALFLIFE_DAYS))
        pct = i.get("seller", {}).get("feedbackPercentage")
        if pct is not None:
            trust_scores.append(_trust_score(float(pct)))
    if not prices:
        return {}
    result = {"median": _weighted_median(prices, weights)}
    if trust_scores:
        result["trust"] = round(statistics.median(trust_scores))
    return result


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


def choose_site_discount(query: str) -> float:
    ql = query.lower()
    if any(b in ql for b in STRONG_BRANDS):
        return STRONG_BRAND_DISCOUNT
    if any(k in ql for k in SLOW_KEYWORDS):
        return SLOW_KEYWORD_DISCOUNT
    return DEFAULT_SITE_DISCOUNT


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
    # site search is a text search bar, not a hashtag browser. Space-stripped  # [dmludGVk]
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
    # Three median levels: replica_median (per-vote "median", one search's
    # listings), bucket_median (a variant's replicas), verdict_median (what
    # the pricing decision is actually based on). variant_idx == 0 is always
    # the base anchor (build_variants puts it first) — the trusted query.
    # Suffix variants are a backup against a base-query miss, not a pricing
    # input: they only enter verdict_median when the base bucket is too thin
    # to stand alone, and even then only pooled alongside the base votes, so
    # a sparse-but-present base sample still pulls its weight.
    base_votes = [v for v in votes if v.get("variant_idx", 0) == 0]

    if len(base_votes) >= MIN_BUCKET_VOTES:
        path = "base"
        bucket_votes = base_votes
    else:
        path = "pooled"
        bucket_votes = votes  # caller already enforced len(votes) >= MIN_VOTES_FOR_CONSENSUS

    bucket_medians = [v["median"] for v in bucket_votes]
    verdict_median = statistics.median(bucket_medians)
    winner = min(bucket_votes, key=lambda v: abs(v["median"] - verdict_median))
    LOGGER.info(
        "Consensus: path=%s base_votes=%d total_votes=%d winner=%s query=%s",
        path, len(base_votes), len(votes), winner["replica"], winner["query"],
    )

    discount = choose_site_discount(base_query)
    sell_price = verdict_median * discount
    confidence = compute_confidence(bucket_medians)

    if clean_buy > 0:
        roi = (sell_price - clean_buy) / clean_buy * 100
        # Display true ROI; judge the verdict on the haircut so thin comp data
        # demotes marginal items but leaves big winners alone.
        verdict = _verdict_from_roi(roi * CONFIDENCE_ROI_HAIRCUT[confidence])
    else:
        roi = FREE_STOCK_ROI_SENTINEL
        verdict = "STRONG BUY" if sell_price >= FREE_STOCK_MIN_SELL else "PASS"

    trust_vals = [v["trust"] for v in bucket_votes if "trust" in v]
    trust = round(statistics.median(trust_vals)) if trust_vals else None
    return {
        "verdict_median": verdict_median,
        "sell_price": sell_price,
        "confidence": confidence,
        "winner": winner["replica"],
        "roi": roi,
        "verdict": verdict,
        "trust": trust,
    }
