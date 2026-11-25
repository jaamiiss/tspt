#!/usr/bin/env bash
# User-local installer for TSPT (Tapo SmartPlug Tray).
# Creates an isolated virtualenv at ~/.local/share/tspt/venv with
# --system-site-packages (for GTK3 / AyatanaAppIndicator3) plus TPAP python-kasa.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tspt"
VENV_DIR="$SHARE_DIR/venv"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

echo "==> Checking system GTK3 & AppIndicator bindings..."
python3 -c "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('AyatanaAppIndicator3', '0.1')" 2>/dev/null || {
    echo "Missing GTK3 / AyatanaAppIndicator3 bindings."
    echo "Install them on Ubuntu/Debian with:"
    echo "  sudo apt update && sudo apt install -y python3-gi python3-venv gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin"
    exit 1
}

echo "==> Creating virtualenv at $VENV_DIR (--system-site-packages)..."
mkdir -p "$SHARE_DIR" "$BIN_DIR" "$APPS_DIR"
python3 -m venv --system-site-packages "$VENV_DIR"

echo "==> Installing TPAP dependency & tspt..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements-tpap.txt"
"$VENV_DIR/bin/pip" install "$ROOT"

ln -sf "$VENV_DIR/bin/tspt" "$BIN_DIR/tspt"
cp -f "$ROOT/src/tspt/icons/tspt-on-symbolic.svg" "$SHARE_DIR/tspt.svg"

cat > "$APPS_DIR/tspt.desktop" <<EOF
[Desktop Entry]
Name=TSPT (Tapo SmartPlug Tray)
Comment=Tapo Smart Plug Taskbar Indicator & Auto-Charge Guard
Exec=$BIN_DIR/tspt
Icon=$SHARE_DIR/tspt.svg
Terminal=false
Type=Application
Categories=Utility;HardwareSettings;
Keywords=tspt;smartplug;tapo;battery;charge;tray;taskbar;
EOF

if [[ "${1:-}" == "--autostart" ]]; then
    mkdir -p "$AUTOSTART_DIR"
    cp -f "$APPS_DIR/tspt.desktop" "$AUTOSTART_DIR/tspt.desktop"
    echo "==> Enabled desktop autostart at $AUTOSTART_DIR/tspt.desktop"
fi

echo ""
echo "Installed tspt -> $BIN_DIR/tspt"
echo "Next steps:"
echo "  1. Run: tspt init"
echo "  2. Launch: tspt"
