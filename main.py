"""
main.py
-------
Bot entry point. Wires together:
  - aiogram 3.x dispatcher + Router
  - Anti-spam middleware (per-user cooldown)
  - /start, /help command handlers
  - TikTok URL auto-detection handler
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
    BufferedInputFile,
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

# Regex pattern that matches all TikTok URL variants:
#   tiktok.com/@user/video/ID
#   vm.tiktok.com/shortcode
#   vt.tiktok.com/shortcode
TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

router = Router()

# ---------------------------------------------------------------------------
# Anti-Spam Middleware
# ---------------------------------------------------------------------------

class CooldownMiddleware:
    """
    Per-user cooldown middleware.
    Rejects requests that arrive faster than COOLDOWN_SEC seconds
    for the same user, preventing Telegram flood-wait errors.

    Implemented as a classic callable middleware compatible with aiogram 3.
    """

    def __init__(self, cooldown: float = 3.0):
        self.cooldown = cooldown
        # Maps user_id → timestamp of their last accepted request
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id if event.from_user else 0
        now = time.monotonic()
        last = self._last_seen.get(user_id, 0.0)
        remaining = self.cooldown - (now - last)

        if remaining > 0:
            # Silently ignore the duplicate — or uncomment below to notify:
            # await event.answer(f"⏳ Please wait {remaining:.1f}s before sending another link.")
            logger.debug("Rate-limited user %d (%.1fs remaining)", user_id, remaining)
            return  # swallow the event

        self._last_seen[user_id] = now
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle the /start command with a friendly welcome message."""
    await message.answer(
        "👋 <b>Welcome to TikTok Downloader Bot!</b>\n\n"
        "Simply paste any TikTok link and I'll fetch:\n"
        "  🎬 <b>Video</b> — HD, no watermark\n"
        "  🎵 <b>Audio</b> — extracted MP3\n"
        "  🖼️ <b>Photos</b> — full carousel album\n\n"
        "Just send a link to get started!",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle the /help command with usage instructions."""
    await message.answer(
        "ℹ️ <b>How to use:</b>\n\n"
        "1. Copy any TikTok link\n"
        "2. Paste it here\n"
        "3. Receive the HD video + MP3 audio (or photo album)\n\n"
        "<b>Supported URL formats:</b>\n"
        "• <code>https://www.tiktok.com/@user/video/ID</code>\n"
        "• <code>https://vm.tiktok.com/shortcode</code>\n"
        "• <code>https://vt.tiktok.com/shortcode</code>\n\n"
        "⚠️ Please wait a few seconds between requests.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Core TikTok Handler
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(TIKTOK_URL_PATTERN))
async def handle_tiktok_link(message: Message, bot: Bot) -> None:
    """
    Main handler triggered whenever a message contains a TikTok URL.

    Flow:
      1. Extract & normalize URL from the message
      2. Check SQLite cache → send instantly if found
      3. Fetch fresh data from API (tikwm → yt-dlp fallback)
      4. Download & upload media, then cache the file_ids
      5. Clean up all temporary files
    """
    # ── Step 1: Extract URL ────────────────────────────────────────────────
    match = TIKTOK_URL_PATTERN.search(message.text or "")
    if not match:
        return
    raw_url  = match.group(0)
    clean_url = api.normalize_url(raw_url)

    user_id = message.from_user.id if message.from_user else 0
    logger.info("User %d → %s", user_id, clean_url)

    # ── Step 2: Cache hit ──────────────────────────────────────────────────
    cached = await database.get_cached(clean_url)
    if cached:
        status_msg = await message.answer("⚡ <i>Sending from cache...</i>", parse_mode=ParseMode.HTML)
        try:
            await _deliver_from_cache(message, bot, cached)
        finally:
            await bot.delete_message(message.chat.id, status_msg.message_id)
        return

    # ── Step 3: Fetch metadata ─────────────────────────────────────────────
    status_msg = await message.answer("⏳ <i>Please wait, processing your link...</i>", parse_mode=ParseMode.HTML)

    tiktok_data = await api.fetch_tiktok_data(clean_url)
    if not tiktok_data:
        await status_msg.edit_text(
            "❌ <b>Failed to fetch this TikTok.</b>\n"
            "The link may be private, deleted, or the service is temporarily down.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Step 4: Download & upload ──────────────────────────────────────────
    try:
        if tiktok_data["content_type"] == "video":
            await _process_video(message, bot, status_msg, clean_url, tiktok_data)
        else:
            await _process_carousel(message, bot, status_msg, clean_url, tiktok_data)
    except Exception as exc:
        logger.exception("Unhandled error processing %s: %s", clean_url, exc)
        await status_msg.edit_text(
            "❌ An unexpected error occurred. Please try again later.",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------

async def _deliver_from_cache(message: Message, bot: Bot, cached: dict) -> None:
    """
    Send media entirely from cached Telegram file_ids.
    Zero bytes transferred — instant delivery.
    """
    file_ids      = cached["file_ids"]
    audio_file_id = cached.get("audio_file_id")
    content_type  = cached["content_type"]

    if content_type == "video":
        await message.answer_video(
            video   = file_ids[0],
            caption = "🎬 <b>HD Video</b> (cached)",
            parse_mode = ParseMode.HTML,
        )
    else:
        # Carousel: send as album using file_ids
        media_group = [InputMediaPhoto(media=fid) for fid in file_ids]
        await message.answer_media_group(media=media_group)

    # Send audio if available
    if audio_file_id:
        await message.answer_audio(
            audio      = audio_file_id,
            caption    = "🎵 <b>Extracted Audio</b>",
            parse_mode = ParseMode.HTML,
        )


async def _process_video(
    message: Message,
    bot: Bot,
    status_msg: Message,
    url: str,
    data: api.VideoData,
) -> None:
    """
    Download a TikTok video + audio, upload to Telegram, then cache file_ids.
    Cleans up temp files regardless of success or failure.
    """
    video_path = audio_path = None

    try:
        await status_msg.edit_text("⬇️ <i>Downloading video...</i>", parse_mode=ParseMode.HTML)

        # Download video to temp file
        video_path = await api.download_to_tempfile(data["video_url"], suffix=".mp4")
        if not video_path:
            await status_msg.edit_text("❌ Failed to download the video file.")
            return

        # Extract audio from the downloaded video
        await status_msg.edit_text("🎵 <i>Extracting audio...</i>", parse_mode=ParseMode.HTML)
        audio_path = await api.extract_audio_from_video(video_path)

        # If extraction fails but we have a direct audio URL, download that instead
        if not audio_path and data.get("audio_url"):
            audio_path = await api.download_to_tempfile(data["audio_url"], suffix=".mp3")

        # Upload video
        await status_msg.edit_text("⬆️ <i>Uploading to Telegram...</i>", parse_mode=ParseMode.HTML)
        video_msg = await message.answer_video(
            video      = FSInputFile(video_path),
            caption    = f"🎬 <b>HD Video</b>\n<i>{data['title'][:900]}</i>" if data["title"] else "🎬 <b>HD Video</b>",
            parse_mode = ParseMode.HTML,
        )
        video_file_id = video_msg.video.file_id

        # Upload audio
        audio_file_id = None
        if audio_path and audio_path.exists():
            audio_msg = await message.answer_audio(
                audio      = FSInputFile(audio_path),
                caption    = "🎵 <b>Extracted Audio</b>",
                parse_mode = ParseMode.HTML,
            )
            audio_file_id = audio_msg.audio.file_id

        # Cache the file_ids for instant future delivery
        await database.set_cached(
            url           = url,
            content_type  = "video",
            file_ids      = [video_file_id],
            audio_file_id = audio_file_id,
        )

        await bot.delete_message(message.chat.id, status_msg.message_id)

    finally:
        # Always delete temp files, even on error
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
) -> None:
    """
    Download carousel images + audio, send as Telegram album, then cache file_ids.
    Cleans up all temp files after upload.
    """
    image_paths: list[Path] = []
    audio_path  = None

    try:
        await status_msg.edit_text("⬇️ <i>Downloading photos...</i>", parse_mode=ParseMode.HTML)

        # Download each carousel image concurrently for speed
        download_tasks = [
            api.download_to_tempfile(img_url, suffix=".jpg")
            for img_url in data["image_urls"]
        ]
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Path):
                image_paths.append(res)
            else:
                logger.warning("One carousel image failed to download: %s", res)

        if not image_paths:
            await status_msg.edit_text("❌ Could not download any photos from this carousel.")
            return

        # Download audio
        if data.get("audio_url"):
            audio_path = await api.download_to_tempfile(data["audio_url"], suffix=".mp3")

        # Build Telegram MediaGroup (album); first item gets the caption
        media_group = [
            InputMediaPhoto(
                media   = FSInputFile(img_path),
                caption = (f"🖼️ <b>Photo {i+1}/{len(image_paths)}</b>" if i == 0 else None),
                parse_mode = ParseMode.HTML if i == 0 else None,
            )
            for i, img_path in enumerate(image_paths)
        ]

        await status_msg.edit_text("⬆️ <i>Uploading album to Telegram...</i>", parse_mode=ParseMode.HTML)
        album_msgs = await message.answer_media_group(media=media_group)

        # Collect file_ids from the uploaded album
        photo_file_ids = []
        for msg in album_msgs:
            if msg.photo:
                photo_file_ids.append(msg.photo[-1].file_id)  # [-1] = largest size

        # Upload audio track
        audio_file_id = None
        if audio_path and audio_path.exists():
            audio_msg = await message.answer_audio(
                audio      = FSInputFile(audio_path),
                caption    = "🎵 <b>Carousel Audio</b>",
                parse_mode = ParseMode.HTML,
            )
            audio_file_id = audio_msg.audio.file_id

        # Cache everything
        if photo_file_ids:
            await database.set_cached(
                url           = url,
                content_type  = "carousel",
                file_ids      = photo_file_ids,
                audio_file_id = audio_file_id,
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
    Runs via APScheduler every 24 hours.
    """
    logger.info("Running scheduled cache cleanup...")
    deleted = await database.delete_expired(ttl_days=CACHE_TTL_DAYS)
    logger.info("Cleanup complete — %d record(s) removed", deleted)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    """
    Initialize all components and start the bot in long-polling mode.
    """
    # Init DB
    await database.init_db()

    # Set up APScheduler — runs cleanup every 24 hours
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_cleanup,
        trigger  = "interval",
        hours    = 24,
        id       = "cache_cleanup",
        max_instances = 1,
    )
    scheduler.start()
    logger.info("Scheduler started — cleanup runs every 24h")

    # Build bot & dispatcher
    bot = Bot(
        token      = BOT_TOKEN,
        default    = DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Register middleware on the message observer
    dp.message.middleware(CooldownMiddleware(cooldown=COOLDOWN_SEC))

    # Register router
    dp.include_router(router)

    logger.info("Bot is starting...")
    try:
        # Drop any pending updates accumulated while the bot was offline
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        scheduler.shutdown()
        await bot.session.close()
        logger.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
