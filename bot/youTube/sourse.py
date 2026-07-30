import asyncio
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.downloader.media import MediaResult, VideoTooLargeError
from bot.downloader.video_tools import (
    compress_to_limit,
    extract_dimensions,
    needs_telegram_normalization,
    normalize_for_telegram,
    resolve_output_path,
)
from bot.settings import (
    CACHE_DIR,
    MAX_VIDEO_SIZE_BYTES,
    MAX_VIDEO_SIZE_MB,
    YOUTUBE_CONCURRENT_FRAGMENTS,
    YOUTUBE_COOKIES,
    YOUTUBE_FAST_DOWNLOAD_ENABLED,
    YOUTUBE_MAX_HEIGHT,
    YOUTUBE_MIN_HEIGHT,
    YOUTUBE_TARGET_SIZE_BYTES,
    YOUTUBE_TARGET_SIZE_MB,
)

# Legacy selectors are intentionally kept intact so the fast mode can be
# disabled instantly with YOUTUBE_FAST_DOWNLOAD_ENABLED=false.
PREFERRED_FORMAT_SELECTOR = (
    f"bv*[height>={YOUTUBE_MIN_HEIGHT}]+ba/"
    f"b[height>={YOUTUBE_MIN_HEIGHT}]/"
    "bv*+ba/"
    "b"
)
FALLBACK_FORMAT_SELECTOR = "bv*+ba/b"
LAST_RESORT_FORMAT_SELECTOR = "bestvideo+bestaudio/best"

# ponytail: download budget of 4x the send limit — bigger files are rejected before
# download, smaller ones get the 720p/480p/360p compression fallback after download.
DOWNLOAD_BUDGET_BYTES = MAX_VIDEO_SIZE_BYTES * 4


def _fast_quality_caps(min_height: int, max_height: int) -> tuple[int, ...]:
    """Return descending quality attempts without going below the minimum."""
    caps = [max_height]
    for cap in (720, 480):
        if min_height <= cap < max_height:
            caps.append(cap)
    return tuple(dict.fromkeys(caps))


def _fast_format_selector(min_height: int, max_height: int) -> str:
    bounds = f"[height>={min_height}][height<={max_height}][fps<=?30]"
    return (
        f"bv{bounds}[vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"b{bounds}[vcodec^=avc1][acodec^=mp4a]/"
        f"bv{bounds}+ba/"
        f"b{bounds}"
    )


def _fast_low_quality_fallback_selector(max_height: int) -> str:
    bounds = f"[height<=?{max_height}][fps<=?30]"
    return f"bv{bounds}+ba/b{bounds}"


def _video_download_attempts() -> tuple[tuple[str, int | None], ...]:
    if not YOUTUBE_FAST_DOWNLOAD_ENABLED:
        return (
            (PREFERRED_FORMAT_SELECTOR, None),
            (FALLBACK_FORMAT_SELECTOR, None),
            (LAST_RESORT_FORMAT_SELECTOR, None),
        )

    attempts = [
        (_fast_format_selector(YOUTUBE_MIN_HEIGHT, cap), cap)
        for cap in _fast_quality_caps(YOUTUBE_MIN_HEIGHT, YOUTUBE_MAX_HEIGHT)
    ]
    # Only reached when no format at or above the requested minimum exists.
    attempts.append((_fast_low_quality_fallback_selector(YOUTUBE_MAX_HEIGHT), None))
    return tuple(attempts)


def _fast_compression_steps(min_height: int, max_height: int) -> tuple[tuple[int, int], ...]:
    heights = [max_height]
    if min_height <= 480 < max_height:
        heights.append(480)
    return tuple((height, 23 if index == 0 else 26) for index, height in enumerate(heights))


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


def _try_smaller_quality(
    info: dict[str, Any],
    attempts: tuple[tuple[str, int | None], ...],
    attempt_index: int,
) -> bool:
    if not YOUTUBE_FAST_DOWNLOAD_ENABLED:
        return False
    size_bytes = _extract_selected_size_bytes(info)
    if size_bytes is None or size_bytes <= YOUTUBE_TARGET_SIZE_BYTES:
        return False
    if attempt_index + 1 >= len(attempts):
        return False
    next_cap = attempts[attempt_index + 1][1]
    if next_cap is None:
        return False

    logger.info(
        "YouTube fast mode: estimated size {:.1f} MB exceeds target {} MB; "
        "trying at most {}p",
        size_bytes / (1024 * 1024),
        YOUTUBE_TARGET_SIZE_MB,
        next_cap,
    )
    return True


def _fit_fast_target(filepath: str) -> str:
    if not YOUTUBE_FAST_DOWNLOAD_ENABLED:
        return filepath
    size_bytes = os.path.getsize(filepath)
    if size_bytes <= YOUTUBE_TARGET_SIZE_BYTES:
        return filepath

    logger.info(
        "YouTube fast mode: downloaded file is {:.1f} MB; compressing toward {} MB",
        size_bytes / (1024 * 1024),
        YOUTUBE_TARGET_SIZE_MB,
    )
    fitted = compress_to_limit(
        filepath,
        YOUTUBE_TARGET_SIZE_BYTES,
        steps=_fast_compression_steps(YOUTUBE_MIN_HEIGHT, YOUTUBE_MAX_HEIGHT),
    )
    if fitted:
        return fitted
    logger.warning(
        "YouTube fast mode: could not reach target {} MB without dropping below {}p; "
        "keeping the capped source",
        YOUTUBE_TARGET_SIZE_MB,
        YOUTUBE_MIN_HEIGHT,
    )
    return filepath


def _download_video_sync(url: str) -> MediaResult:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()

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
    if YOUTUBE_FAST_DOWNLOAD_ENABLED:
        base_options["concurrent_fragment_downloads"] = YOUTUBE_CONCURRENT_FRAGMENTS

    if _is_youtube_url(url):
        base_options["remote_components"] = ["ejs:github"]
        if os.path.isfile(YOUTUBE_COOKIES):
            base_options["cookiefile"] = YOUTUBE_COOKIES

    last_exc: DownloadError | None = None
    attempts = _video_download_attempts()
    for index, (format_selector, height_cap) in enumerate(attempts):
        options = {**base_options, "format": format_selector}
        with YoutubeDL(options) as ydl:
            try:
                probe_info = ydl.extract_info(url, download=False)
                _validate_before_download(probe_info)
                if _try_smaller_quality(probe_info, attempts, index):
                    continue
                info = ydl.extract_info(url, download=True)
                filepath = resolve_output_path(info, ydl)
                if needs_telegram_normalization(info):
                    try:
                        if YOUTUBE_FAST_DOWNLOAD_ENABLED:
                            filepath = normalize_for_telegram(
                                filepath,
                                max_height=height_cap or YOUTUBE_MAX_HEIGHT,
                                crf=23,
                            )
                        else:
                            filepath = normalize_for_telegram(filepath)
                    except RuntimeError as exc:
                        # Keep original downloaded file if ffmpeg normalization fails.
                        logger.warning("YouTube normalization failed: {}", exc)
                filepath = _fit_fast_target(filepath)
                width, height = extract_dimensions(info)
                logger.info(
                    "YouTube video ready: fast_mode={} elapsed={:.1f}s size={:.1f} MB "
                    "selected_height={}p cap={}p",
                    YOUTUBE_FAST_DOWNLOAD_ENABLED,
                    time.monotonic() - started_at,
                    os.path.getsize(filepath) / (1024 * 1024),
                    height,
                    height_cap or YOUTUBE_MAX_HEIGHT,
                )
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
                if index < len(attempts) - 1:
                    continue
                raise RuntimeError(f"Download error: {exc}") from exc

    if last_exc:
        raise RuntimeError(f"Download error: {last_exc}") from last_exc
    raise RuntimeError("Download error: unknown yt-dlp failure.")


async def download_youtube(url: str) -> MediaResult:
    return await asyncio.to_thread(_download_video_sync, url.strip())


AUDIO_CODEC = "mp3"
AUDIO_BITRATE_KBPS = "192"


def _find_sibling_file(base_path: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        candidate = f"{base_path}{suffix}"
        if os.path.exists(candidate):
            return candidate
    return None


def _download_audio_sync(url: str) -> MediaResult:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(CACHE_DIR, "%(extractor)s_%(id)s.audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": AUDIO_CODEC,
                "preferredquality": AUDIO_BITRATE_KBPS,
            },
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
        "retries": 3,
        "fragment_retries": 3,
        "skip_unavailable_fragments": True,
    }
    if os.path.isfile(YOUTUBE_COOKIES):
        options["cookiefile"] = YOUTUBE_COOKIES

    with YoutubeDL(options) as ydl:
        try:
            probe_info = ydl.extract_info(url, download=False)
            _validate_before_download(probe_info)
            info = ydl.extract_info(url, download=True)
        except DownloadError as exc:
            raise RuntimeError(f"Download error: {exc}") from exc

        base_path = os.path.splitext(ydl.prepare_filename(info))[0]
        audio_path = _find_sibling_file(base_path, (f".{AUDIO_CODEC}",))
        if not audio_path:
            raise RuntimeError("Downloaded audio file was not found on disk.")

        thumbnail_path = _find_sibling_file(base_path, (".jpg", ".jpeg"))

        return MediaResult(
            platform="youtube",
            source_url=url,
            media_type="audio",
            path=audio_path,
            title=info.get("title"),
            performer=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            thumbnail_path=thumbnail_path,
        )


async def download_youtube_audio(url: str) -> MediaResult:
    return await asyncio.to_thread(_download_audio_sync, url.strip())
