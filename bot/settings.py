import os
from typing import Final

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Env(BaseSettings):
    TG_MAIN_BOT_TOKEN: str


model_config = SettingsConfigDict(env_file=".env")

ADMIN_ID = 912185600
BOT_NAME: Final = 'TikTubeLoaBot'
BOT_VERSION = "BOT VERSION: 1.0.1 21.12.24"

ALL_COMMANDS = {
    'start': '🚀 start',
}

BASIC_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASIC_DIR, "bot", "cache")
Env = Env()
YOUTUBE_COOKIES = os.path.join(BASIC_DIR, "youTube/cookies.txt")

