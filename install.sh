#!/bin/sh
# Installs sony-headphones-tray for the current user (no sudo):
#   ~/.local/share/sony-headphones-tray/   the two programs
#   ~/.local/bin/sony-headphones           the CLI
#   ~/.local/bin/sony-headphones-tray      the tray
#   ~/.config/autostart/                   starts the tray at login
# Then (re)starts the tray. ./install.sh --uninstall removes all of it.
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/sony-headphones-tray"
BIN="$HOME/.local/bin"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sony-headphones-tray.desktop"

stop_tray() { pkill -f "$PREFIX/sony_headphones_tray.py" 2>/dev/null || true; }

if [ "${1:-}" = "--uninstall" ]; then
    stop_tray
    rm -rf "$PREFIX" "$BIN/sony-headphones" "$BIN/sony-headphones-tray" "$AUTOSTART"
    echo "sony-headphones-tray removed."
    exit 0
fi

if ! /usr/bin/python3 - <<'PY'
import dbus, gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
PY
then
    echo "Missing dependencies. Debian/Ubuntu/Pop!_OS:"
    echo "  sudo apt install python3-gi python3-dbus gir1.2-ayatanaappindicator3-0.1"
    echo "Fedora: sudo dnf install python3-gobject python3-dbus libayatana-appindicator-gtk3"
    echo "Arch:   sudo pacman -S python-gobject python-dbus libayatana-appindicator"
    exit 1
fi

mkdir -p "$PREFIX" "$BIN" "$(dirname "$AUTOSTART")"
install -m 755 "$SRC/sony-headphones" "$SRC/sony_headphones_tray.py" "$PREFIX/"
install -m 644 "$SRC/LICENSE" "$PREFIX/"
printf '#!/bin/sh\nexec /usr/bin/python3 -I "%s/sony-headphones" "$@"\n' "$PREFIX" > "$BIN/sony-headphones"
printf '#!/bin/sh\nexec /usr/bin/python3 -I "%s/sony_headphones_tray.py" "$@"\n' "$PREFIX" > "$BIN/sony-headphones-tray"
chmod 755 "$BIN/sony-headphones" "$BIN/sony-headphones-tray"
sed "s|@EXEC@|$BIN/sony-headphones-tray|" "$SRC/sony-headphones-tray.desktop" > "$AUTOSTART"

stop_tray
sleep 1
setsid "$BIN/sony-headphones-tray" >/dev/null 2>&1 < /dev/null &
echo "Installed. The icon appears while your Sony headphones are connected."
echo "Starts at login from $AUTOSTART (add options such as --always-show to its Exec line)."
