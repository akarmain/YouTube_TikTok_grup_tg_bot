import asyncio
import os
import shutil
import tempfile
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.downloader.media import MediaResult
from bot.downloader.video_tools import (
    SLIDESHOW_HEIGHT,
    SLIDESHOW_WIDTH,
    build_slideshow,
    extract_dimensions,
    ffprobe_duration,
    needs_telegram_normalization,
    normalize_for_telegram,
    resolve_output_path,
)
from bot.settings import CACHE_DIR, INSTAGRAM_COOKIES

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"


class InstagramDownloadError(RuntimeError):
    """Raised when Instagram media cannot be downloaded."""


def _base_opts() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(CACHE_DIR, "instagram_%(id)s.%(ext)s"),
        "retries": 3,
        "fragment_retries": 3,
    }
    if os.path.exists(INSTAGRAM_COOKIES) and os.path.getsize(INSTAGRAM_COOKIES) > 0:
        options["cookiefile"] = INSTAGRAM_COOKIES
    return options


def _has_video_formats(entry: dict[str, Any] | None) -> bool:
    return bool(entry and entry.get("formats"))


def _best_thumbnail_url(entry: dict[str, Any] | None) -> Optional[str]:
    if not entry:
        return None
    thumbs = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if thumbs:
        return max(thumbs, key=lambda t: t.get("width") or 0)["url"]
    return entry.get("thumbnail")


def _finalize_video(info: dict[str, Any], filepath: str, source_url: str) -> MediaResult:
    if needs_telegram_normalization(info):
        try:
            filepath = normalize_for_telegram(filepath)
        except RuntimeError:
            # Keep original downloaded file if ffmpeg normalization fails.
            pass
    width, height = extract_dimensions(info)
    return MediaResult(
        platform="instagram",
        source_url=source_url,
        media_type="video",
        path=filepath,
        width=width,
        height=height,
        title=info.get("title"),
        description=info.get("description"),
        duration=info.get("duration"),
    )


def _download_slideshow_sync(image_urls: list[str], source_url: str, info: dict[str, Any]) -> MediaResult:
    post_id = str(info.get("id") or "post")
    out_path = os.path.join(CACHE_DIR, f"instagram-slideshow-{post_id}.mp4")
    work_dir = tempfile.mkdtemp(prefix="instagram-photos-", dir=CACHE_DIR)
    try:
        image_paths = []
        for index, url in enumerate(image_urls):
            ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
            image_path = os.path.join(work_dir, f"photo-{index:03d}{ext}")
            req = Request(url, headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.instagram.com/"})
            try:
                with urlopen(req, timeout=20) as response, open(image_path, "wb") as handle:
                    shutil.copyfileobj(response, handle)
            except Exception as e:
                raise InstagramDownloadError(f"Не удалось скачать фото из карусели: {e}") from e
            image_paths.append(image_path)

        try:
            build_slideshow(image_paths, None, out_path)
        except RuntimeError as e:
            if os.path.exists(out_path):
                os.remove(out_path)
            raise InstagramDownloadError(f"Не удалось собрать видео из карусели: {e}") from e
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return MediaResult(
        platform="instagram",
        source_url=source_url,
        media_type="slideshow_video",
        path=out_path,
        width=SLIDESHOW_WIDTH,
        height=SLIDESHOW_HEIGHT,
        title=info.get("title"),
        description=info.get("description"),
        duration=ffprobe_duration(out_path),
    )


def _download_sync(url: str) -> MediaResult:
    os.makedirs(CACHE_DIR, exist_ok=True)
    probe_opts = {**_base_opts(), "format": "bv*+ba/b", "ignore_no_formats_error": True}
    try:
        with YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise InstagramDownloadError(f"Не удалось получить данные поста: {e}") from e
    if not info:
        raise InstagramDownloadError("Не удалось получить данные поста Instagram.")
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise InstagramDownloadError("Прямые трансляции не поддерживаются.")

    entries = list(info.get("entries") or []) if info.get("_type") == "playlist" or info.get("entries") else None

    if entries is None:
        if not _has_video_formats(info):
            # Single image post: build a one-photo slideshow instead of sending a photo.
            image_url = _best_thumbnail_url(info)
            if not image_url:
                raise InstagramDownloadError("В посте не найдено видео или изображений.")
            return _download_slideshow_sync([image_url], url, info)
        try:
            with YoutubeDL({**_base_opts(), "format": "bv*+ba/b"}) as ydl:
                downloaded = ydl.extract_info(url, download=True)
                filepath = resolve_output_path(downloaded, ydl)
        except DownloadError as e:
            raise InstagramDownloadError(f"Не удалось скачать видео: {e}") from e
        return _finalize_video(downloaded, filepath, url)

    video_indexes = [i for i, entry in enumerate(entries) if _has_video_formats(entry)]
    if video_indexes:
        # ponytail: a mixed/video carousel sends only its first video; merging all
        # carousel items into one mp4 is the upgrade path if users ask for it.
        item = str(video_indexes[0] + 1)
        try:
            with YoutubeDL({**_base_opts(), "format": "bv*+ba/b", "playlist_items": item}) as ydl:
                downloaded = ydl.extract_info(url, download=True)
                entry = (downloaded.get("entries") or [None])[0] if downloaded else None
                if not entry:
                    raise InstagramDownloadError("Не удалось скачать видео из карусели.")
                filepath = resolve_output_path(entry, ydl)
        except DownloadError as e:
            raise InstagramDownloadError(f"Не удалось скачать видео из карусели: {e}") from e
        return _finalize_video(entry, filepath, url)

    image_urls = [u for u in (_best_thumbnail_url(entry) for entry in entries) if u]
    if not image_urls:
        raise InstagramDownloadError("Не удалось получить изображения карусели (возможно, нужны cookies).")
    return _download_slideshow_sync(image_urls, url, info)


async def download_instagram(url: str) -> MediaResult:
    """Downloads an Instagram reel/video post, or converts an image carousel into one mp4."""
    return await asyncio.to_thread(_download_sync, url.strip())
