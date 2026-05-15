"""Pure-function tests — no network, no Redis, no DB."""

import pytest

from services.ebay import scout_update
from services.ebay.scout_update import (
    analyse,
    charm,
    choose_vinted_discount,
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


# ---------- choose_vinted_discount ----------

def test_choose_vinted_discount_default_when_no_brand_match():
    assert choose_vinted_discount("random thing") == scout_update.DEFAULT_VINTED_DISCOUNT


def test_choose_vinted_discount_strong_brand(monkeypatch):
    monkeypatch.setattr(scout_update, "STRONG_BRANDS", ["barbour"])
    assert choose_vinted_discount("barbour jacket xxl") == 0.65


def test_choose_vinted_discount_slow_keyword(monkeypatch):
    monkeypatch.setattr(scout_update, "STRONG_BRANDS", [])
    monkeypatch.setattr(scout_update, "SLOW_KEYWORDS", ["pyjamas"])
    assert choose_vinted_discount("kids pyjamas") == 0.40


# ---------- analyse ----------

def test_analyse_returns_median():
    items = [{"price": {"value": "5"}}, {"price": {"value": "10"}}, {"price": {"value": "15"}}]
    assert analyse(items) == {"median": 10.0}


def test_analyse_empty_items():
    assert analyse([]) == {}


def test_analyse_skips_items_without_price():
    items = [{"price": {"value": "5"}}, {"title": "no price"}, {"price": {"value": "15"}}]
    assert analyse(items) == {"median": 15.0}


# ---------- charm ----------

def test_charm_basic():
    assert charm(20) == "£19.99"


def test_charm_floors_at_99p():
    assert charm(0) == "£0.99"
    assert charm(0.5) == "£0.99"


# ---------- generate_listing_draft ----------

def test_generate_listing_draft_shape():
    out = generate_listing_draft("Barbour Jacket", ["wax", "olive", "size L"])
    assert set(out.keys()) == {"title", "description", "tags"}
    assert "Barbour Jacket" in out["title"]
    assert "wax" in out["description"]
    assert out["tags"].startswith("#")


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
