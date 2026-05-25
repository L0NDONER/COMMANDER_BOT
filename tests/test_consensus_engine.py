"""Tests for services/ebay/consensus_engine.py."""

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.ebay.consensus_engine import (
    MIN_VOTES_FOR_CONSENSUS,
    build_variants,
    gather_votes,
)


# ── build_variants ──────────────────────────────────────────────


class TestBuildVariants:
    def test_base_and_condition(self):
        v = build_variants("nike hoodie", "used", [])
        assert v[0] == "nike hoodie"
        assert v[1] == "nike hoodie used"

    def test_new_condition_word(self):
        v = build_variants("nike hoodie", "new", [])
        assert v[1] == "nike hoodie new"

    def test_keywords_appended(self):
        v = build_variants("nike hoodie", "used", ["mens", "vintage"])
        assert "nike hoodie mens" in v
        assert "nike hoodie vintage" in v

    def test_duplicate_keyword_skipped(self):
        v = build_variants("nike hoodie", "used", ["Nike"])
        assert len([x for x in v if "nike" in x.lower()]) == len(v)
        assert len(v) == 2

    def test_max_four_variants(self):
        v = build_variants("q", "used", ["a", "b", "c", "d", "e"])
        assert len(v) <= 4

    def test_keyword_limit_three(self):
        v = build_variants("q", "used", ["a", "b", "c", "d"])
        kw_variants = [x for x in v if x not in ("q", "q used")]
        assert len(kw_variants) <= 3

    def test_dedup_preserves_order(self):
        v = build_variants("q", "used", ["used"])
        assert v == ["q", "q used"]

    def test_empty_keyword_ignored(self):
        v = build_variants("q", "used", ["", None, "real"])
        assert "q " not in v
        assert "q real" in v


# ── gather_votes ────────────────────────────────────────────────


class TestGatherVotes:
    @pytest.mark.asyncio
    async def test_collects_successful_votes(self):
        async def fetcher(query, condition, idx):
            return {"median": 10.0, "query": query}

        votes = await gather_votes(["a", "b"], "used", fetcher, timeout=5.0)
        assert len(votes) == 2
        assert all("median" in v for v in votes)

    @pytest.mark.asyncio
    async def test_filters_exceptions(self):
        async def fetcher(query, condition, idx):
            if idx == 0:
                raise ValueError("boom")
            return {"median": 5.0}

        votes = await gather_votes(["a", "b"], "used", fetcher, timeout=5.0)
        assert len(votes) == 1

    @pytest.mark.asyncio
    async def test_filters_none_results(self):
        async def fetcher(query, condition, idx):
            return None

        votes = await gather_votes(["a", "b"], "used", fetcher, timeout=5.0)
        assert votes == []

    @pytest.mark.asyncio
    async def test_filters_dict_without_median(self):
        async def fetcher(query, condition, idx):
            return {"price": 10.0}

        votes = await gather_votes(["a", "b"], "used", fetcher, timeout=5.0)
        assert votes == []

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        async def fetcher(query, condition, idx):
            await asyncio.sleep(10)
            return {"median": 1.0}

        result = await gather_votes(["a"], "used", fetcher, timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_partial_success(self):
        async def fetcher(query, condition, idx):
            if idx == 0:
                return {"median": 20.0}
            raise ConnectionError("down")

        votes = await gather_votes(["a", "b", "c"], "used", fetcher, timeout=5.0)
        assert len(votes) == 1
        assert votes[0]["median"] == 20.0


# ── MIN_VOTES_FOR_CONSENSUS ────────────────────────────────────


def test_min_votes_is_two():
    assert MIN_VOTES_FOR_CONSENSUS == 2
