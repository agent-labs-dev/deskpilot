"""Recording a window with the agent cursor the plugin draws, filmed directly.

The plugin draws the cursor into a window capture that asks for one (`screen.frame`), so a clip needs no cursor of its
own: this takes such a capture a fixed number of times a second and hands the frames on, as a stream for another
encoder or into an mp4 it writes with ffmpeg. The captures also keep the window filmed, so a hidden action shows its
cursor in them (see `agentcursor` in the plugin).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

FPS = 25
# A window that gives no frame this many times in a row has closed or its monitor is off.
MAX_FAILURES = 10
# A recording is bounded, as every action is.
MAX_SECONDS = 600


@dataclass
class Result:
    frames: int
    seconds: float
    error: str | None = None
    failure: Exception | None = None

    def raise_if_empty(self) -> None:
        """A recording that captured no frame failed: raise the capture's own error, so its code is kept."""
        if self.failure and not self.frames:
            raise self.failure

    def to_dict(self) -> dict:
        return {"frames": self.frames, "seconds": round(self.seconds, 2)} | (
            {"error": self.error} if self.error else {}
        )


def ppm_size(frame: bytes) -> tuple[int, int]:
    """The width and height of a binary PPM (P6) frame."""
    fields = frame.split(maxsplit=3)
    if fields[0] != b"P6":
        raise ValueError("not a PPM frame")
    return int(fields[1]), int(fields[2])


def stream(
    take: Callable[[], bytes],
    sink: Callable[[bytes], None],
    stop: threading.Event,
    fps: int = FPS,
    seconds: float | None = None,
) -> Result:
    """Take frames from `take` at a steady `fps` into `sink` until `stop` is set or `seconds` pass. A failed capture,
    and the slots a slow one overran, repeat the last frame, so the stream keeps the real time of the run and no
    longer than it."""
    limit = min(seconds, MAX_SECONDS) if seconds else MAX_SECONDS
    total = max(1, round(limit * fps))
    start = time.monotonic()
    last: bytes | None = None
    written = failures = 0
    failure: Exception | None = None
    while not stop.is_set() and written < total:
        stop.wait(max(0.0, start + written / fps - time.monotonic()))
        if stop.is_set():
            break
        try:
            last = take()
            failures = 0
        except (RuntimeError, TimeoutError) as caught:
            failures += 1
            if failures >= MAX_FAILURES or last is None:
                failure = caught
                break
        assert last is not None
        sink(last)
        written += 1
        while written < total and start + written / fps <= time.monotonic():
            sink(last)
            written += 1
    return Result(written, time.monotonic() - start, str(failure) if failure else None, failure)


class Mp4:
    """An ffmpeg encode of PPM frames at a fixed size, which the first frame sets: a window that is resized mid-run is
    scaled to it."""

    def __init__(self, path: Path, fps: int = FPS):
        self.path, self.fps = path, fps
        self.process: subprocess.Popen | None = None
        self.size: tuple[int, int] | None = None

    def write(self, frame: bytes) -> None:
        if self.process is None:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("recording to a file needs ffmpeg on PATH")
            width, height = ppm_size(frame)
            self.size = (width, height)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fit = f"scale={width // 2 * 2}:{height // 2 * 2},format=yuv420p"
            self.process = subprocess.Popen(
                [
                    *(ffmpeg, "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(self.fps), "-i", "-"),
                    *("-vf", fit, "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-movflags", "+faststart"),
                    str(self.path),
                ],
                stdin=subprocess.PIPE,
            )
        assert self.process.stdin is not None
        self.process.stdin.write(frame)

    def close(self) -> None:
        if self.process is None:
            return
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=60)
        if self.process.returncode:
            raise RuntimeError(f"ffmpeg could not write {self.path} (exit status {self.process.returncode})")


def to_file(
    take: Callable[[], bytes], path: Path, stop: threading.Event, fps: int = FPS, seconds: float | None = None
) -> Result:
    out = Mp4(path, fps)
    try:
        return stream(take, out.write, stop, fps, seconds)
    finally:
        out.close()


def to_stdout(
    take: Callable[[], bytes], out: BinaryIO, stop: threading.Event, fps: int = FPS, seconds: float | None = None
) -> Result:
    def write(frame: bytes) -> None:
        out.write(frame)
        out.flush()

    return stream(take, write, stop, fps, seconds)
