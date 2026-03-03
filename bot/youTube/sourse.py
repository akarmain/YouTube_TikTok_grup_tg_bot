import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.settings import CACHE_DIR, MAX_VIDEO_SIZE_BYTES, MAX_VIDEO_SIZE_MB, YOUTUBE_COOKIES

MIN_HEIGHT = 420
SUPPORTED_URL_PATTERN = (
    r"(?i)^https?://(?:[\w-]+\.)?"
    r"(?:youtube\.com|youtu\.be|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/\S+$"
)
SUPPORTED_URL_RE = re.compile(SUPPORTED_URL_PATTERN)

# Prefer >=420p when available, then fallback to best available stream.
PREFERRED_FORMAT_SELECTOR = (
    f"bv*[height>={MIN_HEIGHT}]+ba/"
    f"b[height>={MIN_HEIGHT}]/"
    "bv*+ba/"
    "b"
)
FALLBACK_FORMAT_SELECTOR = "bv*+ba/b"
LAST_RESORT_FORMAT_SELECTOR = "bestvideo+bestaudio/best"


@dataclass(slots=True)
class DownloadedVideo:
    path: str
    width: int | None
    height: int | None


class VideoTooLargeError(RuntimeError):
    pass


def is_supported_video_url(url: str) -> bool:
    return bool(SUPPORTED_URL_RE.match((url or "").strip()))


def _is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "youtube.com" in host or "youtu.be" in host


def _extract_dimensions(info: dict[str, Any]) -> tuple[int | None, int | None]:
    width = info.get("width")
    height = info.get("height")
    if width and height:
        return width, height

    for fmt in info.get("requested_formats") or []:
        fmt_width = fmt.get("width")
        fmt_height = fmt.get("height")
        if fmt_width and fmt_height:
            return fmt_width, fmt_height

    for fmt in info.get("formats") or []:
        fmt_width = fmt.get("width")
        fmt_height = fmt.get("height")
        if fmt_width and fmt_height:
            return fmt_width, fmt_height

    return None, None


def _resolve_output_path(info: dict[str, Any], ydl: YoutubeDL) -> str:
    candidates: list[Path] = []
    requested_downloads = info.get("requested_downloads") or []
    for item in requested_downloads:
        filepath = item.get("filepath")
        if filepath:
            candidates.append(Path(filepath))

    filename = info.get("_filename")
    if filename:
        candidates.append(Path(filename))

    prepared_filename = ydl.prepare_filename(info)
    if prepared_filename:
        prepared_path = Path(prepared_filename)
        candidates.append(prepared_path)
        candidates.append(prepared_path.with_suffix(".mp4"))

    for path in candidates:
        if path.exists():
            return str(path)

    raise RuntimeError("Downloaded file was not found on disk.")


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


def _validate_size_before_download(info: dict[str, Any]) -> None:
    size_bytes = _extract_selected_size_bytes(info)
    if size_bytes is None:
        return
    if size_bytes > MAX_VIDEO_SIZE_BYTES:
        size_mb = size_bytes / (1024 * 1024)
        raise VideoTooLargeError(
            f"Video is too large: {size_mb:.1f} MB. Limit is {MAX_VIDEO_SIZE_MB} MB."
        )


def _extract_selected_codecs(info: dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    requested_formats = info.get("requested_formats") or []
    video_codec: str | None = None
    audio_codec: str | None = None
    fps: float | None = None

    for fmt in requested_formats:
        if not video_codec and fmt.get("vcodec") and fmt.get("vcodec") != "none":
            video_codec = str(fmt.get("vcodec"))
            fps = fmt.get("fps")
        if not audio_codec and fmt.get("acodec") and fmt.get("acodec") != "none":
            audio_codec = str(fmt.get("acodec"))

    if not video_codec and info.get("vcodec") and info.get("vcodec") != "none":
        video_codec = str(info.get("vcodec"))
        fps = info.get("fps")
    if not audio_codec and info.get("acodec") and info.get("acodec") != "none":
        audio_codec = str(info.get("acodec"))

    return video_codec, audio_codec, fps


def _needs_telegram_normalization(info: dict[str, Any]) -> bool:
    video_codec, audio_codec, fps = _extract_selected_codecs(info)
    ext = str(info.get("ext", "")).lower()

    video_ok = bool(video_codec and (video_codec.startswith("avc1") or video_codec.startswith("h264")))
    audio_ok = audio_codec is None or audio_codec.startswith("mp4a") or audio_codec.startswith("aac")
    fps_ok = fps is None or float(fps) <= 30.0
    container_ok = ext == "mp4"

    return not (video_ok and audio_ok and fps_ok and container_ok)


def _normalize_for_telegram(path: str) -> str:
    src_path = Path(path)
    normalized_path = src_path.with_name(f"{src_path.stem}.tgfix.mp4")

    strict_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(src_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-r",
        "30",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(normalized_path),
    ]
    relaxed_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(src_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(normalized_path),
    ]

    last_output: list[str] = []
    for command in (strict_command, relaxed_command):
        try:
            process = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg not found. Install ffmpeg and retry.") from exc

        if process.returncode == 0 and normalized_path.exists():
            src_path.unlink(missing_ok=True)
            normalized_path.replace(src_path.with_suffix(".mp4"))
            return str(src_path.with_suffix(".mp4"))

        output = (process.stderr or process.stdout or "").strip().splitlines()
        if output:
            last_output = output

    tail = " | ".join(last_output[-3:]) if last_output else "unknown ffmpeg error"
    raise RuntimeError(f"ffmpeg normalization failed: {tail}")


def _download_video_sync(url: str) -> DownloadedVideo:
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
                _validate_size_before_download(probe_info)
                info = ydl.extract_info(url, download=True)
                filepath = _resolve_output_path(info, ydl)
                if _needs_telegram_normalization(info):
                    try:
                        filepath = _normalize_for_telegram(filepath)
                    except RuntimeError:
                        # Keep original downloaded file if ffmpeg normalization fails.
                        pass
                width, height = _extract_dimensions(info)
                return DownloadedVideo(path=filepath, width=width, height=height)
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


async def download_best_video(url: str) -> DownloadedVideo:
    if not is_supported_video_url(url):
        raise RuntimeError("Unsupported URL.")
    return await asyncio.to_thread(_download_video_sync, url.strip())
