"""YouTube fast-mode format, size, and fallback decisions."""

import os

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")

from bot.downloader.video_tools import _normalization_scale_args
from bot.youTube import sourse as youtube


def test_fast_quality_caps_step_down_without_crossing_minimum():
    assert youtube._fast_quality_caps(420, 720) == (720, 480)
    assert youtube._fast_quality_caps(500, 1080) == (1080, 720)
    assert youtube._fast_quality_caps(720, 720) == (720,)


def test_fast_selector_caps_resolution_fps_and_prefers_telegram_codecs():
    selector = youtube._fast_format_selector(420, 720)

    assert "[height>=420][height<=720][fps<=?30]" in selector
    assert "[vcodec^=avc1]+ba[acodec^=mp4a]" in selector
    assert selector.endswith("b[height>=420][height<=720][fps<=?30]")


def test_fast_attempts_use_720_then_480_and_a_capped_fallback(monkeypatch):
    monkeypatch.setattr(youtube, "YOUTUBE_FAST_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(youtube, "YOUTUBE_MIN_HEIGHT", 420)
    monkeypatch.setattr(youtube, "YOUTUBE_MAX_HEIGHT", 720)

    attempts = youtube._video_download_attempts()

    assert [cap for _, cap in attempts] == [720, 480, None]
    assert "[height<=?720]" in attempts[-1][0]


def test_disabling_fast_mode_restores_legacy_attempts(monkeypatch):
    monkeypatch.setattr(youtube, "YOUTUBE_FAST_DOWNLOAD_ENABLED", False)

    attempts = youtube._video_download_attempts()

    assert attempts == (
        (youtube.PREFERRED_FORMAT_SELECTOR, None),
        (youtube.FALLBACK_FORMAT_SELECTOR, None),
        (youtube.LAST_RESORT_FORMAT_SELECTOR, None),
    )


def test_estimated_oversize_steps_down_but_not_below_minimum(monkeypatch):
    monkeypatch.setattr(youtube, "YOUTUBE_FAST_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(youtube, "YOUTUBE_TARGET_SIZE_BYTES", 80)
    monkeypatch.setattr(youtube, "YOUTUBE_TARGET_SIZE_MB", 1)
    info = {
        "requested_formats": [
            {"filesize": 60},
            {"filesize_approx": 30},
        ]
    }
    attempts = (("720", 720), ("480", 480), ("fallback", None))

    assert youtube._try_smaller_quality(info, attempts, 0) is True
    assert youtube._try_smaller_quality(info, attempts, 1) is False


def test_fast_target_compression_never_uses_360p(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 100)
    captured: dict[str, object] = {}

    def fake_compress(filepath, limit_bytes, steps):
        captured.update(filepath=filepath, limit_bytes=limit_bytes, steps=steps)
        return filepath

    monkeypatch.setattr(youtube, "YOUTUBE_FAST_DOWNLOAD_ENABLED", True)
    monkeypatch.setattr(youtube, "YOUTUBE_TARGET_SIZE_BYTES", 80)
    monkeypatch.setattr(youtube, "YOUTUBE_TARGET_SIZE_MB", 1)
    monkeypatch.setattr(youtube, "YOUTUBE_MIN_HEIGHT", 420)
    monkeypatch.setattr(youtube, "YOUTUBE_MAX_HEIGHT", 720)
    monkeypatch.setattr(youtube, "compress_to_limit", fake_compress)

    assert youtube._fit_fast_target(str(path)) == str(path)
    assert captured["steps"] == ((720, 23), (480, 26))


def test_normalization_scale_is_optional():
    assert _normalization_scale_args(None) == []
    assert _normalization_scale_args(720) == ["-vf", "scale=-2:min(720\\,ih)"]
