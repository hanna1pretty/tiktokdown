"""
api.py
------
Handles all TikTok data retrieval and media processing.

Strategy (with automatic fallback):
  1. Primary  → tikwm.com public API (no watermark, reliable)
  2. Fallback → yt-dlp scraper (if API quota exceeded or fails)

Also handles:
  - Content-type detection (video vs. carousel/slideshow)
  - Audio extraction: downloads audio track as MP3
  - Temporary file management (auto-cleanup after upload)
"""

import asyncio
import aiohttp
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type definitions for clean return contracts
# ---------------------------------------------------------------------------

class VideoData(TypedDict):
    content_type: str        # 'video' or 'carousel'
    video_url:    str | None # Direct MP4 URL (video only)
    image_urls:   list[str]  # Direct image URLs (carousel only)
    audio_url:    str | None # Direct audio/MP3 URL
    title:        str        # Post caption / description


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIKWM_API    = "https://www.tikwm.com/api/"
USER_AGENT   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT      = aiohttp.ClientTimeout(total=30)


# ---------------------------------------------------------------------------
# URL Normalizer
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """
    Strip query params and tracking suffixes from a TikTok URL so that
    the same video always maps to the same cache key.

    Example:
      https://www.tiktok.com/@user/video/123?is_from_webapp=1  →
      https://www.tiktok.com/@user/video/123
    """
    return re.sub(r"\?.*$", "", url.strip())


# ---------------------------------------------------------------------------
# Primary API: tikwm.com
# ---------------------------------------------------------------------------

async def _fetch_tikwm(session: aiohttp.ClientSession, url: str) -> VideoData | None:
    """
    Fetch TikTok metadata from the tikwm.com public API.
    Supports both regular videos and carousel/slideshow posts.

    Returns None on any failure so the caller can fall back to yt-dlp.
    """
    try:
        async with session.post(
            TIKWM_API,
            data={"url": url, "hd": 1},
            timeout=TIMEOUT
        ) as resp:
            if resp.status != 200:
                logger.warning("tikwm API HTTP %d", resp.status)
                return None

            payload = await resp.json(content_type=None)

        # tikwm returns code=0 on success
        if payload.get("code") != 0:
            logger.warning("tikwm API error: %s", payload.get("msg"))
            return None

        data = payload["data"]

        # ── Carousel / Slideshow detection ─────────────────────────────────
        # tikwm exposes an `images` list for carousel posts
        if data.get("images"):
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = data["images"],          # list of direct image URLs
                audio_url    = data.get("music"),       # background music track
                title        = data.get("title", ""),
            )

        # ── Standard Video ─────────────────────────────────────────────────
        # Prefer HD play URL; fall back to standard play URL
        video_url = data.get("hdplay") or data.get("play")
        return VideoData(
            content_type = "video",
            video_url    = video_url,
            image_urls   = [],
            audio_url    = data.get("music"),
            title        = data.get("title", ""),
        )

    except Exception as exc:
        logger.error("tikwm fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Fallback: yt-dlp scraper
# ---------------------------------------------------------------------------

async def _fetch_ytdlp(url: str) -> VideoData | None:
    """
    Use yt-dlp in a thread pool to scrape TikTok metadata.
    Runs synchronous yt-dlp calls via asyncio.to_thread to stay non-blocking.

    Returns None if yt-dlp also fails.
    """
    try:
        import yt_dlp  # lazy import — only needed for fallback

        ydl_opts = {
            "quiet":          True,
            "no_warnings":    True,
            "skip_download":  True,   # metadata only; we download separately
            "http_headers":   {"User-Agent": USER_AGENT},
        }

        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        # Run the blocking yt-dlp call in a thread pool
        info = await asyncio.to_thread(_extract)

        # yt-dlp may return a playlist for carousels
        if info.get("_type") == "playlist":
            image_urls = [
                entry.get("url", "") for entry in info.get("entries", [])
            ]
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = [u for u in image_urls if u],
                audio_url    = None,  # yt-dlp doesn't always expose audio separately
                title        = info.get("title", ""),
            )

        # Standard video — pick best non-DASH format that has video+audio
        formats = info.get("formats", [])
        video_url = None
        for fmt in reversed(formats):  # reversed = best quality last
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                video_url = fmt.get("url")
                break
        # Last resort: use the generic url field
        if not video_url:
            video_url = info.get("url")

        audio_url = info.get("url")  # same stream; we'll extract audio separately

        return VideoData(
            content_type = "video",
            video_url    = video_url,
            image_urls   = [],
            audio_url    = audio_url,
            title        = info.get("title", ""),
        )

    except Exception as exc:
        logger.error("yt-dlp fallback failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_tiktok_data(url: str) -> VideoData | None:
    """
    Master fetch function with automatic fallback.

    1. Try tikwm.com API first (fast, no-watermark).
    2. Fall back to yt-dlp if tikwm fails.

    Args:
        url: A normalized TikTok URL.

    Returns:
        VideoData dict on success, or None if both sources fail.
    """
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}
    ) as session:
        data = await _fetch_tikwm(session, url)

    if data is None:
        logger.info("Falling back to yt-dlp for: %s", url)
        data = await _fetch_ytdlp(url)

    return data


# ---------------------------------------------------------------------------
# Media file helpers
# ---------------------------------------------------------------------------

async def download_to_tempfile(url: str, suffix: str = ".mp4") -> Path | None:
    """
    Stream a remote media URL into a temporary file.
    Returns the Path to the temp file, or None on failure.
    The caller is responsible for deleting the file after use.

    Args:
        url   : Direct media URL to download.
        suffix: File extension for the temp file (e.g. '.mp4', '.mp3').
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=TIMEOUT) as resp:
                resp.raise_for_status()

                # Write to a named temp file so aiogram can send it
                tmp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=suffix
                )
                tmp_path = Path(tmp.name)

                # Stream in 64 KB chunks to keep memory usage low
                async for chunk in resp.content.iter_chunked(65536):
                    tmp.write(chunk)
                tmp.close()

        return tmp_path

    except Exception as exc:
        logger.error("Failed to download %s: %s", url, exc)
        return None


async def extract_audio_from_video(video_path: Path) -> Path | None:
    """
    Extract audio from a local video file using ffmpeg (via subprocess).
    Produces an MP3 file alongside the video.
    Runs ffmpeg in a thread pool to stay non-blocking.

    Requires ffmpeg to be installed on the system.

    Args:
        video_path: Path to the local MP4 file.

    Returns:
        Path to the extracted .mp3 file, or None on failure.
    """
    audio_path = video_path.with_suffix(".mp3")

    cmd = [
        "ffmpeg", "-y",             # overwrite output
        "-i", str(video_path),      # input video
        "-vn",                      # drop video stream
        "-acodec", "libmp3lame",    # encode as MP3
        "-ab", "192k",              # 192 kbps quality
        "-ar", "44100",             # standard sample rate
        str(audio_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        if proc.returncode == 0 and audio_path.exists():
            return audio_path
        else:
            logger.error("ffmpeg exited with code %d", proc.returncode)
            return None

    except FileNotFoundError:
        logger.error("ffmpeg not found — please install it (apt install ffmpeg)")
        return None
    except Exception as exc:
        logger.error("Audio extraction failed: %s", exc)
        return None
