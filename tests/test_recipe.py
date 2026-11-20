import json
import threading
from pathlib import Path

import pytest
from test_core import CLIENT, fake_grim, make_desk

from deskpilot import batch, cli, recipe
from deskpilot import record as recording
from deskpilot.plugin import PluginError
from deskpilot.recipe import Pause, Recipe, Shot, Wait

DOC = {
    "version": 1,
    "name": "export an asset",
    "window": "battle.net.exe",
    "steps": [
        {"action": "shot", "out": "before.png"},
        {"action": "click", "x": 100, "y": 50},
        {"action": "pause", "seconds": 0.2},
    ],
}


def kinds(plugin):
    return [call[0] for call in plugin.calls]


def test_a_recipe_reads_the_batch_actions_plus_pause_wait_and_shot():
    doc = {
        **DOC,
        "steps": [
            {"action": "click", "x": 1, "y": 2, "double": True},
            {"action": "key", "combo": "ctrl+a"},
            {"action": "type", "text": "hi"},
            {"action": "wait", "title": "Battle.net", "size": "1266x684"},
            {"action": "pause", "seconds": 1.5},
            {"action": "shot"},
        ],
    }
    parsed = recipe.parse(doc)
    assert isinstance(parsed, Recipe)
    assert [step.action for step in parsed.steps] == ["click", "key", "type", "wait", "pause", "shot"]
    assert parsed.version == 1
    assert isinstance(parsed.steps[1], batch.Key) and parsed.steps[1].combo == "ctrl+a"
    assert isinstance(parsed.steps[4], Pause) and parsed.steps[4].seconds == 1.5
    assert isinstance(parsed.steps[3], Wait) and parsed.steps[3].timeout == 30
    assert isinstance(parsed.steps[5], Shot) and parsed.steps[5].max_width > 0


def test_an_untyped_pause_has_the_reading_beat_a_demo_wants():
    assert recipe.parse({**DOC, "steps": [{"action": "pause"}]}).steps[0] == Pause(action="pause", seconds=0.6)


@pytest.mark.parametrize(
    ("raw", "fault"),
    [
        ({**DOC, "version": 2}, "invalid recipe at version"),
        ({**DOC, "steps": [{"action": "teleport"}]}, "invalid recipe at steps.0"),
        ({**DOC, "steps": [{"action": "click", "x": 1, "y": 2, "warp": True}]}, "invalid recipe at steps.0.click.warp"),
        ({**DOC, "steps": []}, "invalid recipe at steps"),
        ({**DOC, "window": ""}, "invalid recipe at window"),
        ({**DOC, "colour": "red"}, "invalid recipe at colour"),
    ],
)
def test_a_bad_recipe_is_refused_naming_the_first_fault(raw, fault):
    with pytest.raises(ValueError, match=fault):
        recipe.parse(raw)


def test_the_schema_names_every_step_action_and_the_one_version():
    schema = recipe.schema()
    assert schema["properties"]["version"]["const"] == 1
    step = json.dumps(schema["properties"]["steps"])
    assert all(f'"{name}"' in step for name in ("click", "drag", "paste", "pause", "wait", "shot"))


def test_loading_a_file_reads_it_whole_and_a_missing_file_is_named(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(DOC))
    assert recipe.load(path).name == "export an asset"
    absent = tmp_path / "absent.json"
    with pytest.raises(ValueError, match=f"cannot read {absent}"):
        recipe.load(absent)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="is not valid JSON"):
        recipe.load(path)


def test_run_replays_the_steps_on_the_pinned_window_in_window_pixels(tmp_path, monkeypatch):
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    fake_grim(monkeypatch, (1266, 684))
    steps = [
        {"action": "shot", "out": str(tmp_path / "before.png")},
        {"action": "click", "x": 100, "y": 50},
        {"action": "pause", "seconds": 0},
        {"action": "key", "combo": "Return"},
    ]
    result = recipe.run(desk, recipe.parse({**DOC, "steps": steps})).to_dict()
    assert result["ok"] and result["completed"] == 4 and result["failed"] is None
    assert result["window"]["address"] == "0xabc" and result["recording"] is None
    assert [row["action"] for row in result["results"]] == ["shot", "click", "pause", "key"]
    assert Path(result["results"][0]["path"]).is_file()
    # Window pixels, not the pixels of the 0.5-scale shot the desk remembers: the point is 100 right of the window.
    assert (result["results"][1]["x"], result["results"][1]["y"]) == (2527 + 100, 966 + 50)
    assert kinds(plugin) == ["path", "keys"]
    assert not any(call[0] == "dispatch" for call in fake.calls), "a recipe never focuses or moves the pointer"
    assert inp.events == []


def test_a_window_replaced_under_the_pinned_address_stops_the_run_with_partial_results(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake_grim(monkeypatch, (1266, 684))
    path = plugin.path

    def replace_window(*args):
        reply = path(*args)
        fake.clients[0] = {**CLIENT, "pid": 99}  # Hyprland reused the address for another process
        return reply

    plugin.path = replace_window
    steps = [{"action": "click", "x": 10, "y": 10}, {"action": "pause", "seconds": 5}]
    result = recipe.run(desk, recipe.parse({**DOC, "steps": steps})).to_dict()
    assert not result["ok"] and result["completed"] == 1
    assert result["failed"] == {
        "index": 1,
        "action": "pause",
        "code": "not_found",
        "message": result["failed"]["message"],
    }


def test_a_pause_fails_at_once_when_the_window_has_gone(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    window = desk.window("battle.net.exe")
    fake.clients.clear()  # the app died while the beat was running
    monkeypatch.setattr(desk, "wait_for", lambda *args, **kwargs: window)
    steps = [{"action": "pause", "seconds": 5}]
    result = recipe.run(desk, recipe.parse({**DOC, "steps": steps})).to_dict()
    assert not result["ok"] and result["failed"]["index"] == 0 and result["completed"] == 0


def fake_film(desk, monkeypatch, frames=3, order=None):
    """`Desk.record` without a compositor: it films until stopped, as the real one does, and says when it began."""
    calls = []

    def record(query, out, stop, fps, seconds=None, hidden=False, started=None):
        calls.append((query, out, fps))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        if order is not None:
            order.append("first frame")
        assert started is not None
        assert started is not None
        started.set()
        stop.wait(5)
        return recording.Result(frames, 0.12)

    monkeypatch.setattr(desk, "record", record)
    return calls


def fake_launch(desk, fake, monkeypatch, fail=None):
    """`Desk.launch` and `Desk.stop` without a compositor: the app's window opens when it is launched."""
    commands: list[list[str]] = []
    stops: list[bool] = []

    def launch(command, timeout=60):
        commands.append(command)
        if fail:
            raise fail
        return desk.window(fake.clients[0]["address"])

    def stop():
        stops.append(True)

    monkeypatch.setattr(desk, "launch", launch)
    monkeypatch.setattr(desk, "stop", stop)
    return {"launch": commands, "stop": stops}


def test_run_films_the_pinned_window_through_desk_record_and_stops_what_it_launched(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    order = []
    calls = fake_film(desk, monkeypatch, order=order)
    launched = fake_launch(desk, fake, monkeypatch)
    real_do = recipe._do
    monkeypatch.setattr(recipe, "_do", lambda *a: order.append("step") or real_do(*a))
    output = tmp_path / "out" / "demo.mp4"
    doc = {**DOC, "launch": ["walkthrough_app.py"], "record": {"output": str(output), "fps": 30}}
    result = recipe.run(desk, recipe.parse({**doc, "steps": [{"action": "pause", "seconds": 0}]})).to_dict()
    assert result["ok"]
    assert result["recording"] == {"path": str(output), "frames": 3, "seconds": 0.12}
    assert calls == [("0xabc", output, 30)], "the pinned window's address, at the recipe's fps"
    assert order == ["first frame", "step"], "no step runs before the first frame"
    assert launched == {"launch": [["walkthrough_app.py"]], "stop": [True]}, "Desk.launch, then stopped on the way out"


def test_a_failed_step_keeps_the_recording(tmp_path, monkeypatch):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    fake_film(desk, monkeypatch)
    monkeypatch.setattr(desk, "click", lambda *a, **k: (_ for _ in ()).throw(ValueError("outside the window")))
    steps = [{"action": "click", "x": 1, "y": 1}]
    result = recipe.run(desk, recipe.parse({**DOC, "steps": steps}), tmp_path / "kept.mp4").to_dict()
    assert not result["ok"] and result["failed"]["code"] == "invalid"
    assert result["recording"]["path"] == str(tmp_path / "kept.mp4") and (tmp_path / "kept.mp4").is_file()


def test_a_recording_that_captured_nothing_fails_the_run(tmp_path, monkeypatch):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)

    def record(*args, started=None, **kwargs):
        raise RuntimeError("no frame from the window")

    monkeypatch.setattr(desk, "record", record)
    with pytest.raises(RuntimeError, match="no frame from the window"):
        recipe.run(desk, recipe.parse(DOC), tmp_path / "none.mp4")


def test_a_launch_goes_through_desk_launch_with_its_env_in_front_and_runs_hidden(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    launched = fake_launch(desk, fake, monkeypatch)
    seen = {}
    monkeypatch.setattr(desk, "click", lambda *a, **k: seen.update(k) or (1, 2))
    doc = {**DOC, "launch": ["app", "--flag"], "env": {"A": "1"}, "steps": [{"action": "click", "x": 1, "y": 1}]}
    assert recipe.run(desk, recipe.parse(doc)).ok
    assert launched["launch"] == [["env", "A=1", "app", "--flag"]]
    assert seen["hidden"] is True, "a launched app is out of view, so a recipe never takes the user's focus"


def test_a_launch_waits_out_the_agent_of_the_run_before_it_still_stopping(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    launched = fake_launch(desk, fake, monkeypatch)
    real = desk.launch
    refusals = [PluginError("agent DESKPILOT is stopping", "busy")]

    def launch(command, timeout=60):
        if refusals:
            raise refusals.pop()
        return real(command, timeout)

    monkeypatch.setattr(desk, "launch", launch)
    monkeypatch.setattr(recipe, "BUSY_RETRY", 0)
    steps = [{"action": "pause", "seconds": 0}]
    assert recipe.run(desk, recipe.parse({**DOC, "launch": ["app"], "steps": steps})).ok
    assert len(launched["launch"]) == 1


def test_a_launch_that_fails_still_stops_the_agent_and_says_why(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    launched = fake_launch(desk, fake, monkeypatch, fail=RuntimeError("app exited 2 before opening a window"))
    with pytest.raises(RuntimeError, match="exited 2"):
        recipe.run(desk, recipe.parse({**DOC, "launch": ["app"]}))
    assert launched["stop"] == [True]


def test_the_first_window_of_a_launch_must_be_the_one_the_recipe_names(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake_launch(desk, fake, monkeypatch)
    with pytest.raises(recipe.WindowNotFound):
        recipe.run(desk, recipe.parse({**DOC, "window": "other-app", "launch": ["app"]}))


def test_a_move_in_a_launching_recipe_is_refused_since_hidden_shows_no_cursor():
    with pytest.raises(ValueError, match="move only shows the agent cursor"):
        recipe.parse({**DOC, "launch": ["app"], "steps": [{"action": "move", "x": 1, "y": 1}]})


def test_a_shot_refuses_a_window_replaced_under_the_pinned_address(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake_grim(monkeypatch, (1266, 684))
    wait_for = desk.wait_for

    def pin_then_replace(*args, **kwargs):
        pinned = wait_for(*args, **kwargs)
        fake.clients[0] = {**CLIENT, "pid": 99}  # Hyprland reused the address for another process
        return pinned

    monkeypatch.setattr(desk, "wait_for", pin_then_replace)
    result = recipe.run(desk, recipe.parse({**DOC, "steps": [{"action": "shot"}]})).to_dict()
    assert result["failed"]["code"] == "not_found" and result["completed"] == 0


def test_the_cli_checks_a_recipe_without_a_compositor_and_exits_2_on_a_bad_one(tmp_path, capsys):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(DOC))
    assert cli.main(["--json", "recipe", "check", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["name"] == "export an asset" and printed["steps"][1]["action"] == "click"
    assert cli.main(["--json", "recipe", "check", str(tmp_path / "absent.json")]) == 2
    assert "invalid" in capsys.readouterr().err


def test_the_cli_exports_the_recipe_schema(capsys):
    assert cli.main(["--json", "recipe", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Recipe"


def test_the_cli_refuses_arguments_a_recipe_action_would_ignore(tmp_path, capsys):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(DOC))
    assert cli.main(["--json", "recipe", "check", str(path), "--record", "x.mp4"]) == 2
    assert cli.main(["--json", "recipe", "schema", str(path)]) == 2
    assert cli.main(["--json", "recipe", "schema", "--record", "x.mp4"]) == 2
    assert "takes no file" in capsys.readouterr().err


def test_a_launched_window_replaced_before_the_pin_stops_the_run(tmp_path, monkeypatch):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    fake_launch(desk, fake, monkeypatch)
    wait_for = desk.wait_for

    def replaced(*args, **kwargs):
        found = wait_for(*args, **kwargs)
        fake.clients[0] = {**CLIENT, "pid": 99}  # Hyprland reused the launched window's address
        return found

    monkeypatch.setattr(desk, "wait_for", replaced)
    with pytest.raises(recipe.WindowNotFound):
        recipe.run(desk, recipe.parse({**DOC, "launch": ["app"]}))


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_a_wait_on_the_pinned_window_ends_when_it_is_lost_or_replaced(tmp_path, monkeypatch, replacement, explicit):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        fake.clients[:] = [{**CLIENT, "pid": 99, "title": "Saved"}] if replacement else []

    monkeypatch.setattr(recipe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(recipe.time, "sleep", sleep)
    step = {"action": "wait", "title": "Saved", "timeout": 5} | ({"window": CLIENT["address"]} if explicit else {})
    result = recipe.run(desk, recipe.parse({**DOC, "steps": [step]})).to_dict()
    assert result["failed"]["code"] == "not_found" and clock[0] < 5


def test_a_recording_that_ends_at_its_limit_fails_the_run_even_while_it_finalizes(tmp_path, monkeypatch):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    ended = threading.Event()

    def record(query, out, stop, fps, seconds=None, hidden=False, started=None):
        out.write_bytes(b"mp4")
        assert started is not None
        started.set()
        stop.set()  # the recorder's capture is over, as `stream` does at its limit, and the file is still finalizing
        ended.wait(0.3)
        return recording.Result(5, 600.0)

    monkeypatch.setattr(desk, "record", record)
    steps = [{"action": "pause", "seconds": 0.3}, {"action": "pause", "seconds": 0}]
    try:
        result = recipe.run(desk, recipe.parse({**DOC, "steps": steps}), tmp_path / "long.mp4").to_dict()
    finally:
        ended.set()
    assert not result["ok"] and "limit" in result["failed"]["message"]


@pytest.mark.parametrize("explicit", [False, True])
def test_a_wait_refuses_a_replacement_its_own_poll_returns(tmp_path, monkeypatch, explicit):
    desk, fake, _, _ = make_desk(tmp_path, monkeypatch)
    wait_for = desk.wait_for
    calls = []

    def replaced_at_the_first_poll(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:  # the first call pins the window, the second is the wait's first poll
            fake.clients[0] = {**CLIENT, "pid": 99, "title": "Saved"}
        return wait_for(*args, **kwargs)

    monkeypatch.setattr(desk, "wait_for", replaced_at_the_first_poll)
    step = {"action": "wait", "title": "Saved", "timeout": 1} | ({"window": CLIENT["address"]} if explicit else {})
    result = recipe.run(desk, recipe.parse({**DOC, "steps": [step]})).to_dict()
    assert result["failed"]["code"] == "not_found"
