# Recipes

A recipe is a demo written as data: one JSON file naming the window it drives, the steps deskpilot takes in it, and
optionally the app it starts and the MP4 it records. `deskpilot recipe run FILE` replays it, so a demo is edited in a
file instead of a script, and the same steps run again unchanged.

```sh
deskpilot recipe check recipes/export-an-asset.json          # validate; nothing is launched or clicked
deskpilot recipe run recipes/export-an-asset.json --record demo.mp4
deskpilot recipe schema                                      # the JSON Schema of the format, for an editor
```

A recipe is not an agent deciding what to do: every step is what the file says, in that order, aimed at the one
window it names. That is what makes it recordable the same run every time, with the beats a viewer needs to read
each step.

## Format

```json
{
  "version": 1,
  "name": "export an asset",
  "description": "what the demo shows",
  "window": "Deskpilot local demo",
  "size": "1238x758",
  "launch": ["/usr/bin/python3", "scripts/walkthrough_app.py"],
  "env": { "GDK_BACKEND": "wayland" },
  "timeout": 30,
  "steps": [
    { "action": "pause", "seconds": 1 },
    { "action": "click", "x": 81, "y": 247 },
    { "action": "shot", "out": "after.png" }
  ],
  "record": { "output": "demo.mp4", "fps": 60 }
}
```

| Field | Meaning |
| --- | --- |
| `version` | `1`. A newer version is refused rather than guessed at. |
| `name` | What the demo is; the run's report repeats it. |
| `window` | The window every step acts on: an address, a class, or part of a class or title. A name matching several windows is refused with the list. |
| `size` | Optional `WIDTHxHEIGHT` in window pixels the window must reach before the steps start. |
| `launch` | Optional command started first, from the current directory, with our environment plus `env`. The app it starts is stopped when the run ends, pass or fail. A window already open under `window` refuses the run. To aim the app at another compositor, set `WAYLAND_DISPLAY` in `env`. |
| `env` | Extra environment for the launched app only. |
| `timeout` | Seconds to wait for the window to appear (default 60). |
| `steps` | 1 to 200 steps, run in order. |
| `record` | Optional MP4: `output` (required) and `fps` (1 to 60, default 25). |

`--record MP4` on the command line overrides `record.output`, so one recipe can produce several cuts.

## Steps

The batch actions, unchanged: `click`, `move`, `scroll`, `drag`, `key`, `type`, `paste`. See
[the batch format](reference.md#batch) for their fields. On a pointer step a point is in **window pixels**, or in the
pixels of the `shot` it names, or the centre of an `element` of the app's accessibility tree the shot the desk
remembers from an earlier `deskpilot shot` never moves a recipe's point.

Three more steps are what a demo needs:

| Step | Fields | What it does |
| --- | --- | --- |
| `pause` | `seconds` (0 to 60, default 0.6) | A reading beat between two actions. It fails at once if the window has gone, rather than recording a dead screen. |
| `wait` | `window`, `timeout`, `focused`, `title`, `size`, `gone` | Wait for the window to appear, gain focus, change title or size, or close as `deskpilot wait-for` does. |
| `shot` | `out`, `max_width` | Save a still of the window: evidence of the state at that point, or the pixels to pick later points from. |

## What a run reports

```json
{
  "ok": true,
  "name": "export an asset",
  "window": { "address": "0x55d...", "class": "python3", "title": "Deskpilot local demo", "width": 1238, "height": 758 },
  "completed": 3,
  "results": [ { "index": 0, "action": "pause", "seconds": 1.0 }, ... ],
  "failed": null,
  "recording": { "path": "/.../demo.mp4", "frames": 212, "seconds": 8.48 }
}
```

A step that fails stops the run and reports its 0-based `index`, its `action`, and the same `code` a single action
would (`not_found`, `covered`, `not_visible`, `invalid`...). The recording is stopped and kept either way, so a failed
demo can be watched; the process exits 2 on failure, as a failed batch does.

## Recording

The recording is `deskpilot record` of the pinned window: frames of the window itself, with the
agent cursor the plugin draws into them, encoded to an MP4 by `ffmpeg`, which must be on `PATH`. The window's own pixels
are filmed, so nothing else on the monitor is in the clip.

It starts once the window is found, and the first step runs after the first frame, so a clip begins on the first
action. It stops after the last step, on failure too. A window that gives no frame fails the run instead of leaving an
empty MP4.

## Running one on a Hyprland machine

```sh
cd /path/to/deskpilot
uv run deskpilot recipe run recipes/export-an-asset.json --record /tmp/export.mp4
```

The example drives the walkthrough's GTK app, which needs the system `python3` with PyGObject, GTK 4 and librsvg, and
a window tiled to 1238x758 the size it gets on the walkthrough's 1280x800 output. On another layout the recipe
stops with the size it found instead of clicking blind. To get exactly that geometry without touching your own
desktop, run it inside the throwaway compositor the live checks use:

```sh
python3 scripts/nested.py start        # prints {"signature": ..., "wayland": ..., ...}
HYPRLAND_INSTANCE_SIGNATURE=<signature> WAYLAND_DISPLAY=<wayland> \
  uv run deskpilot recipe run recipes/export-an-asset.json --record /tmp/export.mp4
python3 scripts/nested.py stop <signature>
```

## The examples

`recipes/` holds recipes that run on any Hyprland machine with the app installed, each under 20 seconds and ending on
the finished state: `export-an-asset` (the GTK demo app), `calculator` (GNOME Calculator), `text-editor` (GNOME Text
Editor) and `terminal` (kitty). The ones that start a stock app give it a clean profile, so a recording never shows the
user's own history or drafts.

## Limits

- No variables, loops or conditions. A step that changes between runs belongs in the file that is run, not in a
  recipe.
- No assertion beyond `wait` and `shot`: the recipe proves the steps ran, not that the app reached a state. Read the
  stills, or check the app's own output, for that.
- Points are the recipe's own; a window the app resizes or a font that reflows moves the controls under them. Use
  `element` for a control the app reports, and `size` to pin the layout.
- A recipe never uses `--show`, `--take-focus` or `--take-pointer`: it acts where the window already is, as every other
  action does, so it will not move your pointer, your focus or your view.
