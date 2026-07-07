"""Tests for services/market/site_catalog.py."""  # [ZWJheQ==]

import asyncio
import sys
import time

import pytest


class _FakeResponse:
    def __init__(self, data, status_code=200, cookies=None):
        self._data = data
        self.status_code = status_code
        self.cookies = cookies or {}

    def json(self):
        return self._data


class FakeSiteClient:
    def __init__(self, items=None, status_code=200):
        self.get_calls = []
        self._items = items if items is not None else []
        self._status_code = status_code
        self._session_hits = 0

    async def get(self, url, **kwargs):
        self.get_calls.append(url)
        if url.endswith("vinted.co.uk"):
            self._session_hits += 1
            return _FakeResponse({}, cookies={"_vinted_fr": "fake"})
        return _FakeResponse(
            {"items": self._items},
            status_code=self._status_code,
        )


def _make_item(price, title="Nike Air Max 90", age_days=5):
    ts = time.time() - age_days * 86400
    return {
        "title": title,
        "total_item_price": {"amount": str(price)},
        "photo": {"high_resolution": {"timestamp": int(ts)}},
    }


@pytest.fixture
def site(tmp_path, monkeypatch):
    for name in ("database", "services.market.site_catalog"):
        sys.modules.pop(name, None)

    import database
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db())

    import services.market.site_catalog as mod
    fake = FakeSiteClient()
    mod._client = fake
    mod._cookies = None
    mod._cookie_lock = asyncio.Lock()
    # Skip random sleep in tests
    monkeypatch.setattr("services.market.site_catalog.random.uniform", lambda a, b: 0)
    return mod, fake, database


def _run(coro):
    return asyncio.run(coro)


# ── search_site ───────────────────────────────────────────────


def test_search_returns_prices(site):
    mod, fake, _ = site
    fake._items = [_make_item(15), _make_item(20), _make_item(25)]
    prices = _run(mod.search_site("nike air max 90"))
    assert prices == [15.0, 20.0, 25.0]


def test_search_filters_by_title(site):
    mod, fake, _ = site
    fake._items = [_make_item(15, "Nike Air Max 90"), _make_item(20, "Adidas Samba")]
    prices = _run(mod.search_site("nike air max"))
    assert prices == [15.0]


def test_search_filters_old_items(site):
    mod, fake, _ = site
    fake._items = [_make_item(15, age_days=5), _make_item(20, age_days=60)]
    prices = _run(mod.search_site("nike air max 90"))
    assert prices == [15.0]


def test_search_filters_price_range(site):
    mod, fake, _ = site
    fake._items = [
        _make_item(0.50),   # too low
        _make_item(15),     # ok
        _make_item(600),    # too high
    ]
    prices = _run(mod.search_site("nike air max 90"))
    assert prices == [15.0]


def test_search_handles_missing_price_key(site):
    mod, fake, _ = site
    fake._items = [{"title": "Nike", "photo": {}}]
    prices = _run(mod.search_site("nike"))
    assert prices == []


def test_search_empty_on_non_200(site):
    mod, fake, _ = site
    fake._status_code = 500
    prices = _run(mod.search_site("test"))
    assert prices == []


def test_search_refreshes_on_401(site):
    mod, fake, _ = site
    fake._status_code = 401
    prices = _run(mod.search_site("test"))
    assert prices == []
    # Should have hit the session endpoint twice (initial + refresh)
    assert fake._session_hits == 2


def test_search_no_photo_timestamp_passes(site):
    mod, fake, _ = site
    fake._items = [{
        "title": "Nike Air Max 90",
        "total_item_price": {"amount": "20"},
        "photo": {"high_resolution": {}},
    }]
    prices = _run(mod.search_site("nike air max 90"))
    assert prices == [20.0]


# ── get_site_stats ────────────────────────────────────────────


def test_stats_returns_median(site):
    mod, fake, _ = site
    fake._items = [_make_item(10), _make_item(20), _make_item(30)]
    stats = _run(mod.get_site_stats("nike air max 90"))
    assert stats["median"] == 20.0


def test_stats_caches_result(site):
    mod, fake, _ = site
    fake._items = [_make_item(10, "Nike Hoodie"), _make_item(20, "Nike Hoodie XL")]

    async def two_lookups():
        await mod.get_site_stats("nike hoodie")
        search_before = [u for u in fake.get_calls if "catalog" in u]
        await mod.get_site_stats("nike hoodie")
        search_after = [u for u in fake.get_calls if "catalog" in u]
        return len(search_before), len(search_after)

    before, after = _run(two_lookups())
    assert before == after


def test_stats_empty_on_no_results(site):
    mod, fake, _ = site
    fake._items = []
    stats = _run(mod.get_site_stats("nothing"))
    assert stats == {}


# ── get_site_vote ─────────────────────────────────────────────


def test_vote_returns_normalized_median(site):
    mod, fake, _ = site
    fake._items = [_make_item(10), _make_item(20), _make_item(30)]
    vote = _run(mod.get_site_vote("nike air max 90", "used", 0))
    assert vote is not None
    # median=20, normalized = 20 / 0.75
    expected = 20.0 * (1 / 0.75)
    assert abs(vote["median"] - expected) < 0.01
    assert vote["replica"] == "#V0"


def test_vote_returns_none_on_no_data(site):
    mod, fake, _ = site
    fake._items = []
    vote = _run(mod.get_site_vote("nothing", "used", 0))
    assert vote is None


# ── session management ──────────────────────────────────────────


def test_ensure_session_only_once(site):
    mod, fake, _ = site
    fake._items = [_make_item(10)]
    _run(mod.search_site("a"))
    _run(mod.search_site("b"))
    assert fake._session_hits == 1


def test_refresh_session_resets_cookies(site):
    mod, fake, _ = site
    fake._items = [_make_item(10)]
    _run(mod.search_site("a"))
    _run(mod.refresh_session())
    assert fake._session_hits == 2


def test_warmup_succeeds(site):
    mod, fake, _ = site
    _run(mod.warmup())
    assert fake._session_hits == 1
