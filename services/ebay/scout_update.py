#!/usr/bin/env python3
"""
Enhanced multi-replica pricing system with Listing Generation.
"""

import base64
import hashlib
import json
import logging
import os
import socket
import statistics
import sys
import time
import re
from typing import Dict, List

import redis
import requests

# --- BOOTSTRAP PATHING ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(1, PROJECT_ROOT)

# --- LOCAL IMPORTS ---
from credentials import EBAY_APP_ID, EBAY_SECRET
try:
    from brands import is_low_value, STRONG_BRANDS, SLOW_KEYWORDS
except ImportError:
    from services.ebay.brands import is_low_value, STRONG_BRANDS, SLOW_KEYWORDS

import scout_vision

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

MARKETPLACE = "EBAY_GB"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

DEFAULT_VINTED_DISCOUNT = 0.50
CONSENSUS_REQUIRED = 3
CONSENSUS_TIMEOUT_SECONDS = 10
REDIS_TTL_SECONDS = 300

ANARCHY_MODE = os.getenv("ANARCHY_MODE", "true").lower() == "true"

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

def detect_condition(caption: str) -> str:
    if caption and NEW_CONDITION_PATTERNS.search(caption):
        return "new"
    return "used"


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Redis Core Functions
# ------------------------------------------------------------------------------

def get_redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)





# ------------------------------------------------------------------------------
# Consensus Voting
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
# eBay API Operations
# ------------------------------------------------------------------------------

def get_token() -> str:
    r = get_redis()
    cached = r.get("ebay_token")
    if cached:
        return cached
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_SECRET}".encode()).decode()
    res = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=10
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
        params={"q": query, "limit": 15, "filter": f"conditionIds:{{{cond_ids}}},price:[5..500],priceCurrency:GBP"},
        timeout=10
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
            r.setex(cache_key, 3600, json.dumps(stats))
        return stats
    except Exception as e:
        LOGGER.error(f"eBay Error: {e}")
        return {}


# ------------------------------------------------------------------------------
# Logic & Listing Generation
# ------------------------------------------------------------------------------

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
    return {
        "title": title[:80],
        "description": description,
        "tags": seo_tags
    }

def diversify_query(base: str, replica: str, condition: str = "used") -> str:
    cond_word = "new" if condition == "new" else "used"
    variants = [base, f"{base} {cond_word}", f"{base} mens", f"{base} womens", f"{base} vintage"]
    return variants[int(os.getenv("WORKER_INDEX", "0")) % len(variants)]

def compute_confidence(vals: List[float]) -> str:
    if not vals: return "LOW"
    avg = sum(vals) / len(vals)
    spread = max(vals) - min(vals)
    if avg == 0: return "LOW"
    ratio = spread / avg
    if ratio < 0.20: return "HIGH"
    if ratio < 0.40: return "MEDIUM"
    return "LOW"
charm = lambda p: f"£{max(0.99, int(p) - 0.01):.2f}"

def choose_vinted_discount(query: str) -> float:
    ql = query.lower()
    if any(b in ql for b in STRONG_BRANDS): return 0.65
    if any(k in ql for k in SLOW_KEYWORDS): return 0.40
    return DEFAULT_VINTED_DISCOUNT
def evaluate_with_consensus(image_path: str, buy_price: str) -> Dict:
    replica = socket.gethostname()
    r = get_redis()

    try:
        numeric_price = re.sub(r'[^\d.]', '', str(buy_price))
        clean_buy = float(numeric_price) if numeric_price else 1.0
    except (ValueError, TypeError):
        clean_buy = 1.0

    condition = detect_condition(str(buy_price))
    LOGGER.info("Caption condition=%s", condition)

    with open(image_path, "rb") as f:
        img_hash = hashlib.md5(f.read()).hexdigest()

    vision_data = r.get(f"vision:{img_hash}")
    if vision_data:
        p = json.loads(vision_data)
        base_query, keywords = p["query"], p["keywords"]
    else:
        base_query, keywords = scout_vision.identify_item(image_path)
        r.setex(f"vision:{img_hash}", 3600, json.dumps({"query": base_query, "keywords": keywords}))

    # Clear stale votes from a prior run on this image
    r.delete(f"votes:{img_hash}")

    # Dispatch to workers — include base_query so they can search
    r.publish("scout_tasks", json.dumps({
        "img_hash": img_hash,
        "base_query": base_query,
        "condition": condition,
    }))

    query = diversify_query(base_query, replica, condition) if ANARCHY_MODE else base_query
    stats = get_stats(query, replica, condition)
    if "median" in stats:
        cast_vote(img_hash, replica, stats["median"], query)

    votes = []
    for _ in range(CONSENSUS_TIMEOUT_SECONDS):
        votes = get_votes(img_hash)
        if len(votes) >= CONSENSUS_REQUIRED:
            break
        time.sleep(1)

    if not votes:
        return {"status": "error", "message": "No pricing data collected."}

    medians = [v["median"] for v in votes]
    avg_median = statistics.median(medians)

    winner_data = min(votes, key=lambda v: abs(v["median"] - avg_median))
    winner_replica = winner_data["replica"]
    LOGGER.info("Consensus: %d votes, winner=%s", len(votes), winner_replica)

    confidence = compute_confidence(medians)
    sell_price = avg_median * choose_vinted_discount(base_query)
    profit = sell_price - clean_buy
    roi = (profit / clean_buy) * 100 if clean_buy > 0 else 0

    final_verdict = "STRONG BUY" if roi >= 150 else "BUY" if roi >= 80 else "MAYBE" if roi >= 30 else "PASS"

    listing = generate_listing_draft(base_query, keywords)


    return {
        "median": round(avg_median, 2),
        "median_pretty": charm(avg_median),
        "sell_for": charm(avg_median * choose_vinted_discount(base_query)),
        "sell_price_num": round(avg_median * choose_vinted_discount(base_query), 2),
        "fast_sale": charm(avg_median * choose_vinted_discount(base_query) * 0.88),
        "confidence": confidence,
        "winner": winner_replica,
        "roi": round(roi, 0),
        "query": base_query,
        "status": "success",
        "verdict": final_verdict,
        "title": listing["title"],
        "description": listing["description"],
        "tags": listing["tags"]
    }

if __name__ == "__main__":
    if len(sys.argv) == 3:
        print(json.dumps(evaluate_with_consensus(sys.argv[1], sys.argv[2]), indent=2))
