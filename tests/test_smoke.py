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
    from services.market import scout_update
    # Pure helpers + verdict math survived the Redis-strip refactor.
    assert hasattr(scout_update, "analyse")
    assert hasattr(scout_update, "charm")
    assert hasattr(scout_update, "_score")
    assert hasattr(scout_update, "detect_condition")


def test_scout_async_imports():
    from services.market import scout_async
    assert hasattr(scout_async, "evaluate_with_consensus_saas")
    assert hasattr(scout_async, "get_token_async")
    assert hasattr(scout_async, "get_worker_vote_async")


def test_database_imports():
    import database
    assert hasattr(database, "init_db")
    assert hasattr(database, "get_cached_value")
    assert hasattr(database, "set_cached_value")
    assert hasattr(database, "log_buy")
    assert hasattr(database, "log_sale")
    assert hasattr(database, "pnl")
