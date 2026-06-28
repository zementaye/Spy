"""
db.py — SQLite persistence for Word Wolf bot.

Two responsibilities:
1. Leaderboard data (permanent, survives restarts).
2. Active game state snapshots (allows crash recovery on restart).

Crash-recovery limits:
- In-flight asyncio timers are NOT restored (they restart fresh).
- Game resumes from the correct state/round, but countdown reminders restart.
- Hints already submitted in the current round are preserved.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Optional

DB_PATH = Path("wordwolf.db")
logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                username    TEXT,
                games_played    INTEGER DEFAULT 0,
                civ_wins        INTEGER DEFAULT 0,
                civ_losses      INTEGER DEFAULT 0,
                imp_wins        INTEGER DEFAULT 0,
                imp_losses      INTEGER DEFAULT 0,
                times_caught    INTEGER DEFAULT 0,
                times_guessed_back INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS game_state (
                chat_id     INTEGER PRIMARY KEY,
                state_json  TEXT NOT NULL,
                updated_at  REAL NOT NULL
            );
        """)


# ── Leaderboard ──────────────────────────────────────────────────────────────

def ensure_player(conn: sqlite3.Connection, user_id: int, chat_id: int, username: str) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO leaderboard (user_id, chat_id, username)
        VALUES (?, ?, ?)
    """, (user_id, chat_id, username))
    conn.execute("""
        UPDATE leaderboard SET username = ? WHERE user_id = ? AND chat_id = ?
    """, (username, user_id, chat_id))


def record_game_result(
    chat_id: int,
    civilians: list[dict],     # list of {user_id, username}
    imposters: list[dict],     # list of {user_id, username}
    civilians_won: bool,
    caught_imposter_ids: set[int],
    guessed_back_ids: set[int],
) -> None:
    """Update leaderboard stats after a completed game."""
    with get_conn() as conn:
        for p in civilians:
            ensure_player(conn, p["user_id"], chat_id, p["username"])
            if civilians_won:
                conn.execute(
                    "UPDATE leaderboard SET games_played=games_played+1, civ_wins=civ_wins+1 WHERE user_id=? AND chat_id=?",
                    (p["user_id"], chat_id)
                )
            else:
                conn.execute(
                    "UPDATE leaderboard SET games_played=games_played+1, civ_losses=civ_losses+1 WHERE user_id=? AND chat_id=?",
                    (p["user_id"], chat_id)
                )

        for p in imposters:
            ensure_player(conn, p["user_id"], chat_id, p["username"])
            imp_won = not civilians_won
            caught = p["user_id"] in caught_imposter_ids
            guessed = p["user_id"] in guessed_back_ids

            conn.execute("""
                UPDATE leaderboard SET
                    games_played = games_played + 1,
                    imp_wins     = imp_wins + ?,
                    imp_losses   = imp_losses + ?,
                    times_caught = times_caught + ?,
                    times_guessed_back = times_guessed_back + ?
                WHERE user_id=? AND chat_id=?
            """, (
                1 if imp_won else 0,
                1 if not imp_won else 0,
                1 if caught else 0,
                1 if guessed else 0,
                p["user_id"], chat_id
            ))


def get_leaderboard(chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
    """Return top players sorted by wins (imp + civ), then games played."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT username,
                   games_played,
                   civ_wins + imp_wins AS total_wins,
                   civ_wins, civ_losses,
                   imp_wins, imp_losses,
                   times_caught, times_guessed_back
            FROM leaderboard
            WHERE chat_id = ?
            ORDER BY total_wins DESC, games_played DESC
            LIMIT ?
        """, (chat_id, limit)).fetchall()


# ── Game State Snapshots ──────────────────────────────────────────────────────

def save_game_state(chat_id: int, state: dict) -> None:
    """Snapshot current game state to DB for crash recovery."""
    import time
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO game_state (chat_id, state_json, updated_at)
            VALUES (?, ?, ?)
        """, (chat_id, json.dumps(state), time.time()))


def load_game_state(chat_id: int) -> Optional[dict]:
    """Load a previously saved game state (returns None if none exists)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state_json FROM game_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if row:
            return json.loads(row["state_json"])
    return None


def delete_game_state(chat_id: int) -> None:
    """Remove a game state snapshot (called when game ends cleanly)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM game_state WHERE chat_id=?", (chat_id,))


def load_all_game_states() -> list[tuple[int, dict]]:
    """Load all saved game states on startup (for crash recovery)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id, state_json FROM game_state").fetchall()
        return [(r["chat_id"], json.loads(r["state_json"])) for r in rows]
