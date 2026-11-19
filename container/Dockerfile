# deskpilot in a container with no GPU and no parent compositor: Hyprland on its DRM backend, driving the host's vkms
# output through seatd, rendered by Mesa's software rasteriser.
FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899

# Every package from one Arch Linux Archive snapshot, so Hyprland and its headers are the same pinned build.
ARG ARCH_SNAPSHOT=2026/11/12
RUN echo "Server = https://archive.archlinux.org/repos/${ARCH_SNAPSHOT}/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist \
    && pacman -Syyuu --noconfirm --needed \
        hyprland seatd mesa grim wf-recorder wl-clipboard at-spi2-core dbus glib2 \
        gcc make pkgconf wayland libxkbcommon cairo pixman libdrm libxcb \
        python uv zenity ttf-dejavu strace \
    && pacman -Scc --noconfirm

COPY . /opt/deskpilot
RUN uv pip install --system --break-system-packages /opt/deskpilot
RUN gcc -o /usr/local/bin/eglprobe /opt/deskpilot/container/eglprobe.c -lEGL -lGLESv2 -lgbm

COPY container/spike.sh /usr/local/bin/deskpilot-spike
RUN useradd --create-home --uid 1000 agent
