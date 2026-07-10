"""
database.py
-----------
Handles all SQLite caching logic using aiosqlite for non-blocking I/O.
Stores Telegram file_ids keyed by TikTok URL to avoid re-uploading.

Cache schema:
  - url          : Original TikTok URL (primary key)
  - content_type : 'video' | 'carousel'
  - file_ids     : JSON-encoded list of Telegram file_ids
  - audio_file_id: Telegram file_id for the extracted MP3
  - created_at   : Unix timestamp for TTL cleanup
"""

import aiosqlite
import json
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("cache.db")


# CHANGE: Replace init_db() only — everything else in database.py stays the same

async def init_db() -> None:
    """
    Initialize the SQLite database.
    UPDATED: Added 'platform' column (tiktok | instagram | facebook)
    for multi-platform cache tracking.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                url           TEXT PRIMARY KEY,
                platform      TEXT NOT NULL DEFAULT 'tiktok',
                content_type  TEXT NOT NULL,
                file_ids      TEXT NOT NULL,
                audio_file_id TEXT,
                created_at    REAL NOT NULL
            )
        """)
        # Non-destructive migration: add column if upgrading from old schema
        try:
            await db.execute("ALTER TABLE cache ADD COLUMN platform TEXT NOT NULL DEFAULT 'tiktok'")
        except Exception:
            pass  # Column already exists — safe to ignore
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


# CHANGE: Replace set_cached() to accept platform parameter

async def set_cached(
    url: str,
    content_type: str,
    file_ids: list[str],
    audio_file_id: str | None = None,
    platform: str = "tiktok",          # NEW parameter
) -> None:
    """
    Insert or replace a cache record.
    UPDATED: Now stores platform alongside the entry.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO cache
                (url, platform, content_type, file_ids, audio_file_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (url, platform, content_type, json.dumps(file_ids), audio_file_id, time.time())
        )
        await db.commit()
    logger.debug("Cached [%s][%s]: %s", platform, content_type, url)
