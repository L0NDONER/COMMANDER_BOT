"""Test setup: stub EC2-only modules so imports resolve in dev / CI."""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


_stub(
    "credentials",
    TELEGRAM_BOT_TOKEN="test-telegram",
    TELEGRAM_CHAT_ID="0",
    ALLOWED_CHAT_IDS=["0"],
    GROQ_API_KEY="test-groq",
    GROQ_MODEL="llama-3.3-70b-versatile",
    EBAY_APP_ID="test-app",
    EBAY_SECRET="test-secret",
    GEMINI_API_KEY="test-gemini",
)


# Stub scout_vision so scout_update can import it without pulling in
# pyzbar / google-genai / PIL (heavy deps not needed for pure-function tests).
_vision_stub = dict(
    identify_item=lambda image_path: ("stubbed query", ["stubbed-keyword"]),
    groq_identify=lambda image_path: "stubbed, query",
)
_stub("scout_vision", **_vision_stub)
_stub("services.ebay.scout_vision", **_vision_stub)


brands_attrs = dict(
    STRONG_BRANDS=[],
    SLOW_KEYWORDS=[],
    is_low_value=lambda q: False,
    handle_brands=lambda *a, **kw: "",
    get_brand_tip=lambda *a, **kw: None,
)
_stub("brands", **brands_attrs)
_stub("services.ebay.brands", **brands_attrs)


# Stub consensus_engine — real implementation is gitignored, EC2-only.
# This stub mirrors today's contract so orchestration tests can run in CI.
# Future tuning on EC2 stays private.
import asyncio as _asyncio


def _stub_build_variants(base_query, condition, keywords):
    cond_word = "new" if condition == "new" else "used"
    variants = [base_query, f"{base_query} {cond_word}"]
    base_lower = base_query.lower()
    for kw in keywords[:3]:
        if kw and kw.lower() not in base_lower:
            variants.append(f"{base_query} {kw}")
    return list(dict.fromkeys(variants))[:4]


async def _stub_gather_votes(variants, condition, fetch_vote, timeout):
    tasks = [fetch_vote(v, condition, i) for i, v in enumerate(variants)]
    try:
        results = await _asyncio.wait_for(
            _asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except _asyncio.TimeoutError:
        return None
    return [r for r in results if isinstance(r, dict) and "median" in r]


async def _stub_vinted_vote(query, condition, index=0):
    return None

async def _stub_warmup():
    pass

_stub("services.ebay.vinted_catalog", get_vinted_vote=_stub_vinted_vote, warmup=_stub_warmup)

def _stub_record_consensus(base_query, condition, keywords, votes):
    pass


_stub(
    "services.ebay.consensus_engine",
    MIN_VOTES_FOR_CONSENSUS=2,
    build_variants=_stub_build_variants,
    gather_votes=_stub_gather_votes,
    record_consensus=_stub_record_consensus,
)
