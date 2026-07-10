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


async def init_db() -> None:
    """
    Initialize the SQLite database and create the cache table if it doesn't exist.
    Called once at bot startup.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                url           TEXT PRIMARY KEY,
                content_type  TEXT NOT NULL,
                file_ids      TEXT NOT NULL,
                audio_file_id TEXT,
                created_at    REAL NOT NULL
            )
        """)
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def get_cached(url: str) -> dict | None:
    """
    Look up a TikTok URL in the cache.

    Args:
        url: The normalized TikTok URL string.

    Returns:
        A dict with keys {content_type, file_ids, audio_file_id} if found,
        or None if the URL is not cached.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content_type, file_ids, audio_file_id FROM cache WHERE url = ?",
            (url,)
        ) as cursor:
            row = await cursor.fetchone()

    if row:
        return {
            "content_type":  row[0],
            "file_ids":      json.loads(row[1]),
            "audio_file_id": row[2],
        }
    return None


async def set_cached(
    url: str,
    content_type: str,
    file_ids: list[str],
    audio_file_id: str | None = None
) -> None:
    """
    Insert or replace a cache record for a TikTok URL.

    Args:
        url          : The normalized TikTok URL.
        content_type : 'video' or 'carousel'.
        file_ids     : List of Telegram file_ids (1 for video, N for carousel images).
        audio_file_id: Telegram file_id for the extracted MP3 audio.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO cache (url, content_type, file_ids, audio_file_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, content_type, json.dumps(file_ids), audio_file_id, time.time())
        )
        await db.commit()
    logger.debug("Cached entry saved: %s [%s]", url, content_type)


async def delete_expired(ttl_days: int = 7) -> int:
    """
    Delete all cache records older than `ttl_days` days.
    Called by the background scheduler.

    Args:
        ttl_days: Number of days before a record is considered stale.

    Returns:
        Number of rows deleted.
    """
    cutoff = time.time() - (ttl_days * 86400)  # 86400 seconds per day
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM cache WHERE created_at < ?", (cutoff,)
        )
        await db.commit()
        deleted = cursor.rowcount

    if deleted:
        logger.info("Cache cleanup: removed %d expired record(s)", deleted)
    return deleted
