# ─────────────────────────────────────────────────────────────────────────────
# ADD at the top of api.py — update the imports section
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import aiohttp
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# ── Constants (keep your existing ones, add these) ───────────────────────────

INSTAGRAM_API  = "https://instagram-downloader-download-instagram-videos-stories.p.rapidapi.com/index"
FACEBOOK_API   = "https://facebook-reel-and-video-downloader.p.rapidapi.com/app/main.php"

# RapidAPI key — add to your .env file as RAPIDAPI_KEY=your_key_here
# Free tier: 100–500 req/month depending on the API plan
RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")


# ─────────────────────────────────────────────────────────────────────────────
# ADD: Detect platform from URL
# ─────────────────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    """
    Detect which platform a URL belongs to.

    Returns:
        'tiktok' | 'instagram' | 'facebook'
    """
    url_lower = url.lower()
    if "tiktok.com" in url_lower:
        return "tiktok"
    if "instagram.com" in url_lower or "instagr.am" in url_lower:
        return "instagram"
    if "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower:
        return "facebook"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# ADD: Instagram fetcher
# Strategy: yt-dlp primary (free, no API key needed), instaloader fallback
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_instagram(url: str) -> "VideoData | None":
    """
    Fetch Instagram Reel, Post (single image or carousel) using yt-dlp.
    Falls back to instaloader for carousel/album posts that yt-dlp misses.

    Handles:
        - Reels      → VideoData(content_type='video')
        - Single photo → VideoData(content_type='carousel', image_urls=[1 item])
        - Carousel   → VideoData(content_type='carousel', image_urls=[N items])
    """
    # ── Primary: yt-dlp ───────────────────────────────────────────────────
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

        # Carousel / sidecar post → yt-dlp returns a playlist
        if info.get("_type") == "playlist":
            entries    = info.get("entries", [])
            image_urls = []
            video_urls = []

            for entry in entries:
                # Each entry can be a video or a photo
                if entry.get("vcodec") and entry.get("vcodec") != "none":
                    video_urls.append(entry.get("url", ""))
                else:
                    # Photo entry — get the best thumbnail as the image
                    thumb = entry.get("thumbnail") or entry.get("url", "")
                    image_urls.append(thumb)

            # If all entries are videos (rare multi-video carousel), treat as carousel of videos
            # Otherwise treat as photo carousel
            if image_urls:
                return VideoData(
                    content_type = "carousel",
                    video_url    = None,
                    image_urls   = [u for u in image_urls if u],
                    audio_url    = None,
                    title        = info.get("title", ""),
                )

        # Single Reel / video post
        formats   = info.get("formats", [])
        video_url = None
        for fmt in reversed(formats):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                video_url = fmt.get("url")
                break
        if not video_url:
            video_url = info.get("url")

        # Thumbnail as fallback image
        thumbnail = info.get("thumbnail")

        if video_url:
            return VideoData(
                content_type = "video",
                video_url    = video_url,
                image_urls   = [],
                audio_url    = None,   # extracted from video via ffmpeg
                title        = info.get("title", ""),
            )

        # If no video found but we have a thumbnail, treat as single photo
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

    # ── Fallback: instaloader ─────────────────────────────────────────────
    try:
        import instaloader

        def _insta_extract():
            L        = instaloader.Instaloader()
            shortcode = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
            if not shortcode:
                return None
            post = instaloader.Post.from_shortcode(L.context, shortcode.group(1))

            if post.typename == "GraphSidecar":
                # Carousel post
                images = [node.display_url for node in post.get_sidecar_nodes()]
                return {
                    "content_type": "carousel",
                    "image_urls":   images,
                    "video_url":    None,
                    "audio_url":    None,
                    "title":        post.caption or "",
                }
            elif post.is_video:
                return {
                    "content_type": "video",
                    "video_url":    post.video_url,
                    "image_urls":   [],
                    "audio_url":    None,
                    "title":        post.caption or "",
                }
            else:
                return {
                    "content_type": "carousel",
                    "image_urls":   [post.url],
                    "video_url":    None,
                    "audio_url":    None,
                    "title":        post.caption or "",
                }

        result = await asyncio.to_thread(_insta_extract)
        if result:
            return VideoData(**result)

    except Exception as exc:
        logger.error("instaloader fallback also failed: %s", exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# ADD: Facebook fetcher
# Strategy: yt-dlp only (handles FB Reels, Watch, and public video pages)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_facebook(url: str) -> "VideoData | None":
    """
    Fetch Facebook video / Reel using yt-dlp.
    Works for public FB Watch videos, Reels, and page video posts.

    Note: Private or login-required FB content cannot be fetched
    without cookies. Only public content is supported.
    """
    try:
        import yt_dlp

        ydl_opts = {
            "quiet":         True,
            "no_warnings":   True,
            "skip_download": True,
            "http_headers":  {"User-Agent": USER_AGENT},
            # Use HD format where available
            "format":        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(_extract)

        formats   = info.get("formats", [])
        video_url = None

        # Pick best combined video+audio format
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
            audio_url    = None,   # extracted via ffmpeg
            title        = info.get("title", ""),
        )

    except Exception as exc:
        logger.error("Facebook fetch failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MODIFY: Rename fetch_tiktok_data → fetch_media_data (universal dispatcher)
# Replace your existing fetch_tiktok_data() with this function
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_media_data(url: str, platform: str = "tiktok") -> "VideoData | None":
    """
    Universal fetch dispatcher. Routes to the correct platform handler.

    Args:
        url     : Normalized URL.
        platform: 'tiktok' | 'instagram' | 'facebook'

    Returns:
        VideoData on success, None on failure.
    """
    if platform == "tiktok":
        # Your existing two-tier logic (tikwm → yt-dlp)
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
