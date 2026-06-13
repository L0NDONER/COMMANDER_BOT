"""Extended tests for services/market/scout_async.py — covers paths  # [ZWJheQ==]
the existing test_scout_async.py doesn't reach."""

import asyncio
import hashlib
import sys

import pytest


# Reuse the fake client from test_scout_async
class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeAsyncClient:
    def __init__(self, search_items=None, fail_queries=None):
        self.token_calls = 0
        self.search_queries = []
        self._items = search_items if search_items is not None else [
            {"price": {"value": "20"}},
            {"price": {"value": "25"}},
            {"price": {"value": "30"}},
        ]
        self._fail_queries = fail_queries or set()

    async def post(self, url, **kwargs):
        if "oauth2/token" in url:
            self.token_calls += 1
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


@pytest.fixture
def scout(tmp_path):
    for name in ("database", "services.market.scout_async"):
        sys.modules.pop(name, None)

    import database
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db())

    import services.market.scout_async as scout_mod
    fake = FakeAsyncClient()
    scout_mod._client = fake
    scout_mod._token_lock = asyncio.Lock()
    return scout_mod, fake, database


@pytest.fixture
def image_path(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"fake-image-bytes")
    return str(p)


def _run(coro):
    return asyncio.run(coro)


# ── _md5_file ───────────────────────────────────────────────────

def test_md5_file_matches_hashlib(scout, image_path):
    scout_mod, _, _ = scout
    expected = hashlib.md5(b"fake-image-bytes").hexdigest()
    assert scout_mod._md5_file(image_path) == expected


# ── aclose ──────────────────────────────────────────────────────

def test_aclose_clears_client(scout):
    scout_mod, _, _ = scout
    assert scout_mod._client is not None
    _run(scout_mod.aclose())
    assert scout_mod._client is None


def test_aclose_when_no_client(scout):
    scout_mod, _, _ = scout
    scout_mod._client = None
    _run(scout_mod.aclose())
    assert scout_mod._client is None


# ── get_stats_async ─────────────────────────────────────────────

def test_get_stats_async_returns_median(scout):
    scout_mod, _, _ = scout
    stats = _run(scout_mod.get_stats_async("nike hoodie", "used"))
    assert stats["median"] == 25.0


def test_get_stats_async_caches_result(scout):
    scout_mod, fake, _ = scout
    _run(scout_mod.get_stats_async("nike hoodie", "used"))
    _run(scout_mod.get_stats_async("nike hoodie", "used"))
    assert fake.search_queries.count("nike hoodie") == 1


def test_get_stats_async_empty_on_no_items(scout):
    scout_mod, fake, _ = scout
    fake._items = []
    stats = _run(scout_mod.get_stats_async("nothing here", "used"))
    assert stats == {}


# ── get_worker_vote_async ───────────────────────────────────────

def test_worker_vote_returns_dict_with_median(scout):
    scout_mod, _, _ = scout
    vote = _run(scout_mod.get_worker_vote_async("test query", "used", 0))
    assert vote["median"] == 25.0
    assert vote["replica"] == "#0"


def test_worker_vote_returns_none_on_empty_stats(scout):
    scout_mod, fake, _ = scout
    fake._items = []
    vote = _run(scout_mod.get_worker_vote_async("test query", "used", 0))
    assert vote is None


# ── free stock (buy_price=0) ────────────────────────────────────

def test_free_stock_strong_buy(scout, image_path):
    scout_mod, _, _ = scout
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "0"))
    assert result["status"] == "success"
    assert result["verdict"] == "STRONG BUY"
    assert result["roi"] == 999


def test_free_stock_pass_when_low_median(scout, image_path):
    scout_mod, fake, _ = scout
    fake._items = [
        {"price": {"value": "2"}},
        {"price": {"value": "3"}},
        {"price": {"value": "2.5"}},
    ]
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "0"))
    assert result["status"] == "success"
    assert result["verdict"] == "PASS"


# ── condition detection via caption ─────────────────────────────

def test_new_condition_from_caption(scout, image_path):
    scout_mod, fake, _ = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5 bnwt"))
    assert any("new" in q for q in fake.search_queries)


def test_used_condition_default(scout, image_path):
    scout_mod, fake, _ = scout
    _run(scout_mod.evaluate_with_consensus_saas(image_path, "5"))
    assert any("used" in q for q in fake.search_queries)


# ── all variants fail → error ───────────────────────────────────

def test_all_variants_fail_returns_error(scout, image_path):
    scout_mod, fake, _ = scout
    fake._items = []
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5"))
    assert result["status"] == "error"


# ── response shape ──────────────────────────────────────────────

def test_response_contains_listing_fields(scout, image_path):
    scout_mod, _, _ = scout
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5"))
    assert result["title"]
    assert result["description"]
    assert result["tags"]


def test_response_sell_price_num_is_float(scout, image_path):
    scout_mod, _, _ = scout
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5"))
    assert isinstance(result["sell_price_num"], float)


def test_response_roi_is_rounded(scout, image_path):
    scout_mod, _, _ = scout
    result = _run(scout_mod.evaluate_with_consensus_saas(image_path, "5"))
    assert result["roi"] == round(result["roi"], 0)
