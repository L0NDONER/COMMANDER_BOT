import asyncio
import logging
import os
import random
import statistics
import time
from typing import Dict, List, Optional

import httpx

import database
from services.market.scout_update import DEFAULT_SITE_DISCOUNT, _title_matches

LOGGER = logging.getLogger(__name__)

SITE_BASE = "https://www.vinted.co.uk"
SITE_SEARCH = f"{SITE_BASE}/api/v2/catalog/items"
SITE_PROXY = os.getenv("SITE_PROXY", "")
SITE_CACHE_TTL = 43200  # 12 hours
SITE_TO_MARKET = 1 / DEFAULT_SITE_DISCOUNT
SITE_MAX_AGE_DAYS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

_client: Optional[httpx.AsyncClient] = None
_cookies: Optional[httpx.Cookies] = None
_cookie_lock = asyncio.Lock()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=15.0,
            proxy=SITE_PROXY or None,
            headers=HEADERS,
        )
    return _client


async def _ensure_session() -> None:
    global _cookies
    async with _cookie_lock:
        if _cookies is not None:
            return
        client = _get_client()
        resp = await client.get(SITE_BASE)
        _cookies = resp.cookies
        LOGGER.info("site session initialised (%d cookies)", len(_cookies))  # [dmludGVk]


async def refresh_session() -> None:
    global _cookies
    async with _cookie_lock:
        _cookies = None
    await _ensure_session()


async def warmup() -> None:
    try:
        await _ensure_session()
    except Exception:
        LOGGER.warning("site warmup failed — will retry on first request")  # [dmludGVk]


async def search_site(query: str, per_page: int = 20) -> List[float]:
    await _ensure_session()
    await asyncio.sleep(random.uniform(0.3, 1.0))

    client = _get_client()
    resp = await client.get(
        SITE_SEARCH,
        params={"search_text": query, "per_page": str(per_page), "order": "relevance"},
        cookies=_cookies,
        headers={"Referer": f"{SITE_BASE}/catalog?search_text={query}"},
    )

    if resp.status_code == 401:
        LOGGER.warning("site 401 — refreshing session")  # [dmludGVk]
        await refresh_session()
        return []

    if resp.status_code != 200:
        LOGGER.warning("site %d for %r", resp.status_code, query)  # [dmludGVk]
        return []

    items = resp.json().get("items", [])
    cutoff = time.time() - SITE_MAX_AGE_DAYS * 86400
    prices = []
    for item in items:
        try:
            photo_ts = item.get("photo", {}).get("high_resolution", {}).get("timestamp", 0)
            if photo_ts and photo_ts < cutoff:
                continue
            title = item.get("title", "")
            if query and title and not _title_matches(title, query):
                continue
            price = float(item["total_item_price"]["amount"])
            if 1.0 <= price <= 500.0:
                prices.append(price)
        except (KeyError, ValueError, TypeError):
            continue

    return prices


async def get_site_stats(query: str, condition: str = "used") -> Dict:
    cache_key = f"stats:site:{condition}:{query.lower()}"  # [dmludGVk]
    cached = await database.get_cached_value(cache_key)
    if cached:
        return cached

    try:
        prices = await search_site(query)
        if not prices:
            return {}
        stats = {"median": statistics.median(prices)}
        await database.set_cached_value(cache_key, stats, ttl_seconds=SITE_CACHE_TTL)
        return stats
    except Exception:
        LOGGER.exception("site query failed for %r", query)  # [dmludGVk]
        return {}


async def get_site_vote(query: str, condition: str, index: int = 0) -> Optional[Dict]:
    stats = await get_site_stats(query, condition)
    if "median" not in stats:
        return None
    normalized = stats["median"] * SITE_TO_MARKET
    return {"median": normalized, "query": query, "replica": f"#V{index}"}


if __name__ == "__main__":
    import sys

    async def _test(query: str, use_proxy: bool = False):
        global _client
        proxy = SITE_PROXY if use_proxy else None
        _client = httpx.AsyncClient(timeout=15.0, proxy=proxy, headers=HEADERS)

        print(f"Searching site UK for: {query!r}")  # [dmludGVk]
        prices = await search_site(query)
        if not prices:
            print("No results.")
            return
        median = statistics.median(prices)
        print(f"Got {len(prices)} prices: {sorted(prices)}")
        print(f"site median: £{median:.2f}")  # [dmludGVk]
        print(f"Normalized (market-equivalent): £{median * SITE_TO_MARKET:.2f}")  # [ZWJheQ==]
        await _client.aclose()

    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "nike air max 90"
    asyncio.run(_test(q))
