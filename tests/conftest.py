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


# Tests must not require a running Redis. Stub the module so imports succeed;
# any test that actually exercises Redis-touching code should monkeypatch get_redis.
class _FakeRedis:
    @classmethod
    def from_url(cls, *a, **kw):
        raise RuntimeError("Redis not available in tests — monkeypatch get_redis")


redis_stub = types.ModuleType("redis")
redis_stub.Redis = _FakeRedis
sys.modules["redis"] = redis_stub


# Stub scout_vision so scout_update can import it without pulling in
# pyzbar / google-genai / PIL (heavy deps not needed for pure-function tests).
_vision_stub = dict(identify_item=lambda image_path: ("stubbed query", ["stubbed-keyword"]))
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
