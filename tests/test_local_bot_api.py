"""Local Bot API Server session wiring: bot.main.build_bot_session().

Run: pytest tests/test_local_bot_api.py
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")


def _reload_with_env(monkeypatch, **env: str) -> tuple:
    for key in ("TG_LOCAL_BOT_API_ENABLED",):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import bot.settings as settings
    import bot.main as main

    importlib.reload(settings)
    importlib.reload(main)
    return settings, main


def test_defaults_to_cloud_api_session(monkeypatch):
    settings, main = _reload_with_env(monkeypatch)
    assert settings.LOCAL_BOT_API_BASE_URL is None
    assert main.build_bot_session() is None


def test_disabled_flag_is_treated_as_cloud_api(monkeypatch):
    settings, main = _reload_with_env(monkeypatch, TG_LOCAL_BOT_API_ENABLED="false")
    assert settings.LOCAL_BOT_API_BASE_URL is None
    assert main.build_bot_session() is None


def test_enabled_flag_builds_local_session(monkeypatch):
    settings, main = _reload_with_env(monkeypatch, TG_LOCAL_BOT_API_ENABLED="true")
    session = main.build_bot_session()
    assert session is not None
    assert session.api.is_local is True
    assert session.api.base == "http://telegram_bot_api:8081/bot{token}/{method}"
    assert session.api.file == "http://telegram_bot_api:8081/file/bot{token}/{path}"


def test_max_video_size_defaults_to_cloud_api_ceiling(monkeypatch):
    settings, _ = _reload_with_env(monkeypatch)
    assert settings.MAX_VIDEO_SIZE_MB == 49
    assert settings.MAX_VIDEO_SIZE_BYTES == 49 * 1024 * 1024


def test_max_video_size_rises_when_local_api_enabled(monkeypatch):
    settings, _ = _reload_with_env(monkeypatch, TG_LOCAL_BOT_API_ENABLED="true")
    assert settings.MAX_VIDEO_SIZE_MB == 1950
    assert settings.MAX_VIDEO_SIZE_BYTES == 1950 * 1024 * 1024


def test_compose_only_telegram_api_keys_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TG_MAIN_BOT_TOKEN=0:test",
                "TG_LOCAL_BOT_API_ENABLED=true",
                "TELEGRAM_API_ID=123456",
                "TELEGRAM_API_HASH=test-hash",
            )
        ),
        encoding="utf-8",
    )
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run(
        [sys.executable, "-c", "import bot.settings; print('SETTINGS_OK')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SETTINGS_OK"
