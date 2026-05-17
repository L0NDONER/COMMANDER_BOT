"""sales_db + /sold parser tests with a tmp-path DB."""

import sys

import pytest


@pytest.fixture
def sales_db(tmp_path):
    if "sales_db" in sys.modules:
        del sys.modules["sales_db"]
    import sales_db as mod
    mod.DB_PATH = tmp_path / "sales.db"
    mod.init_db()
    return mod


def test_log_sale_returns_id_and_persists(sales_db):
    rid = sales_db.log_sale("12345", "Jordan 1 Low uk 9", 55.0, "Jordan 1 Low uk 9 £55")
    assert rid == 1
    rows = sales_db.recent_sales()
    assert len(rows) == 1
    assert rows[0][1] == "jordan 1 low uk 9"  # query lower-cased
    assert rows[0][2] == 55.0


def test_recent_sales_orders_newest_first(sales_db):
    sales_db.log_sale("u", "first item", 10.0, "first")
    sales_db.log_sale("u", "second item", 20.0, "second")
    rows = sales_db.recent_sales()
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


def test_log_buy_persists(sales_db):
    rid = sales_db.log_buy("u", "Jordan 1 Low uk 9", 4.50,
                           median=70.0, vinted_target=50.4,
                           verdict="✅ BUY", raw="4.50")
    assert rid == 1
    rows = sales_db.recent_buys()
    assert len(rows) == 1
    assert rows[0][1] == "jordan 1 low uk 9"
    assert rows[0][2] == 4.50


def test_pnl_matches_buy_and_sale_on_lowercased_query(sales_db):
    sales_db.log_buy("u", "Jordan 1 Low uk 9", 4.50, raw="4.50")
    sales_db.log_sale("u", "Jordan 1 Low UK 9", 55.0, "Jordan 1 Low UK 9 £55")
    rows = sales_db.pnl()
    assert len(rows) == 1
    q, bought, sold, net, bn, sn = rows[0]
    assert q == "jordan 1 low uk 9"
    assert bought == 4.50
    assert sold == 55.0
    assert net == 50.50
    assert (bn, sn) == (1, 1)


def test_pnl_includes_orphans_on_both_sides(sales_db):
    sales_db.log_buy("u", "unmatched buy", 10.0, raw="10")
    sales_db.log_sale("u", "unmatched sale", 25.0, "unmatched sale £25")
    rows = {r[0]: r for r in sales_db.pnl()}
    assert rows["unmatched buy"][1:5] == (10.0, 0, -10.0, 1)
    assert rows["unmatched sale"][1:5] == (0, 25.0, 25.0, 0)


def test_pnl_sums_multiple_buys_and_sales_per_query(sales_db):
    sales_db.log_buy("u", "fleece", 5.0, raw="5")
    sales_db.log_buy("u", "fleece", 7.0, raw="7")
    sales_db.log_sale("u", "fleece", 20.0, "fleece £20")
    sales_db.log_sale("u", "fleece", 18.0, "fleece £18")
    rows = sales_db.pnl()
    assert len(rows) == 1
    q, bought, sold, net, bn, sn = rows[0]
    assert (bought, sold, net, bn, sn) == (12.0, 38.0, 26.0, 2, 2)


def test_pnl_fuzzy_matches_extra_modifier(sales_db):
    # Buy was logged with BNIB (from photo eval), sale was logged without it.
    sales_db.log_buy("u", "Jordan 1 Low uk 9 BNIB", 4.50, raw="4.50")
    sales_db.log_sale("u", "Jordan 1 Low uk 9", 55.0, "Jordan 1 Low uk 9 £55")
    rows = sales_db.pnl()
    assert len(rows) == 1
    _q, bought, sold, net, bn, sn = rows[0]
    assert (bought, sold, net, bn, sn) == (4.50, 55.0, 50.50, 1, 1)


def test_pnl_fuzzy_rejects_size_mismatch(sales_db):
    # Different sizes are NOT the same listing — must stay as orphans.
    sales_db.log_buy("u", "Jordan 1 uk 9", 4.50, raw="4.50")
    sales_db.log_sale("u", "Jordan 1 uk 11", 55.0, "Jordan 1 uk 11 £55")
    rows = {r[0]: r for r in sales_db.pnl()}
    assert rows["jordan 1 uk 9"][1:5] == (4.50, 0, -4.50, 1)
    assert rows["jordan 1 uk 11"][1:5] == (0, 55.0, 55.0, 0)


def test_pnl_fuzzy_tolerates_punctuation(sales_db):
    sales_db.log_buy("u", "Air Jordan 1 (Low) UK-9", 4.50, raw="4.50")
    sales_db.log_sale("u", "air jordan 1 low uk 9", 55.0, "air jordan 1 low uk 9 £55")
    rows = sales_db.pnl()
    assert len(rows) == 1
    assert rows[0][3] == 50.50  # net


def test_pnl_greedy_picks_best_match_first(sales_db):
    # Two buys could each plausibly match one sale; greedy must pick the higher-similarity pair.
    sales_db.log_buy("u", "barbour bedale wax jacket size 42", 20.0, raw="20")
    sales_db.log_buy("u", "barbour jacket", 15.0, raw="15")
    sales_db.log_sale("u", "barbour bedale wax jacket size 42", 80.0, "barbour bedale wax jacket size 42 £80")
    rows = {r[0]: r for r in sales_db.pnl()}
    # Exact match wins; the looser "barbour jacket" buy stays an orphan.
    assert rows["barbour bedale wax jacket size 42"][1:5] == (20.0, 80.0, 60.0, 1)
    assert rows["barbour jacket"][1:5] == (15.0, 0, -15.0, 1)
