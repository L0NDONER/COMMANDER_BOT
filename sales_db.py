#!/usr/bin/env python3
"""SQLite store for Vinted arbitrage moat — buys (auto-logged from photo evals)
and sales (logged via /sold). /pnl joins them by lowercased query.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "sales.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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
    """Per-query rollup joining buys ↔ sales. Returns rows of
    (query, total_buy, total_sale, net, buy_count, sale_count).
    Includes orphans on either side (count=0 for the missing side).
    """
    with _conn() as c:
        return c.execute("""
            WITH b AS (
                SELECT query,
                       SUM(buy_price) AS total_buy,
                       COUNT(*)       AS n
                FROM buys GROUP BY query
            ),
            s AS (
                SELECT query,
                       SUM(price) AS total_sale,
                       COUNT(*)   AS n
                FROM sales GROUP BY query
            )
            SELECT b.query,
                   b.total_buy,
                   COALESCE(s.total_sale, 0),
                   COALESCE(s.total_sale, 0) - b.total_buy,
                   b.n,
                   COALESCE(s.n, 0)
            FROM b LEFT JOIN s ON b.query = s.query
            UNION ALL
            SELECT s.query,
                   0,
                   s.total_sale,
                   s.total_sale,
                   0,
                   s.n
            FROM s WHERE s.query NOT IN (SELECT query FROM b)
            ORDER BY 4 DESC
        """).fetchall()
