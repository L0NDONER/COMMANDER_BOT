#!/usr/bin/env python3
"""SQLite store for Vinted arbitrage moat — buys (auto-logged from photo evals)
and sales (logged via /sold). /pnl joins them by Jaccard token similarity.
"""

import re
import sqlite3
from pathlib import Path
from typing import Set

DB_PATH = Path(__file__).parent / "sales.db"

# Two queries are merged in /pnl if their token-set Jaccard similarity
# meets this threshold. 0.65 catches BNIB-style additions ("jordan 1 low uk 9"
# ↔ "jordan 1 low uk 9 bnib", 5/6 = 0.83) while rejecting size mismatches
# ("jordan 1 uk 9" ↔ "jordan 1 uk 11", 3/5 = 0.6).
MATCH_THRESHOLD = 0.65

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _tokens(query: str) -> Set[str]:
    return {t for t in _TOKEN_SPLIT.split(query.lower()) if t}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   TEXT NOT NULL,
                query     TEXT NOT NULL,
                price     REAL NOT NULL,
                raw       TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
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


def log_sale(chat_id: str, query: str, price: float, raw: str) -> int:
    with _conn() as c:
        cursor = c.execute(
            "INSERT INTO sales (chat_id, query, price, raw) VALUES (?, ?, ?, ?)",
            (str(chat_id), query.lower().strip(), float(price), raw),
        )
        return cursor.lastrowid


def recent_sales(limit: int = 20) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT id, query, price, timestamp FROM sales "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def log_buy(chat_id: str, query: str, buy_price: float,
            median: float = None, vinted_target: float = None,
            verdict: str = None, raw: str = "") -> int:
    with _conn() as c:
        cursor = c.execute(
            "INSERT INTO buys (chat_id, query, buy_price, median, vinted_target, "
            "verdict, raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), query.lower().strip(), float(buy_price),
             float(median) if median is not None else None,
             float(vinted_target) if vinted_target is not None else None,
             verdict, raw),
        )
        return cursor.lastrowid


def recent_buys(limit: int = 20) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT id, query, buy_price, timestamp FROM buys "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def pnl() -> list:
    """Per-query rollup joining buys ↔ sales by token-set Jaccard similarity.

    Returns rows of (query, total_buy, total_sale, net, buy_count, sale_count),
    sorted by net descending. Includes orphans on either side (count=0 for the
    missing side). Each buy-group matches at most one sale-group (greedy by
    similarity), so identical totals can't be double-counted across pairs.
    """
    with _conn() as c:
        buys = c.execute(
            "SELECT query, SUM(buy_price), COUNT(*) FROM buys GROUP BY query"
        ).fetchall()
        sales = c.execute(
            "SELECT query, SUM(price), COUNT(*) FROM sales GROUP BY query"
        ).fetchall()

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
