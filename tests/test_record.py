import io
import subprocess
import threading
from dataclasses import replace

import pytest
from test_core import fake_grim, win

from deskpilot import record, screen


def ppm(width=4, height=2, fill=0):
    return b"P6\n%d %d\n255\n" % (width, height) + bytes([fill]) * (width * height * 3)


def fake_ppm_grim(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, ppm(), b"")

    monkeypatch.setattr(screen.subprocess, "run", run)
    return calls


def test_only_a_recording_frame_asks_for_the_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    window = replace(win(w=100, h=50), stable_id="18000001")
    calls = fake_grim(monkeypatch, (100, 50))
    screen.capture(window)
    assert "-c" not in calls[0]
    frames = fake_ppm_grim(monkeypatch)
    assert screen.frame(window) == ppm()
    assert frames[0] == ["grim", "-t", "ppm", "-c", "-T", "18000001", "-"]


def test_a_window_without_a_toplevel_id_cannot_be_filmed():
    with pytest.raises(RuntimeError, match="no stable id"):
        screen.frame(win(w=100, h=50))


def test_ppm_size():
    assert record.ppm_size(ppm(6, 3)) == (6, 3)
    with pytest.raises(ValueError):
        record.ppm_size(b"P5\n1 1\n255\n\0")


def test_a_stream_holds_the_frame_rate_and_repeats_the_last_frame_when_a_capture_fails():
    frames = [ppm(fill=1), RuntimeError("no frame"), ppm(fill=2)]
    seen = []

    def take():
        item = frames.pop(0) if frames else ppm(fill=9)
        if isinstance(item, Exception):
            raise item
        return item

    result = record.stream(take, seen.append, threading.Event(), fps=50, seconds=0.1)
    assert result.error is None
    assert seen[:3] == [ppm(fill=1), ppm(fill=1), ppm(fill=2)]
    assert 4 <= result.frames <= 6


def test_a_window_that_never_gives_a_frame_ends_the_stream_with_its_error():
    def take():
        raise RuntimeError("window gone")

    result = record.stream(take, lambda _: None, threading.Event(), fps=50, seconds=1)
    assert (result.frames, result.error) == (0, "window gone")


def test_stop_ends_a_stream():
    stop = threading.Event()
    stop.set()
    assert record.stream(ppm, lambda _: None, stop).frames == 0


def test_desk_record_streams_ppm_of_the_window(tmp_path, monkeypatch):
    from test_core import make_desk

    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**fake.clients[0], "xwayland": False, "stableId": "18000001"}
    fake_ppm_grim(monkeypatch)
    out = io.BytesIO()
    result = desk.record("battle", out, threading.Event(), fps=50, seconds=0.06)
    assert result.frames >= 2
    assert out.getvalue() == ppm() * result.frames


def test_desk_record_refuses_a_bad_rate(tmp_path, monkeypatch):
    from test_core import make_desk

    desk, *_ = make_desk(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="fps"):
        desk.record("battle", io.BytesIO(), threading.Event(), fps=0)
