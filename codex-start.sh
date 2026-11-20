#!/bin/bash
set -eu
export DISPLAY="${CODEX_DISPLAY:-:88}"
export HOME="${CODEX_HOME:-/data/codex}"
export XDG_CONFIG_HOME="$HOME/.config" XDG_CACHE_HOME="${CODEX_CACHE_HOME:-/tmp/codex-cache}" XDG_DATA_HOME="$HOME/.local/share"
mkdir -p "$HOME/.cache" "$XDG_CACHE_HOME/codex-runtimes"
# The official app uses this path directly, ignoring XDG_CACHE_HOME. Keep its
# downloadable runtime off the small persistent volume without touching config.
if [ -d "$HOME/.cache/codex-runtimes" ] && [ ! -L "$HOME/.cache/codex-runtimes" ]; then
  cp -a "$HOME/.cache/codex-runtimes/." "$XDG_CACHE_HOME/codex-runtimes/"
  rm -rf "$HOME/.cache/codex-runtimes"
fi
ln -sfn "$XDG_CACHE_HOME/codex-runtimes" "$HOME/.cache/codex-runtimes"
IDE="${CODEX_DESKTOP_BIN:-/usr/lib/chatgpt/ChatGPT}"
NOVNC="${NOVNC_DIR:-/usr/share/novnc}"
if [ "$(id -u)" = 0 ]; then
  chown -R codexuser:codexuser "$HOME" "$XDG_CACHE_HOME"
  as_desktop() { runuser -u codexuser -- env HOME="$HOME" XDG_CONFIG_HOME="$XDG_CONFIG_HOME" XDG_CACHE_HOME="$XDG_CACHE_HOME" XDG_DATA_HOME="$XDG_DATA_HOME" DISPLAY="$DISPLAY" "$@"; }
else
  as_desktop() { "$@"; }
fi
cleanup() { kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT TERM INT
as_desktop Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -ac &
for i in $(seq 1 50); do [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ] && break; sleep .1; done
as_desktop openbox &
as_desktop x11vnc -display "$DISPLAY" -localhost -no6 -nopw -forever -shared -rfbport 5988 &
as_desktop x11vnc -display "$DISPLAY" -localhost -no6 -passwdfile "$HOME/.vnc_password" -forever -shared -rfbport 5902 &
as_desktop "$IDE" --no-sandbox --disable-gpu --disable-dev-shm-usage --user-data-dir="$HOME/.config/codex-desktop" &
as_desktop websockify --web "$NOVNC" "127.0.0.1:${CODEX_DESKTOP_HTTP_PORT:-6088}" 127.0.0.1:5988 &
wait -n
exit 1
