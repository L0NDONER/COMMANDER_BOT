#!/usr/bin/env python3
"""Multi-replica eBay pricing with consensus voting and Vinted listing generation."""

import base64
import hashlib
import json
import logging
import os
import re
import socket
import statistics
import sys
import time
from typing import Dict, List, Tuple

import redis
import requests

from credentials import EBAY_APP_ID, EBAY_SECRET
from services.ebay import scout_vision
from services.ebay.brands import is_low_value, STRONG_BRANDS, SLOW_KEYWORDS

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

MARKETPLACE = "EBAY_GB"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Vinted discount tiers (eBay median × tier = Vinted list price)
DEFAULT_VINTED_DISCOUNT = 0.72
STRONG_BRAND_DISCOUNT = 0.65
SLOW_KEYWORD_DISCOUNT = 0.40
FAST_SALE_MULTIPLIER = 0.88        # Vinted "Fast Sale" = list × this

# Consensus
CONSENSUS_REQUIRED = 3
CONSENSUS_TIMEOUT_SECONDS = 10
CONSENSUS_POLL_INTERVAL_SECONDS = 0.1

# Redis TTLs
REDIS_TTL_SECONDS = 300
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

ANARCHY_MODE = os.getenv("ANARCHY_MODE", "true").lower() == "true"
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

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# Redis
# ------------------------------------------------------------------------------

_REDIS_CLIENT = None  # lazy singleton — one ConnectionPool shared across calls


def get_redis() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _REDIS_CLIENT


# ------------------------------------------------------------------------------
# Consensus voting
# ------------------------------------------------------------------------------

def cast_vote(img_hash: str, replica: str, median: float, query: str) -> None:
    key = f"votes:{img_hash}"
    payload = json.dumps({"median": median, "query": query, "replica": replica})
    r = get_redis()
    r.hset(key, replica, payload)
    r.expire(key, REDIS_TTL_SECONDS)


def get_votes(img_hash: str) -> List[Dict]:
    r = get_redis()
    raw = r.hgetall(f"votes:{img_hash}")
    votes = []
    for v in raw.values():
        try:
            votes.append(json.loads(v))
        except json.JSONDecodeError:
            continue
    return votes


# ------------------------------------------------------------------------------
# eBay API
# ------------------------------------------------------------------------------

def get_token() -> str:
    r = get_redis()
    cached = r.get("ebay_token")
    if cached:
        return cached
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_SECRET}".encode()).decode()
    res = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    token = data["access_token"]
    ttl = max(60, data.get("expires_in", 7200) - 60)
    r.setex("ebay_token", ttl, token)
    return token


def search_listings(query: str, token: str, condition: str = "used") -> List[dict]:
    cond_ids = CONDITION_FILTERS.get(condition, CONDITION_FILTERS["used"])
    res = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE},
        params={
            "q": query,
            "limit": 15,
            "filter": f"conditionIds:{{{cond_ids}}},price:[5..500],priceCurrency:GBP",
        },
        timeout=10,
    )
    res.raise_for_status()
    return res.json().get("itemSummaries", [])


def analyse(items: List[dict]) -> Dict:
    prices = sorted(float(i["price"]["value"]) for i in items if "price" in i)
    return {"median": prices[len(prices) // 2]} if prices else {}


def get_stats(query: str, replica: str = "default", condition: str = "used") -> Dict:
    if is_low_value(query):
        return {}
    r = get_redis()
    cache_key = f"stats:{condition}:{query.lower()}"
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        token = get_token()
        stats = analyse(search_listings(query, token, condition))
        if stats:
            r.setex(cache_key, STATS_CACHE_TTL_SECONDS, json.dumps(stats))
        return stats
    except Exception:
        LOGGER.exception("eBay query failed for %r (cond=%s)", query, condition)
        return {}


# ------------------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------------------

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
    seo_tags = "#" + " #".join([k.replace(" ", "") for k in keywords])
    return {"title": title[:80], "description": description, "tags": seo_tags}


# ------------------------------------------------------------------------------
# Consensus pipeline helpers
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


def _md5_file(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _vision_lookup(r: redis.Redis, img_hash: str, image_path: str) -> Tuple[str, List[str]]:
    cached = r.get(f"vision:{img_hash}")
    if cached:
        p = json.loads(cached)
        return p["query"], p["keywords"]
    base_query, keywords = scout_vision.identify_item(image_path)
    r.setex(
        f"vision:{img_hash}",
        VISION_CACHE_TTL_SECONDS,
        json.dumps({"query": base_query, "keywords": keywords}),
    )
    return base_query, keywords


def _dispatch_workers(r: redis.Redis, img_hash: str, base_query: str, condition: str) -> None:
    r.delete(f"votes:{img_hash}")  # clear stale votes from a prior run on this image
    r.publish("scout_tasks", json.dumps({
        "img_hash": img_hash,
        "base_query": base_query,
        "condition": condition,
    }))


def _wait_for_consensus(img_hash: str) -> List[Dict]:
    iterations = int(CONSENSUS_TIMEOUT_SECONDS / CONSENSUS_POLL_INTERVAL_SECONDS)
    votes: List[Dict] = []
    for _ in range(iterations):
        votes = get_votes(img_hash)
        if len(votes) >= CONSENSUS_REQUIRED:
            break
        time.sleep(CONSENSUS_POLL_INTERVAL_SECONDS)
    return votes


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
    LOGGER.info("Consensus: %d votes, winner=%s", len(votes), winner["replica"])

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


# ------------------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------------------

def evaluate_with_consensus(image_path: str, buy_price: str) -> Dict:
    replica = socket.gethostname()
    r = get_redis()

    clean_buy = _parse_buy_price(buy_price)
    condition = detect_condition(str(buy_price))
    LOGGER.info("Caption condition=%s buy=£%.2f", condition, clean_buy)

    img_hash = _md5_file(image_path)
    base_query, keywords = _vision_lookup(r, img_hash, image_path)
    _dispatch_workers(r, img_hash, base_query, condition)

    # Leader casts its own vote alongside the workers
    query = diversify_query(base_query, replica, condition) if ANARCHY_MODE else base_query
    stats = get_stats(query, replica, condition)
    if "median" in stats:
        cast_vote(img_hash, replica, stats["median"], query)

    votes = _wait_for_consensus(img_hash)
    if not votes:
        return {"status": "error", "message": "No pricing data collected."}

    scored = _score(votes, base_query, clean_buy)
    listing = generate_listing_draft(base_query, keywords)

    sell_price = scored["sell_price"]
    return {
        "status": "success",
        "median": round(scored["avg_median"], 2),
        "median_pretty": charm(scored["avg_median"]),
        "sell_for": charm(sell_price),
        "sell_price_num": round(sell_price, 2),
        "fast_sale": charm(sell_price * FAST_SALE_MULTIPLIER),
        "confidence": scored["confidence"],
        "winner": scored["winner"],
        "roi": round(scored["roi"], 0),
        "verdict": scored["verdict"],
        "query": base_query,
        "title": listing["title"],
        "description": listing["description"],
        "tags": listing["tags"],
    }


if __name__ == "__main__":
    if len(sys.argv) == 3:
        print(json.dumps(evaluate_with_consensus(sys.argv[1], sys.argv[2]), indent=2))
