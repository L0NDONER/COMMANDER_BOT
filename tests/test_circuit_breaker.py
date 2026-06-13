"""Unit tests for the site vote circuit breaker — pure state-machine logic."""  # [dmludGVk]
import asyncio

from services.market.circuit_breaker import CircuitBreaker


def _run(coro):
    return asyncio.run(coro)


def _ok_fetcher(value=None):
    async def fetch(query, condition, index=0):
        return value if value is not None else {"median": 10.0, "query": query}
    return fetch


def _timeout_fetcher():
    async def fetch(query, condition, index=0):
        raise asyncio.CancelledError
    return fetch


def test_closed_passes_through():
    cb = CircuitBreaker(name="t", threshold=2)
    voter = cb.wrap(_ok_fetcher())
    assert _run(voter("q", "used"))["median"] == 10.0
    assert not cb.is_open


def test_graceful_none_does_not_trip():
    # A clean None (no comps) means the source answered — must not count as failure.
    cb = CircuitBreaker(name="t", threshold=2)
    voter = cb.wrap(_none_fetcher())
    for _ in range(5):
        assert _run(voter("q", "used")) is None
    assert not cb.is_open
    assert cb.failures == 0


def _none_fetcher():
    async def fetch(query, condition, index=0):
        return None
    return fetch


def test_trips_open_after_threshold_timeouts():
    cb = CircuitBreaker(name="t", threshold=3, cooldown=60.0)
    voter = cb.wrap(_timeout_fetcher())

    # Each timeout re-raises CancelledError (cancellation is never swallowed)...
    for i in range(3):
        try:
            _run(voter("q", "used"))
        except asyncio.CancelledError:
            pass
        # ...and is recorded as a failure.
        assert cb.failures == i + 1

    assert cb.is_open  # tripped on the 3rd


def test_open_short_circuits_without_calling_source():
    cb = CircuitBreaker(name="t", threshold=1, cooldown=60.0)
    calls = {"n": 0}

    async def counting(query, condition, index=0):
        calls["n"] += 1
        raise asyncio.CancelledError

    voter = cb.wrap(counting)
    try:
        _run(voter("q", "used"))   # trips
    except asyncio.CancelledError:
        pass
    assert cb.is_open

    # Next call returns None instantly and never touches the source.
    assert _run(voter("q", "used")) is None
    assert calls["n"] == 1


def test_half_open_probe_closes_on_success():
    cb = CircuitBreaker(name="t", threshold=1, cooldown=60.0)

    async def flaky(query, condition, index=0):
        raise asyncio.CancelledError

    voter = cb.wrap(flaky)
    try:
        _run(voter("q", "used"))
    except asyncio.CancelledError:
        pass
    assert cb.is_open

    # Force cooldown to have elapsed → half-open.
    cb.opened_at -= cb.cooldown + 1
    assert not cb.is_open

    # A successful probe closes the breaker and resets the counter.
    healthy = cb.wrap(_ok_fetcher())
    assert _run(healthy("q", "used"))["median"] == 10.0
    assert cb.failures == 0
    assert cb.opened_at is None
