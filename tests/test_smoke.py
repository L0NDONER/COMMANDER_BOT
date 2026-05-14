"""Smoke tests: catch module-level import breakage (renamed APIs, removed names).

These don't exercise behaviour — they just verify the deployed modules
still load after dependency bumps. Cheap insurance for upgrades.
"""

def test_telegram_app_imports():
    import telegram_app
    assert hasattr(telegram_app, "main")
    assert hasattr(telegram_app, "handle_photo")
    assert hasattr(telegram_app, "start")


def test_scout_update_imports():
    from services.ebay import scout_update
    assert hasattr(scout_update, "evaluate_with_consensus")
    assert hasattr(scout_update, "get_stats")
    assert hasattr(scout_update, "cast_vote")


def test_worker_imports():
    from services.ebay import worker
    assert hasattr(worker, "handle_task")
    assert hasattr(worker, "run_worker")
