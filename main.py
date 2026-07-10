"""
main.py
-------
Bot entry point. Wires together:
  - aiogram 3.x dispatcher + Router
  - Anti-spam middleware (per-user cooldown)
  - /start, /help command handlers
  - TikTok, Instagram, Facebook URL auto-detection
  - Silent cache delivery (no "from cache" messages)
  - Audio delivery for TikTok only
  - APScheduler background task for cache cleanup
  - Graceful startup & shutdown lifecycle hooks
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Any, Awaitable

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    InputMediaPhoto,
    FSInputFile,
)
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import database
import api

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SECONDS", "3"))
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

# ---------------------------------------------------------------------------
# URL Regex Patterns
# ---------------------------------------------------------------------------

TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

INSTAGRAM_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|stories)/\S+",
    re.IGNORECASE,
)

FACEBOOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch|fb\.com)/\S+",
    re.IGNORECASE,
)

# Combined pattern used as the message handler filter
ANY_SUPPORTED_URL_PATTERN = re.compile(
    r"https?://(?:"
    r"(?:www\.|vm\.|vt\.)?tiktok\.com"
    r"|(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|tv|stories)"
    r"|(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch|fb\.com)"
    r")/\S+",
    re.IGNORECASE,
)

router = Router()

# ---------------------------------------------------------------------------
# Anti-Spam Middleware
# ---------------------------------------------------------------------------

class CooldownMiddleware:
    """
    Per-user cooldown middleware.
    Rejects requests faster than COOLDOWN_SEC seconds per user,
    preventing Telegram flood-wait errors.
    """

    def __init__(self, cooldown: float = 3.0):
        self.cooldown = cooldown
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user_id   = event.from_user.id if event.from_user else 0
        now       = time.monotonic()
        last      = self._last_seen.get(user_id, 0.0)
        remaining = self.cooldown - (now - last)

        if remaining > 0:
            logger.debug("Rate-limited user %d (%.1fs remaining)", user_id, remaining)
            return  # Silently swallow the event

        self._last_seen[user_id] = now
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    /start — Personalized welcome using the user's first name.
    Clean formatting with minimal emojis.
    """
    first_name = message.from_user.first_name if message.from_user else "there"

    await message.answer(
        f"Welcome, <b>{first_name}</b>.\n\n"
        "This bot downloads media from <b>TikTok</b>, <b>Instagram</b>, and "
        "<b>Facebook</b> — delivered directly in Telegram, no watermarks.\n\n"
        "<b>What you get</b>\n"
        "<blockquote>"
        "· TikTok — HD video + extracted MP3 audio\n"
        "· Instagram — Reels as video, Posts as photo album\n"
        "· Facebook — Public videos and Reels"
        "</blockquote>\n"
        "<b>How to use</b>\n"
        "<blockquote>"
        "1. Copy any supported link\n"
        "2. Paste it here — no commands needed\n"
        "3. Your media will be sent momentarily"
        "</blockquote>\n"
        "<i>Only public content is supported. "
        "Private accounts and login-required content cannot be accessed.</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """
    /help — Lists all supported URL formats across platforms.
    """
    await message.answer(
        "<b>Supported Platforms</b>\n\n"
        "<b>TikTok</b>\n"
        "<blockquote>"
        "tiktok.com/@user/video/ID\n"
        "vm.tiktok.com/shortcode\n"
        "vt.tiktok.com/shortcode"
        "</blockquote>\n"
        "<b>Instagram</b>\n"
        "<blockquote>"
        "instagram.com/p/shortcode\n"
        "instagram.com/reel/shortcode"
        "</blockquote>\n"
        "<b>Facebook</b>\n"
        "<blockquote>"
        "facebook.com/watch?v=ID\n"
        "facebook.com/reel/ID\n"
        "fb.watch/shortcode"
        "</blockquote>\n"
        "<i>A 3-second cooldown applies between requests "
        "to ensure stable delivery.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Core Media Handler
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(ANY_SUPPORTED_URL_PATTERN))
async def handle_media_link(message: Message, bot: Bot) -> None:
    """
    Universal handler for TikTok, Instagram, and Facebook URLs.

    Flow:
      1. Extract & detect platform from the message
      2. Check SQLite cache — deliver silently if found
      3. Show "Processing..." only for fresh fetches
      4. Fetch, download, upload, then cache file_ids
      5. Clean up all temporary files
    """
    text = message.text or ""

    # Match URL in priority order
    match = (
        TIKTOK_URL_PATTERN.search(text)
        or INSTAGRAM_URL_PATTERN.search(text)
        or FACEBOOK_URL_PATTERN.search(text)
    )
    if not match:
        return

    raw_url   = match.group(0)
    clean_url = api.normalize_url(raw_url)
    platform  = api.detect_platform(clean_url)

    user_id = message.from_user.id if message.from_user else 0
    logger.info("User %d | %s | %s", user_id, platform.upper(), clean_url)

    # ── Cache hit: completely silent delivery ──────────────────────────────
    cached = await database.get_cached(clean_url)
    if cached:
        await _deliver_from_cache(message, bot, cached)
        return

    # ── Fresh fetch: show status message ──────────────────────────────────
    status_msg = await message.answer(
        "<i>Processing your link, please wait...</i>",
        parse_mode=ParseMode.HTML,
    )

    media_data = await api.fetch_media_data(clean_url, platform=platform)

    if not media_data:
        platform_hints = {
            "instagram": "\n<i>Make sure the account is public.</i>",
            "facebook":  "\n<i>Only public Facebook content is supported.</i>",
            "tiktok":    "",
        }
        hint = platform_hints.get(platform, "")
        await status_msg.edit_text(
            f"<b>Unable to process this link.</b>\n"
            f"The content may be private, deleted, or temporarily unavailable.{hint}",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Route to correct processor ─────────────────────────────────────────
    try:
        if media_data["content_type"] == "video":
            await _process_video(
                message, bot, status_msg, clean_url, media_data, platform=platform
            )
        else:
            await _process_carousel(
                message, bot, status_msg, clean_url, media_data, platform=platform
            )
    except Exception as exc:
        logger.exception(
            "Unhandled error [%s] %s: %s", platform, clean_url, exc
        )
        await status_msg.edit_text(
            "<b>An unexpected error occurred.</b> Please try again in a moment.",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Delivery Helpers
# ---------------------------------------------------------------------------

async def _deliver_from_cache(message: Message, bot: Bot, cached: dict) -> None:
    """
    Deliver media from cached Telegram file_ids.
    Completely silent — no status messages, no labels.
    Audio only sent for TikTok entries.
    """
    file_ids      = cached["file_ids"]
    audio_file_id = cached.get("audio_file_id")
    content_type  = cached["content_type"]
    platform      = cached.get("platform", "tiktok")

    if content_type == "video":
        await message.answer_video(video=file_ids[0])
    else:
        media_group = [InputMediaPhoto(media=fid) for fid in file_ids]
        await message.answer_media_group(media=media_group)

    # Audio only for TikTok
    if platform == "tiktok" and audio_file_id:
        await message.answer_audio(audio=audio_file_id)


async def _process_video(
    message: Message,
    bot: Bot,
    status_msg: Message,
    url: str,
    data: api.VideoData,
    platform: str = "tiktok",
) -> None:
    """
    Download a video, upload to Telegram, cache file_ids.
    Audio extraction and upload only applies to TikTok.
    Cleans up all temp files regardless of success or failure.
    """
    video_path = audio_path = None

    try:
        await status_msg.edit_text(
            "<i>Downloading video...</i>", parse_mode=ParseMode.HTML
        )

        video_path = await api.download_to_tempfile(
            data["video_url"], suffix=".mp4"
        )
        if not video_path:
            await status_msg.edit_text(
                "<b>Download failed.</b> The source file could not be retrieved.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Audio extraction — TikTok only
        if platform == "tiktok":
            await status_msg.edit_text(
                "<i>Extracting audio...</i>", parse_mode=ParseMode.HTML
            )
            audio_path = await api.extract_audio_from_video(video_path)

            # Fallback to direct audio URL if ffmpeg extraction fails
            if not audio_path and data.get("audio_url"):
                audio_path = await api.download_to_tempfile(
                    data["audio_url"], suffix=".mp3"
                )

        await status_msg.edit_text(
            "<i>Uploading to Telegram...</i>", parse_mode=ParseMode.HTML
        )

        # Upload video
        caption = (
            f"<b>{data['title'][:900]}</b>" if data.get("title") else None
        )
        video_msg = await message.answer_video(
            video      = FSInputFile(video_path),
            caption    = caption,
            parse_mode = ParseMode.HTML,
        )
        video_file_id = video_msg.video.file_id

        # Upload audio — TikTok only
        audio_file_id = None
        if platform == "tiktok" and audio_path and audio_path.exists():
            audio_msg = await message.answer_audio(
                audio = FSInputFile(audio_path),
            )
            audio_file_id = audio_msg.audio.file_id

        # Persist to cache
        await database.set_cached(
            url           = url,
            content_type  = "video",
            file_ids      = [video_file_id],
            audio_file_id = audio_file_id,  # None for non-TikTok
            platform      = platform,
        )

        await bot.delete_message(message.chat.id, status_msg.message_id)

    finally:
        for path in (video_path, audio_path):
            if path and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


async def _process_carousel(
    message: Message,
    bot: Bot,
    status_msg: Message,
    url: str,
    data: api.VideoData,
    platform: str = "tiktok",
) -> None:
    """
    Download carousel images + audio, send as Telegram album, cache file_ids.
    Audio only applies to TikTok carousels (slideshow posts with music).
    Cleans up all temp files regardless of success or failure.
    """
    image_paths: list[Path] = []
    audio_path  = None

    try:
        await status_msg.edit_text(
            "<i>Downloading photos...</i>", parse_mode=ParseMode.HTML
        )

        # Download all images concurrently
        download_tasks = [
            api.download_to_tempfile(img_url, suffix=".jpg")
            for img_url in data["image_urls"]
        ]
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Path):
                image_paths.append(res)
            else:
                logger.warning("Carousel image download failed: %s", res)

        if not image_paths:
            await status_msg.edit_text(
                "<b>Could not download any photos</b> from this post.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Download audio — TikTok only
        if platform == "tiktok" and data.get("audio_url"):
            audio_path = await api.download_to_tempfile(
                data["audio_url"], suffix=".mp3"
            )

        # Build album — no captions for clean look
        media_group = [
            InputMediaPhoto(media=FSInputFile(img_path))
            for img_path in image_paths
        ]

        await status_msg.edit_text(
            "<i>Uploading album...</i>", parse_mode=ParseMode.HTML
        )
        album_msgs = await message.answer_media_group(media=media_group)

        # Collect file_ids from uploaded photos
        photo_file_ids = []
        for msg in album_msgs:
            if msg.photo:
                photo_file_ids.append(msg.photo[-1].file_id)

        # Upload audio — TikTok only
        audio_file_id = None
        if platform == "tiktok" and audio_path and audio_path.exists():
            audio_msg = await message.answer_audio(
                audio = FSInputFile(audio_path),
            )
            audio_file_id = audio_msg.audio.file_id

        # Persist to cache
        if photo_file_ids:
            await database.set_cached(
                url           = url,
                content_type  = "carousel",
                file_ids      = photo_file_ids,
                audio_file_id = audio_file_id,  # None for non-TikTok
                platform      = platform,
            )

        await bot.delete_message(message.chat.id, status_msg.message_id)

    finally:
        for path in image_paths + ([audio_path] if audio_path else []):
            if path and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Background Scheduler
# ---------------------------------------------------------------------------

async def scheduled_cleanup() -> None:
    """
    Periodic task: deletes cache records older than CACHE_TTL_DAYS.
    Runs every 24 hours via APScheduler.
    """
    logger.info("Running scheduled cache cleanup...")
    deleted = await database.delete_expired(ttl_days=CACHE_TTL_DAYS)
    logger.info("Cleanup complete — %d record(s) removed", deleted)


# ---------------------------------------------------------------------------
# Main Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Initialize all components and start the bot in long-polling mode.
    """
    # Initialize SQLite database
    await database.init_db()

    # APScheduler — cache cleanup every 24 hours
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_cleanup,
        trigger       = "interval",
        hours         = 24,
        id            = "cache_cleanup",
        max_instances = 1,
    )
    scheduler.start()
    logger.info("Scheduler started — cleanup runs every 24h")

    # Build bot & dispatcher
    bot = Bot(
        token   = BOT_TOKEN,
        default = DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Register cooldown middleware
    dp.message.middleware(CooldownMiddleware(cooldown=COOLDOWN_SEC))

    # Register router
    dp.include_router(router)

    logger.info("Bot starting — TikTok · Instagram · Facebook")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        scheduler.shutdown()
        await bot.session.close()
        logger.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
