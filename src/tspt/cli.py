"""Command-line entrypoint for tspt (Tapo SmartPlug Tray)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from . import __version__
from .config import default_config_path, ensure_config, save_json_atomic
from .guard import (
    detect_battery_path,
    manual_plug_action,
    read_battery,
    read_connection_cache,
    resolve_battery_path,
    run_guard_cycle,
)


def cmd_init(config_path: Path) -> int:
    """Interactive setup wizard to configure plug IP, credentials, and safety checks."""
    cfg = ensure_config(config_path)
    print(f"TSPT (Tapo SmartPlug Tray) v{__version__} - Interactive Configuration")
    print(f"Config file: {config_path}\n")

    current_ip = cfg["plug"].get("ip", "")
    plug_ip = input(f"Tapo Smart Plug IP [{current_ip}]: ").strip() or current_ip
    cfg["plug"]["ip"] = plug_ip

    current_model = cfg["plug"].get("model", "P105")
    plug_model = input(f"Plug Model [{current_model}]: ").strip() or current_model
    cfg["plug"]["model"] = plug_model

    cred_path = Path(cfg.get("credentials_file", "~/.config/tspt/credentials.json")).expanduser()
    update_creds = input(f"Save Tapo credentials to {cred_path} (chmod 600)? [y/N]: ").strip().lower()
    if update_creds == "y":
        email = input("Tapo Account Email: ").strip()
        password = getpass.getpass("Tapo Account Password: ")
        if email and password:
            save_json_atomic(cred_path, {"tapo_email": email, "tapo_password": password}, mode=0o600)
            print(f"Saved private credentials to {cred_path} (mode 0600).")

    try:
        detected_bat = detect_battery_path()
        print(f"Detected Linux battery: {detected_bat}")
        cfg["battery"]["path"] = "auto"
    except FileNotFoundError:
        print("Warning: No /sys/class/power_supply/BAT* battery detected on this machine.")

    if plug_ip and cred_path.exists():
        probe = input("Connect to plug now to auto-fill verified MAC address? [Y/n]: ").strip().lower()
        if probe in ("", "y", "yes"):
            save_json_atomic(config_path, cfg)
            try:
                info = asyncio.run(manual_plug_action("status", cfg))
                cfg["plug"]["mac"] = info["mac"]
                cfg["plug"]["model"] = info["model"]
                print(f"Verified {info['model']} at {plug_ip} (MAC: {info['mac']}, ON={info['is_on']})")
            except Exception as exc:
                print(f"Could not probe plug right now ({exc}). You can set 'plug.mac' manually later.")

    confirm_charger = input(
        "Confirm ONLY your laptop charger is plugged into this smart plug? [y/N]: "
    ).strip().lower()
    if confirm_charger == "y":
        cfg["auto_charge"]["charger_only_confirmed"] = True
        enable_auto = input("Enable 20%–80% Auto-Charge Guard now? [Y/n]: ").strip().lower()
        cfg["auto_charge"]["enabled"] = enable_auto in ("", "y", "yes")

    save_json_atomic(config_path, cfg)
    print(f"\nConfiguration saved to {config_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tspt",
        description="TSPT (Tapo SmartPlug Tray) - standalone Linux taskbar indicator & 20-80% Auto-Charge Guard.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Launch the top-bar taskbar indicator (default)")
    sub.add_parser("init", help="Interactive setup wizard for plug IP, credentials, and MAC verification")

    for name, help_text in (
        ("status", "Query live battery and smart plug status"),
        ("on", "Turn the smart plug ON and cancel recovery countdown"),
        ("off", "Arm 15-minute failsafe timer and turn the smart plug OFF"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", help="Emit JSON output")
        if name == "status":
            p.add_argument("--cached", action="store_true", help="Read cached plug state without network call")

    p_check = sub.add_parser("check", help="Run a single Auto-Charge Guard evaluation cycle")
    p_check.add_argument("--apply", action="store_true", help="Apply relay changes (omit for dry-run)")
    p_check.add_argument("--json", action="store_true", help="Emit JSON output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = args.config or default_config_path()
    command = args.command or "run"

    if command == "init":
        return cmd_init(config_path)

    cfg = ensure_config(config_path)

    if command == "run":
        from .indicator import TsptTrayApp

        app = TsptTrayApp(config_path=config_path)
        app.run()
        return 0

    if command == "status":
        try:
            bat_path = resolve_battery_path(str(cfg.get("battery", {}).get("path", "auto")))
            bat = read_battery(bat_path, strict=False)
        except FileNotFoundError:
            bat = read_battery(Path("/nonexistent"), strict=False)

        if getattr(args, "cached", False):
            plug_data = read_connection_cache()
        else:
            plug_data = asyncio.run(manual_plug_action("status", cfg))

        payload = {
            "battery": {
                "percent": bat.percent,
                "status": bat.status,
                "power_w": bat.power_w,
                "voltage_v": bat.voltage_v,
                "path": bat.path,
            },
            "plug": plug_data,
            "auto_charge": cfg.get("auto_charge", {}),
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"Battery : {bat.percent}% ({bat.status}) [{bat.power_w}W]")
            is_on = plug_data.get("is_on", plug_data.get("plug_on"))
            print(f"Plug    : {'ON' if is_on else 'OFF' if is_on is False else 'Unknown'}")
            auto = cfg.get("auto_charge", {})
            print(
                f"Guard   : {'Enabled' if auto.get('enabled') else 'Disabled'} "
                f"({auto.get('turn_on', 20)}%–{auto.get('turn_off', 80)}%)"
            )
        return 0

    if command in ("on", "off"):
        result = asyncio.run(manual_plug_action(command, cfg))
        if args.json:
            print(json.dumps(result))
        else:
            print(f"Plug turned {command.upper()} (is_on={result['is_on']})")
        return 0

    if command == "check":
        msg = run_guard_cycle(cfg, apply=args.apply)
        if args.json:
            print(json.dumps({"status": "ok", "message": msg}))
        else:
            print(msg)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
