import threading

import pytest

from deskpilot import record


@pytest.mark.parametrize("capture_seconds", [0.08, 0.2])
def test_slow_captures_repeat_frames_without_extending_the_recording(monkeypatch, capture_seconds):
    clock = [0.0]
    captured = []
    written = []
    stop = threading.Event()

    def wait(delay):
        clock[0] += max(0, delay)
        return False

    def take():
        clock[0] += capture_seconds
        frame = str(len(captured)).encode()
        captured.append(frame)
        return frame

    monkeypatch.setattr(record.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stop, "wait", wait)
    result = record.stream(take, written.append, stop, fps=25, seconds=1)

    assert result.seconds <= 1 + capture_seconds + 1e-9, "overdue frame slots must not each start another capture"
    assert result.frames == len(written) == 25, "the constant-rate stream must still cover the requested second"
    assert len(captured) < len(written), "missed frame slots must reuse the last captured frame"
