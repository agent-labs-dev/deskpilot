# deskpilot with no GPU and no parent compositor: Hyprland on its DRM backend, driving the host's vkms output, rendered
# by Mesa's software rasteriser. Run with `--device /dev/dri` after `modprobe vkms` on the host; see README.md.
FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899

# Every package from one Arch Linux Archive snapshot, so Hyprland and the headers the plugin builds against match.
ARG ARCH_SNAPSHOT=2026/11/12
RUN echo "Server = https://archive.archlinux.org/repos/${ARCH_SNAPSHOT}/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist \
    && pacman -Syyuu --noconfirm --needed \
        hyprland mesa grim wf-recorder wl-clipboard at-spi2-core dbus glib2 \
        gcc make pkgconf wayland libxkbcommon cairo pixman libdrm libxcb \
        python uv zenity ttf-dejavu \
    && pacman -Scc --noconfirm

COPY . /opt/deskpilot
RUN uv pip install --system --break-system-packages /opt/deskpilot \
    && install -m 755 /opt/deskpilot/container/entrypoint.sh /usr/local/bin/deskpilot-entrypoint \
    && useradd --create-home --uid 1000 agent \
    && install -d -m 700 -o agent -g agent /run/user/1000

USER agent
ENV DESKPILOT_STANDALONE=1 DESKPILOT_AGENT=agent XDG_RUNTIME_DIR=/run/user/1000
# The plugin and the session's input client, built once against this image's Hyprland.
RUN deskpilot plugin build
USER root
ENTRYPOINT ["deskpilot-entrypoint"]
CMD ["bash"]
