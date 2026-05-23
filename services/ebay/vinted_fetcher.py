import asyncio
import logging
import os
import random
import statistics
from typing import Dict, List, Optional

import httpx

import database

LOGGER = logging.getLogger(__name__)

VINTED_BASE = "https://www.vinted.co.uk"
VINTED_SEARCH = f"{VINTED_BASE}/api/v2/catalog/items"
VINTED_PROXY = os.getenv("VINTED_PROXY", "")
VINTED_CACHE_TTL = 43200  # 12 hours
VINTED_TO_EBAY = 1 / 0.72

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

_client: Optional[httpx.AsyncClient] = None
_cookies: Optional[httpx.Cookies] = None
_cookie_lock = asyncio.Lock()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=15.0,
            proxy=VINTED_PROXY or None,
            headers=HEADERS,
        )
    return _client


async def _ensure_session() -> None:
    global _cookies
    async with _cookie_lock:
        if _cookies is not None:
            return
        client = _get_client()
        resp = await client.get(VINTED_BASE)
        _cookies = resp.cookies
        LOGGER.info("Vinted session initialised (%d cookies)", len(_cookies))


async def refresh_session() -> None:
    global _cookies
    async with _cookie_lock:
        _cookies = None
    await _ensure_session()


async def search_vinted(query: str, per_page: int = 20) -> List[float]:
    await _ensure_session()
    await asyncio.sleep(random.uniform(0.3, 1.0))

    client = _get_client()
    resp = await client.get(
        VINTED_SEARCH,
        params={"search_text": query, "per_page": str(per_page), "order": "relevance"},
        cookies=_cookies,
    )

    if resp.status_code == 401:
        LOGGER.warning("Vinted 401 — refreshing session")
        await refresh_session()
        return []

    if resp.status_code != 200:
        LOGGER.warning("Vinted %d for %r", resp.status_code, query)
        return []

    items = resp.json().get("items", [])
    prices = []
    for item in items:
        try:
            price = float(item["total_item_price"]["amount"])
            if 1.0 <= price <= 500.0:
                prices.append(price)
        except (KeyError, ValueError, TypeError):
            continue

    return prices


async def get_vinted_stats(query: str, condition: str = "used") -> Dict:
    cache_key = f"stats:vinted:{condition}:{query.lower()}"
    cached = await database.get_cached_value(cache_key)
    if cached:
        return cached

    try:
        prices = await search_vinted(query)
        if not prices:
            return {}
        stats = {"median": statistics.median(prices)}
        await database.set_cached_value(cache_key, stats, ttl_seconds=VINTED_CACHE_TTL)
        return stats
    except Exception:
        LOGGER.exception("Vinted query failed for %r", query)
        return {}


async def get_vinted_vote(query: str, condition: str, index: int = 0) -> Optional[Dict]:
    stats = await get_vinted_stats(query, condition)
    if "median" not in stats:
        return None
    normalized = stats["median"] * VINTED_TO_EBAY
    return {"median": normalized, "query": query, "replica": f"#V{index}"}


if __name__ == "__main__":
    import sys

    async def _test(query: str, use_proxy: bool = False):
        global _client
        proxy = VINTED_PROXY if use_proxy else None
        _client = httpx.AsyncClient(timeout=15.0, proxy=proxy, headers=HEADERS)

        print(f"Searching Vinted UK for: {query!r}")
        prices = await search_vinted(query)
        if not prices:
            print("No results.")
            return
        median = statistics.median(prices)
        print(f"Got {len(prices)} prices: {sorted(prices)}")
        print(f"Vinted median: £{median:.2f}")
        print(f"Normalized (eBay-equivalent): £{median * VINTED_TO_EBAY:.2f}")
        await _client.aclose()

    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "nike air max 90"
    asyncio.run(_test(q))
