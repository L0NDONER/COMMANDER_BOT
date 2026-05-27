"""Tests for the vision-audit comparator, shadow runner, and analyser."""
import asyncio

from services.ebay import vision_audit


def _run(coro):
    return asyncio.run(coro)


# ── same_product ──────────────────────────────────────────────────────────────
def test_agrees_ignoring_size_and_order():
    a = "Ralph Lauren, Polo Shirt, L, Preppy"
    b = "Ralph Lauren Polo classic"
    assert vision_audit.same_product(a, b)


def test_brand_mismatch_is_a_split():
    # The dangerous case: confident but different brand.
    assert not vision_audit.same_product("Ralph Lauren Polo Shirt",
                                         "Uniqlo Polo Shirt")


def test_low_overlap_is_a_split():
    assert not vision_audit.same_product("Nike Air Max 90 trainers",
                                         "Nike backpack rucksack bag")


def test_empty_read_never_agrees():
    assert not vision_audit.same_product("", "Gant Shirt")
    assert not vision_audit.same_product("NOT_FOUND", "")


# ── run_shadow ────────────────────────────────────────────────────────────────
def test_run_shadow_logs_comparison(caplog):
    def fake_groq(image_path):
        return "Ralph Lauren, Polo Shirt, M"

    import logging
    with caplog.at_level(logging.INFO, logger="services.ebay.vision_audit"):
        _run(vision_audit.run_shadow("img.jpg", "Ralph Lauren Polo Shirt", fake_groq))
    assert any("VISION_AUDIT" in r.message for r in caplog.records)
    assert any('"agree": true' in r.message for r in caplog.records)


def test_run_shadow_swallows_reader_errors(caplog):
    def boom(image_path):
        raise RuntimeError("groq 500")

    # Must not raise — a diagnostic never disturbs the request.
    _run(vision_audit.run_shadow("img.jpg", "Gant Shirt", boom))


# ── analyser ──────────────────────────────────────────────────────────────────
def test_analyse_counts_splits(capsys):
    recs = [{"gemini": "A Shirt", "groq": "A Shirt", "agree": True}] * 18
    recs += [{"gemini": "Ralph Lauren Polo", "groq": "Uniqlo Polo", "agree": False}] * 2
    vision_audit.analyse(recs)
    out = capsys.readouterr().out
    assert "reads=20" in out
    assert "10%" in out          # split rate
    assert "VERDICT" in out
