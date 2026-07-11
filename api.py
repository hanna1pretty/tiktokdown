"""
api.py
------
Handles all media data retrieval and processing.

Platform strategy:
  - TikTok    : tikwm.com API (primary) → yt-dlp (fallback)
  - Instagram : RapidAPI (primary) → tidak ada fallback (login required)
  - Facebook  : RapidAPI (primary) → yt-dlp (fallback untuk public video)

Also handles:
  - Content-type detection (video vs carousel)
  - Audio extraction via ffmpeg (TikTok only)
  - Temporary file management
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
    video_url    : str | None  # Direct MP4 URL
    image_urls   : list[str]   # Direct image URLs (carousel)
    audio_url    : str | None  # Direct audio URL (TikTok only)
    title        : str         # Post caption


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIKWM_API  = "https://www.tikwm.com/api/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT    = aiohttp.ClientTimeout(total=30)

# RapidAPI — set via .env
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

# RapidAPI hosts — ganti jika kamu subscribe API yang berbeda
RAPIDAPI_IG_HOST = "social-media-video-downloader.p.rapidapi.com"
RAPIDAPI_FB_HOST = "facebook-reel-and-video-downloader.p.rapidapi.com"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """
    Strip query params from URL so the same video always maps
    to the same cache key.
    """
    return re.sub(r"\?.*$", "", url.strip())


def detect_platform(url: str) -> str:
    """
    Detect platform from URL.
    Returns: 'tiktok' | 'instagram' | 'facebook'
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
# TikTok — Primary: tikwm.com
# ---------------------------------------------------------------------------

async def _fetch_tikwm(session: aiohttp.ClientSession, url: str) -> VideoData | None:
    """
    Fetch TikTok metadata from tikwm.com public API.
    Supports videos and carousel/slideshow posts.
    """
    try:
        async with session.post(
            TIKWM_API,
            data    = {"url": url, "hd": 1},
            timeout = TIMEOUT,
        ) as resp:
            if resp.status != 200:
                logger.warning("tikwm API HTTP %d", resp.status)
                return None
            payload = await resp.json(content_type=None)

        if payload.get("code") != 0:
            logger.warning("tikwm API error: %s", payload.get("msg"))
            return None

        data = payload["data"]

        # Carousel / slideshow
        if data.get("images"):
            return VideoData(
                content_type = "carousel",
                video_url    = None,
                image_urls   = data["images"],
                audio_url    = data.get("music"),
                title        = data.get("title", ""),
            )

        # Standard video
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
    Fetch TikTok via yt-dlp as fallback when tikwm fails.
    Runs in thread pool to stay non-blocking.
    """
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

        if info.get("_type") == "playlist":
            image_urls = []
            for entry in info.get("entries", []):
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
# Instagram — Primary: RapidAPI
# (yt-dlp tidak bisa tanpa login sejak 2024)
# ---------------------------------------------------------------------------

async def _fetch_instagram(url: str) -> VideoData | None:
    """
    Fetch Instagram Reel/Post via RapidAPI.

    API: social-media-video-downloader.p.rapidapi.com
    Handles: Reels (video) dan Posts (single photo / carousel)

    Requires RAPIDAPI_KEY in .env
    """
    if not RAPIDAPI_KEY:
        logger.error(
            "Instagram: RAPIDAPI_KEY not set. "
            "Daftar di rapidapi.com dan set RAPIDAPI_KEY di .env"
        )
        return None

    # ── Coba endpoint 1: social-media-video-downloader ────────────────────
    try:
        headers = {
            "X-RapidAPI-Key":  RAPIDAPI_KEY,
            "X-RapidAPI-Host": RAPIDAPI_IG_HOST,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://{RAPIDAPI_IG_HOST}/smvd/get/all",
                headers = headers,
                params  = {"url": url},
                timeout = TIMEOUT,
            ) as resp:
                logger.info("Instagram RapidAPI status: %d", resp.status)
                raw = await resp.text()
                logger.debug("Instagram RapidAPI raw response: %s", raw[:500])

                if resp.status == 200:
                    data = await resp.json(content_type=None)

                    # Format response bisa berbeda tergantung API plan
                    # Coba berbagai key yang mungkin
                    links = data.get("links") or data.get("medias") or []

                    if isinstance(links, list) and links:
                        # Filter video links
                        video_links = [
                            l.get("link") or l.get("url") or l.get("src")
                            for l in links
                            if isinstance(l, dict) and (
                                "video" in str(l.get("type", "")).lower()
                                or "mp4" in str(l.get("link", l.get("url", ""))).lower()
                                or l.get("quality") in ["hd", "sd", "auto"]
                            )
                        ]
                        video_links = [v for v in video_links if v]

                        # Image links untuk carousel
                        image_links = [
                            l.get("link") or l.get("url") or l.get("src")
                            for l in links
                            if isinstance(l, dict) and (
                                "image" in str(l.get("type", "")).lower()
                                or "jpg" in str(l.get("link", l.get("url", ""))).lower()
                                or "jpeg" in str(l.get("link", l.get("url", ""))).lower()
                            )
                        ]
                        image_links = [i for i in image_links if i]

                        if video_links:
                            return VideoData(
                                content_type = "video",
                                video_url    = video_links[0],
                                image_urls   = [],
                                audio_url    = None,
                                title        = data.get("title", ""),
                            )
                        if image_links:
                            return VideoData(
                                content_type = "carousel",
                                video_url    = None,
                                image_urls   = image_links,
                                audio_url    = None,
                                title        = data.get("title", ""),
                            )

                    # Fallback: cek key langsung
                    video_url = (
                        data.get("url")
                        or data.get("video")
                        or data.get("download_url")
                        or data.get("hd")
                        or data.get("sd")
                    )
                    if video_url and isinstance(video_url, str):
                        return VideoData(
                            content_type = "video",
                            video_url    = video_url,
                            image_urls   = [],
                            audio_url    = None,
                            title        = data.get("title", ""),
                        )

                    logger.warning(
                        "Instagram RapidAPI: tidak ada media URL. Response: %s",
                        raw[:300]
                    )
                else:
                    logger.warning(
                        "Instagram RapidAPI HTTP %d. Response: %s",
                        resp.status, raw[:300]
                    )

    except Exception as exc:
        logger.error("Instagram RapidAPI error: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Facebook — Primary: RapidAPI, Fallback: yt-dlp
# ---------------------------------------------------------------------------

async def _fetch_facebook(url: str) -> VideoData | None:
    """
    Fetch Facebook video/Reel.

    Primary  : RapidAPI (facebook-reel-and-video-downloader)
    Fallback : yt-dlp (hanya works untuk beberapa public video)
    """
    # ── Primary: RapidAPI ─────────────────────────────────────────────────
    if RAPIDAPI_KEY:
        try:
            headers = {
                "X-RapidAPI-Key":  RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_FB_HOST,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://{RAPIDAPI_FB_HOST}/app/main.php",
                    headers = headers,
                    params  = {"url": url},
                    timeout = TIMEOUT,
                ) as resp:
                    logger.info("Facebook RapidAPI status: %d", resp.status)
                    raw = await resp.text()
                    logger.debug("Facebook RapidAPI raw response: %s", raw[:500])

                    if resp.status == 200:
                        data = await resp.json(content_type=None)

                        # HD dulu, fallback ke SD
                        video_url = (
                            data.get("hd")
                            or data.get("sd")
                            or data.get("url")
                            or data.get("download_url")
                            or data.get("video")
                        )
                        if video_url and isinstance(video_url, str):
                            return VideoData(
                                content_type = "video",
                                video_url    = video_url,
                                image_urls   = [],
                                audio_url    = None,
                                title        = data.get("title", ""),
                            )

                        logger.warning(
                            "Facebook RapidAPI: tidak ada video URL. Response: %s",
                            raw[:300]
                        )
                    else:
                        logger.warning(
                            "Facebook RapidAPI HTTP %d. Response: %s",
                            resp.status, raw[:300]
                        )

        except Exception as exc:
            logger.warning("Facebook RapidAPI error: %s — mencoba yt-dlp", exc)

    # ── Fallback: yt-dlp ──────────────────────────────────────────────────
    logger.info("Facebook: mencoba yt-dlp sebagai fallback")
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

        info      = await asyncio.to_thread(_extract)
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

    except Exception as exc:
        logger.error("Facebook yt-dlp fallback juga gagal: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Universal dispatcher
# ---------------------------------------------------------------------------

async def fetch_media_data(url: str, platform: str = "tiktok") -> VideoData | None:
    """
    Route ke handler yang sesuai berdasarkan platform.

    Args:
        url     : Normalized URL
        platform: 'tiktok' | 'instagram' | 'facebook'
    """
    if platform == "tiktok":
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}
        ) as session:
            data = await _fetch_tikwm(session, url)
        if data is None:
            logger.info("TikTok: fallback ke yt-dlp")
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
    Returns Path to temp file, or None on failure.
    Caller is responsible for deleting the file after use.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout = TIMEOUT,
                headers = {"User-Agent": USER_AGENT},
            ) as resp:
                resp.raise_for_status()
                tmp     = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
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
    Produces an MP3 file. Used for TikTok only.
    Requires ffmpeg installed on the system.
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
        logger.error("ffmpeg tidak ditemukan — install dengan: apt install ffmpeg")
        return None
    except Exception as exc:
        logger.error("Audio extraction failed: %s", exc)
        return None
