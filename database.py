"""Unified async SQLite store for commander-saas.

Holds the SaaS user ledger, a TTL-aware key/value cache that replaces the
Redis string keys (ebay_token, vision:*, stats:*), and the buys/sales tables
migrated from the old sales_db.py.

WAL mode lets the photo pipeline read/write concurrently from multiple awaits
without write locks blocking reads.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

import aiosqlite

DB_PATH = Path(__file__).parent / "commander_saas.db"

# Jaccard threshold for fuzzy buy↔sale matching in pnl().
# 0.65 catches BNIB-style additions while rejecting size mismatches —
# see test_pnl_fuzzy_* for the load-bearing cases.
MATCH_THRESHOLD = 0.65

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ------------------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------------------

async def checkpoint() -> None:
    """Fold the WAL back into the main .db on shutdown so a container recreation
    (every deploy) never strands committed writes in an in-container -wal sidecar
    — the per-file bind mount keeps -wal/-shm inside the container. Best-effort:
    never raises, blocks nothing on the way down."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id   INTEGER PRIMARY KEY,
                stars_balance INTEGER DEFAULT 0,
                joined_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kv_cache (
                cache_key   TEXT PRIMARY KEY,
                cache_value TEXT,
                expires_at  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   TEXT NOT NULL,
                query     TEXT NOT NULL,
                price     REAL NOT NULL,
                raw       TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS buys (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       TEXT NOT NULL,
                query         TEXT NOT NULL,
                buy_price     REAL NOT NULL,
                median        REAL,
                vinted_target REAL,
                verdict       TEXT,
                raw           TEXT NOT NULL,
                timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


# ------------------------------------------------------------------------------
# kv_cache — replaces Redis string keys (ebay_token, vision:*, stats:*)
# ------------------------------------------------------------------------------

async def get_cached_value(key: str):
    """Return the cached value (json-decoded) or None if missing/expired."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT cache_value, expires_at FROM kv_cache WHERE cache_key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    value, expires_at = row
    if expires_at and expires_at <= _iso(_now()):
        return None
    return json.loads(value)


async def set_cached_value(key: str, value, ttl_seconds: int = 3600) -> None:
    expires_at = _iso(_now() + timedelta(seconds=ttl_seconds))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO kv_cache (cache_key, cache_value, expires_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(value), expires_at),
        )
        await db.commit()


async def delete_cached_value(key: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM kv_cache WHERE cache_key = ?", (key,))
        await db.commit()


# ------------------------------------------------------------------------------
# Buys & sales (migrated from sales_db.py)
# ------------------------------------------------------------------------------

def _tokens(query: str) -> Set[str]:
    return {t for t in _TOKEN_SPLIT.split(query.lower()) if t}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def log_sale(chat_id: str, query: str, price: float, raw: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO sales (chat_id, query, price, raw) VALUES (?, ?, ?, ?)",
            (str(chat_id), query.lower().strip(), float(price), raw),
        )
        await db.commit()
        return cursor.lastrowid


async def recent_sales(limit: int = 20) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, query, price, timestamp FROM sales "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return await cursor.fetchall()


async def log_buy(
    chat_id: str,
    query: str,
    buy_price: float,
    median: Optional[float] = None,
    vinted_target: Optional[float] = None,
    verdict: Optional[str] = None,
    raw: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO buys (chat_id, query, buy_price, median, vinted_target, "
            "verdict, raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(chat_id),
                query.lower().strip(),
                float(buy_price),
                float(median) if median is not None else None,
                float(vinted_target) if vinted_target is not None else None,
                verdict,
                raw,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def recent_buys(limit: int = 20) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, query, buy_price, timestamp FROM buys "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return await cursor.fetchall()


async def pnl() -> list:
    """Per-query rollup joining buys↔sales by token-set Jaccard similarity.

    Returns rows of (query, total_buy, total_sale, net, buy_count, sale_count),
    sorted by net descending. Includes orphans on either side (count=0 for the
    missing side). Each buy-group matches at most one sale-group (greedy by
    similarity), so identical totals can't be double-counted across pairs.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT query, SUM(buy_price), COUNT(*) FROM buys GROUP BY query"
        ) as cursor:
            buys = await cursor.fetchall()
        async with db.execute(
            "SELECT query, SUM(price), COUNT(*) FROM sales GROUP BY query"
        ) as cursor:
            sales = await cursor.fetchall()

    candidates = []
    for bq, _bt, _bn in buys:
        for sq, _st, _sn in sales:
            score = _similarity(bq, sq)
            if score >= MATCH_THRESHOLD:
                candidates.append((score, bq, sq))
    candidates.sort(reverse=True)

    buy_lookup = {q: (t, n) for q, t, n in buys}
    sale_lookup = {q: (t, n) for q, t, n in sales}
    used_buys: Set[str] = set()
    used_sales: Set[str] = set()
    rows = []

    for _score, bq, sq in candidates:
        if bq in used_buys or sq in used_sales:
            continue
        bt, bn = buy_lookup[bq]
        st, sn = sale_lookup[sq]
        rows.append((bq, bt, st, st - bt, bn, sn))
        used_buys.add(bq)
        used_sales.add(sq)

    for bq, bt, bn in buys:
        if bq not in used_buys:
            rows.append((bq, bt, 0, -bt, bn, 0))
    for sq, st, sn in sales:
        if sq not in used_sales:
            rows.append((sq, 0, st, st, 0, sn))

    return sorted(rows, key=lambda r: r[3], reverse=True)
