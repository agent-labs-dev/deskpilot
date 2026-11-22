#!/bin/bash
# Installs packages and their dependencies from one Arch Linux Archive snapshot: install-snapshot.sh DATE PACKAGE...
# The archive's mirror carries the snapshot's databases and every package, but not the packages under the snapshot's
# own paths, so the databases come from the snapshot and each package from the archive's per-package layout, which
# both hosts serve. The first host that answers serves the whole install.
set -euo pipefail
snapshot=$1
shift
for host in archive.archlinux.org umea.archive.pkgbuild.com; do
    curl --fail --silent --show-error --connect-timeout 10 --max-time 120 --output /dev/null \
        "https://$host/repos/$snapshot/core/os/x86_64/core.db" && break
    host=
done
[ -n "$host" ] || { echo "no Arch Linux Archive host serves the $snapshot snapshot" >&2; exit 1; }
echo "Server = https://$host/repos/$snapshot/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist
pacman -Syy
pacman -Suu --needed --print --print-format '%n %f' "$@" | while read -r name file; do
    for part in "$file" "$file.sig"; do
        printf 'url = "https://%s/packages/%.1s/%s/%s"\noutput = "/var/cache/pacman/pkg/%s"\n' \
            "$host" "$name" "$name" "$part" "$part"
    done
done | curl --fail --silent --show-error --location --parallel --config -
pacman -Suu --noconfirm --needed "$@"
find /var/cache/pacman/pkg -mindepth 1 -delete
