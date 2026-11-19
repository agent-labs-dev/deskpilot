#!/bin/bash
# Smoke test of the image: start the standalone session, launch a GTK app, click its entry, type, read the text back
# from its accessibility tree, and record the whole run. Artifacts go to $OUT.
set -euo pipefail
OUT=${OUT:-/out}
TEXT="hello from deskpilot"

logs() {
    cp "$XDG_RUNTIME_DIR"/deskpilot/sessions/*/*.log "$OUT/" 2>/dev/null || true
    for log in "$XDG_RUNTIME_DIR"/hypr/*/hyprland.log; do [ -e "$log" ] && cp "$log" "$OUT/hyprland-full.log"; done
    find "$OUT" -mindepth 1 -user "$(id -u)" -exec chmod a+rX {} +
}
fail() {
    echo "smoke: $*" >&2
    logs
    tail -n 40 "$OUT"/*.log >&2 || true
    exit 1
}
trap 'fail "failed at line $LINENO"' ERR
field() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

exports=$(deskpilot --session env) || fail "the session did not start"
eval "$exports"
echo "session: HYPRLAND_INSTANCE_SIGNATURE=$HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
hyprctl monitors | tee "$OUT/monitors.txt"
deskpilot --session plugin status

output=$(hyprctl -j monitors | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["name"])')
wf-recorder -o "$output" -f "$OUT/smoke.mp4" > "$OUT/wf-recorder.log" 2>&1 &
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
logs
deskpilot --session stop
echo "smoke: ok"
