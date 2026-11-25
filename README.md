# TSPT: Tapo SmartPlug Tray

**TSPT** (**T**apo **S**mart**P**lug **T**ray) is a lightweight native Linux taskbar indicator and **20%–80% Auto-Charge Guard** for TP-Link Tapo smart plugs (including modern **TPAP** firmware `1.4.0+` on the Tapo P105).

Built with native **GTK 3** and **AyatanaAppIndicator3**, with no Electron/Tauri bloat, no local web server, and no external daemon required.

## Features

- **Native Top-Bar Indicator:** Monochrome symbolic icons (`ON` / `OFF`) with optional live `{battery}%` panel label.
- **20%–80% Auto-Charge Guard:** Automatic battery hysteresis presets (`20–80%`, `25–80%`, `20–85%`, `30–75%`) for Linux laptops without EC/ACPI charge limits.
- **15-Minute Hardware Failsafe:** Arms and verifies an onboard 15-minute `ON` countdown inside the plug before cutting power, so a sleeping or disconnected laptop never stays unplugged.
- **GUI & CLI Setup:** Configure your Plug IP, `chmod 600` Tapo credentials, and verified hardware MAC lock from the **Setup Plug & Credentials…** tray dialog or `tspt init`.

## Quick Start

Install system GTK3 / AppIndicator packages (Ubuntu / Debian):

```bash
sudo apt update
sudo apt install -y python3-gi python3-venv gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin xdg-utils
```

Install `tspt` (`~/.local/share/tspt/venv` + `~/.local/bin/tspt`) and launch:

```bash
git clone https://github.com/jaamiiss/tspt.git
cd tspt
./scripts/install.sh --autostart
tspt
```

Or build and install the standalone Debian `.deb` package:

```bash
./packaging/build_deb.sh
sudo dpkg -i dist/tspt_1.0.0_all.deb
```

## CLI Summary

```bash
tspt                 # Launch top-bar indicator + background Auto-Charge Guard
tspt init            # Interactive setup & plug MAC verification wizard
tspt status          # Live battery + smart plug + failsafe status (--json supported)
tspt on              # Turn plug ON and cancel recovery timer
tspt off             # Arm 15-min failsafe timer and turn plug OFF
tspt check --apply   # Run a single Auto-Charge Guard evaluation cycle
```

## Usage & Configuration Guide

See [**GUIDE.md**](src/tspt/GUIDE.md) (also accessible from **Usage & Config Guide** in the top-bar menu) for:

- Top-bar menu controls walkthrough
- Full `~/.config/tspt/config.json` field reference (`plug.ip`, `plug.mac`, thresholds, `ui.editor`, labels)
- Managing `~/.config/tspt/credentials.json` (`chmod 600`)

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

Released under the [MIT License](LICENSE).
