#!/bin/bash
# Smoke test of the image: start the standalone session, launch a GTK app, click its entry, type, read the text back
# from its accessibility tree, and record the whole run. Artifacts go to $OUT.
set -euo pipefail
OUT=${OUT:-/out}
TEXT="hello from deskpilot"

fail() {
    echo "smoke: $*" >&2
    cat "$XDG_RUNTIME_DIR"/deskpilot/sessions/*/worker.log "$XDG_RUNTIME_DIR"/deskpilot/sessions/*/seatd.log 2>/dev/null >&2 || true
    exit 1
}
field() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

eval "$(deskpilot --session env)" || fail "the session did not start"
echo "session: HYPRLAND_INSTANCE_SIGNATURE=$HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
hyprctl monitors | tee "$OUT/monitors.txt"
deskpilot --session plugin status

wf-recorder -o Virtual-1 -f "$OUT/smoke.mp4" > "$OUT/wf-recorder.log" 2>&1 &
recorder=$!
sleep 1

window=$(deskpilot --session --json launch zenity --entry --title=Smoke --text="Type below" | field address)
deskpilot --session wait-for "$window" --focused --timeout 10 > /dev/null || true
deskpilot --session shot "$window" -o "$OUT/before.png" > /dev/null
deskpilot --session elements "$window" | tee "$OUT/elements-before.txt"
entry=$(grep -m1 -E '\beditable\b' "$OUT/elements-before.txt" | awk '{ print $1 }')
[ -n "$entry" ] || fail "no text entry in the tree"

deskpilot --session click "$window" --element "$entry"
deskpilot --session type --window "$window" "$TEXT"
sleep 0.5
deskpilot --session shot "$window" -o "$OUT/after.png" > /dev/null
deskpilot --session elements "$window" | tee "$OUT/elements-after.txt"
grep -qF "text='$TEXT'" "$OUT/elements-after.txt" || fail "the entry does not hold the typed text"

sleep 1
kill -INT "$recorder"
wait "$recorder" || true
[ -s "$OUT/smoke.mp4" ] || fail "wf-recorder wrote no clip"
deskpilot --session stop
echo "smoke: ok"
