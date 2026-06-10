"""Vinted brand watcher — async fan-out over all brands in one sweep.

Auth pattern mirrors the eBay token lock in scout_async.py:
  - _token_lock: first coroutine to find the bearer token expired queues a
    refresh; all concurrent waiters get the new token for free (knock-wait).
  - asyncio.gather fans out all brand searches in parallel once the token is
    confirmed live; each search is individually time-boxed.

Token: Vinted issues 2-hour JWT bearer tokens (access_token_web from
devtools). Set VINTED_TOKEN env var or paste into ACCESS_TOKEN below.
Refresh by logging in again and copying the new token — no refresh endpoint
is exposed publicly.
"""

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

LOGGER = logging.getLogger(__name__)

# -------------------------
# CONFIG
# -------------------------

@dataclass
class BrandConfig:
    name: str
    max_price: float  # GBP
    size_label: Optional[str] = "XL"  # None = any size


BRANDS: List[BrandConfig] = [
    BrandConfig("Rab Hoodie", 60, size_label=None),
    BrandConfig("Champion Reverse Weave", 25),
    BrandConfig("Napapijri", 35),
    BrandConfig("Penfield", 20),
    BrandConfig("Patagonia", 35),
    BrandConfig("MKI Miyuki Zoku", 10),
    BrandConfig("Barbour", 40),
    BrandConfig("Arc'teryx", 30),
    BrandConfig("Reiss", 20),
    BrandConfig("Boss Orange", 25),
    BrandConfig("Gant", 20),
    BrandConfig("Abercrombie & Fitch", 18),
    BrandConfig("Fred Perry", 25),
]

# Set VINTED_TOKEN in env, or paste here (never commit a real value).
ACCESS_TOKEN: str = os.getenv("VINTED_TOKEN", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.vinted.co.uk/",
}

POLL_MIN = 7
POLL_MAX = 18
SEARCH_TIMEOUT_SECONDS = 12.0
FAVOURITE_DELAY_MIN = 20
FAVOURITE_DELAY_MAX = 40
MAX_FAVOURITES_PER_RUN = 20

# Proactive refresh margin: treat token as expired this many seconds early.
TOKEN_EXPIRY_MARGIN = 120


# -------------------------
# SHARED CLIENT + TOKEN LOCK
# -------------------------

_client: Optional[httpx.AsyncClient] = None
# Knock-wait: first caller to find the token expired or missing fetches a new
# one; all concurrent callers queue here and then read the refreshed value
# without hitting the network again (double-checked pattern).
_token_lock = asyncio.Lock()
_token: str = ACCESS_TOKEN
_token_exp: float = 0.0   # unix timestamp; 0 = unknown, treat as expired


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=10.0)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# -------------------------
# TOKEN KNOCK-WAIT
# -------------------------

def _token_live() -> bool:
    return bool(_token) and time.time() < (_token_exp - TOKEN_EXPIRY_MARGIN)


def _parse_exp(token: str) -> float:
    """Decode exp claim from a JWT without a third-party library."""
    import base64, json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)   # re-pad
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


def load_token(raw: str) -> None:
    """Accept a freshly pasted token and update module state."""
    global _token, _token_exp
    _token = raw.strip()
    _token_exp = _parse_exp(_token)
    LOGGER.info("[TOKEN] loaded, exp in %.0fs", _token_exp - time.time())


def token_expires_in() -> float:
    """Seconds until token expires. Negative if already expired."""
    return _token_exp - time.time()


async def get_token() -> str:
    """
    Return a live bearer token. Raises RuntimeError if expired — callers
    must handle this gracefully rather than blocking on stdin.
    """
    if _token_live():
        return _token

    async with _token_lock:
        if _token_live():
            return _token
        raise RuntimeError("VINTED_TOKEN expired — paste a fresh token via /vinted")


def invalidate_token() -> None:
    """Force a re-prompt on next get_token() call (e.g. after a 401)."""
    global _token_exp
    _token_exp = 0.0


# -------------------------
# SEARCH & FILTER
# -------------------------

async def search_brand(brand: BrandConfig, token: str) -> List[Dict[str, Any]]:
    """Async search for a single brand using bearer auth."""
    client = _get_client()
    resp = await client.get(
        "https://www.vinted.co.uk/api/v2/catalog/items",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "search_text": brand.name,
            "order": "newest_first",
            "per_page": 50,
            "size_id": 206,
        },
    )
    if resp.status_code in (401, 403):
        invalidate_token()
        raise PermissionError(f"{resp.status_code} on search for {brand.name}")
    resp.raise_for_status()
    return resp.json().get("items", [])


_WOMENS_KEYWORDS = {"women", "ladies", "girl", "baby", "kids", "skirt"}
_ACCESSORY_KEYWORDS = {"bag", "sling", "daypack", "hat", "bum bag", "sticker"}
_TITLE_SIZE_WORDS = {"xl": "xl", "l": "large", "m": "medium", "s": "small"}
# UK dress sizes 10+ in size_title indicate women's clothing
_WOMENS_UK_SIZE_RE = re.compile(r"uk\s*(?:1\d|2\d)", re.IGNORECASE)


def _size_matches(item: Dict[str, Any], target: str) -> bool:
    size = (item.get("size_title") or "").lower()
    title = (item.get("title") or "").lower()
    t = target.lower()

    if any(w in title for w in _ACCESSORY_KEYWORDS):
        return False
    if any(w in title for w in _WOMENS_KEYWORDS):
        return False
    if _WOMENS_UK_SIZE_RE.search(size):
        return False

    # if size_title is set, it's authoritative — don't fall back to title
    if size:
        return size == t or size.startswith(t + " ") or size.startswith(t + "/")

    # no size_title: use unambiguous long-form title match
    word = _TITLE_SIZE_WORDS.get(t)
    return bool(word and word in title)


def _brand_relevance(item: Dict[str, Any], brand: BrandConfig) -> float:
    from difflib import SequenceMatcher
    title = (item.get("title") or "").lower()
    b = brand.name.lower()
    if b in title:
        return 1.0
    sim = SequenceMatcher(None, title, b).ratio()
    sim2 = SequenceMatcher(None, title.replace(" ", ""), b.replace(" ", "")).ratio()
    return max(sim, sim2)


def _value_score(price: float, max_price: float) -> float:
    if max_price <= 0:
        return 0.0
    return round(max((max_price - price) / max_price, 0.0), 2)


_REL_THRESHOLD = 0.60


def _filter(items: List[Dict[str, Any]], brand: BrandConfig) -> List[Dict[str, Any]]:
    results = []
    for i in items:
        rel = _brand_relevance(i, brand)
        if rel < _REL_THRESHOLD:
            continue
        if brand.size_label and not _size_matches(i, brand.size_label):
            continue
        price = _safe_price(i)
        if price > brand.max_price:
            continue
        i["_rel"] = round(rel, 2)
        i["_value"] = _value_score(price, brand.max_price)
        results.append(i)
    return results


# -------------------------
# FAVOURITE
# -------------------------

async def favourite_item(item_id: int, token: str) -> bool:
    client = _get_client()
    resp = await client.post(
        f"https://www.vinted.co.uk/api/v2/items/{item_id}/favorite",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    if resp.status_code in (401, 403):
        invalidate_token()
        LOGGER.warning("[AUTH] %s favouriting %s — token invalidated", resp.status_code, item_id)
        return False
    if resp.status_code == 200:
        LOGGER.info("[+] Favourited %s", item_id)
        return True
    LOGGER.warning("[!] Favourite %s → %s", item_id, resp.status_code)
    return False


# -------------------------
# FAN-OUT SWEEP
# -------------------------

async def _human_search_brand(brand: BrandConfig, token: str) -> tuple[BrandConfig, List[Dict[str, Any]]]:
    await asyncio.sleep(random.uniform(0.4, 2.2))

    if random.random() < 0.05:
        LOGGER.info("[DRIFT] skipped %s this sweep", brand.name)
        return brand, []

    items = await search_brand(brand, token)

    if items and all(
        _safe_price(i) > brand.max_price for i in items[:5]
    ):
        await asyncio.sleep(random.uniform(1.0, 3.0))

    return brand, _filter(items, brand)


def _safe_price(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("price", {}).get("amount", 0))
    except (ValueError, TypeError):
        return 0.0


async def _search_timed(brand: BrandConfig, token: str) -> tuple[BrandConfig, List[Dict[str, Any]]]:
    try:
        brand, candidates = await asyncio.wait_for(
            _human_search_brand(brand, token), timeout=SEARCH_TIMEOUT_SECONDS
        )
        LOGGER.info("[SCAN] %s → %d candidates", brand.name, len(candidates))
        return brand, candidates
    except asyncio.TimeoutError:
        LOGGER.warning("[TIMEOUT] %s", brand.name)
        return brand, []
    except Exception as exc:
        LOGGER.error("[ERROR] %s: %s", brand.name, exc)
        return brand, []


async def fetch_seller(user_id: int, token: str) -> Dict[str, Any]:
    client = _get_client()
    try:
        resp = await client.get(
            f"https://www.vinted.co.uk/api/v2/users/{user_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8.0,
        )
        if resp.status_code == 200:
            return resp.json().get("user", {})
    except Exception as exc:
        LOGGER.debug("[SELLER] fetch failed for %s: %s", user_id, exc)
    return {}


def seller_trust(profile: Dict[str, Any]) -> float:
    rep = float(profile.get("feedback_reputation", 0))  # already 0–1
    sales = profile.get("feedback_count", 0)
    if sales >= 200:
        sales_w = 1.0
    elif sales >= 50:
        sales_w = 0.7
    elif sales >= 10:
        sales_w = 0.4
    else:
        sales_w = 0.2
    verified = 0.1 if profile.get("is_verified") else 0.0
    return max(0.1, min(1.0, rep * 0.7 + sales_w * 0.2 + verified * 0.1))


async def _enrich_with_seller(brand_item: tuple, token: str) -> tuple:
    brand, item = brand_item
    user_id = (item.get("user") or {}).get("id")
    if user_id:
        profile = await fetch_seller(user_id, token)
        trust = seller_trust(profile)
        item["_trust"] = round(trust, 2)
        item["_seller_sales"] = profile.get("feedback_count", 0)
        item["_seller_rep"] = profile.get("feedback_reputation", 0)
    else:
        item["_trust"] = 0.1
    return brand, item


async def sweep() -> List[tuple[BrandConfig, Dict[str, Any]]]:
    """
    Confirm the token is live once, then fan out all brand searches in
    parallel. Enrich candidates with seller profile data after filtering.
    """
    token = await get_token()
    results = await asyncio.gather(*(_search_timed(b, token) for b in BRANDS))
    candidates = [(brand, item) for brand, items in results for item in items]

    enriched = await asyncio.gather(
        *(_enrich_with_seller(c, token) for c in candidates)
    )
    return [(b, i) for b, i in enriched if i.get("_trust", 0) >= 0.35]


# -------------------------
# NUGGET DETECTION
# -------------------------

NUGGET_VAL   = 0.40
NUGGET_REL   = 1.00
NUGGET_TRUST = 0.70

_alerted_ids: set = set()


def is_nugget(item: Dict[str, Any]) -> bool:
    return (
        item.get("_value", 0)   >= NUGGET_VAL
        and item.get("_rel", 0) >= NUGGET_REL
        and item.get("_trust", 0) >= NUGGET_TRUST
    )


def format_nugget_alert(brand: "BrandConfig", item: Dict[str, Any]) -> str:
    price  = item.get("price", {}).get("amount", "?")
    val    = item.get("_value", 0)
    trust  = item.get("_trust", 0)
    size   = item.get("size_title", "?")
    title  = item.get("title", "?")
    url    = item.get("url", f"https://www.vinted.co.uk/items/{item['id']}")
    return (
        f"🔥 *Nugget detected ({brand.name})*\n"
        f"£{price} — val={val:.2f} — trust={trust:.2f} — {size}\n"
        f"{title}\n"
        f"{url}"
    )


async def nugget_loop(send_fn, interval_min: int = 8, interval_max: int = 20) -> None:
    """
    Background loop: sweep every ~10 minutes, call send_fn(text) for each
    new nugget. Pass a coroutine function that accepts a single string.
    """
    LOGGER.info("[NUGGET] loop started")
    while True:
        try:
            candidates = await sweep()
            for brand, item in candidates:
                item_id = item.get("id")
                if not item_id or item_id in _alerted_ids:
                    continue
                if is_nugget(item):
                    msg = format_nugget_alert(brand, item)
                    LOGGER.info("[NUGGET] %s £%s", brand.name, item.get("price", {}).get("amount"))
                    await send_fn(msg)
                    _alerted_ids.add(item_id)
        except RuntimeError as exc:
            LOGGER.warning("[NUGGET] paused: %s", exc)
            # wait until token is refreshed (load_token sets _token_exp)
            while not _token_live():
                await asyncio.sleep(30)
            LOGGER.info("[NUGGET] token refreshed, resuming")
            continue
        except Exception as exc:
            LOGGER.error("[NUGGET] sweep error: %s", exc)

        delay = random.uniform(interval_min * 60, interval_max * 60)
        LOGGER.info("[NUGGET] next sweep in %.0fs", delay)
        await asyncio.sleep(delay)


# -------------------------
# MAIN LOOP
# -------------------------

async def run_watcher() -> None:
    if ACCESS_TOKEN:
        load_token(ACCESS_TOKEN)

    favourites_done = 0

    while favourites_done < MAX_FAVOURITES_PER_RUN:
        candidates = await sweep()
        LOGGER.info("[SWEEP] %d candidates across all brands", len(candidates))

        for brand, item in candidates:
            if favourites_done >= MAX_FAVOURITES_PER_RUN:
                break
            item_id = item.get("id")
            if not item_id:
                continue

            token = await get_token()
            delay = random.uniform(FAVOURITE_DELAY_MIN, FAVOURITE_DELAY_MAX)
            LOGGER.info("[WAIT] %.1fs before favouriting %s (%s)", delay, item_id, brand.name)
            await asyncio.sleep(delay)

            if await favourite_item(item_id, token):
                favourites_done += 1

        sweep_delay = random.uniform(POLL_MIN, POLL_MAX)
        LOGGER.info("[SWEEP DONE] sleeping %.1fs", sweep_delay)
        await asyncio.sleep(sweep_delay)

    LOGGER.info("[END] reached MAX_FAVOURITES_PER_RUN=%d", MAX_FAVOURITES_PER_RUN)
    await aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run_watcher())
