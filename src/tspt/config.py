"""XDG configuration, credentials validation, and runtime paths."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "tspt"

DEFAULT_CONFIG: dict[str, Any] = {
    "plug": {
        "ip": "",
        "mac": "",
        "model": "P105",
        "protocol": "tpap",
    },
    "battery": {
        "path": "auto",
        "serial": "",
    },
    "auto_charge": {
        "enabled": False,
        "charger_only_confirmed": False,
        "turn_on": 20,
        "turn_off": 80,
        "check_interval_sec": 30,
        "health_interval_sec": 300,
    },
    "ui": {
        "editor": "",
        "refresh_interval_sec": 3,
        "show_label": False,
        "label_format": "{battery}%",
        "show_notifications": True,
        "icons": {
            "plug_on": "tspt-on-symbolic",
            "plug_off": "tspt-off-symbolic",
        },
        "labels": {
            "battery_format": "Battery: {capacity}% ({status}){power_suffix}",
            "plug_on": "Plug: On",
            "plug_off": "Plug: Off (15m failsafe)",
            "plug_unknown": "Plug: Unknown",
            "turn_on": "Turn On",
            "turn_off": "Turn Off",
            "refresh": "Refresh Status",
            "auto_on": "Auto-Charge: On ({low}%–{high}%)",
            "auto_off": "Auto-Charge: Off",
            "presets": "Threshold Presets",
            "setup": "Setup Plug & Credentials…",
            "settings": "Edit Config",
            "logs": "View Activity Log",
            "help": "Usage & Config Guide",
            "quit": "Quit",
        },
    },
    "credentials_file": "~/.config/tspt/credentials.json",
    "kasa_python": "",
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / APP_NAME


def default_config_path() -> Path:
    override = os.environ.get("TSPT_CONFIG", "")
    return Path(override).expanduser() if override else config_dir() / "config.json"


def deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default) if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(default) if default is not None else {}


def save_json_atomic(path: Path, data: dict[str, Any], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(path)


def ensure_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        save_json_atomic(cfg_path, DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = load_json(cfg_path, default={})
    if not isinstance(raw, dict):
        raw = {}
    return deep_merge(DEFAULT_CONFIG, raw)


def load_credentials(config: dict[str, Any]) -> tuple[str, str]:
    env_email = os.environ.get("TAPO_EMAIL", "").strip()
    env_password = os.environ.get("TAPO_PASSWORD", "").strip()
    if env_email and env_password:
        return env_email, env_password

    cred_file = Path(
        config.get("credentials_file", "~/.config/tspt/credentials.json")
    ).expanduser()
    if not cred_file.exists():
        raise FileNotFoundError(
            f"Credentials file not found at {cred_file}. Run 'tspt init' first."
        )
    if cred_file.stat().st_mode & 0o077:
        raise PermissionError(
            f"Credentials file {cred_file} must be private (chmod 600)."
        )
    data = json.loads(cred_file.read_text(encoding="utf-8"))
    email = str(data.get("tapo_email", "")).strip()
    password = str(data.get("tapo_password", ""))
    if not email or not password:
        raise ValueError(f"Missing 'tapo_email' or 'tapo_password' in {cred_file}.")
    return email, password


def resolve_kasa_python(config: dict[str, Any]) -> str | None:
    """Return an external Python interpreter path only if configured or needed."""
    configured = str(config.get("kasa_python", "")).strip()
    if configured:
        expanded = str(Path(configured).expanduser())
        if Path(expanded).exists():
            return expanded
    try:
        import kasa  # noqa: F401
        return None
    except ImportError:
        venv_candidate = Path.home() / ".local/share/tspt/venv/bin/python"
        if venv_candidate.exists():
            return str(venv_candidate)
    return None
