#!/usr/bin/env bash
# Builds a standalone Debian package: dist/tspt_1.0.0_all.deb
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="1.0.0"
PACKAGE="tspt"
DIST="$ROOT/dist"
mkdir -p "$DIST"

STAGE="$(mktemp -d "$DIST/.stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p \
    "$STAGE/DEBIAN" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/lib/python3/dist-packages" \
    "$STAGE/usr/share/$PACKAGE/icons" \
    "$STAGE/usr/share/applications" \
    "$STAGE/etc/xdg/autostart" \
    "$STAGE/usr/lib/systemd/user" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps"

cp -a "$ROOT/src/tspt" "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE/usr/lib/python3/dist-packages" -name "__pycache__" -type d -exec rm -rf {} +
cp -a "$ROOT/src/tspt/icons/"*.svg "$STAGE/usr/share/$PACKAGE/icons/"
cp "$ROOT/src/tspt/icons/tspt-on-symbolic.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$PACKAGE.svg"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: TSPT Contributors
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1
Recommends: libnotify-bin, xdg-utils
Description: Tapo SmartPlug Tray (TSPT) - Linux taskbar indicator and charge guard
 Native GTK3 and AyatanaAppIndicator3 top-bar applet for Tapo smart plugs
 (including P105 TPAP firmware) with optional 20-80% laptop battery charge
 hysteresis and a verified 15-minute onboard failsafe countdown timer.
EOF

cat > "$STAGE/usr/bin/$PACKAGE" <<'EOF'
#!/usr/bin/env python3
import sys
from tspt.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
EOF
chmod 755 "$STAGE/usr/bin/$PACKAGE"

cat > "$STAGE/usr/share/applications/$PACKAGE.desktop" <<EOF
[Desktop Entry]
Name=TSPT (Tapo SmartPlug Tray)
Comment=Tapo Smart Plug Taskbar Indicator & Auto-Charge Guard
Exec=/usr/bin/$PACKAGE
Icon=$PACKAGE
Terminal=false
Type=Application
Categories=Utility;HardwareSettings;
Keywords=tspt;smartplug;tapo;battery;charge;tray;taskbar;
EOF
cp "$STAGE/usr/share/applications/$PACKAGE.desktop" "$STAGE/etc/xdg/autostart/$PACKAGE.desktop"

cat > "$STAGE/usr/lib/systemd/user/$PACKAGE.service" <<EOF
[Unit]
Description=TSPT (Tapo SmartPlug Tray) taskbar indicator and auto-charge guard
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/bin/$PACKAGE
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

dpkg-deb --build --root-owner-group "$STAGE" "$DIST/${PACKAGE}_${VERSION}_all.deb"
echo "Built $DIST/${PACKAGE}_${VERSION}_all.deb"
