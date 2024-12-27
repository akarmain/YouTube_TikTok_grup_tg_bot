import asyncio
import re

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.settings import CACHE_DIR, YOUTUBE_COOKIES


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


async def get_best_video_format(yt_url: str) -> int:
    """
    Возвращает формат ID лучшего видео в формате MP4, размером до 40 МБ.

    :param yt_url: URL YouTube видео
    :return: format_id лучшего качества видео
    """
    loop = asyncio.get_event_loop()

    def extract_best_format():
        with YoutubeDL({"quiet": True, "cookiefile": YOUTUBE_COOKIES}) as ydl:
            info = ydl.extract_info(yt_url, download=False)
            formats = info.get("formats", [])
            # Фильтруем форматы по условиям
            mp4_formats = [
                {
                    "format_id": fmt["format_id"],
                    "resolution": fmt.get("height", 0),
                    "filesize": fmt.get("filesize", 0),
                }
                for fmt in formats
                if fmt["ext"] == "mp4" and fmt.get("vcodec") != "none"
            ]
            # Отбираем подходящие форматы по размеру
            valid_formats = [
                fmt for fmt in mp4_formats
                if fmt["filesize"] and fmt["filesize"] <= 40 * 1024 * 1024
            ]
            # Сортируем по разрешению (лучшее качество)
            best_format = max(valid_formats, key=lambda x: x["resolution"], default=None)

            return best_format["format_id"] if best_format else None

    format_id = await loop.run_in_executor(None, extract_best_format)
    return format_id


async def download_video(yt_url, format_id) -> str:
    """
    Загружает видео по указанному URL и формату.

    :param yt_url: Ссылка на YouTube видео
    :param format_id: Идентификатор формата для загрузки
    :param output_path: Путь к каталогу для сохранения
    :return: None
    """
    loop = asyncio.get_event_loop()
    output_file = {}

    def extract_and_download():
        options = {
            "format": f"{format_id}+bestaudio[ext=m4a]/mp4",
            "merge_output_format": "mp4",
            "cookiefile": YOUTUBE_COOKIES,
            "quiet": True,
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
            except DownloadError as e:
                raise RuntimeError(f"Download error: {str(e)}")

    await loop.run_in_executor(None, extract_and_download)
    return output_file.get("path")


async def get_tiktok_video_dimensions(yt_url: str) -> dict:
    """
    Извлекает высоту и ширину видео с YouTube по его ID.

    :param yt_url: Ссылка на YouTube видео
    :return: Словарь с высотой и шириной видео
    """
    loop = asyncio.get_event_loop()

    def extract_dimensions():
        with YoutubeDL({"quiet": True, "cookiefile": YOUTUBE_COOKIES}) as ydl:
            info = ydl.extract_info(yt_url, download=False)
            formats = info.get("formats", [])
            for fmt in formats:
                if fmt.get("width") and fmt.get("height"):
                    return {"width": fmt["width"], "height": fmt["height"]}
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
    url = "https://www.tiktok.com/@username/video/1234567890123456789"  # Пример ссылки на TikTok видео
    best_format = await get_best_tiktok_video_format(url)
    print("Лучший формат:", best_format)
    downloaded_path = await download_tiktok_video(url, best_format)
    print("Видео сохранено в:", downloaded_path)


async def test():
    url = "https://youtube.com/shorts/ntrQfA47n1s?si=1l6P6KNrjHkQynMw"  # Видео от моих лучших друзей
    t = await get_best_video_format(url)
    print(await get_video_dimensions(url))
    print(await download_video(url, t))


if __name__ == "__main__":
    asyncio.run(test_tiktok())
    asyncio.run(test())
