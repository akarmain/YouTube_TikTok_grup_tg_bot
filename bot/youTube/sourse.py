import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.downloader.media import MediaResult, VideoTooLargeError
from bot.downloader.video_tools import (
    extract_dimensions,
    needs_telegram_normalization,
    normalize_for_telegram,
    resolve_output_path,
)
from bot.settings import CACHE_DIR, MAX_VIDEO_SIZE_BYTES, MAX_VIDEO_SIZE_MB, YOUTUBE_COOKIES

MIN_HEIGHT = 420

# Prefer >=420p when available, then fallback to best available stream.
PREFERRED_FORMAT_SELECTOR = (
    f"bv*[height>={MIN_HEIGHT}]+ba/"
    f"b[height>={MIN_HEIGHT}]/"
    "bv*+ba/"
    "b"
)
FALLBACK_FORMAT_SELECTOR = "bv*+ba/b"
LAST_RESORT_FORMAT_SELECTOR = "bestvideo+bestaudio/best"

# ponytail: download budget of 4x the send limit — bigger files are rejected before
# download, smaller ones get the 720p/480p/360p compression fallback after download.
DOWNLOAD_BUDGET_BYTES = MAX_VIDEO_SIZE_BYTES * 4


def _is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "youtube.com" in host or "youtu.be" in host


def _extract_selected_size_bytes(info: dict[str, Any]) -> int | None:
    total_size = 0
    found_any = False

    for fmt in info.get("requested_formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            total_size += int(size)
            found_any = True

    if found_any:
        return total_size

    for item in info.get("requested_downloads") or []:
        size = item.get("filesize") or item.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            total_size += int(size)
            found_any = True

    if found_any:
        return total_size

    size = info.get("filesize") or info.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    return None


def _validate_before_download(info: dict[str, Any]) -> None:
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise RuntimeError("Live streams are not supported.")

    size_bytes = _extract_selected_size_bytes(info)
    if size_bytes is None:
        return
    if size_bytes > DOWNLOAD_BUDGET_BYTES:
        size_mb = size_bytes / (1024 * 1024)
        raise VideoTooLargeError(
            f"Video is too large: {size_mb:.1f} MB. Limit is {MAX_VIDEO_SIZE_MB} MB."
        )


def _download_video_sync(url: str) -> MediaResult:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

    base_options: dict[str, Any] = {
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(CACHE_DIR, "%(extractor)s_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
        "writethumbnail": False,
        "retries": 3,
        "fragment_retries": 3,
        "skip_unavailable_fragments": True,
    }

    if _is_youtube_url(url):
        base_options["remote_components"] = ["ejs:github"]
        if os.path.isfile(YOUTUBE_COOKIES):
            base_options["cookiefile"] = YOUTUBE_COOKIES

    last_exc: DownloadError | None = None
    selectors = (
        PREFERRED_FORMAT_SELECTOR,
        FALLBACK_FORMAT_SELECTOR,
        LAST_RESORT_FORMAT_SELECTOR,
    )
    for index, format_selector in enumerate(selectors):
        options = {**base_options, "format": format_selector}
        with YoutubeDL(options) as ydl:
            try:
                probe_info = ydl.extract_info(url, download=False)
                _validate_before_download(probe_info)
                info = ydl.extract_info(url, download=True)
                filepath = resolve_output_path(info, ydl)
                if needs_telegram_normalization(info):
                    try:
                        filepath = normalize_for_telegram(filepath)
                    except RuntimeError:
                        # Keep original downloaded file if ffmpeg normalization fails.
                        pass
                width, height = extract_dimensions(info)
                return MediaResult(
                    platform="youtube",
                    source_url=url,
                    media_type="video",
                    path=filepath,
                    width=width,
                    height=height,
                    title=info.get("title"),
                    description=info.get("description"),
                    duration=info.get("duration"),
                )
            except VideoTooLargeError:
                raise
            except DownloadError as exc:
                last_exc = exc
                if index < len(selectors) - 1:
                    continue
                raise RuntimeError(f"Download error: {exc}") from exc

    if last_exc:
        raise RuntimeError(f"Download error: {last_exc}") from last_exc
    raise RuntimeError("Download error: unknown yt-dlp failure.")


async def download_youtube(url: str) -> MediaResult:
    return await asyncio.to_thread(_download_video_sync, url.strip())
