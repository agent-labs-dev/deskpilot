#!/bin/bash
# Gives the agent user the groups of the display nodes passed in with --device, whose gids come from the host, then
# runs the command as that user: Hyprland refuses root, and opens the node directly.
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
    for node in /dev/dri/card*; do
        [ -e "$node" ] || continue
        gid=$(stat -c %g "$node")
        getent group "$gid" > /dev/null || groupadd -g "$gid" "dri$gid"
        usermod -aG "$(getent group "$gid" | cut -d: -f1)" agent
    done
    exec runuser -u agent -- "$@"
fi
exec "$@"
