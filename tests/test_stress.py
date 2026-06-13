"""Stress test — hammers evaluate_with_consensus_saas with concurrency and
varied trust/feedback fixtures. Catches races on shared state (token lock,
stats cache) and verifies trust integer aggregation is stable under load."""

import asyncio
import sys
import time

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _items_with_feedback(*feedback_pcts):
    """Build fake market item list with given seller feedbackPercentage values."""  # [ZWJheQ==]
    prices = [20.0, 25.0, 30.0]
    return [
        {
            "price": {"value": str(prices[i % len(prices)])},
            "seller": {"feedbackPercentage": str(pct)},
        }
        for i, pct in enumerate(feedback_pcts)
    ]


class FakeAsyncClient:
    def __init__(self, items):
        self.token_calls = 0
        self._items = items

    async def post(self, url, **kwargs):
        if "oauth2/token" in url:
            self.token_calls += 1
            await asyncio.sleep(0)
            return _FakeResponse({"access_token": "tok", "expires_in": 7200})
        raise AssertionError(f"Unexpected POST: {url}")

    async def get(self, url, **kwargs):
        if "item_summary/search" in url:
            return _FakeResponse({"itemSummaries": self._items})
        raise AssertionError(f"Unexpected GET: {url}")

    async def aclose(self):
        pass


# ── Fixture ───────────────────────────────────────────────────────────────────

def _make_scout(tmp_path, items):
    for name in ("database", "services.market.scout_async"):
        sys.modules.pop(name, None)

    import database
    database.DB_PATH = tmp_path / "stress.db"
    asyncio.run(database.init_db())

    import services.market.scout_async as mod
    fake = FakeAsyncClient(items)
    mod._client = fake
    mod._token_lock = asyncio.Lock()
    return mod, fake


@pytest.fixture
def image_path(tmp_path):
    p = tmp_path / "img.jpg"
    p.write_bytes(b"fake-image")
    return str(p)


# ── Stress helpers ────────────────────────────────────────────────────────────

async def _blast(mod, image_path, buy_price, n):
    coros = [mod.evaluate_with_consensus_saas(image_path, buy_price) for _ in range(n)]
    return await asyncio.gather(*coros)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_concurrent_50_all_succeed(tmp_path, image_path):
    """50 concurrent evals on a warm cache all return success with no exceptions."""
    items = _items_with_feedback(100, 99, 97)
    mod, _ = _make_scout(tmp_path, items)
    results = asyncio.run(_blast(mod, image_path, "5.00", 50))
    errors = [r for r in results if r.get("status") != "success"]
    assert not errors, f"{len(errors)} failures: {errors[:3]}"


def test_token_fetched_once_under_50_concurrent(tmp_path, image_path):
    """Token lock must prevent stampede — OAuth endpoint hit exactly once."""
    items = _items_with_feedback(100, 98)
    mod, fake = _make_scout(tmp_path, items)
    asyncio.run(_blast(mod, image_path, "5.00", 50))
    assert fake.token_calls == 1, f"token fetched {fake.token_calls}× — lock broken"


def test_trust_3_when_all_100_percent(tmp_path, image_path):
    items = _items_with_feedback(100, 100, 100)
    mod, _ = _make_scout(tmp_path, items)
    results = asyncio.run(_blast(mod, image_path, "5.00", 20))
    trusts = {r["trust"] for r in results if r.get("status") == "success"}
    assert trusts == {3}


def test_trust_2_when_all_98_percent(tmp_path, image_path):
    items = _items_with_feedback(98, 98, 98)
    mod, _ = _make_scout(tmp_path, items)
    results = asyncio.run(_blast(mod, image_path, "5.00", 20))
    trusts = {r["trust"] for r in results if r.get("status") == "success"}
    assert trusts == {2}


def test_trust_1_when_all_95_percent(tmp_path, image_path):
    items = _items_with_feedback(95, 96, 97)
    mod, _ = _make_scout(tmp_path, items)
    results = asyncio.run(_blast(mod, image_path, "5.00", 20))
    trusts = {r["trust"] for r in results if r.get("status") == "success"}
    assert trusts == {1}


def test_trust_0_when_below_95_percent(tmp_path, image_path):
    items = _items_with_feedback(90, 91, 88)
    mod, _ = _make_scout(tmp_path, items)
    results = asyncio.run(_blast(mod, image_path, "5.00", 20))
    trusts = {r["trust"] for r in results if r.get("status") == "success"}
    assert trusts == {0}


def test_trust_median_mixed_feedback(tmp_path, image_path):
    # 100→3, 98→2, 90→0 — median of [3,2,0] = 2
    items = _items_with_feedback(100, 98, 90)
    mod, _ = _make_scout(tmp_path, items)
    result = asyncio.run(mod.evaluate_with_consensus_saas(image_path, "5.00"))
    assert result["status"] == "success"
    assert result["trust"] == 2


def test_trust_none_when_no_feedback_field(tmp_path, image_path):
    """Items with no seller.feedbackPercentage → trust is None, not a crash."""
    items = [
        {"price": {"value": "20"}},
        {"price": {"value": "25"}},
        {"price": {"value": "30"}},
    ]
    mod, _ = _make_scout(tmp_path, items)
    result = asyncio.run(mod.evaluate_with_consensus_saas(image_path, "5.00"))
    assert result["status"] == "success"
    assert result["trust"] is None


def test_spread_is_integer(tmp_path, image_path):
    """Smoke-check spread (added alongside trust) is an int."""
    items = _items_with_feedback(100, 98, 97)
    mod, _ = _make_scout(tmp_path, items)
    result = asyncio.run(mod.evaluate_with_consensus_saas(image_path, "5.00"))
    assert result["status"] == "success"
    assert isinstance(result["spread"], int)


def test_throughput_50_in_under_2s(tmp_path, image_path):
    """50 concurrent evals (all cached after first) must complete in <2 s."""
    items = _items_with_feedback(100, 99, 97)
    mod, _ = _make_scout(tmp_path, items)
    # warm the cache
    asyncio.run(mod.evaluate_with_consensus_saas(image_path, "5.00"))
    t0 = time.monotonic()
    asyncio.run(_blast(mod, image_path, "5.00", 50))
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"50 evals took {elapsed:.2f}s — too slow"
