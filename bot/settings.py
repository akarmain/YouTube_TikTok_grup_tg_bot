import os
from typing import Final

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


CLOUD_API_MAX_VIDEO_SIZE_MB: Final = 49
LOCAL_API_MAX_VIDEO_SIZE_MB: Final = 1950
LOCAL_BOT_API_INTERNAL_URL: Final = "http://telegram_bot_api:8081"


class Env(BaseSettings):
    TG_MAIN_BOT_TOKEN: str
    TG_ADMIN_ID: int = 912185600
    TG_LOCAL_BOT_API_ENABLED: bool = False
    TG_FFMPEG_THREADS: int | None = None
    TG_FFMPEG_MAX_JOBS: int | None = None
    TG_DOWNLOAD_WORKERS: int | None = None
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def compute_ffmpeg_concurrency(
    cpu_count: int | None,
    explicit_max_jobs: int | None,
    explicit_threads: int | None,
) -> tuple[int, int]:
    cores = cpu_count if cpu_count and cpu_count > 0 else 1

    max_jobs = max(1, explicit_max_jobs) if explicit_max_jobs is not None else max(1, cores // 2)
    threads = max(1, explicit_threads) if explicit_threads is not None else max(1, cores // max_jobs)

    return max_jobs, threads


BOT_NAME: Final = 'TikTubeLoaBot'
BOT_VERSION = "BOT VERSION: 1.0.1 21.12.24"

ALL_COMMANDS = {
    'start': '🚀 start',
}

ADMIN_COMMANDS = {
    "cmd": "показать список команд администратора",
    "send_db": "экспорт базы пользователей",
}

BASIC_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASIC_DIR, "cache")
Env = Env()
ADMIN_ID = Env.TG_ADMIN_ID
LOCAL_BOT_API_ENABLED = Env.TG_LOCAL_BOT_API_ENABLED
LOCAL_BOT_API_BASE_URL = LOCAL_BOT_API_INTERNAL_URL if LOCAL_BOT_API_ENABLED else None
MAX_VIDEO_SIZE_MB = LOCAL_API_MAX_VIDEO_SIZE_MB if LOCAL_BOT_API_ENABLED else CLOUD_API_MAX_VIDEO_SIZE_MB
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
FFMPEG_MAX_JOBS, FFMPEG_THREADS = compute_ffmpeg_concurrency(
    os.cpu_count(), Env.TG_FFMPEG_MAX_JOBS, Env.TG_FFMPEG_THREADS
)
DOWNLOAD_WORKERS = max(1, Env.TG_DOWNLOAD_WORKERS) if Env.TG_DOWNLOAD_WORKERS is not None else FFMPEG_MAX_JOBS
JSON_DB_PATH = os.path.join(BASIC_DIR, "database", "users_videos.json")
SQLITE_DB_PATH = os.path.join(BASIC_DIR, "database", "users_videos.sqlite3")
YOUTUBE_COOKIES = os.path.join(BASIC_DIR, "youTube/cookies.txt")
TIKTOK_COOKIES = os.path.join(BASIC_DIR, "tiktok", "cookies.txt")
INSTAGRAM_COOKIES = os.path.join(BASIC_DIR, "instagram", "cookies.txt")
