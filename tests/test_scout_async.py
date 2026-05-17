"""End-to-end test for the async consensus pipeline.

Mocks the shared httpx.AsyncClient (so no real eBay calls) and leans on the
conftest stub for scout_vision. Catches wiring mistakes — missing awaits,
wrong vote shape, broken cache reads, lock regressions — without needing
real API keys.
"""

import asyncio
import sys

import pytest


# ------------------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeAsyncClient:
    """Records every call, returns canned eBay/OAuth responses."""

    def __init__(self, search_items=None, fail_queries=None):
        self.token_calls = 0
        self.search_queries = []
        # Default: 3 items with prices 20, 25, 30 → median 25
        self._items = search_items if search_items is not None else [
            {"price": {"value": "20"}},
            {"price": {"value": "25"}},
            {"price": {"value": "30"}},
        ]
        # Queries in this set return zero items (simulates "no market data")
        self._fail_queries = fail_queries or set()

    async def post(self, url, **kwargs):
        if "oauth2/token" in url:
            self.token_calls += 1
            # Tiny await to give other tasks a chance to interleave —
            # important for the lock-contention test.
            await asyncio.sleep(0)
            return _FakeResponse({"access_token": "test-token", "expires_in": 7200})
        raise AssertionError(f"Unexpected POST: {url}")

    async def get(self, url, **kwargs):
        if "item_summary/search" in url:
            q = kwargs["params"]["q"]
            self.search_queries.append(q)
            if q in self._fail_queries:
                return _FakeResponse({"itemSummaries": []})
            return _FakeResponse({"itemSummaries": self._items})
        raise AssertionError(f"Unexpected GET: {url}")

    async def aclose(self):
        pass


# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------

@pytest.fixture
def scout(tmp_path):
    # Fresh import so the test gets an isolated module-level _client / _token_lock.
    for name in ("database", "services.ebay.scout_async"):
        sys.modules.pop(name, None)

    import database
    database.DB_PATH = tmp_path / "test_saas.db"
    asyncio.run(database.init_db())

    import services.ebay.scout_async as scout_mod
    fake = FakeAsyncClient()
    scout_mod._client = fake
    # Fresh lock bound to the test's event loop (asyncio.run creates a new loop per call,
    # but the lock has no loop affinity in 3.10+ until first use).
    scout_mod._token_lock = asyncio.Lock()
    return scout_mod, fake, database


@pytest.fixture
def image_path(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"fake-image-bytes")
    return str(p)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------------------

def test_happy_path_returns_success_verdict(scout, image_path):
    scout_mod, _fake, _db = scout
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))

    assert result["status"] == "success"
    # Conftest stubs identify_item → ("stubbed query", ["stubbed-keyword"])
    assert result["query"] == "stubbed query"
    # 3 items at 20/25/30 → median 25; with 5 votes all at 25, avg_median = 25
    assert result["median"] == 25.0
    # Required dict shape — catches missing keys before they reach Telegram formatter.
    for key in ("median_pretty", "sell_for", "sell_price_num", "fast_sale",
                "confidence", "winner", "roi", "verdict", "title",
                "description", "tags"):
        assert key in result, f"missing key: {key}"


def test_five_way_fanout_queries_all_variants(scout, image_path):
    scout_mod, fake, _db = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))

    # 5 variants per the spec: base, "{base} used", "{base} mens", "{base} womens", "{base} vintage"
    assert len(fake.search_queries) == 5
    expected_suffixes = {"", " used", " mens", " womens", " vintage"}
    actual_suffixes = {q.removeprefix("stubbed query") for q in fake.search_queries}
    assert actual_suffixes == expected_suffixes


def test_token_lock_prevents_concurrent_refresh(scout, image_path):
    """Cold cache + 5 concurrent variants must hit OAuth endpoint exactly once."""
    scout_mod, fake, _db = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))
    assert fake.token_calls == 1, (
        f"Token endpoint hit {fake.token_calls}× — lock or double-check failed"
    )


def test_token_cache_reused_across_runs(scout, image_path, tmp_path):
    """Second photo eval should reuse the cached token, no OAuth round-trip."""
    scout_mod, fake, _db = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))
    # Different image so vision/stats caches don't short-circuit eBay calls
    second = tmp_path / "photo2.jpg"
    second.write_bytes(b"different-image-bytes")
    _run(scout_mod.evaluate_with_consensus_saas(str(second), "5.00"))
    assert fake.token_calls == 1


def test_insufficient_votes_returns_error(scout, image_path):
    """If 4 of 5 variants return empty market data, < MIN_VOTES_FOR_CONSENSUS — error."""
    scout_mod, fake, _db = scout
    # Fail every variant except the bare base query → only 1 valid vote
    fake._fail_queries = {
        "stubbed query used",
        "stubbed query mens",
        "stubbed query womens",
        "stubbed query vintage",
    }
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))
    assert result["status"] == "error"
    assert "Insufficient" in result["message"]


def test_vision_result_is_cached(scout, image_path):
    """Same image hash → vision lookup runs once, second eval reuses cached query."""
    scout_mod, _fake, db = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5.00"))

    # After one run, vision:{md5} should be populated in kv_cache.
    img_hash = scout_mod._md5_file(image_path)
    cached = _run(db.get_cached_value(f"vision:{img_hash}"))
    assert cached == {"query": "stubbed query", "keywords": ["stubbed-keyword"]}
