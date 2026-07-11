"""
api.py
------
Handles all media data retrieval and processing.

Platforms supported:
  - TikTok   : tikwm.com API (primary) → yt-dlp (fallback)
  - Instagram : yt-dlp (primary) → instaloader (fallback)
  - Facebook  : yt-dlp

Also handles:
  - Content-type detection (video vs carousel)
  - Audio extraction via ffmpeg (TikTok only)
  - Temp file download & management
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
# Type definition
# ---------------------------------------------------------------------------

class VideoData(TypedDict):
    content_type : str         # 'video' or 'carousel'
    video_url    : str | None  # Direct MP4 URL (video only)
    image_urls   : list[str]   # Direct image URLs (carousel only)
    audio_url    : str | None  # Direct audio URL (TikTok only)
    title        : str         # Post caption / description

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
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """
    Strip query params so the same video always maps to the same cache key.

    Example:
      https://www.tiktok.com/@user/video/123?is_from_webapp=1
      → https://www.tiktok.com/@user/video/123
    """
    return re.sub(r"\?.*$", "", url.strip())


def detect_platform(url: str) -> str:
    """
    Detect which platform a URL belongs to.

    Returns:
        'tiktok' | 'instagram' | 'facebook' | 'unknown'
    """
    url_lower = url.lower()
    if "tiktok.com" in url_lower:
        return "tiktok"
    if "instagram.com" in url_lower or "instagr.am" in url_lower:
        return "instagram"
    if "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower:
        return "facebook"
    return "unknown"

# ---------------------------------------------------------------------------
# TikTok — Primary: tikwm.com API
# ---------------------------------------------------------------------------

async def _fetch_tikwm(session: aiohttp.ClientSession, url: str) -> VideoData | None:
    """
    Fetch TikTok metadata from tikwm.com public API.
    Supports regular videos and carousel/slideshow posts.
    Returns None on any failure so caller can fall back to yt-dlp.
    """
    try:
        async with session.post(
            TIKWM_API,
            data    = {"url": url, "hd": 1},
            timeout = TIMEOUT
        ) as resp:
            if resp.status != 200:
                logger.warning("tikwm API HTTP %d", resp.status)
                return None
            payload = await resp.json(content_type=None)

        if payload.get("code") != 0:
            logger.warning("tikwm API error: %s", payload.get("msg"))
            return None

        data = payload["data"]

        # Carousel detection
        if data.get("images"):
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = data["images"],
                audio_url    = data.get("music"),
                title        = data.get("title", ""),
            )

        # Standard video — prefer HD
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
# TikTok — Fallback: yt-dlp
# ---------------------------------------------------------------------------

async def _fetch_ytdlp(url: str) -> VideoData | None:
    """
    Use yt-dlp in a thread pool to scrape TikTok metadata.
    asyncio.to_thread keeps the event loop non-blocking.
    """
    try:
        import yt_dlp

        ydl_opts = {
            "quiet":         True,
            "no_warnings":   True,
            "skip_download": True,
            "http_headers":  {"User-Agent": USER_AGENT},
        }

        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_extract)

        # Playlist = carousel
        if info.get("_type") == "playlist":
            image_urls = [
                entry.get("url", "") for entry in info.get("entries", [])
            ]
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = [u for u in image_urls if u],
                audio_url    = None,
                title        = info.get("title", ""),
            )

        # Standard video
        formats   = info.get("formats", [])
        video_url = None
        for fmt in reversed(formats):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                video_url = fmt.get("url")
                break
        if not video_url:
            video_url = info.get("url")

        return VideoData(
            content_type = "video",
            video_url    = video_url,
            image_urls   = [],
            audio_url    = info.get("url"),
            title        = info.get("title", ""),
        )

    except Exception as exc:
        logger.error("yt-dlp TikTok fallback failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Instagram — Primary: yt-dlp, Fallback: instaloader
# ---------------------------------------------------------------------------

async def _fetch_instagram(url: str) -> VideoData | None:
    """
    Fetch Instagram Reel, Post (single image or carousel).
    yt-dlp handles most cases; instaloader catches the rest.
    """
    # Primary: yt-dlp
    try:
        import yt_dlp

        ydl_opts = {
            "quiet":         True,
            "no_warnings":   True,
            "skip_download": True,
            "http_headers":  {"User-Agent": USER_AGENT},
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_extract)

        # Carousel — yt-dlp returns a playlist
        if info.get("_type") == "playlist":
            entries    = info.get("entries", [])
            image_urls = []
            for entry in entries:
                thumb = entry.get("thumbnail") or entry.get("url", "")
                if thumb:
                    image_urls.append(thumb)
            if image_urls:
                return VideoData(
                    content_type = "carousel",
                    video_url    = None,
                    image_urls   = image_urls,
                    audio_url    = None,
                    title        = info.get("title", ""),
                )

        # Single Reel / video
        formats   = info.get("formats", [])
        video_url = None
        for fmt in reversed(formats):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                video_url = fmt.get("url")
                break
        if not video_url:
            video_url = info.get("url")

        if video_url:
            return VideoData(
                content_type = "video",
                video_url    = video_url,
                image_urls   = [],
                audio_url    = None,
                title        = info.get("title", ""),
            )

        # Single photo fallback
        thumbnail = info.get("thumbnail")
        if thumbnail:
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = [thumbnail],
                audio_url    = None,
                title        = info.get("title", ""),
            )

    except Exception as exc:
        logger.warning("yt-dlp Instagram failed: %s — trying instaloader", exc)

    # Fallback: instaloader
    try:
        import instaloader

        def _insta_extract():
            L         = instaloader.Instaloader()
            shortcode = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
            if not shortcode:
                return None
            post = instaloader.Post.from_shortcode(L.context, shortcode.group(1))

            if post.typename == "GraphSidecar":
                images = [node.display_url for node in post.get_sidecar_nodes()]
                return VideoData(
                    content_type = "carousel",
                    video_url    = None,
                    image_urls   = images,
                    audio_url    = None,
                    title        = post.caption or "",
                )
            elif post.is_video:
                return VideoData(
                    content_type = "video",
                    video_url    = post.video_url,
                    image_urls   = [],
                    audio_url    = None,
                    title        = post.caption or "",
                )
            else:
                return VideoData(
                    content_type = "carousel",
                    video_url    = None,
                    image_urls   = [post.url],
                    audio_url    = None,
                    title        = post.caption or "",
                )

        return await asyncio.to_thread(_insta_extract)

    except Exception as exc:
        logger.error("instaloader fallback also failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Facebook — yt-dlp only
# ---------------------------------------------------------------------------

async def _fetch_facebook(url: str) -> VideoData | None:
    """
    Fetch Facebook video / Reel using yt-dlp.
    Only public content is supported.
    """
    try:
        import yt_dlp

        ydl_opts = {
            "quiet":         True,
            "no_warnings":   True,
            "skip_download": True,
            "http_headers":  {"User-Agent": USER_AGENT},
            "format":        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_extract)

        formats   = info.get("formats", [])
        video_url = None
        for fmt in reversed(formats):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                video_url = fmt.get("url")
                break
        if not video_url:
            video_url = info.get("url")

        if not video_url:
            logger.warning("Facebook: no video URL extracted")
            return None

        return VideoData(
            content_type = "video",
            video_url    = video_url,
            image_urls   = [],
            audio_url    = None,
            title        = info.get("title", ""),
        )

    except Exception as exc:
        logger.error("Facebook fetch failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Universal dispatcher
# ---------------------------------------------------------------------------

async def fetch_media_data(url: str, platform: str = "tiktok") -> VideoData | None:
    """
    Route to the correct platform fetcher.

    Args:
        url     : Normalized URL.
        platform: 'tiktok' | 'instagram' | 'facebook'

    Returns:
        VideoData on success, None on failure.
    """
    if platform == "tiktok":
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}
        ) as session:
            data = await _fetch_tikwm(session, url)
        if data is None:
            logger.info("TikTok: falling back to yt-dlp for %s", url)
            data = await _fetch_ytdlp(url)
        return data

    elif platform == "instagram":
        return await _fetch_instagram(url)

    elif platform == "facebook":
        return await _fetch_facebook(url)

    else:
        logger.error("Unknown platform: %s", platform)
        return None

# ---------------------------------------------------------------------------
# Media file helpers
# ---------------------------------------------------------------------------

async def download_to_tempfile(url: str, suffix: str = ".mp4") -> Path | None:
    """
    Stream a remote media URL into a temporary file.
    Returns the Path to the temp file, or None on failure.
    Caller is responsible for deleting the file after use.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                tmp      = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp_path = Path(tmp.name)
                async for chunk in resp.content.iter_chunked(65536):
                    tmp.write(chunk)
                tmp.close()
        return tmp_path

    except Exception as exc:
        logger.error("Failed to download %s: %s", url, exc)
        return None


async def extract_audio_from_video(video_path: Path) -> Path | None:
    """
    Extract audio from a local video file using ffmpeg.
    Produces an MP3 file. Non-blocking via asyncio subprocess.
    Requires ffmpeg installed on the system (apt install ffmpeg).
    """
    audio_path = video_path.with_suffix(".mp3")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "192k",
        "-ar", "44100",
        str(audio_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout = asyncio.subprocess.DEVNULL,
            stderr = asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        if proc.returncode == 0 and audio_path.exists():
            return audio_path
        else:
            logger.error("ffmpeg exited with code %d", proc.returncode)
            return None

    except FileNotFoundError:
        logger.error("ffmpeg not found — install with: apt install ffmpeg")
        return None
    except Exception as exc:
        logger.error("Audio extraction failed: %s", exc)
        return None
