#!/usr/bin/env python3
"""Simple SQLite balance store for Telegram Stars credits."""

import hashlib
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "stars.db"
STARS_PER_SCOUT = 5  # cost per scout query
_HASH_SALT = "commander_scout_v1"


def _user_hash(chat_id: str) -> str:
    """One-way pseudonymous ID — consistent per user, never reversible."""
    return hashlib.sha256(f"{_HASH_SALT}:{chat_id}".encode()).hexdigest()[:16]


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


BOUNTY_TIERS = {
    "story":    ("📸 Story/Tweet screenshot", 20),
    "video":    ("🎬 TikTok/Reel with bot link", 100),
    "viral":    ("🔥 10k+ views milestone", 500),
}


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                chat_id TEXT PRIMARY KEY,
                stars   INTEGER NOT NULL DEFAULT 0,
                region  TEXT NOT NULL DEFAULT 'uk'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_rewards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    TEXT NOT NULL,
                tier       TEXT NOT NULL,
                url        TEXT NOT NULL,
                submitted  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved   INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS claimed_bounties (
                chat_id TEXT PRIMARY KEY
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS scout_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_hash  TEXT,
                query      TEXT NOT NULL,
                verdict    TEXT NOT NULL,
                median     REAL,
                timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def log_scout(query: str, verdict: str, median: float = None, chat_id: str = None):
    """Log anonymised scout data. chat_id is hashed — never stored raw."""
    user_hash = _user_hash(chat_id) if chat_id else None
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO scout_log (user_hash, query, verdict, median) VALUES (?, ?, ?, ?)",
                (user_hash, query.lower(), verdict, median)
            )
        finally:
            c.close()


def get_trends(limit: int = 10) -> list:
    """Return top queries by strong verdict count for brands.py mining."""
    with _conn() as c:
        try:
            return c.execute("""
                SELECT query, COUNT(*) as scouts, AVG(median) as avg_price,
                       SUM(CASE WHEN verdict LIKE '%BUY%' THEN 1 ELSE 0 END) as buys
                FROM scout_log
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY query
                ORDER BY buys DESC, scouts DESC
                LIMIT ?
            """, (limit,)).fetchall()
        finally:
            c.close()


def get_expert_users(min_scouts: int = 20, min_buy_rate: float = 0.7) -> list:
    """Return pseudonymous users with high scout volume and strong BUY rate."""
    with _conn() as c:
        try:
            return c.execute("""
                SELECT user_hash,
                       COUNT(*) as total_scouts,
                       ROUND(AVG(median), 2) as avg_median,
                       ROUND(SUM(CASE WHEN verdict LIKE '%BUY%' THEN 1.0 ELSE 0 END) / COUNT(*), 2) as buy_rate
                FROM scout_log
                WHERE user_hash IS NOT NULL
                AND timestamp > datetime('now', '-30 days')
                GROUP BY user_hash
                HAVING total_scouts >= ? AND buy_rate >= ?
                ORDER BY buy_rate DESC, total_scouts DESC
            """, (min_scouts, min_buy_rate)).fetchall()
        finally:
            c.close()


def get_balance(chat_id: str) -> int:
    with _conn() as c:
        try:
            row = c.execute("SELECT stars FROM balances WHERE chat_id=?", (chat_id,)).fetchone()
            return row[0] if row else 0
        finally:
            c.close()


def add_stars(chat_id: str, amount: int):
    with _conn() as c:
        try:
            c.execute("""
                INSERT INTO balances (chat_id, stars) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET stars = stars + excluded.stars
            """, (chat_id, amount))
        finally:
            c.close()


def get_region(chat_id: str) -> str:
    with _conn() as c:
        try:
            row = c.execute("SELECT region FROM balances WHERE chat_id=?", (chat_id,)).fetchone()
            return row[0] if row else "uk"
        finally:
            c.close()


def set_region(chat_id: str, region: str):
    with _conn() as c:
        try:
            c.execute("""
                INSERT INTO balances (chat_id, stars, region) VALUES (?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET region = excluded.region
            """, (chat_id, region))
        finally:
            c.close()


def has_claimed_bounty(chat_id: str) -> bool:
    with _conn() as c:
        try:
            row = c.execute("SELECT 1 FROM claimed_bounties WHERE chat_id=?", (chat_id,)).fetchone()
            return row is not None
        finally:
            c.close()


def submit_video_for_review(chat_id: str, tier: str, url: str) -> bool:
    """Returns False if already claimed."""
    if has_claimed_bounty(chat_id):
        return False
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO pending_rewards (chat_id, tier, url) VALUES (?, ?, ?)",
                (chat_id, tier, url)
            )
            return True
        finally:
            c.close()


def get_pending_rewards() -> list:
    with _conn() as c:
        try:
            rows = c.execute(
                "SELECT id, chat_id, tier, url, submitted FROM pending_rewards WHERE approved=0"
            ).fetchall()
            return rows
        finally:
            c.close()


def approve_reward(reward_id: int) -> tuple:
    """Approve a pending reward. Returns (chat_id, stars_awarded)."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT chat_id, tier FROM pending_rewards WHERE id=? AND approved=0", (reward_id,)
            ).fetchone()
            if not row:
                return None, 0
            chat_id, tier = row
            _, stars = BOUNTY_TIERS.get(tier, ("", 0))
            c.execute("UPDATE pending_rewards SET approved=1 WHERE id=?", (reward_id,))
            c.execute("INSERT OR IGNORE INTO claimed_bounties (chat_id) VALUES (?)", (chat_id,))
            return chat_id, stars
        finally:
            c.close()


def deduct_stars(chat_id: str, amount: int) -> bool:
    """Returns False if insufficient balance. Atomic check-and-deduct."""
    with _conn() as c:
        try:
            rows = c.execute(
                "UPDATE balances SET stars = stars - ? WHERE chat_id = ? AND stars >= ?",
                (amount, chat_id, amount)
            ).rowcount
            return rows > 0
        finally:
            c.close()
