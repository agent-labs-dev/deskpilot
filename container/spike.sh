#!/bin/bash
# Phase 0 spike: Hyprland on the vkms DRM output inside a container, then capture, plugin, elements, click and a clip.
# Run as root; it starts seatd and continues as the agent user. Artifacts go to $OUT.
set -uo pipefail
OUT=${OUT:-/out}

if [ "$(id -u)" = 0 ]; then
    mkdir -p "$OUT" && chmod 777 "$OUT"
    ls -l /dev/dri /sys/class/drm/ || true
    for c in /sys/class/drm/card*; do
        [ -e "$c/device" ] && echo "$c $(readlink -f "$c/device")"
    done
    SEATD_VTBOUND=0 seatd -g agent -l debug > "$OUT/seatd.log" 2>&1 &
    for _ in $(seq 50); do [ -S /run/seatd.sock ] && break; sleep 0.1; done
    ls -l /run/seatd.sock
    exec runuser -u agent -- env OUT="$OUT" "$0"
fi

export XDG_RUNTIME_DIR=/tmp/runtime-agent LIBSEAT_BACKEND=seatd
mkdir -p -m 700 "$XDG_RUNTIME_DIR"
card=""
for c in /sys/class/drm/card*; do
    case "$c" in *-*) continue ;; esac
    [ "$(basename "$(readlink -f "$c/device")")" = vkms ] && card=/dev/dri/$(basename "$c")
done
echo "vkms card: $card"
export AQ_DRM_DEVICES=$card

mkdir -p ~/.config/hypr
cat > ~/.config/hypr/hyprland.lua <<'EOF'
hl.monitor({ output = "", mode = "1280x800", position = "0x0", scale = 1 })
hl.config({
    misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
    animations = { enabled = false },
})
EOF
EGL_LOG_LEVEL=debug AQ_TRACE=1 HYPRLAND_NO_SD_NOTIFY=1 Hyprland -c ~/.config/hypr/hyprland.lua > "$OUT/hyprland.log" 2>&1 &
hypr=$!
for _ in $(seq 150); do
    sig=$(ls "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)
    [ -n "$sig" ] && [ -S "$XDG_RUNTIME_DIR/hypr/$sig/.socket.sock" ] && break
    kill -0 $hypr 2>/dev/null || break
    sleep 0.1
done
if ! kill -0 $hypr 2>/dev/null; then
    echo "Hyprland exited"
    cp "$XDG_RUNTIME_DIR"/hypr/*/hyprland.log "$OUT/hyprland-full.log"
    tail -80 "$OUT/hyprland-full.log"
    cp ~/.cache/hyprland/hyprlandCrashReport*.txt "$OUT/" 2>/dev/null
    cat ~/.cache/hyprland/hyprlandCrashReport*.txt 2>/dev/null | head -80
    ls -l /usr/lib/dri /usr/lib/gbm 2>/dev/null
    exit 1
fi
export HYPRLAND_INSTANCE_SIGNATURE=$sig
export WAYLAND_DISPLAY=$(ls "$XDG_RUNTIME_DIR" | grep -m1 '^wayland-[0-9]*$')
sleep 1
hyprctl dismissnotify
hyprctl monitors | tee "$OUT/monitors.txt"
grep -iE "render|gbm|egl|allocator|drm|vkms|llvmpipe" "$OUT/hyprland.log" | head -40

dbus-daemon --session --fork --print-address=1 --address="unix:path=$XDG_RUNTIME_DIR/bus" > /dev/null
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
/usr/lib/at-spi-bus-launcher --launch-immediately &
sleep 1
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
    --method org.freedesktop.DBus.Properties.Set org.a11y.Status IsEnabled '<true>'
/usr/lib/at-spi2-registryd --use-gnome-session=false &
sleep 1

wf-recorder -o "$(hyprctl -j monitors | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["name"])')" \
    -f "$OUT/clip.mp4" > "$OUT/wf-recorder.log" 2>&1 &
rec=$!

GDK_BACKEND=wayland zenity --entry --title=Smoke --text="Type here" > "$OUT/zenity.out" 2> "$OUT/zenity.err" &
app=$!
for _ in $(seq 100); do hyprctl -j clients | grep -q '"Smoke"' && break; sleep 0.1; done
hyprctl clients | tee "$OUT/clients.txt"

grim "$OUT/screen.png" && echo "grim screen ok"
addr=$(hyprctl -j clients | python3 -c 'import json,sys; print(next(c["address"] for c in json.load(sys.stdin) if c["title"]=="Smoke"))')
stable=$(hyprctl -j clients | python3 -c 'import json,sys; print(next(c.get("stableId","") for c in json.load(sys.stdin) if c["title"]=="Smoke"))')
echo "window $addr stable $stable"
grim -T "$stable" "$OUT/toplevel.png" && echo "grim -T ok"
python3 - "$OUT/toplevel.png" "$OUT/screen.png" <<'EOF'
import sys
from PIL import Image
for p in sys.argv[1:]:
    try:
        im = Image.open(p).convert("RGB")
    except OSError as e:
        print(p, "unreadable", e); continue
    colours = len(set(im.getdata()))
    print(p, im.size, "distinct colours:", colours, "extrema:", im.getextrema())
EOF

deskpilot plugin load && echo "plugin loaded"
deskpilot plugin status
deskpilot shot Smoke -o "$OUT/shot.png"
deskpilot elements Smoke | tee "$OUT/elements.txt"
entry=$(grep -m1 -iE " (text|entry)" "$OUT/elements.txt" | awk '{print $1}')
ok=$(grep -m1 -E "button 'OK'" "$OUT/elements.txt" | awk '{print $1}')
echo "entry=$entry ok=$ok"
deskpilot click Smoke --element "$entry" && echo "click entry ok"
deskpilot type --window Smoke "hello deskpilot" && echo "type ok"
sleep 0.5
deskpilot shot Smoke -o "$OUT/typed.png"
deskpilot click Smoke --element "$ok" && echo "click OK ok"
for _ in $(seq 50); do kill -0 $app 2>/dev/null || break; sleep 0.1; done
echo "zenity printed: $(cat "$OUT/zenity.out")"
kill -INT $rec; wait $rec
ls -l "$OUT"
kill $hypr
