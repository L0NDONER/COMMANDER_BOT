"""Pure-function tests — no network, no Redis, no DB."""

import pytest

from services.market import scout_update
from services.market.scout_update import (
    _title_matches,
    analyse,
    charm,
    choose_site_discount,
    compute_confidence,
    detect_condition,
    diversify_query,
    generate_listing_draft,
)


# ---------- diversify_query ----------

def test_diversify_query_returns_base_for_index_0(monkeypatch):
    monkeypatch.setenv("WORKER_INDEX", "0")
    assert diversify_query("barbour", "leader") == "barbour"


def test_diversify_query_varies_by_index(monkeypatch):
    seen = set()
    for i in range(5):
        monkeypatch.setenv("WORKER_INDEX", str(i))
        seen.add(diversify_query("barbour", f"w{i}"))
    assert len(seen) == 5, f"expected 5 distinct variants, got {seen}"


# ---------- compute_confidence ----------

def test_compute_confidence_empty():
    assert compute_confidence([]) == "LOW"


def test_compute_confidence_tight_spread_is_high():
    assert compute_confidence([100.0, 105.0, 110.0]) == "HIGH"


def test_compute_confidence_wide_spread_is_low():
    assert compute_confidence([10.0, 50.0, 100.0]) == "LOW"


def test_compute_confidence_zero_avg():
    assert compute_confidence([0.0, 0.0, 0.0]) == "LOW"


# ---------- confidence ROI haircut (verdict gating) ----------

def test_haircut_low_confidence_demotes_marginal_strong_to_maybe():
    # Wide median spread → LOW confidence. True ROI ~156% would be STRONG on its
    # own, but the 0.5 haircut drops it below the 80% BUY line → MAYBE.
    votes = [
        {"median": 50.0, "replica": "#0", "query": "widget"},
        {"median": 100.0, "replica": "#1", "query": "widget"},
        {"median": 150.0, "replica": "#2", "query": "widget"},
    ]
    res = scout_update._score(votes, "widget", clean_buy=29.3)
    assert res["confidence"] == "LOW"
    assert round(res["roi"]) == 156          # displayed ROI is the true value
    assert res["verdict"] == "MAYBE"


def test_haircut_high_confidence_keeps_strong():
    # Same ROI, tight spread → HIGH confidence, no haircut → stays STRONG BUY.
    votes = [
        {"median": 95.0, "replica": "#0", "query": "widget"},
        {"median": 100.0, "replica": "#1", "query": "widget"},
        {"median": 105.0, "replica": "#2", "query": "widget"},
    ]
    res = scout_update._score(votes, "widget", clean_buy=29.3)
    assert res["confidence"] == "HIGH"
    assert res["verdict"] == "STRONG BUY"


def test_haircut_low_confidence_spares_big_winner():
    # Even halved, a huge ROI clears the STRONG line — LOW shouldn't demote it.
    votes = [
        {"median": 50.0, "replica": "#0", "query": "widget"},
        {"median": 100.0, "replica": "#1", "query": "widget"},
        {"median": 150.0, "replica": "#2", "query": "widget"},
    ]
    res = scout_update._score(votes, "widget", clean_buy=10.0)
    assert res["confidence"] == "LOW"
    assert res["verdict"] == "STRONG BUY"


# ---------- choose_site_discount ----------

def test_choose_site_discount_default_when_no_brand_match():
    assert choose_site_discount("random thing") == scout_update.DEFAULT_SITE_DISCOUNT


def test_choose_site_discount_strong_brand(monkeypatch):
    monkeypatch.setattr(scout_update, "STRONG_BRANDS", ["barbour"])
    assert choose_site_discount("barbour jacket xxl") == scout_update.STRONG_BRAND_DISCOUNT


def test_choose_site_discount_slow_keyword(monkeypatch):
    monkeypatch.setattr(scout_update, "STRONG_BRANDS", [])
    monkeypatch.setattr(scout_update, "SLOW_KEYWORDS", ["pyjamas"])
    assert choose_site_discount("kids pyjamas") == 0.40


# ---------- analyse ----------

def test_analyse_returns_median():
    items = [{"price": {"value": "5"}}, {"price": {"value": "10"}}, {"price": {"value": "15"}}]
    assert analyse(items) == {"median": 10.0}


def test_analyse_empty_items():
    assert analyse([]) == {}


def test_analyse_skips_items_without_price():
    items = [{"price": {"value": "5"}}, {"title": "no price"}, {"price": {"value": "15"}}]
    assert analyse(items) == {"median": 10.0}


def test_analyse_filters_irrelevant_titles():
    items = [
        {"title": "Gant Gingham Shirt XL", "price": {"value": "25"}},
        {"title": "Nike Air Max Trainers", "price": {"value": "80"}},
        {"title": "Gant Oxford Shirt Large", "price": {"value": "30"}},
    ]
    assert analyse(items, query="Gant Shirt") == {"median": 27.5}


def test_analyse_keeps_items_without_title():
    items = [
        {"price": {"value": "10"}},
        {"title": "Gant Shirt M", "price": {"value": "20"}},
    ]
    assert analyse(items, query="Gant Shirt") == {"median": 15.0}


def test_analyse_filters_non_gb_listings():
    items = [
        {"title": "Gant Shirt", "price": {"value": "20"}, "itemLocation": {"country": "GB"}},
        {"title": "Gant Shirt", "price": {"value": "50"}, "itemLocation": {"country": "US"}},
        {"title": "Gant Shirt", "price": {"value": "30"}, "itemLocation": {"country": "GB"}},
    ]
    assert analyse(items, query="Gant Shirt") == {"median": 25.0}


def test_analyse_keeps_items_without_location():
    items = [
        {"title": "Gant Shirt", "price": {"value": "20"}},
        {"title": "Gant Shirt", "price": {"value": "30"}, "itemLocation": {"country": "GB"}},
    ]
    assert analyse(items, query="Gant Shirt") == {"median": 25.0}


def test_analyse_freshness_weights_newer_listings_higher(monkeypatch):
    from datetime import datetime, timedelta, timezone
    # Half-life = 1 day so the 30-day-old listing has negligible weight.
    # Fresh items: £10 and £20 → weighted median should be dominated by them, not £50.
    monkeypatch.setattr(scout_update, "FRESHNESS_HALFLIFE_DAYS", 1)
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old   = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        {"price": {"value": "10"}, "itemCreationDate": fresh},
        {"price": {"value": "50"}, "itemCreationDate": old},
        {"price": {"value": "20"}, "itemCreationDate": fresh},
    ]
    result = analyse(items)
    assert result["median"] < 25.0, "old £50 outlier should be downweighted below simple median"


def test_analyse_passes_items_without_creation_date():
    # No date → age treated as 0 (weight 1.0), participates at full strength.
    items = [
        {"price": {"value": "10"}},
        {"price": {"value": "20"}},
    ]
    assert analyse(items) == {"median": 15.0}


def test_title_matches_requires_min_tokens():
    assert _title_matches("Gant Gingham Shirt XL", "Gant Shirt") is True
    assert _title_matches("Gant Polo Jumper", "Gant Shirt") is False
    assert _title_matches("Random Shirt Blue", "Gant Shirt") is False


def test_title_matches_single_token_query():
    assert _title_matches("Gant Oxford Shirt", "Gant") is True
    assert _title_matches("Nike Trainer", "Gant") is False


# ---------- charm ----------

def test_charm_basic():
    assert charm(20) == "£19.99"


def test_charm_floors_at_99p():
    assert charm(0) == "£0.99"
    assert charm(0.5) == "£0.99"


def test_charm_rounds_not_truncates():
    # 20.75 should round up to 21 then drop to 20.99 — not truncate to 19.99.
    assert charm(20.75) == "£20.99"
    # Below the .5 boundary still rounds down.
    assert charm(20.40) == "£19.99"


# ---------- generate_listing_draft ----------

def test_generate_listing_draft_shape():
    out = generate_listing_draft("Barbour Jacket", ["wax", "olive", "size L"])
    assert set(out.keys()) == {"title", "description", "tags"}
    assert "Barbour Jacket" in out["title"]
    assert "wax" in out["description"]
    assert out["tags"] == "wax, olive, size L"  # comma-separated, spaces kept


def test_generate_listing_draft_title_capped_at_80():
    long_query = "x" * 200
    out = generate_listing_draft(long_query, ["k1"])
    assert len(out["title"]) <= 80


# ---------- detect_condition ----------

@pytest.mark.parametrize("caption", [
    "4.50 brand new never worn still in box",
    "5 BNIB",
    "10 sealed",
    "3.50 BNWT",
    "8 new with tags",
    "12 unworn",
    "6 new in box",
])
def test_detect_condition_new(caption):
    assert detect_condition(caption) == "new"


@pytest.mark.parametrize("caption", [
    "4.50",
    "5 charity shop find",
    "10",
    "",
    "new arrival in store",  # "new" alone must not trigger
])
def test_detect_condition_used(caption):
    assert detect_condition(caption) == "used"


def test_detect_condition_handles_none():
    assert detect_condition(None) == "used"


# ---------- diversify_query condition swap ----------

def test_diversify_query_swaps_used_for_new(monkeypatch):
    monkeypatch.setenv("WORKER_INDEX", "1")  # the "{base} used"/"new" slot
    assert diversify_query("jordan 1", "w1", "used") == "jordan 1 used"
    assert diversify_query("jordan 1", "w1", "new") == "jordan 1 new"
