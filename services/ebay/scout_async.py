"""Async, single-process consensus pipeline — replaces the Redis-fanout
worker pool with asyncio.gather over a shared httpx.AsyncClient.

Verdict math, discount tiers, and listing draft generation are reused from
scout_update so the pricing rules stay identical to the legacy path.
"""

import asyncio
import base64
import hashlib
import logging
import os
from typing import Dict, List, Optional, Tuple

import httpx

import database
from credentials import EBAY_APP_ID, EBAY_SECRET
from services.ebay import scout_vision, vision_audit
from services.ebay.brands import is_low_value
from services.ebay.circuit_breaker import CircuitBreaker
from services.ebay.vinted_fetcher import get_vinted_vote
from services.ebay.consensus_engine import (
    MIN_VOTES_FOR_CONSENSUS,
    build_variants,
    gather_votes,
    record_consensus,
)
from services.ebay.scout_update import (
    CONDITION_FILTERS,
    EBAY_TIMEOUT_SECONDS,
    VINTED_TIMEOUT_SECONDS,
    FAST_SALE_MULTIPLIER,
    MARKETPLACE,
    STATS_CACHE_TTL_SECONDS,
    VISION_CACHE_TTL_SECONDS,
    _parse_buy_price,
    _score,
    analyse,
    charm,
    detect_condition,
    generate_listing_draft,
)

LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Shared httpx client + token lock
# ------------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_token_lock = asyncio.Lock()

# Vinted is the polite, flaky source — guard it with a breaker so a stretch of
# timeouts stops holding up the fan-out. Module-scoped: state persists across
# photos, which is where the breaker earns its keep (skip Vinted for the next
# ~60s once it goes dark, rather than waiting out the timeout on every photo).
_vinted_breaker = CircuitBreaker(name="vinted", threshold=3, cooldown=60.0)
_vinted_vote_guarded = _vinted_breaker.wrap(get_vinted_vote)

# Fire-and-forget vision-audit tasks. Held so the loop doesn't GC them mid-flight;
# each removes itself on completion. Off the verdict path entirely.
_audit_tasks: set = set()


def _schedule_vision_audit(image_path: str, gemini_query: str) -> None:
    """Kick off the independent Groq read in the background (cache-miss only).
    OFF by default: the 2026-05-27 audit found Gemini reads reliable and Groq's
    divergence mostly noise (see project_vision_audit memory), so Groq is out of
    the live path. Set env VISION_AUDIT=1 to re-enable, e.g. for a real-photo run."""
    if os.getenv("VISION_AUDIT", "0") != "1":
        return
    task = asyncio.create_task(
        vision_audit.run_shadow(image_path, gemini_query, scout_vision.groq_identify)
    )
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


def _get_client() -> httpx.AsyncClient:
    """Lazy singleton — created on first use inside the running event loop."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def aclose() -> None:
    """Close the shared client. Call from app shutdown if you want a clean exit."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ------------------------------------------------------------------------------
# eBay OAuth token (with double-checked async lock)
# ------------------------------------------------------------------------------

async def get_token_async() -> str:
    cached = await database.get_cached_value("ebay_token")
    if cached:
        return cached

    async with _token_lock:
        # Re-check after acquiring — another waiter may have just refreshed.
        cached = await database.get_cached_value("ebay_token")
        if cached:
            return cached

        creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_SECRET}".encode()).decode()
        client = _get_client()
        res = await client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        )
        res.raise_for_status()
        data = res.json()
        token = data["access_token"]
        ttl = max(60, data.get("expires_in", 7200) - 60)
        await database.set_cached_value("ebay_token", token, ttl_seconds=ttl)
        return token


# ------------------------------------------------------------------------------
# eBay search
# ------------------------------------------------------------------------------

async def fetch_ebay_api_async(query: str, token: str, condition: str = "used") -> List[dict]:
    cond_ids = CONDITION_FILTERS.get(condition, CONDITION_FILTERS["used"])
    client = _get_client()
    res = await client.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        },
        params={
            "q": query,
            "limit": 15,
            "filter": f"conditionIds:{{{cond_ids}}},price:[5..500],priceCurrency:GBP",
        },
    )
    res.raise_for_status()
    return res.json().get("itemSummaries", [])


async def get_stats_async(query: str, condition: str = "used") -> Dict:
    if is_low_value(query):
        return {}
    cache_key = f"stats:{condition}:{query.lower()}"
    cached = await database.get_cached_value(cache_key)
    if cached:
        return cached
    try:
        token = await get_token_async()
        stats = analyse(await fetch_ebay_api_async(query, token, condition), query=query)
        if stats:
            await database.set_cached_value(cache_key, stats, ttl_seconds=STATS_CACHE_TTL_SECONDS)
        return stats
    except Exception:
        LOGGER.exception("eBay query failed for %r (cond=%s)", query, condition)
        return {}


async def get_worker_vote_async(query: str, condition: str, index: int = 0) -> Optional[Dict]:
    """One variant's vote. Returns a dict matching the legacy vote shape, or None."""
    stats = await get_stats_async(query, condition)
    if "median" not in stats:
        return None
    return {"median": stats["median"], "query": query, "replica": f"#{index}"}


# ------------------------------------------------------------------------------
# Vision (sync Gemini SDK wrapped via to_thread)
# ------------------------------------------------------------------------------

def _md5_file(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


async def _vision_lookup_async(img_hash: str, image_path: str) -> Tuple[str, List[str]]:
    cached = await database.get_cached_value(f"vision:{img_hash}")
    if cached:
        return cached["query"], cached["keywords"]
    base_query, keywords = await asyncio.to_thread(scout_vision.identify_item, image_path)
    await database.set_cached_value(
        f"vision:{img_hash}",
        {"query": base_query, "keywords": keywords},
        ttl_seconds=VISION_CACHE_TTL_SECONDS,
    )
    _schedule_vision_audit(image_path, base_query)   # cache-miss only; off hot path
    return base_query, keywords


# ------------------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------------------

async def evaluate_with_consensus_saas(image_path: str, buy_price: str) -> Dict:
    clean_buy = _parse_buy_price(buy_price)
    condition = detect_condition(str(buy_price))
    LOGGER.info("Caption condition=%s buy=£%.2f", condition, clean_buy)

    img_hash = await asyncio.to_thread(_md5_file, image_path)
    base_query, keywords = await _vision_lookup_async(img_hash, image_path)

    variants = build_variants(base_query, condition, keywords)
    ebay_coro = gather_votes(variants, condition, get_worker_vote_async, EBAY_TIMEOUT_SECONDS)
    vinted_coro = gather_votes(variants, condition, _vinted_vote_guarded, VINTED_TIMEOUT_SECONDS)

    ebay_result, vinted_result = await asyncio.gather(ebay_coro, vinted_coro, return_exceptions=True)

    votes = []
    if isinstance(ebay_result, list):
        votes.extend(ebay_result)
    if isinstance(vinted_result, list):
        votes.extend(vinted_result)

    if not votes:
        return {"status": "error", "message": "Pricing lookup timed out."}

    if len(votes) < MIN_VOTES_FOR_CONSENSUS:
        return {"status": "error", "message": "Insufficient market data."}

    scored = _score(votes, base_query, clean_buy)
    record_consensus(base_query, condition, keywords, votes)
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
