"""Minimal self-check: URL routing, slideshow timing, compression decision.

Run: python tests/test_downloader.py
"""

import os
import sys
import tempfile

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.downloader.router import detect_platform  # noqa: E402
from bot.downloader.video_tools import compress_to_limit, photo_durations  # noqa: E402


def test_detect_platform():
    cases = {
        "https://www.tiktok.com/@user/video/123": "tiktok",
        "https://vm.tiktok.com/ZM123abc/": "tiktok",
        "https://vt.tiktok.com/ZS456def": "tiktok",
        "https://www.tiktok.com/@user/photo/7412345": "tiktok",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "youtube",
        "https://youtube.com/shorts/abc123": "youtube",
        "https://youtu.be/dQw4w9WgXcQ": "youtube",
        "https://m.youtube.com/watch?v=x": "youtube",
        "https://www.instagram.com/reel/Cabc123/": "instagram",
        "https://instagram.com/p/Cabc123/": "instagram",
        "https://www.instagram.com/stories/user/123/": "instagram",
        "https://vk.com/video123": None,
        "https://eviltiktok.com/video/1": None,
        "https://example.com/?u=https://tiktok.com": None,
        "not a url": None,
        "ftp://tiktok.com/video/1": None,
    }
    for url, expected in cases.items():
        got = detect_platform(url)
        assert got == expected, f"{url}: expected {expected}, got {got}"


def test_photo_durations():
    # No audio: 5 seconds per photo.
    assert photo_durations(3, None) == [5.0, 5.0, 5.0]
    assert photo_durations(2, 0) == [5.0, 5.0]
    # Audio <= 40s: equal split over the audio duration.
    assert photo_durations(5, 40.0) == [8.0] * 5
    assert photo_durations(4, 20.0) == [5.0] * 4
    # Audio > 40s: 7s per photo, last photo holds the remainder.
    assert photo_durations(3, 60.0) == [7.0, 7.0, 46.0]
    # Audio > 40s but too many photos for 7s each: equal split.
    durations = photo_durations(10, 41.0)
    assert durations == [4.1] * 10
    # Total never exceeds the audio duration.
    for count, audio in ((1, 60.0), (5, 40.0), (3, 41.0), (7, 100.0)):
        assert abs(sum(photo_durations(count, audio)) - audio) < 1e-6


def test_compress_decision():
    # A file already under the limit is returned unchanged, no ffmpeg run.
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(b"x" * 1024)
        f.flush()
        assert compress_to_limit(f.name, limit_bytes=2048) == f.name


if __name__ == "__main__":
    test_detect_platform()
    test_photo_durations()
    test_compress_decision()
    print("OK")
