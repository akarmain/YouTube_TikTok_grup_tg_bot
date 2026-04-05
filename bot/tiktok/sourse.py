import asyncio
import os
import random
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.ffmpeg import ffmpeg_command, run_ffmpeg
from bot.settings import CACHE_DIR, TIKTOK_COOKIES

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
MOBILE_APP_UA = "com.zhiliaoapp.musically/2022100901 (Linux; U; Android 10; en_US) okhttp/3.14.9.4"
MOBILE_WEB_UA = "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
MAX_TIKTOK_HEIGHT = 720
TIKTOK_DEVICE_ID_FILE = os.path.join(CACHE_DIR, "tiktok_device_id.txt")
TIKTOK_IID_FILE = os.path.join(CACHE_DIR, "tiktok_iid.txt")


@dataclass
class TikTokMedia:
    video_path: Optional[str] = None
    audio_path: Optional[str] = None
    cover_path: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None


class TikTokDownloadError(RuntimeError):
    """Raised when TikTok media cannot be downloaded."""


def _has_cookies() -> bool:
    return os.path.exists(TIKTOK_COOKIES) and os.path.getsize(TIKTOK_COOKIES) > 0


def _with_cookies(options: dict) -> dict:
    if _has_cookies():
        options["cookiefile"] = TIKTOK_COOKIES
    return options


def _resolve_tiktok_url(tt_url: str) -> str:
    if "tiktok.com" not in tt_url:
        return tt_url
    parsed = urlparse(tt_url)
    host = parsed.netloc.lower()
    if host not in {"vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com"} and not parsed.path.startswith("/t/"):
        return tt_url
    try:
        req = Request(tt_url, headers={"User-Agent": DESKTOP_UA})
        with urlopen(req, timeout=10) as response:
            return response.geturl()
    except Exception:
        return tt_url


def _load_or_create_numeric_id(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
            if value.isdigit():
                return value
    value = str(random.randint(7_250_000_000_000_000_000, 7_325_099_899_999_994_577))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


@lru_cache(maxsize=1)
def _tiktok_extractor_args() -> dict:
    device_id = _load_or_create_numeric_id(TIKTOK_DEVICE_ID_FILE)
    iid = _load_or_create_numeric_id(TIKTOK_IID_FILE)
    app_info = f"{iid}/musical_ly/35.1.3/2023501030/0"
    return {"tiktok": {"device_id": [device_id], "app_info": [app_info]}}


def _extract_dimensions(info: dict) -> tuple[Optional[int], Optional[int]]:
    width = info.get("width")
    height = info.get("height")
    if width and height:
        return width, height

    for fmt in info.get("formats", []):
        if fmt.get("width") and fmt.get("height"):
            return fmt["width"], fmt["height"]

    return None, None


def _pick_cover_url(info: dict) -> Optional[str]:
    if info.get("thumbnail"):
        return info["thumbnail"]

    thumbs = info.get("thumbnails") or []
    for thumb in reversed(thumbs):
        if thumb.get("url"):
            return thumb["url"]

    return None


def _build_format_candidates() -> list[tuple[str, bool]]:
    return [
        ("bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]", False),
        ("bv*[height<=720]+ba/b[height<=720]", False),
        ("b[height<=720]/best[height<=720]", False),
        ("bv*+ba/b", True),
    ]


def _downscale_to_720p(src_path: str, dest_path: str) -> None:
    cmd = ffmpeg_command(
        "-y",
        "-i",
        src_path,
        "-vf",
        "scale=-2:min(720\\,ih)",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        dest_path,
    )
    run_ffmpeg(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def download_tiktok_media(tt_url: str) -> TikTokMedia:
    """
    Downloads a TikTok video and related assets (audio track and cover).

    :param tt_url: TikTok URL (short links are supported).
    :return: TikTokMedia with paths to downloaded assets.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    loop = asyncio.get_event_loop()
    media = TikTokMedia()
    resolved_url = _resolve_tiktok_url(tt_url)

    def download_video_and_info():
        base_opts = {
            "quiet": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(CACHE_DIR, "tiktok-%(id)s.%(ext)s"),
            "geo_bypass_country": "US",
            "geo_bypass": True,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "extractor_args": _tiktok_extractor_args(),
        }

        format_candidates = _build_format_candidates()
        extractor_variants = [
            {
                "http_headers": {
                    "User-Agent": MOBILE_APP_UA,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            },
            {
                "http_headers": {
                    "User-Agent": MOBILE_WEB_UA,
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            },
            {
                "http_headers": {
                    "User-Agent": DESKTOP_UA,
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            },
        ]

        last_error = None
        for extractor_opts in extractor_variants:
            for fmt, needs_scale in format_candidates:
                options = _with_cookies({**base_opts, **extractor_opts, "format": fmt})
                with YoutubeDL(options) as ydl:
                    try:
                        info = ydl.extract_info(resolved_url, download=True)
                        path = ydl.prepare_filename(info)
                        # Отсекаем аудио-форматы, если вдруг yt_dlp подобрал только звук.
                        if info.get("vcodec") == "none":
                            last_error = TikTokDownloadError("yt_dlp вернул только аудио, пробуем другой формат.")
                            continue
                        return path, info, needs_scale
                    except DownloadError as e:
                        last_error = e
                        continue

        raise TikTokDownloadError(f"Не удалось скачать видео: {last_error}")

    try:
        media.video_path, info, needs_scale = await loop.run_in_executor(None, download_video_and_info)
    except TikTokDownloadError:
        raise
    except DownloadError as e:
        raise TikTokDownloadError(f"Не удалось скачать видео: {e}") from e
    except Exception as e:
        raise TikTokDownloadError(f"Не удалось скачать видео: {e}") from e

    if not media.video_path or not os.path.exists(media.video_path) or media.video_path.endswith(".mp3"):
        raise TikTokDownloadError("Видео не было загружено (получен только аудио-файл).")

    media.width, media.height = _extract_dimensions(info)
    media.title = info.get("title")
    media.description = info.get("description")

    if needs_scale or (media.height and media.height > MAX_TIKTOK_HEIGHT):
        if not shutil.which("ffmpeg"):
            raise TikTokDownloadError("Требуется ffmpeg для приведения видео к 720p или ниже.")
        base_name, _ = os.path.splitext(media.video_path)
        scaled_path = f"{base_name}-720p.mp4"

        def _scale():
            _downscale_to_720p(media.video_path, scaled_path)

        try:
            await loop.run_in_executor(None, _scale)
            if os.path.exists(media.video_path):
                os.remove(media.video_path)
            media.video_path = scaled_path
            if media.height:
                media.width = int(media.width * MAX_TIKTOK_HEIGHT / media.height) if media.width else None
                if media.width:
                    media.width -= media.width % 2
                media.height = MAX_TIKTOK_HEIGHT
        except subprocess.CalledProcessError as e:
            if os.path.exists(scaled_path):
                os.remove(scaled_path)
            raise TikTokDownloadError(f"Не удалось привести видео к 720p: {e}") from e

    async def download_audio_track() -> Optional[str]:
        def _download():
            options = _with_cookies({
                "format": "bestaudio[ext=m4a]/bestaudio",
                "quiet": True,
                "outtmpl": os.path.join(CACHE_DIR, "tiktok-%(id)s.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "http_headers": {
                    "User-Agent": MOBILE_WEB_UA,
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "geo_bypass_country": "US",
                "geo_bypass": True,
                "extractor_args": _tiktok_extractor_args(),
            })
            with YoutubeDL(options) as ydl:
                audio_info = ydl.extract_info(resolved_url, download=True)
                return ydl.prepare_filename(audio_info)

        try:
            return await loop.run_in_executor(None, _download)
        except DownloadError:
            return None

    media.audio_path = await download_audio_track()

    cover_url = _pick_cover_url(info)

    async def download_cover() -> Optional[str]:
        if not cover_url:
            return None

        def _download():
            parsed = urlparse(cover_url)
            ext = os.path.splitext(parsed.path)[1] or ".jpg"
            filename = f"tiktok-{info.get('id', 'cover')}-cover{ext}"
            dest = os.path.join(CACHE_DIR, filename)
            urllib.request.urlretrieve(cover_url, dest)
            return dest

        try:
            return await loop.run_in_executor(None, _download)
        except Exception:
            return None

    media.cover_path = await download_cover()

    return media
