import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.settings import CACHE_DIR, YOUTUBE_COOKIES


class VideoFormatError(RuntimeError):
    """Raised when we cannot select or download a video format."""

def _youtube_option_sets():
    """
    Генерирует наборы опций для yt_dlp.

    Первый проход без куков с мобильными клиентами, чтобы избежать n-challenge.
    Второй проход — с куками (если есть) и web-клиентом для приватных/возрастных видео.
    """
    po_token_android = os.getenv("YOUTUBE_PO_TOKEN_ANDROID")
    po_token_ios = os.getenv("YOUTUBE_PO_TOKEN_IOS")
    po_tokens = []
    if po_token_android:
        po_tokens.append(f"android.gvs+{po_token_android}")
    if po_token_ios:
        po_tokens.append(f"ios.gvs+{po_token_ios}")

    # Включаем EJS для n-challenge: нужны JS рантаймы (deno/node/quickjs/bun) и разрешение на загрузку solver-скриптов.
    def _parse_js_runtimes(raw: str) -> dict:
        if not raw:
            return {}
        raw = raw.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return {str(item): {} for item in data if str(item).strip()}
                if isinstance(data, dict):
                    cleaned = {}
                    for key, value in data.items():
                        if value is None or isinstance(value, dict):
                            cleaned[str(key)] = value
                        else:
                            cleaned[str(key)] = {}
                    return cleaned
            except json.JSONDecodeError:
                pass
        runtimes: dict[str, dict] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            # runtime[:path] — если путь указан, кладём в config
            if ":" in item:
                runtime, path = item.split(":", 1)
                runtimes[runtime] = {"path": path}
            else:
                runtimes[item] = {}
        return runtimes

    js_runtimes = _parse_js_runtimes(os.getenv("YOUTUBE_JS_RUNTIMES", "deno,node,quickjs,bun"))
    remote_components = [
        comp.strip() for comp in os.getenv("YOUTUBE_REMOTE_COMPONENTS", "ejs:github").split(",") if comp.strip()
    ]

    has_cookies = os.path.exists(YOUTUBE_COOKIES) and os.path.getsize(YOUTUBE_COOKIES) > 0
    option_sets = [
        {
            "use_cookies": False,
            # Если нет PO токенов, убираем android/ios HTTPS клиенты, чтобы не ловить 403/всплывающие warning-и.
            "clients": ["android", "tv_embedded", "web"] if po_tokens else ["tv_embedded", "web"],
        },
        {
            "use_cookies": has_cookies,
            "clients": ["web", "web_embedded", "tv_embedded"],
        },
    ]

    for opt in option_sets:
        base_opts = {
            "quiet": True,
            "extractor_retries": 2,
            "http_headers": {
                # Подменяем UA на Android-клиент, чтобы сервер отдавал менее защищенные потоки.
                "User-Agent": "com.google.android.youtube/19.20.33 (Linux; U; Android 10) gzip",
            },
            # Приоритет JS рантаймов и разрешение на загрузку solver-скриптов для n-challenge (EJS).
            "js_runtimes": js_runtimes,
            "remote_components": remote_components,
            "extractor_args": {
                "youtube": {
                    "player_client": opt["clients"],
                },
            },
        }
        if po_tokens:
            base_opts["extractor_args"]["youtube"]["po_token"] = po_tokens
        if opt["use_cookies"]:
            base_opts["cookiefile"] = YOUTUBE_COOKIES
        yield base_opts


def get_youtube_video_id(url: str) -> str:
    """
    Extracts the video ID from a YouTube URL.

    :param url: The YouTube video URL (any format).
    :return: The video ID if found, otherwise an empty string.
    """
    # Regular expression to match YouTube video IDs
    pattern = r"(?:v=|vi=|v%3D|vi%3D|youtu\.be/|/v/|/vi/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})"
    match = re.search(pattern, url)

    return match.group(1) if match else ""


async def get_best_video_format(yt_url: str) -> tuple[str, bool]:
    """
    Возвращает формат лучшего видео (format_id, has_audio) без ограничений по качеству.

    :param yt_url: URL YouTube видео
    :return: Пара (format_id, has_audio)
    """
    loop = asyncio.get_event_loop()

    def extract_best_format():
        last_error = None
        for opts in _youtube_option_sets():
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(yt_url, download=False)
                    formats = info.get("formats", [])

                    video_formats = []
                    for fmt in formats:
                        if fmt.get("vcodec") == "none":
                            continue
                        if not fmt.get("url"):
                            continue
                        video_formats.append(
                            {
                                "format_id": fmt["format_id"],
                                "height": fmt.get("height") or 0,
                                "fps": fmt.get("fps") or 0,
                                "tbr": fmt.get("tbr") or 0,
                                "filesize": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                                "ext": fmt.get("ext") or "",
                                "has_audio": fmt.get("acodec") != "none",
                            }
                        )

                    if not video_formats:
                        continue

                    mp4_candidates = [fmt for fmt in video_formats if fmt["ext"] == "mp4"]
                    candidates = mp4_candidates if mp4_candidates else video_formats
                    if not candidates:
                        continue

                    best_format = max(
                        candidates,
                        key=lambda x: (
                            x["height"],
                            x["fps"],
                            x["tbr"],
                            x["filesize"],
                            x["has_audio"],
                        ),
                    )
                    return best_format["format_id"], best_format["has_audio"]
            except DownloadError as e:
                last_error = e
                continue

        msg = f"Не удалось получить информацию о видео: {last_error}" if last_error else "Не удалось получить информацию о видео: подходящий формат не найден."
        raise VideoFormatError(msg)

    try:
        return await loop.run_in_executor(None, extract_best_format)
    except DownloadError as e:
        raise VideoFormatError(f"Не удалось получить информацию о видео: {e}") from e


async def download_video(yt_url: str, format_info: tuple[str, bool]) -> str:
    """
    Загружает видео по указанному URL и формату.

    :param yt_url: Ссылка на YouTube видео
    :param format_info: Идентификатор формата и информация о наличии аудио
    :return: Путь к загруженному файлу
    """
    loop = asyncio.get_event_loop()
    output_file = {}
    size_limit_mb = int(os.getenv("YOUTUBE_SIZE_LIMIT_MB", 48))
    size_limit = size_limit_mb * 1024 * 1024  # потолок для отправки

    if not format_info:
        raise VideoFormatError("Видео формат не определен, скачивание отменено.")
    format_id, has_audio = format_info
    if not format_id:
        raise VideoFormatError("Видео формат не определен, скачивание отменено.")

    def extract_and_download():
        primary_selector = f"{format_id}" if has_audio else f"{format_id}+bestaudio[ext=m4a]"

        format_selectors = [
            # Сначала пытаемся получить максимальное качество.
            "bv*+ba/best",
            "bv*+ba[ext=m4a]/b[ext=mp4]/best",
            # Затем пробуем конкретный формат, найденный при анализе.
            f"{primary_selector}/best",
            # Безопасный фолбэк.
            "best[ext=mp4][acodec!=none]/best",
        ]

        last_error = None
        for opts in _youtube_option_sets():
            for selector in format_selectors:
                options = {
                    **opts,
                    "format": selector,
                    "format_sort": [
                        "res",
                        "fps",
                        "tbr",
                        "ext:mp4",
                        "codec:h264",
                        "proto:https",
                    ],
                    "format_sort_force": True,
                    "merge_output_format": "mp4",
                    "postprocessors": [
                        {
                            "key": "FFmpegVideoConvertor",
                            "preferedformat": "mp4",
                        }
                    ],
                    "outtmpl": f"{CACHE_DIR}/%(title)s.%(ext)s",
                }
                with YoutubeDL(options) as ydl:
                    try:
                        result = ydl.extract_info(yt_url, download=True)
                        output_file["path"] = ydl.prepare_filename(result)
                        return
                    except DownloadError as e:
                        last_error = e
                        continue

        msg = f"Ошибка скачивания: {str(last_error)}" if last_error else "Ошибка скачивания: подходящий формат не найден."
        raise VideoFormatError(msg)

    await loop.run_in_executor(None, extract_and_download)
    file_path = output_file.get("path")

    if not file_path or not os.path.exists(file_path):
        return file_path

    # Если вышли за лимит — пытаемся максимально сохранить качество и поэтапно уменьшать.
    if os.path.getsize(file_path) > size_limit:
        if not shutil.which("ffmpeg"):
            os.remove(file_path)
            raise VideoFormatError("Требуется ffmpeg для уменьшения размера видео.")

        base_crf = int(os.getenv("YOUTUBE_FFMPEG_CRF", "22"))
        presets = [
            (3840, base_crf),
            (2560, base_crf + 1),
            (1920, base_crf + 2),
            (1280, base_crf + 3),
            (854, base_crf + 4),
            (640, base_crf + 6),
        ]

        def _try_transcode(max_width: int, crf: int) -> str:
            tmp_out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i", file_path,
                "-vf", f"scale='min({max_width},iw)':-2",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", str(crf),
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                tmp_out,
            ]
            subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return tmp_out

        last_error = None
        for max_width, crf in presets:
            try:
                tmp_out = _try_transcode(max_width, crf)
            except subprocess.CalledProcessError as e:
                last_error = e
                continue
            if os.path.getsize(tmp_out) <= size_limit:
                os.remove(file_path)
                os.replace(tmp_out, file_path)
                break
            os.remove(tmp_out)
        else:
            os.remove(file_path)
            raise VideoFormatError(f"Видео всё ещё больше {size_limit_mb} МБ после сжатия. {last_error or ''}".strip())

    return file_path


async def get_tiktok_video_dimensions(yt_url: str) -> dict:
    """
    Извлекает высоту и ширину видео с YouTube по его ID.

    :param yt_url: Ссылка на YouTube видео
    :return: Словарь с высотой и шириной видео
    """
    loop = asyncio.get_event_loop()

    def extract_dimensions():
        last_error = None
        for opts in _youtube_option_sets():
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(yt_url, download=False)
                    formats = info.get("formats", [])
                    for fmt in formats:
                        if fmt.get("width") and fmt.get("height"):
                            return {"width": fmt["width"], "height": fmt["height"]}
            except DownloadError as e:
                last_error = e
                continue
        if last_error:
            raise VideoFormatError(f"Не удалось получить размеры видео: {last_error}")
        return {"width": None, "height": None}

    dimensions = await loop.run_in_executor(None, extract_dimensions)
    return dimensions


async def get_best_tiktok_video_format(tt_url: str) -> int:
    """
    Возвращает лучший формат видео из TikTok.

    :param tt_url: URL видео TikTok
    :return: format_id лучшего качества видео
    """
    loop = asyncio.get_event_loop()

    def extract_best_format():
        with YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(tt_url, download=False)
            formats = info.get("formats", [])
            # Выбор форматов с видео
            video_formats = [
                {
                    "format_id": fmt["format_id"],
                    "resolution": fmt.get("height", 0),
                    "filesize": fmt.get("filesize", 0),
                }
                for fmt in formats
                if fmt.get("vcodec") != "none"
            ]
            # Сортировка форматов по разрешению
            best_format = max(video_formats, key=lambda x: x["resolution"], default=None)

            return best_format["format_id"] if best_format else None

    format_id = await loop.run_in_executor(None, extract_best_format)
    return format_id


async def download_tiktok_video(tt_url: str, format_id: int) -> str:
    """
    Загружает видео TikTok по указанному URL и формату.

    :param tt_url: Ссылка на TikTok видео
    :param format_id: Идентификатор формата для загрузки
    :return: Путь к сохраненному файлу
    """
    loop = asyncio.get_event_loop()
    output_file = {}

    def extract_and_download():
        options = {
            "format": f"{format_id}",
            "merge_output_format": "mp4",
            "quiet": True,
            "outtmpl": f"{CACHE_DIR}/%(title)s.%(ext)s",
        }
        with YoutubeDL(options) as ydl:
            try:
                result = ydl.extract_info(tt_url, download=True)
                output_file["path"] = ydl.prepare_filename(result)
            except DownloadError as e:
                raise RuntimeError(f"Download error: {str(e)}")

    await loop.run_in_executor(None, extract_and_download)
    return output_file.get("path")


async def test_tiktok():
    url = "https://www.@username/video/1234567890123456789"  # Пример ссылки на TikTok видео
    best_format = await get_best_tiktok_video_format(url)
    print("Лучший формат:", best_format)
    downloaded_path = await download_tiktok_video(url, best_format)
    print("Видео сохранено в:", downloaded_path)


async def test():
    url = "https://youtube.com/shorts/ntrQfA47n1s?si=1l6P6KNrjHkQynMw"  # Видео от моих лучших друзей
    format_info = await get_best_video_format(url)
    print(await get_tiktok_video_dimensions(url))
    print(await download_video(url, format_info))


if __name__ == "__main__":
    asyncio.run(test_tiktok())
    asyncio.run(test())
