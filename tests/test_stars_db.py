"""Stars DB CRUD tests with a tmp-path DB so the real stars.db is untouched."""

import importlib
import sys

import pytest


@pytest.fixture
def stars_db(tmp_path, monkeypatch):
    """Reload stars_db with DB_PATH pointed at a temp file."""
    monkeypatch.syspath_prepend(str(tmp_path))
    if "stars_db" in sys.modules:
        del sys.modules["stars_db"]
    import stars_db as mod
    mod.DB_PATH = tmp_path / "stars.db"
    mod.init_db()
    return mod


def test_new_user_has_zero_balance(stars_db):
    assert stars_db.get_balance("alice") == 0


def test_add_and_get_balance(stars_db):
    stars_db.add_stars("alice", 100)
    assert stars_db.get_balance("alice") == 100
    stars_db.add_stars("alice", 50)
    assert stars_db.get_balance("alice") == 150


def test_deduct_succeeds_when_sufficient(stars_db):
    stars_db.add_stars("alice", 100)
    assert stars_db.deduct_stars("alice", 30) is True
    assert stars_db.get_balance("alice") == 70


def test_deduct_refuses_overdraw(stars_db):
    stars_db.add_stars("alice", 10)
    assert stars_db.deduct_stars("alice", 30) is False
    assert stars_db.get_balance("alice") == 10


def test_signup_bonus_only_grants_once(stars_db):
    assert stars_db.claim_signup_bonus("alice", 100) is True
    assert stars_db.get_balance("alice") == 100
    assert stars_db.claim_signup_bonus("alice", 100) is False
    assert stars_db.get_balance("alice") == 100


def test_region_defaults_to_uk_and_can_be_set(stars_db):
    assert stars_db.get_region("alice") == "uk"
    stars_db.set_region("alice", "us")
    assert stars_db.get_region("alice") == "us"


def test_bounty_can_only_be_claimed_once(stars_db):
    assert stars_db.has_claimed_bounty("alice") is False
    assert stars_db.submit_video_for_review("alice", "story", "https://x.com/foo") is True
    rewards = stars_db.get_pending_rewards()
    assert len(rewards) == 1
    reward_id = rewards[0][0]
    chat, stars = stars_db.approve_reward(reward_id)
    assert chat == "alice"
    assert stars == 20
    assert stars_db.has_claimed_bounty("alice") is True
    assert stars_db.submit_video_for_review("alice", "story", "https://x.com/bar") is False


def test_user_hash_is_stable_and_truncated(stars_db):
    h1 = stars_db._user_hash("12345")
    h2 = stars_db._user_hash("12345")
    assert h1 == h2
    assert len(h1) == 16
    assert h1 != stars_db._user_hash("67890")
