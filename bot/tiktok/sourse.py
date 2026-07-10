import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from http.cookiejar import MozillaCookieJar
from urllib.request import HTTPCookieProcessor, build_opener

from loguru import logger
from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional stability dependency
    curl_requests = None

from bot.downloader.media import MediaResult
from bot.downloader.video_tools import (
    SLIDESHOW_HEIGHT,
    SLIDESHOW_WIDTH,
    build_slideshow,
    extract_dimensions,
    ffprobe_duration,
)
from bot.ffmpeg import ffmpeg_command, run_ffmpeg
from bot.settings import CACHE_DIR, TIKTOK_COOKIES

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
MOBILE_APP_UA = "com.zhiliaoapp.musically/2022100901 (Linux; U; Android 10; en_US) okhttp/3.14.9.4"
MOBILE_WEB_UA = "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
MAX_TIKTOK_HEIGHT = 720
TIKTOK_DEVICE_ID_FILE = os.path.join(CACHE_DIR, "tiktok_device_id.txt")
TIKTOK_IID_FILE = os.path.join(CACHE_DIR, "tiktok_iid.txt")

# TikTok hydration blobs, in the order they are usually present/useful.
_SCRIPT_JSON_PATTERNS = (
    ("UNIVERSAL_DATA", re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>', re.S)),
    ("SIGI_STATE", re.compile(r'<script[^>]+id="SIGI_STATE"[^>]*>(.+?)</script>', re.S)),
    ("SIGI_STATE_JS", re.compile(r"window\[['\"]SIGI_STATE['\"]\]\s*=\s*(\{.+?\})\s*;", re.S)),
    ("NEXT_DATA", re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>', re.S)),
)
_IMAGE_URL_KEYS = ("imageURL", "image_url", "displayImage", "display_image", "thumbnail")
_URL_LIST_KEYS = ("urlList", "url_list")


class TikTokDownloadError(RuntimeError):
    """Raised when TikTok media cannot be downloaded."""


class TikTokBotCheckError(TikTokDownloadError):
    """Raised when TikTok returns a captcha/security-check page instead of media data."""


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


def _build_format_candidates() -> list[tuple[str, bool]]:
    return [
        ("best/b", True),
        ("bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]", False),
        ("bv*[height<=720]+ba/b[height<=720]", False),
        ("b[height<=720]/best[height<=720]", False),
        ("bv*+ba/b", True),
    ]


def _downscale_to_720p(src_path: str, dest_path: str) -> None:
    cmd = ffmpeg_command(
        "-y",
        "-i", src_path,
        "-vf", "scale=-2:min(720\\,ih)",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        dest_path,
    )
    run_ffmpeg(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _extractor_variants() -> list[dict]:
    return [
        {
            "http_headers": {
                "User-Agent": DESKTOP_UA,
                "Referer": "https://www.tiktok.com/",
                "Origin": "https://www.tiktok.com",
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
                "User-Agent": MOBILE_APP_UA,
                "Accept-Language": "en-US,en;q=0.9",
            },
        },
    ]


def _base_ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "geo_bypass_country": "US",
        "geo_bypass": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "impersonate": ImpersonateTarget.from_str("chrome"),
        "extractor_args": _tiktok_extractor_args(),
    }


def _is_tiktok_unavailable_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "video not available",
            "status code 0",
            "login required",
            "captcha",
        )
    )


def _download_video_sync(source_url: str, resolved_url: str) -> MediaResult:
    base_opts = {
        **_base_ydl_opts(),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(CACHE_DIR, "tiktok-%(id)s.%(ext)s"),
    }

    last_error = None
    path = info = needs_scale = None
    for extractor_opts in _extractor_variants():
        for fmt, scale in _build_format_candidates():
            options = _with_cookies({**base_opts, **extractor_opts, "format": fmt})
            with YoutubeDL(options) as ydl:
                try:
                    candidate_info = ydl.extract_info(resolved_url, download=True)
                    candidate_path = ydl.prepare_filename(candidate_info)
                    # Отсекаем аудио-форматы, если вдруг yt_dlp подобрал только звук.
                    if candidate_info.get("vcodec") == "none":
                        last_error = TikTokDownloadError("yt_dlp вернул только аудио, пробуем другой формат.")
                        continue
                    path, info, needs_scale = candidate_path, candidate_info, scale
                    break
                except DownloadError as e:
                    last_error = e
                    if _is_tiktok_unavailable_error(e):
                        raise TikTokDownloadError(f"Не удалось скачать видео: {e}") from e
                    continue
        if path:
            break

    if not path or info is None:
        raise TikTokDownloadError(f"Не удалось скачать видео: {last_error}")
    if not os.path.exists(path) or path.endswith(".mp3"):
        raise TikTokDownloadError("Видео не было загружено (получен только аудио-файл).")

    width, height = extract_dimensions(info)

    if needs_scale or (height and height > MAX_TIKTOK_HEIGHT):
        if not shutil.which("ffmpeg"):
            raise TikTokDownloadError("Требуется ffmpeg для приведения видео к 720p или ниже.")
        base_name, _ = os.path.splitext(path)
        scaled_path = f"{base_name}-720p.mp4"
        try:
            _downscale_to_720p(path, scaled_path)
        except subprocess.CalledProcessError as e:
            if os.path.exists(scaled_path):
                os.remove(scaled_path)
            raise TikTokDownloadError(f"Не удалось привести видео к 720p: {e}") from e
        if os.path.exists(path):
            os.remove(path)
        path = scaled_path
        if height:
            width = int(width * MAX_TIKTOK_HEIGHT / height) if width else None
            if width:
                width -= width % 2
            height = MAX_TIKTOK_HEIGHT

    return MediaResult(
        platform="tiktok",
        source_url=source_url,
        media_type="video",
        path=path,
        width=width,
        height=height,
        title=info.get("title"),
        description=info.get("description"),
        duration=info.get("duration"),
    )


def _extract_json_blobs(html: str) -> list[tuple[str, dict]]:
    blobs = []
    for name, pattern in _SCRIPT_JSON_PATTERNS:
        for match in pattern.finditer(html):
            try:
                data = json.loads(match.group(1).strip())
            except ValueError:
                continue
            if isinstance(data, dict):
                blobs.append((name, data))
    return blobs


def _image_obj_url(obj) -> Optional[str]:
    """Best URL from a TikTok image object: {imageURL|displayImage|...: {urlList|url_list: [...]}}."""
    if not isinstance(obj, dict):
        return None
    containers = [obj[key] for key in _IMAGE_URL_KEYS if isinstance(obj.get(key), dict)]
    containers.append(obj)  # url list directly on the image object
    for container in containers:
        for list_key in _URL_LIST_KEYS:
            urls = container.get(list_key)
            if isinstance(urls, list):
                for url in urls:
                    if isinstance(url, str) and url.startswith("http"):
                        return url
    return None


def _collect_image_posts(node, found: list[tuple[list, dict]], depth: int = 0) -> None:
    """Recursively finds dicts holding imagePost/image_post_info with an images list."""
    if depth > 40:
        return
    if isinstance(node, dict):
        for post_key in ("imagePost", "image_post_info"):
            image_post = node.get(post_key)
            if isinstance(image_post, dict):
                images = image_post.get("images")
                if isinstance(images, list) and images:
                    found.append((images, node))
        for value in node.values():
            _collect_image_posts(value, found, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect_image_posts(value, found, depth + 1)
    elif isinstance(node, str) and ("\"imagePost\"" in node or "\"image_post_info\"" in node):
        # Hydration blobs sometimes embed item JSON as an escaped string.
        try:
            _collect_image_posts(json.loads(node), found, depth + 1)
        except ValueError:
            pass


def _item_metadata(item: dict) -> tuple[Optional[str], Optional[str]]:
    title = item.get("title")
    title = title if isinstance(title, str) and title.strip() else None
    description = None
    for key in ("desc", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            description = value
            break
    return title, description


def _parse_photo_post_html(html: str, post_id: Optional[str] = None) -> tuple[list[str], Optional[str], Optional[str]]:
    """Returns (image_urls, title, description); empty list when no photo post data was found."""
    candidates: list[tuple[list, dict]] = []
    for _, data in _extract_json_blobs(html):
        _collect_image_posts(data, candidates)

    if not candidates:
        return [], None, None

    # Prefer the item matching the post id from the URL (pages may embed related posts too).
    chosen = candidates[0]
    if post_id:
        for images, item in candidates:
            if str(item.get("id") or item.get("aweme_id") or "") == post_id:
                chosen = (images, item)
                break

    images, item = chosen
    urls = [url for url in (_image_obj_url(img) for img in images) if url]
    unique_urls = list(dict.fromkeys(urls))
    title, description = _item_metadata(item)
    return unique_urls, title, description


def _page_failure_hint(html: str, final_url: str) -> str:
    lowered = html[:200_000].lower()
    if "login" in final_url:
        return "redirected to login"
    for marker in ("captcha", "verify to continue", "security check"):
        if marker in lowered:
            return f"page looks like a bot check ({marker})"
    for marker in ("item doesn't exist", "video currently unavailable", "page not available"):
        if marker in lowered:
            return "post looks unavailable/deleted"
    return "no known failure marker"


def _tiktok_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Referer": "https://www.tiktok.com/",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _tiktok_cookie_jar() -> MozillaCookieJar | None:
    if not _has_cookies():
        return None
    jar = MozillaCookieJar(TIKTOK_COOKIES)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError):
        return None
    return jar


def _tiktok_cookie_dict() -> dict[str, str]:
    jar = _tiktok_cookie_jar()
    if jar is None:
        return {}
    return {cookie.name: cookie.value for cookie in jar}


def _fetch_tiktok_page(url: str, user_agent: str) -> tuple[str, str]:
    headers = _tiktok_headers(user_agent)
    if curl_requests is not None:
        response = curl_requests.get(
            url,
            headers=headers,
            cookies=_tiktok_cookie_dict() or None,
            impersonate="chrome124",
            timeout=15,
            allow_redirects=True,
        )
        response.raise_for_status()
        return str(response.url), response.text

    request = Request(url, headers=headers)
    if _has_cookies():
        jar = _tiktok_cookie_jar()
        if jar is not None:
            with build_opener(HTTPCookieProcessor(jar)).open(request, timeout=15) as response:
                return response.geturl(), response.read().decode("utf-8", "replace")
    with urlopen(request, timeout=15) as response:
        return response.geturl(), response.read().decode("utf-8", "replace")


def _fetch_photo_post(resolved_url: str) -> tuple[list[str], Optional[str], Optional[str]]:
    """Returns (image_urls, title, description) parsed from the TikTok photo post page.

    yt-dlp cannot extract slideshow images, so we read them from the hydration
    JSON blobs embedded in the page (UNIVERSAL_DATA / SIGI_STATE / NEXT_DATA).
    """
    post_id = urlparse(resolved_url).path.rstrip("/").split("/")[-1] or None
    last_error: Optional[Exception] = None
    for user_agent in (DESKTOP_UA, MOBILE_WEB_UA):
        try:
            final_url, html = _fetch_tiktok_page(resolved_url, user_agent)
        except Exception as e:
            last_error = e
            continue

        images, title, description = _parse_photo_post_html(html, post_id)
        if images:
            return images, title, description

        blobs = _extract_json_blobs(html)
        failure_hint = _page_failure_hint(html, final_url)
        logger.warning(
            "TikTok photo post parse failed: url={} blobs={} top_keys={} hint={}",
            resolved_url,
            [name for name, _ in blobs],
            [sorted(data.keys())[:8] for _, data in blobs],
            failure_hint,
        )
        if "bot check" in failure_hint:
            last_error = TikTokBotCheckError("TikTok вернул антибот-проверку/captcha вместо данных поста.")
            continue

    if last_error is not None:
        if isinstance(last_error, TikTokBotCheckError):
            raise last_error
        raise TikTokDownloadError(f"Не удалось открыть страницу фото-поста: {last_error}") from last_error
    raise TikTokDownloadError("Не удалось разобрать список фотографий поста TikTok.")


def _download_slideshow_audio(resolved_url: str, dest_dir: str) -> Optional[str]:
    # yt-dlp's TikTok extractor only matches /video/ URLs; for photo posts the
    # same item id served as /video/ yields the audio-only slideshow format.
    audio_url = resolved_url.replace("/photo/", "/video/")
    options = _with_cookies({
        **_base_ydl_opts(),
        "format": "ba/b",
        "outtmpl": os.path.join(dest_dir, "audio-%(id)s.%(ext)s"),
        "http_headers": {
            "User-Agent": DESKTOP_UA,
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
    })
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(audio_url, download=True)
            path = ydl.prepare_filename(info)
            return path if os.path.exists(path) else None
    except Exception:
        return None


def _download_slideshow_sync(source_url: str, resolved_url: str) -> MediaResult:
    os.makedirs(CACHE_DIR, exist_ok=True)
    image_urls, title, description = _fetch_photo_post(resolved_url)

    post_id = urlparse(resolved_url).path.rstrip("/").split("/")[-1] or "post"
    out_path = os.path.join(CACHE_DIR, f"tiktok-slideshow-{post_id}.mp4")
    work_dir = tempfile.mkdtemp(prefix="tiktok-photos-", dir=CACHE_DIR)
    try:
        image_paths = []
        for index, url in enumerate(image_urls):
            ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
            image_path = os.path.join(work_dir, f"photo-{index:03d}{ext}")
            req = Request(url, headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.tiktok.com/"})
            try:
                with urlopen(req, timeout=20) as response, open(image_path, "wb") as handle:
                    shutil.copyfileobj(response, handle)
            except Exception as e:
                raise TikTokDownloadError(f"Не удалось скачать фото из поста: {e}") from e
            image_paths.append(image_path)

        audio_path = _download_slideshow_audio(resolved_url, work_dir)
        try:
            build_slideshow(image_paths, audio_path, out_path)
        except RuntimeError as e:
            if os.path.exists(out_path):
                os.remove(out_path)
            raise TikTokDownloadError(f"Не удалось собрать видео из фото-поста: {e}") from e
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return MediaResult(
        platform="tiktok",
        source_url=source_url,
        media_type="slideshow_video",
        path=out_path,
        width=SLIDESHOW_WIDTH,
        height=SLIDESHOW_HEIGHT,
        title=title,
        description=description,
        duration=ffprobe_duration(out_path),
    )


async def download_tiktok(url: str) -> MediaResult:
    """Downloads a TikTok video or photo/slideshow post as a single mp4 (short links supported)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    resolved_url = await asyncio.to_thread(_resolve_tiktok_url, url)
    if "/photo/" in urlparse(resolved_url).path:
        return await asyncio.to_thread(_download_slideshow_sync, url, resolved_url)
    return await asyncio.to_thread(_download_video_sync, url, resolved_url)
