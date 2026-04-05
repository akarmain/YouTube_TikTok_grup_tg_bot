import subprocess
from contextlib import contextmanager
from threading import BoundedSemaphore
from typing import Any, Iterator

from bot.settings import FFMPEG_MAX_JOBS, FFMPEG_THREADS

_ffmpeg_jobs = BoundedSemaphore(FFMPEG_MAX_JOBS)


@contextmanager
def ffmpeg_job_slot() -> Iterator[None]:
    # Serialize heavyweight ffmpeg work to avoid saturating all CPU cores.
    _ffmpeg_jobs.acquire()
    try:
        yield
    finally:
        _ffmpeg_jobs.release()


def ffmpeg_command(*args: str) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        str(FFMPEG_THREADS),
        *args,
    ]


def run_ffmpeg(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    with ffmpeg_job_slot():
        return subprocess.run(command, **kwargs)
