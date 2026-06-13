"""database + /sold parser tests with a tmp-path DB.

Async DB calls are driven via asyncio.run() per test, so the suite stays
synchronous and we avoid an extra pytest-asyncio dependency.
"""

import asyncio
import sys

import pytest


@pytest.fixture
def db(tmp_path):
    if "database" in sys.modules:
        del sys.modules["database"]
    import database as mod
    mod.DB_PATH = tmp_path / "commander_saas.db"
    asyncio.run(mod.init_db())
    return mod


def _run(coro):
    return asyncio.run(coro)


def test_checkpoint_runs_and_persists_sale(db):
    # A logged sale survives a WAL checkpoint (the shutdown fold-down path).
    _run(db.log_sale("12345", "Rab Hoodie XL", 30.0, "Rab Hoodie XL £30"))
    _run(db.checkpoint())                      # must not raise
    assert _run(db.recent_sales())             # sale still readable post-checkpoint


def test_log_sale_returns_id_and_persists(db):
    rid = _run(db.log_sale("12345", "Jordan 1 Low uk 9", 55.0, "Jordan 1 Low uk 9 £55"))
    assert rid == 1
    rows = _run(db.recent_sales())
    assert len(rows) == 1
    assert rows[0][1] == "jordan 1 low uk 9"  # query lower-cased
    assert rows[0][2] == 55.0


def test_recent_sales_orders_newest_first(db):
    _run(db.log_sale("u", "first item", 10.0, "first"))
    _run(db.log_sale("u", "second item", 20.0, "second"))
    rows = _run(db.recent_sales())
    assert rows[0][1] == "second item"
    assert rows[1][1] == "first item"


@pytest.fixture
def parse_sold():
    if "telegram_app" in sys.modules:
        del sys.modules["telegram_app"]
    from telegram_app import parse_sold as fn
    return fn


def test_parse_sold_extracts_simple_price(parse_sold):
    query, price = parse_sold("Jordan 1 Low uk 9 £55")
    assert query == "Jordan 1 Low uk 9"
    assert price == 55.0


def test_parse_sold_handles_pence(parse_sold):
    query, price = parse_sold("North Face fleece M £12.50")
    assert query == "North Face fleece M"
    assert price == 12.50


def test_parse_sold_strips_trailing_dashes(parse_sold):
    query, price = parse_sold(
        "Jordan Air Jordan 1 Low uk 9 - exellent condition - "
        "aura, squadron blue -BNIB £55"
    )
    assert price == 55.0
    assert "BNIB" in query
    assert not query.endswith("-")


def test_parse_sold_returns_none_without_pound_prefix(parse_sold):
    query, price = parse_sold("Jordan 1 Low uk 9 sold for 55")
    assert query is None
    assert price is None


def test_log_buy_persists(db):
    rid = _run(db.log_buy("u", "Jordan 1 Low uk 9", 4.50,
                          median=70.0, site_target=50.4,  # [dmludGVk]
                          verdict="✅ BUY", raw="4.50"))
    assert rid == 1
    rows = _run(db.recent_buys())
    assert len(rows) == 1
    assert rows[0][1] == "jordan 1 low uk 9"
    assert rows[0][2] == 4.50


def test_pnl_matches_buy_and_sale_on_lowercased_query(db):
    _run(db.log_buy("u", "Jordan 1 Low uk 9", 4.50, raw="4.50"))
    _run(db.log_sale("u", "Jordan 1 Low UK 9", 55.0, "Jordan 1 Low UK 9 £55"))
    rows = _run(db.pnl())
    assert len(rows) == 1
    q, bought, sold, net, bn, sn = rows[0]
    assert q == "jordan 1 low uk 9"
    assert bought == 4.50
    assert sold == 55.0
    assert net == 50.50
    assert (bn, sn) == (1, 1)


def test_pnl_includes_orphans_on_both_sides(db):
    _run(db.log_buy("u", "unmatched buy", 10.0, raw="10"))
    _run(db.log_sale("u", "unmatched sale", 25.0, "unmatched sale £25"))
    rows = {r[0]: r for r in _run(db.pnl())}
    assert rows["unmatched buy"][1:5] == (10.0, 0, -10.0, 1)
    assert rows["unmatched sale"][1:5] == (0, 25.0, 25.0, 0)


def test_pnl_sums_multiple_buys_and_sales_per_query(db):
    _run(db.log_buy("u", "fleece", 5.0, raw="5"))
    _run(db.log_buy("u", "fleece", 7.0, raw="7"))
    _run(db.log_sale("u", "fleece", 20.0, "fleece £20"))
    _run(db.log_sale("u", "fleece", 18.0, "fleece £18"))
    rows = _run(db.pnl())
    assert len(rows) == 1
    q, bought, sold, net, bn, sn = rows[0]
    assert (bought, sold, net, bn, sn) == (12.0, 38.0, 26.0, 2, 2)


def test_pnl_fuzzy_matches_extra_modifier(db):
    # Buy was logged with BNIB (from photo eval), sale was logged without it.
    _run(db.log_buy("u", "Jordan 1 Low uk 9 BNIB", 4.50, raw="4.50"))
    _run(db.log_sale("u", "Jordan 1 Low uk 9", 55.0, "Jordan 1 Low uk 9 £55"))
    rows = _run(db.pnl())
    assert len(rows) == 1
    _q, bought, sold, net, bn, sn = rows[0]
    assert (bought, sold, net, bn, sn) == (4.50, 55.0, 50.50, 1, 1)


def test_pnl_fuzzy_rejects_size_mismatch(db):
    # Different sizes are NOT the same listing — must stay as orphans.
    _run(db.log_buy("u", "Jordan 1 uk 9", 4.50, raw="4.50"))
    _run(db.log_sale("u", "Jordan 1 uk 11", 55.0, "Jordan 1 uk 11 £55"))
    rows = {r[0]: r for r in _run(db.pnl())}
    assert rows["jordan 1 uk 9"][1:5] == (4.50, 0, -4.50, 1)
    assert rows["jordan 1 uk 11"][1:5] == (0, 55.0, 55.0, 0)


def test_pnl_fuzzy_tolerates_punctuation(db):
    _run(db.log_buy("u", "Air Jordan 1 (Low) UK-9", 4.50, raw="4.50"))
    _run(db.log_sale("u", "air jordan 1 low uk 9", 55.0, "air jordan 1 low uk 9 £55"))
    rows = _run(db.pnl())
    assert len(rows) == 1
    assert rows[0][3] == 50.50  # net


def test_pnl_greedy_picks_best_match_first(db):
    # Two buys could each plausibly match one sale; greedy must pick the higher-similarity pair.
    _run(db.log_buy("u", "barbour bedale wax jacket size 42", 20.0, raw="20"))
    _run(db.log_buy("u", "barbour jacket", 15.0, raw="15"))
    _run(db.log_sale("u", "barbour bedale wax jacket size 42", 80.0, "barbour bedale wax jacket size 42 £80"))
    rows = {r[0]: r for r in _run(db.pnl())}
    # Exact match wins; the looser "barbour jacket" buy stays an orphan.
    assert rows["barbour bedale wax jacket size 42"][1:5] == (20.0, 80.0, 60.0, 1)
    assert rows["barbour jacket"][1:5] == (15.0, 0, -15.0, 1)


# ------------------------------------------------------------------------------
# kv_cache — new TTL store that replaces Redis string keys
# ------------------------------------------------------------------------------

def test_kv_cache_round_trip(db):
    _run(db.set_cached_value("token", "abc123", ttl_seconds=60))
    assert _run(db.get_cached_value("token")) == "abc123"


def test_kv_cache_returns_none_when_missing(db):
    assert _run(db.get_cached_value("not-there")) is None


def test_kv_cache_returns_none_when_expired(db):
    _run(db.set_cached_value("k", "v", ttl_seconds=-1))
    assert _run(db.get_cached_value("k")) is None


def test_kv_cache_stores_json_serialisable_dicts(db):
    payload = {"median": 42.5, "query": "barbour"}
    _run(db.set_cached_value("stats:used:barbour", payload, ttl_seconds=60))
    assert _run(db.get_cached_value("stats:used:barbour")) == payload


def test_kv_cache_delete(db):
    _run(db.set_cached_value("k", "v", ttl_seconds=60))
    _run(db.delete_cached_value("k"))
    assert _run(db.get_cached_value("k")) is None
