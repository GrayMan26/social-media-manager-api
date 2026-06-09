"""
SQLite database for the Social Media Manager.
Tables:
  pending_posts  — posts in any stage (draft → approved → posted)
  chat_history   — conversation history per session
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "manager.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT    NOT NULL,
                content      TEXT    NOT NULL,
                image_url    TEXT    DEFAULT '',
                scheduled_at TEXT    DEFAULT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending_approval',
                created_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
        """)


# ── pending_posts ──────────────────────────────────────────────────────────────

def create_post(platform: str, content: str, image_url: str = "",
                scheduled_at: str | None = None) -> int:
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO pending_posts (platform, content, image_url, scheduled_at, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending_approval', ?)",
            (platform, content, image_url, scheduled_at, now),
        )
        return cur.lastrowid


def get_post(post_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM pending_posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None


def approve_post(post_id: int):
    with _conn() as c:
        c.execute("UPDATE pending_posts SET status = 'approved' WHERE id = ?", (post_id,))


def reject_post(post_id: int):
    with _conn() as c:
        c.execute("UPDATE pending_posts SET status = 'rejected' WHERE id = ?", (post_id,))


def mark_posted(post_id: int):
    with _conn() as c:
        c.execute("UPDATE pending_posts SET status = 'posted' WHERE id = ?", (post_id,))


def get_due_posts() -> list[dict]:
    """Return approved posts whose scheduled_at is now or in the past."""
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM pending_posts WHERE status = 'approved' AND scheduled_at <= ?",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_posts_by_status(status: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM pending_posts WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── chat_history ───────────────────────────────────────────────────────────────

def save_message(session_id: str, role: str, content: str):
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_history (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )


def get_history(session_id: str, limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_history WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
