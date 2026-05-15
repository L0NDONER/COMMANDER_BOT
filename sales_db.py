#!/usr/bin/env python3
"""SQLite store for /sold Vinted sale logs — the strategic moat per
project_vinted_data. Sale price only; buy price not captured yet.
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
