import os
from typing import Final

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Env(BaseSettings):
    TG_MAIN_BOT_TOKEN: str
    TG_ADMIN_ID: int = 912185600
    TG_MAX_VIDEO_SIZE_MB: int = 49
    TG_FFMPEG_THREADS: int | None = None
    TG_FFMPEG_MAX_JOBS: int = 1
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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
DEFAULT_FFMPEG_THREADS = 1
ADMIN_ID = Env.TG_ADMIN_ID
MAX_VIDEO_SIZE_MB = Env.TG_MAX_VIDEO_SIZE_MB
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
FFMPEG_THREADS = max(1, Env.TG_FFMPEG_THREADS or DEFAULT_FFMPEG_THREADS)
FFMPEG_MAX_JOBS = max(1, Env.TG_FFMPEG_MAX_JOBS)
JSON_DB_PATH = os.path.join(BASIC_DIR, "database", "users_videos.json")
YOUTUBE_COOKIES = os.path.join(BASIC_DIR, "youTube/cookies.txt")
TIKTOK_COOKIES = os.path.join(BASIC_DIR, "tiktok", "cookies.txt")
