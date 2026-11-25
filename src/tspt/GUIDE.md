# TSPT (Tapo SmartPlug Tray) - Usage & Configuration Guide

## 1. Using the Top-Bar Menu

- **Battery: XX% (Status) · X.XW**: Live Linux `sysfs` battery level, charging state, and power draw (refreshes every 3 seconds).
- **Plug: On / Plug: Off (15m failsafe)**: Current cached state of your Tapo smart plug.
- **Turn On**: Turns the smart plug ON immediately and cancels any active failsafe countdown.
- **Turn Off**: Verifies a 15-minute onboard ON failsafe timer on the plug first, then turns relay power OFF.
- **Refresh Status**: Queries the smart plug live over LAN and updates the menu.
- **Auto-Charge: On / Off**: Pauses or resumes automatic battery charge hysteresis.
- **Threshold Presets**: Switches `turn_on` / `turn_off` between `20%–80%`, `25%–80%`, `20%–85%`, and `30%–75%`.
- **Setup Plug & Credentials…**: Opens the native GUI setup window to enter or update your Plug IP, Tapo Email, and Password (`chmod 600` saved automatically) and auto-verify your plug's MAC address with no terminal required.
- **Edit Config**: Opens `~/.config/tspt/config.json` in your text editor. Saving the file hot-reloads settings automatically without restarting `tspt`.
- **View Activity Log**: Opens `~/.local/state/tspt/history.log`.
- **Usage & Config Guide**: Opens this quick-reference guide.
- **Quit**: Exits the taskbar indicator.

---

## 2. Editing `~/.config/tspt/config.json`

You can run `tspt init` in a terminal for an interactive setup wizard, or edit `~/.config/tspt/config.json` directly:

```json
{
  "plug": {
    "ip": "192.168.1.50",
    "mac": "AA:BB:CC:DD:EE:FF",
    "model": "P105",
    "protocol": "tpap"
  },
  "battery": {
    "path": "auto",
    "serial": ""
  },
  "auto_charge": {
    "enabled": true,
    "charger_only_confirmed": true,
    "turn_on": 20,
    "turn_off": 80,
    "check_interval_sec": 30,
    "health_interval_sec": 300
  },
  "ui": {
    "editor": "",
    "refresh_interval_sec": 3,
    "show_label": false,
    "label_format": "{battery}%",
    "show_notifications": true
  },
  "credentials_file": "~/.config/tspt/credentials.json"
}
```

### Field Reference

- **`plug.ip`**: Local LAN IPv4 address of your Tapo smart plug (check your router DHCP table or Tapo app → *Device Info*). Assign a static DHCP reservation on your router so the IP stays fixed.
- **`plug.mac`**: 12-hex-digit hardware MAC address (`AA:BB:CC:DD:EE:FF`). Required for Auto-Charge Guard so `tspt` never toggles the wrong device if DHCP reassigns the IP. Run `tspt init` to auto-detect and verify it.
- **`plug.model`**: Expected plug model (e.g. `"P105"`). Set `""` to skip model verification.
- **`plug.protocol`**: `"tpap"` (required for Tapo P105 firmware `1.4.0+`) or `"auto"` for older Kasa/Tapo plugs.
- **`battery.path`**: `"auto"` (detects `BAT0`/`BAT1` automatically) or an explicit sysfs path like `"/sys/class/power_supply/BAT1"`.
- **`battery.serial`**: Optional battery serial lock (`""` accepts any present battery).
- **`auto_charge.enabled`**: Enables or pauses automatic threshold switching (`true` / `false`).
- **`auto_charge.charger_only_confirmed`**: Safety confirmation (`true` / `false`). Set to `true` only when **exclusively** your laptop charger is plugged into the smart plug.
- **`auto_charge.turn_on` / `turn_off`**: Hysteresis bounds (`10 <= turn_on < turn_off <= 95`). Turns ON at/below `turn_on`% and OFF at/above `turn_off`%.
- **`ui.show_label` & `ui.label_format`**: Set `"show_label": true` to display text beside the tray icon (`{battery}`, `{status}`, `{plug}`).
- **`ui.editor`**: Custom editor command (e.g. `"subl"`, `"code"`, `"gnome-text-editor"`). Leave `""` to use your desktop's default `text/plain` editor.
- **`ui.show_notifications`**: Show or silence desktop notifications (`true` / `false`).

---

## 3. Updating Tapo Account Credentials

Credentials are stored separately in `~/.config/tspt/credentials.json` with strict `0600` permissions (`chmod 600 ~/.config/tspt/credentials.json`), or via `TAPO_EMAIL` / `TAPO_PASSWORD` environment variables:

```json
{
  "tapo_email": "you@example.com",
  "tapo_password": "your-tapo-password"
}
```

Or run `tspt init` in a terminal to update credentials and re-verify your plug's MAC address interactively.

---

## 4. Terminal CLI Commands

```bash
tspt                 # Launch top-bar indicator + background Auto-Charge Guard
tspt init            # Interactive wizard (IP, credentials, MAC probe)
tspt status          # Live battery + plug + failsafe timer status
tspt status --json   # JSON status output
tspt on              # Turn plug ON and cancel failsafe countdown
tspt off             # Arm 15-min failsafe countdown and turn plug OFF
tspt check           # Dry-run single Auto-Charge Guard check
tspt check --apply   # Run single Auto-Charge Guard check and apply relay
```
