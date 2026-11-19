"""Saved recipes: the steps of a demo, replayed in order and recorded to an MP4.

A recipe is one JSON file: the window a demo drives, the steps deskpilot takes in it, and optionally the app it
starts and the MP4 it records. `deskpilot recipe run FILE --record demo.mp4` replays it, so a demo is edited as data
rather than as a script, and the same steps run again unchanged. Like a `batch`, the whole file is validated before
anything is launched, looked up or clicked, and a step stops the run at the first expected failure.

The steps are the batch actions (`click`, `move`, `scroll`, `drag`, `key`, `type`, `paste`) plus three a demo needs:
`pause` for a reading beat, `wait` for the window to look as expected, and `shot` for a still of it. A point is in
window pixels, or in the pixels of the shot it names, or the centre of an element of the app's accessibility tree.
A recipe is not a batch: it may hold more steps and a window whose identity is pinned once, so a window Hyprland
replaces under the same address stops the run instead of receiving the rest of the demo's clicks.

Recording is `Desk.record`, the same window film `deskpilot record` makes: frames of the window with the agent cursor
the plugin draws into them. It starts before the first step and stops after the last, on failure too, so a failed
recipe still leaves the recording of what went wrong.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import batch, screen
from . import record as recording
from .desk import EXPECTED, error_code
from .hypr import Window, WindowNotFound

if TYPE_CHECKING:
    from .desk import Desk

# More than a batch's 32: a demo pauses, shoots and waits between its clicks.
MAX_STEPS = 200
# How often a pause checks the window is still the one pinned, so an app that died fails the run at once.
PAUSE_TICK = 0.25


class _Step(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Pause(_Step):
    """A deliberate beat, so a viewer can read what the last step did before the next one lands."""

    action: Literal["pause"]
    seconds: float = Field(0.6, ge=0, le=60, description="seconds to wait, checking the window is still there")


class Wait(_Step):
    """Wait for a window that matches, as `deskpilot wait-for` does: it appears, gains focus, changes title or size."""

    action: Literal["wait"]
    window: str | None = Field(None, min_length=1, description="a window other than the recipe's own")
    timeout: float = Field(30.0, gt=0, le=600)
    focused: bool = False
    title: str | None = Field(None, description="wait until its title contains this")
    size: str | None = Field(None, pattern=r"^\d+x\d+$", description="wait until it is WIDTHxHEIGHT window pixels")
    gone: bool = False


class Shot(_Step):
    """Save a screenshot of the window as evidence, or to pick the points of later steps from."""

    action: Literal["shot"]
    out: str | None = Field(None, min_length=1, description="path to write the PNG to; a shot id by default")
    max_width: int = Field(screen.DEFAULT_MAX_WIDTH, ge=1, description="scale a wider window down to this width")


# The batch actions unchanged, so a recipe and a one-window batch read the same, plus the three a demo adds.
Step = Annotated[
    batch.Click | batch.Move | batch.Scroll | batch.Drag | batch.Key | batch.Type | batch.Paste | Pause | Wait | Shot,
    Field(discriminator="action"),
]


class Record(_Step):
    """Film the window, with the agent cursor, from before the first step until after the last."""

    output: str = Field(min_length=1, description="the MP4 to write, made a directory if needed")
    fps: int = Field(recording.FPS, ge=1, le=60)


class Recipe(_Step):
    version: Literal[1] = 1
    name: str = Field(min_length=1, description="what the demo is, for the run's report")
    description: str | None = None
    window: str = Field(min_length=1, description="the window every step acts on: address, class or part of a title")
    launch: list[str] | None = Field(None, min_length=1, description="an app to start before the first step")
    env: dict[str, str] = Field(default_factory=dict, description="environment for the launched app, on top of ours")
    timeout: float = Field(60.0, gt=0, le=600, description="seconds to wait for the window to appear")
    size: str | None = Field(None, pattern=r"^\d+x\d+$", description="the window's size in window pixels, waited for")
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    record: Record | None = None


def _size(text: str | None) -> tuple[int, int] | None:
    if text is None:
        return None
    w, _, h = text.partition("x")
    return int(w), int(h)


def parse(raw: Any) -> Recipe:
    """The recipe from untrusted JSON, or a ValueError (code `invalid`) naming the first fault."""
    try:
        return Recipe.model_validate(raw)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ValueError(f"invalid recipe at {where or 'document'}: {first['msg']}") from None


def load(path: Path) -> Recipe:
    """Read and validate a recipe file. Nothing is launched or clicked until this returns."""
    try:
        text = path.read_text()
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error.strerror}") from error
    try:
        raw = json.loads(text)
    except ValueError as error:  # bad JSON, or bytes that are not UTF-8
        raise ValueError(f"{path} is not valid JSON: {error}") from error
    return parse(raw)


def schema() -> dict:
    """The JSON Schema of the recipe format, for an editor or a check in another tool."""
    return Recipe.model_json_schema()


@dataclass(frozen=True)
class Failure:
    """The step that failed, by its 0-based index in `steps`, and the code and message it failed with."""

    index: int
    action: str
    code: str
    message: str


@dataclass
class RunResult:
    name: str = ""
    window: dict = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)
    failed: Failure | None = None
    recording: dict | None = None

    @property
    def ok(self) -> bool:
        return self.failed is None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "name": self.name,
            "window": self.window,
            "completed": len(self.results),
            "results": self.results,
            "failed": None if self.failed is None else vars(self.failed),
            "recording": self.recording,
        }


class _Film:
    """`Desk.record` of the pinned window on a thread of its own, from its first frame until `stop`."""

    def __init__(self, desk: Desk, address: str, path: Path, fps: int) -> None:
        self.path = path.expanduser().resolve()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._result: recording.Result | None = None
        self._error: Exception | None = None

        def film() -> None:
            try:
                self._result = desk.record(address, self.path, self._stop, fps, started=self._started)
            except Exception as caught:  # re-raised by finish(), on the thread that owns the run
                self._error = caught

        self._thread = threading.Thread(target=film, name="recipe-film", daemon=True)
        self._thread.start()
        # No step runs before the first frame, so the clip starts on the first action rather than somewhere in it.
        while not self._started.wait(0.1):
            if not self._thread.is_alive():
                self.finish()
                raise RuntimeError(f"the recording of {address} stopped before its first frame")

    def finish(self) -> dict:
        """Stop filming and report the clip. A recording that captured nothing raises the capture's own error."""
        self._stop.set()
        self._thread.join()
        if self._error:
            raise self._error
        assert self._result is not None
        self._result.raise_if_empty()
        return {"path": str(self.path), **self._result.to_dict()}


def _spawn(recipe: Recipe, stderr: IO[bytes]) -> subprocess.Popen:
    """Start the recipe's app the way the user's own shell would, in its own process group so stop ends its children."""
    command = [os.path.expanduser(part) for part in recipe.launch or []]
    return subprocess.Popen(
        command,
        env={**os.environ, **recipe.env},
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        start_new_session=True,
    )


def _stop_app(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _await_window(desk: Desk, recipe: Recipe, app: subprocess.Popen | None, stderr: IO[bytes]) -> Window:
    """The recipe's window, as soon as it is there at its size. An app that failed before showing one fails the run
    with its own exit status and last words, instead of the run waiting out the whole timeout. A launcher that hands
    off and exits 0 is fine: its window is still waited for."""
    deadline = time.monotonic() + recipe.timeout
    while True:
        try:
            window = desk.wait_for(recipe.window, timeout=min(1.0, max(0.0, deadline - time.monotonic())),
                                   size=_size(recipe.size))  # fmt: skip
            assert window is not None
            return window
        except TimeoutError:
            if app is not None and app.poll():
                stderr.seek(0)
                said = stderr.read().decode(errors="replace").strip().splitlines()[-3:]
                command = (recipe.launch or [""])[0]
                raise ValueError(
                    f"{command} exited with status {app.returncode} before {recipe.window!r} appeared"
                    + (": " + " / ".join(said) if said else "")
                ) from None
            if time.monotonic() >= deadline:
                raise


def run(desk: Desk, recipe: Recipe, record: Path | None = None) -> RunResult:
    """Replay the recipe, recording it too when the recipe or `record` names an MP4.

    The window is looked up and pinned before anything is recorded, every step is aimed at that pinned window, and
    the app the recipe launched is stopped on the way out, as is the recording.
    """
    result = RunResult(name=recipe.name)
    app = film = None
    with tempfile.TemporaryFile() as stderr:
        try:
            if recipe.launch:
                app = _spawn(recipe, stderr)
            window = _await_window(desk, recipe, app, stderr)
            result.window = window.to_dict()
            output = record or (Path(recipe.record.output) if recipe.record else None)
            if output is not None:
                film = _Film(desk, window.address, output, recipe.record.fps if recipe.record else recording.FPS)
            for index, step in enumerate(recipe.steps):
                try:
                    result.results.append({"index": index, "action": step.action, **_do(desk, step, recipe, window)})
                except EXPECTED as error:
                    result.failed = Failure(index, step.action, error_code(error), str(error))
                    return result
        finally:
            # The clip is kept whatever happened, so a failed demo can be watched; the app it started is not.
            try:
                if film is not None:
                    result.recording = film.finish()
            finally:
                _stop_app(app)
    return result


def _pinned(desk: Desk, window: Window) -> Window:
    """The window at the pinned address, refused if the window now there is not the one the run started on."""
    now = desk.window(window.address)
    if (now.pid, now.stable_id) != (window.pid, window.stable_id):
        raise WindowNotFound(f"window {window.address} is no longer the {window.cls} the recipe started on")
    return now


def _do(desk: Desk, step: Any, recipe: Recipe, window: Window) -> dict:
    """One step through the Desk action of the same name, aimed at the pinned window."""
    if isinstance(step, Pause):
        _pause(desk, window, step.seconds)
        return {"seconds": step.seconds}
    if isinstance(step, Wait):
        target = step.window or window.address
        win = desk.wait_for(target, step.timeout, step.gone, step.focused, step.title, _size(step.size))
        return {"window": None if win is None else win.to_dict()}
    if isinstance(step, Shot):
        return desk.shot(window.address, Path(step.out).expanduser() if step.out else None, step.max_width).to_dict()
    _pinned(desk, window)
    address = window.address
    # A point is in window pixels, or in the shot the step names: a shot taken earlier never moves it.
    scale = None if getattr(step, "shot", None) else 1.0
    match step:
        case batch.Click():
            point = desk.click(
                address, step.x, step.y, step.button, 2 if step.double else 1, scale, shot=step.shot,
                element=step.element,
            )  # fmt: skip
        case batch.Move():
            point = desk.move(address, step.x, step.y, scale, shot=step.shot, element=step.element)
        case batch.Scroll():
            point = desk.scroll(address, step.x, step.y, step.dy, scale, shot=step.shot, element=step.element)
        case batch.Drag():
            point = desk.drag(address, (step.x1, step.y1), (step.x2, step.y2), step.button, step.duration,
                              scale, shot=step.shot)  # fmt: skip
        case batch.Key():
            desk.key(step.combo, address, hold=step.hold)
            return {}
        case batch.Type():
            desk.type(step.text, address, step.delay)
            return {}
        case batch.Paste():
            desk.paste(step.text, address, combo=step.keys)
            return {}
        case _:  # pragma: no cover - every member of Step is above, and parse refuses anything else
            raise AssertionError(step.action)
    return {"x": point[0], "y": point[1]}


def _pause(desk: Desk, window: Window, seconds: float) -> None:
    """Wait, failing at once if the app or its window has gone, rather than recording a dead screen."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(PAUSE_TICK, remaining))
        _pinned(desk, window)
