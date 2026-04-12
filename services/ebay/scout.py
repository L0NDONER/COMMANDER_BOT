#!/usr/bin/env python3
"""Vinted-aware pricing and decision support using eBay Browse API data.

Design notes:
- eBay remains the market reference because it provides a wider price signal.
- Final pricing and verdicts are adjusted down for Vinted to avoid optimistic buys.
- The module keeps the existing public get_stats() entry point and adds
  evaluate_vinted() for downstream handlers.
- Mock mode is preserved for testing without live API access.
"""

import base64
import logging
import random
import re
import sys
from typing import Dict, List

import requests

sys.path.insert(0, "/home/martin/ansible/commander")
from credentials import EBAY_APP_ID, EBAY_SECRET

from services.ebay.brands import STRONG_BRANDS, SLOW_KEYWORDS


# Configuration
MARKETPLACE = "EBAY_GB"
SANDBOX = False
MOCK = False

API_BASE = "https://api.sandbox.ebay.com" if SANDBOX else "https://api.ebay.com"
AUTH_BASE = "https://api.sandbox.ebay.com" if SANDBOX else "https://api.ebay.com"

EBAY_FEE_RATE = 0.135
EBAY_POSTAGE = 3.85
VINTED_BUYER_BUFFER = 0.00
VINTED_POSTAGE = 0.00  # buyer pays postage on Vinted

DEFAULT_VINTED_DISCOUNT = 0.72
STRONG_BRAND_VINTED_DISCOUNT = 0.82
SLOW_ITEM_VINTED_DISCOUNT = 0.62
FAST_SALE_EXTRA_DISCOUNT = 0.12

LOGGER = logging.getLogger(__name__)

MOCK_DATA = {
    "barbour": (28.00, 67.00, 145.00, 14),
    "ralph lauren": (12.00, 38.00, 85.00, 22),
    "levi": (14.00, 32.00, 68.00, 31),
    "fred perry": (10.00, 28.00, 55.00, 18),
    "stone island": (85.00, 195.00, 380.00, 8),
    "cp company": (65.00, 160.00, 320.00, 6),
    "lacoste": (10.00, 25.00, 52.00, 24),
    "adidas": (8.00, 22.00, 48.00, 35),
    "dr martens": (18.00, 45.00, 90.00, 19),
    "lego": (12.00, 42.00, 120.00, 27),
    "denby": (4.00, 14.00, 35.00, 16),
    "portmeirion": (5.00, 18.00, 42.00, 12),
    "technics": (45.00, 120.00, 280.00, 9),
    "default": (8.00, 25.00, 65.00, 12),
}


def get_mock_stats(query: str) -> Dict[str, object]:
    """Return realistic mock price data based on query keywords."""
    query_lower = query.lower()
    stats = MOCK_DATA["default"]

    for keyword, data in MOCK_DATA.items():
        if keyword in query_lower:
            stats = data
            break

    low, median, high, count = stats

    return {
        "count": count,
        "low": round(low * random.uniform(0.85, 1.15), 2),
        "median": round(median * random.uniform(0.90, 1.10), 2),
        "high": round(high * random.uniform(0.85, 1.15), 2),
        "mock": True,
    }


def get_token() -> str:
    """Request an OAuth token for the eBay API."""
    credentials = base64.b64encode(
        f"{EBAY_APP_ID}:{EBAY_SECRET}".encode("utf-8")
    ).decode("utf-8")

    response = requests.post(
        f"{AUTH_BASE}/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=(
            "grant_type=client_credentials"
            "&scope=https://api.ebay.com/oauth/api_scope"
        ),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def search_listings(query: str, token: str, limit: int = 20) -> List[dict]:
    """Search eBay used listings in GBP for the given query."""
    response = requests.get(
        f"{API_BASE}/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        },
        params={
            "q": query,
            "limit": limit,
            "filter": (
                "conditionIds:{3000|4000|5000},"
                "price:[5..500],priceCurrency:GBP"
            ),
            "sort": "bestMatch",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("itemSummaries", [])


def analyse(items: List[dict]) -> Dict[str, object]:
    """Create trimmed price stats from eBay result items."""
    prices: List[float] = []

    for item in items:
        try:
            prices.append(float(item.get("price", {}).get("value", 0)))
        except (TypeError, ValueError):
            LOGGER.debug("Skipping unparseable item price: %s", item)

    if not prices:
        return {"error": "No listings found"}

    prices.sort()
    trim = max(1, len(prices) // 10)
    trimmed = prices[trim:-trim] if len(prices) > 4 else prices

    return {
        "count": len(prices),
        "low": round(trimmed[0], 2),
        "median": round(trimmed[len(trimmed) // 2], 2),
        "high": round(trimmed[-1], 2),
        "mock": False,
    }


def get_stats(query: str) -> Dict[str, object]:
    """Get price stats from mock data or live eBay API."""
    if MOCK:
        return get_mock_stats(query)

    token = get_token()
    items = search_listings(query, token)
    return analyse(items)


def normalise_text(text: str) -> str:
    """Collapse repeated spaces and trim leading/trailing whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def is_strong_brand(query: str) -> bool:
    """Return True when the query contains a stronger clothing brand."""
    query_lower = query.lower()
    return any(brand in query_lower for brand in STRONG_BRANDS)


def is_slow_item(query: str) -> bool:
    """Return True for categories that tend to move slower on Vinted."""
    query_lower = query.lower()
    return any(keyword in query_lower for keyword in SLOW_KEYWORDS)


def choose_vinted_discount(query: str) -> float:
    """Pick a Vinted adjustment multiplier from query keywords."""
    if is_strong_brand(query):
        return STRONG_BRAND_VINTED_DISCOUNT

    if is_slow_item(query):
        return SLOW_ITEM_VINTED_DISCOUNT

    return DEFAULT_VINTED_DISCOUNT


def calculate_vinted_prices(query: str, ebay_median: float) -> Dict[str, float]:
    """Return realistic Vinted prices based on eBay median."""
    list_discount = choose_vinted_discount(query)
    list_price = round(ebay_median * list_discount, 2)
    fast_price = round(list_price * (1.0 - FAST_SALE_EXTRA_DISCOUNT), 2)

    return {
        "list_price": list_price,
        "fast_price": fast_price,
        "discount": list_discount,
    }


def calculate_profit(buy_price: float, sell_price: float) -> Dict[str, float]:
    """Return simple sell economics for Vinted-style pricing."""
    fees = round(sell_price * VINTED_BUYER_BUFFER, 2)
    net = round(sell_price - fees - VINTED_POSTAGE, 2)
    profit = round(net - buy_price, 2)
    roi = round((profit / buy_price) * 100, 0) if buy_price > 0 else 0.0

    return {
        "fees": fees,
        "postage": VINTED_POSTAGE,
        "net": net,
        "profit": profit,
        "roi": roi,
    }


def build_title(query: str) -> str:
    """Build a clean Vinted title from the search query."""
    title = normalise_text(query)
    words = [word.capitalize() if not word.isupper() else word for word in title.split()]
    return " ".join(words)[:100]


def build_description(query: str) -> str:
    """Build a generic ready-to-paste Vinted description."""
    title = build_title(query)
    return (
        f"{title}\n\n"
        "Good used condition.\n"
        "Any obvious flaws should be visible in the photos.\n"
        "Open to sensible offers.\n"
        "Posted promptly."
    )


def verdict(buy_price: float, stats: Dict[str, object], query: str) -> Dict[str, object]:
    """Build a Vinted-aware verdict using eBay as price reference."""
    if "error" in stats:
        return {"verdict": "❓ UNKNOWN", "reason": "No price data found"}

    ebay_median = float(stats["median"])
    vinted_prices = calculate_vinted_prices(query, ebay_median)
    economics = calculate_profit(buy_price, vinted_prices["list_price"])

    roi = economics["roi"]
    if buy_price <= 0:
        emoji = "🔥 STRONG BUY (free item)"
        roi_display = "∞"
    elif roi >= 150:
        emoji = "🔥 STRONG BUY"
        roi_display = f"{int(roi)}%"
    elif roi >= 80:
        emoji = "✅ BUY"
        roi_display = f"{int(roi)}%"
    elif roi >= 30:
        emoji = "🤔 MAYBE"
        roi_display = f"{int(roi)}%"
    else:
        emoji = "❌ PASS"
        roi_display = f"{int(roi)}%"

    return {
        "verdict": emoji,
        "ebay_sell_for": f"£{ebay_median:.2f}",
        "sell_for": f"£{vinted_prices['list_price']:.2f}",
        "fast_sale": f"£{vinted_prices['fast_price']:.2f}",
        "fees": f"£{economics['fees']:.2f}",
        "postage": f"£{economics['postage']:.2f}",
        "profit": f"£{economics['profit']:.2f}",
        "roi": roi_display,
        "title": build_title(query),
        "description": build_description(query),
        "discount": vinted_prices["discount"],
        "mock": stats.get("mock", False),
    }
