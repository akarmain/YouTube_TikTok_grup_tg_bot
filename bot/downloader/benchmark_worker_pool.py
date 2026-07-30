import argparse
import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from bot.ffmpeg import ffmpeg_command, run_ffmpeg
from bot.settings import DOWNLOAD_WORKERS

_lock = threading.Lock()
_live_pids: set[int] = set()
_peak_process_count = 0


def _synthetic_clip_command(out_path: str) -> list[str]:
    return ffmpeg_command(
        "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast",
        out_path,
    )


def _run_download_stage(tag: str) -> None:
    global _peak_process_count
    out_path = os.path.join(tempfile.gettempdir(), f"bench-download-{tag}-{time.time_ns()}.mp4")
    process = subprocess.Popen(
        _synthetic_clip_command(out_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    with _lock:
        _live_pids.add(process.pid)
        _peak_process_count = max(_peak_process_count, len(_live_pids))
    try:
        process.wait()
    finally:
        with _lock:
            _live_pids.discard(process.pid)
        Path(out_path).unlink(missing_ok=True)


def _run_compress_stage(tag: str) -> None:
    out_path = os.path.join(tempfile.gettempdir(), f"bench-compress-{tag}-{time.time_ns()}.mp4")
    run_ffmpeg(_synthetic_clip_command(out_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(out_path).unlink(missing_ok=True)


async def _rss_monitor(stop_event: asyncio.Event, samples: list[int]) -> None:
    while not stop_event.is_set():
        with _lock:
            pids = list(_live_pids)
        if pids:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", ",".join(str(pid) for pid in pids)],
                capture_output=True,
                text=True,
            ).stdout
            samples.append(sum(int(x) for x in out.split() if x.strip().isdigit()))
        await asyncio.sleep(0.05)


async def _pipeline(tag: str) -> None:
    await asyncio.to_thread(_run_download_stage, tag)
    await asyncio.to_thread(_run_compress_stage, tag)


async def _pipeline_bounded(tag: str, gate: asyncio.Semaphore) -> None:
    async with gate:
        await _pipeline(tag)


async def _run_scenario(label: str, n: int, bounded: bool) -> None:
    global _peak_process_count
    _peak_process_count = 0
    samples: list[int] = []
    stop_event = asyncio.Event()
    monitor = asyncio.create_task(_rss_monitor(stop_event, samples))

    start = time.perf_counter()
    if bounded:
        gate = asyncio.Semaphore(DOWNLOAD_WORKERS)
        await asyncio.gather(*(_pipeline_bounded(str(i), gate) for i in range(n)))
    else:
        await asyncio.gather(*(_pipeline(str(i)) for i in range(n)))
    elapsed = time.perf_counter() - start

    stop_event.set()
    await monitor
    peak_rss_mb = (max(samples) / 1024) if samples else 0.0
    print(
        f"{label:<28} n={n:<3} elapsed={elapsed:.2f}s "
        f"peak_concurrent_download_processes={_peak_process_count:<3} peak_combined_rss={peak_rss_mb:.1f}MB"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare an unbounded download->compress pipeline against the worker-pool-bounded one."
    )
    parser.add_argument("--n", type=int, default=8)
    args = parser.parse_args()

    print(f"DOWNLOAD_WORKERS={DOWNLOAD_WORKERS} (auto-derived from os.cpu_count()={os.cpu_count()})")
    await _run_scenario("before (unbounded pipeline)", args.n, bounded=False)
    await _run_scenario("after (worker-pool bounded)", args.n, bounded=True)


if __name__ == "__main__":
    asyncio.run(main())
