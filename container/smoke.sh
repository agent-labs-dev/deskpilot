#!/bin/bash
# Smoke test of the image: start the standalone session, open a GTK app, type into its entry, read the text back from
# its accessibility tree, click OK and check the app's answer, recording the whole run. Artifacts go to $OUT.
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

# Started here rather than through `launch`, so its answer and exit status show which button the click reached.
zenity --entry --title=Smoke --text="Type below" > "$OUT/zenity.out" 2> "$OUT/zenity.log" &
app=$!
window=""
for _ in $(seq 100); do
    window=$(deskpilot --session --json windows | python3 -c '
import json, sys
print(next((w["address"] for w in json.load(sys.stdin) if w["title"] == "Smoke"), ""))')
    [ -n "$window" ] && break
    sleep 0.1
done
[ -n "$window" ] || fail "zenity did not open its window"
deskpilot --session wait-for "$window" --focused --timeout 10 > /dev/null
deskpilot --session shot "$window" -o "$OUT/before.png" > /dev/null
deskpilot --session elements "$window" | tee "$OUT/elements-before.txt"

# The entry has focus from the start: type into it, read the text back from the tree, then click OK.
deskpilot --session type --window "$window" "$TEXT"
sleep 0.5
deskpilot --session shot "$window" -o "$OUT/after.png" > /dev/null
deskpilot --session elements "$window" | tee "$OUT/elements-after.txt"
grep -qF "text='$TEXT'" "$OUT/elements-after.txt" || fail "the entry does not hold the typed text"
ok=$(grep -m1 -E " button 'OK' " "$OUT/elements-after.txt" | awk '{ print $1 }')
[ -n "$ok" ] || fail "no OK button in the tree"
# The window closing under the click can fail its read-back (#128); the app's answer is the check.
deskpilot --session click "$window" --element "$ok" || echo "smoke: click exited $?"
status=0
wait "$app" || status=$?
[ "$status" = 0 ] || fail "zenity exited $status: the click did not reach OK"
[ "$(cat "$OUT/zenity.out")" = "$TEXT" ] || fail "zenity answered '$(cat "$OUT/zenity.out")'"

sleep 1
kill -INT "$recorder"
wait "$recorder" || true
[ -s "$OUT/smoke.mp4" ] || fail "wf-recorder wrote no clip"
logs
deskpilot --session stop
echo "smoke: ok"
