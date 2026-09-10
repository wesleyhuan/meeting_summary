import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    speaker TEXT NOT NULL CHECK (speaker IN ('you', 'other')),
    text TEXT NOT NULL,
    confidence REAL,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "mic_device_id": "",
    "stt_language": "en",
    "whisper_model_size": "base",
}


def default_db_path() -> str:
    appdata = os.getenv("APPDATA", str(Path.home()))
    db_dir = Path(appdata) / "livesubtitle"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = str(db_dir / "livesubtitle.db")
    logger.info("Using default db path: %s", path)
    return path


def connect(db_path: str) -> sqlite3.Connection:
    logger.info("Connecting to db at %s", db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def create_meeting(conn: sqlite3.Connection, title: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO meetings (title, started_at) VALUES (?, ?)", (title, now)
    )
    conn.commit()
    logger.info("Created meeting id=%s title=%r", cur.lastrowid, title)
    return cur.lastrowid


def end_meeting(conn: sqlite3.Connection, meeting_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE meetings SET ended_at = ? WHERE id = ?", (now, meeting_id))
    conn.commit()
    logger.info("Ended meeting id=%s", meeting_id)


def add_segment(
    conn: sqlite3.Connection,
    meeting_id: int,
    speaker: str,
    text: str,
    confidence: Optional[float],
    started_at: Optional[str] = None,
) -> int:
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO transcript_segments (meeting_id, speaker, text, confidence, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (meeting_id, speaker, text, confidence, started_at),
    )
    conn.commit()
    logger.debug(
        "Added segment meeting_id=%s speaker=%s confidence=%s started_at=%s text=%r",
        meeting_id, speaker, confidence, started_at, text,
    )
    return cur.lastrowid


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()


def get_transcript(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY started_at",
        (meeting_id,),
    ).fetchall()


def list_meetings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings ORDER BY started_at DESC"
    ).fetchall()


def get_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    logger.info("Set setting %s=%r", key, value)


def get_all_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({row["key"]: row["value"] for row in rows})
    return settings
