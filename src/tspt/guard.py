"""Battery telemetry, device verification, and 20-80% auto-charge guard logic."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import subprocess
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import load_credentials, load_json, save_json_atomic, state_dir

LOG = logging.getLogger("tspt")
FAILURE_THRESHOLD = 3
VALID_BATTERY_STATUSES = {"Charging", "Discharging", "Full", "Not charging"}


@dataclass(frozen=True)
class BatteryReading:
    percent: int
    status: str
    power_w: float = 0.0
    voltage_v: float = 0.0
    serial: str = ""
    path: str = ""
    present: bool = True


def detect_battery_path(power_supply_root: Path = Path("/sys/class/power_supply")) -> Path:
    """Find the first Linux sysfs Battery supply (e.g., BAT0 or BAT1)."""
    if not power_supply_root.exists():
        raise FileNotFoundError(f"Power supply sysfs directory not found: {power_supply_root}")
    for candidate in sorted(power_supply_root.iterdir()):
        type_file = candidate / "type"
        try:
            if type_file.exists() and type_file.read_text(encoding="utf-8").strip() == "Battery":
                return candidate
        except OSError:
            continue
    raise FileNotFoundError(f"No battery found in {power_supply_root}")


def resolve_battery_path(configured: str, power_supply_root: Path = Path("/sys/class/power_supply")) -> Path:
    clean = (configured or "auto").strip()
    if clean.lower() == "auto":
        return detect_battery_path(power_supply_root)
    return Path(clean)


def read_battery(
    configured_path: str | Path = "auto",
    expected_serial: str = "",
    *,
    strict: bool = True,
) -> BatteryReading:
    """Read sysfs battery state. When strict=True, reject invalid, absent, or mismatched batteries."""
    path = (
        resolve_battery_path(configured_path)
        if isinstance(configured_path, str)
        else configured_path
    )
    if not strict:
        return _read_battery_soft(path)

    if (path / "type").read_text(encoding="utf-8").strip() != "Battery":
        raise ValueError(f"Configured power supply {path} is not a Battery")
    if (path / "present").read_text(encoding="utf-8").strip() != "1":
        raise ValueError("Battery is absent")

    serial_file = path / "serial_number"
    actual_serial = serial_file.read_text(encoding="utf-8").strip() if serial_file.exists() else ""
    if expected_serial and actual_serial != expected_serial:
        raise ValueError("Battery serial changed; update config before switching")

    percent = int((path / "capacity").read_text(encoding="utf-8").strip())
    status = (path / "status").read_text(encoding="utf-8").strip()
    if not 0 <= percent <= 100 or status not in VALID_BATTERY_STATUSES:
        raise ValueError(f"Invalid battery reading: {percent}% ({status})")

    power_w, voltage_v = _read_power_metrics(path)
    return BatteryReading(
        percent=percent,
        status=status,
        power_w=power_w,
        voltage_v=voltage_v,
        serial=actual_serial,
        path=str(path),
        present=True,
    )


def _read_power_metrics(path: Path) -> tuple[float, float]:
    voltage_v = 0.0
    power_w = 0.0
    try:
        v_uv = float((path / "voltage_now").read_text(encoding="utf-8").strip())
        voltage_v = round(v_uv / 1_000_000, 2)
    except (OSError, ValueError):
        v_uv = 0.0
    try:
        if (path / "power_now").exists():
            p_uw = float((path / "power_now").read_text(encoding="utf-8").strip())
            power_w = round(p_uw / 1_000_000, 1)
        elif v_uv and (path / "current_now").exists():
            c_ua = float((path / "current_now").read_text(encoding="utf-8").strip())
            power_w = round((v_uv * c_ua) / 1_000_000_000_000, 1)
    except (OSError, ValueError):
        pass
    return power_w, voltage_v


def _read_battery_soft(path: Path) -> BatteryReading:
    """Non-throwing battery snapshot for UI display."""
    uevent = path / "uevent"
    if not uevent.exists():
        return BatteryReading(percent=0, status="Unknown", present=False, path=str(path))
    try:
        kv: dict[str, str] = {}
        for line in uevent.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        present = kv.get("POWER_SUPPLY_PRESENT") == "1"
        percent = int(kv.get("POWER_SUPPLY_CAPACITY", 0))
        status = kv.get("POWER_SUPPLY_STATUS", "Unknown")
        serial = kv.get("POWER_SUPPLY_SERIAL_NUMBER", "")
        v_uv = float(kv.get("POWER_SUPPLY_VOLTAGE_NOW", 0))
        c_ua = float(kv.get("POWER_SUPPLY_CURRENT_NOW", 0))
        if "POWER_SUPPLY_POWER_NOW" in kv:
            power_w = round(float(kv["POWER_SUPPLY_POWER_NOW"]) / 1_000_000, 1)
        elif v_uv and c_ua:
            power_w = round((v_uv * c_ua) / 1_000_000_000_000, 1)
        else:
            power_w = 0.0
        return BatteryReading(
            percent=percent,
            status=status,
            power_w=power_w,
            voltage_v=round(v_uv / 1_000_000, 2) if v_uv else 0.0,
            serial=serial,
            path=str(path),
            present=present,
        )
    except (OSError, ValueError):
        return BatteryReading(percent=0, status="Unknown", present=False, path=str(path))


def desired_power(percent: int, low: int, high: int) -> bool | None:
    """Return True at/below low, False at/above high, or None within the hysteresis band."""
    if not 0 <= percent <= 100 or not 10 <= low < high <= 95:
        raise ValueError("Thresholds must satisfy 10 <= turn_on < turn_off <= 95")
    if percent <= low:
        return True
    if percent >= high:
        return False
    return None


def normalize_mac(value: str) -> str:
    return value.replace(":", "").replace("-", "").strip().lower()


def verify_plug(device: Any, plug_cfg: dict[str, Any], *, require_mac: bool = True) -> None:
    """Verify plug identity and single-outlet type before any relay change."""
    expected_mac = normalize_mac(str(plug_cfg.get("mac", "")))
    if require_mac or expected_mac:
        if len(expected_mac) != 12 or any(c not in "0123456789abcdef" for c in expected_mac):
            raise ValueError("A verified 12-hex-digit plug MAC address is required for auto-charge")
        if normalize_mac(device.mac) != expected_mac:
            raise ValueError("Device MAC mismatch; refusing to switch")

    expected_model = str(plug_cfg.get("model", "")).strip()
    if expected_model and device.model != expected_model:
        raise ValueError(f"Device model mismatch ({device.model} != {expected_model}); refusing to switch")

    if not hasattr(device, "is_on") or getattr(device, "is_strip", True):
        raise ValueError("Only a single-outlet smart plug is supported")


async def evaluate_and_control(device: Any, config: dict[str, Any], *, apply: bool) -> str:
    """Re-read battery after connecting, enforce thresholds, and verify failsafe recovery."""
    plug_cfg = config.get("plug", {})
    bat_cfg = config.get("battery", {})
    auto_cfg = config.get("auto_charge", {})

    verify_plug(device, plug_cfg, require_mac=True)
    battery = read_battery(bat_cfg.get("path", "auto"), str(bat_cfg.get("serial", "")), strict=True)
    target = desired_power(
        battery.percent,
        int(auto_cfg.get("turn_on", 20)),
        int(auto_cfg.get("turn_off", 80)),
    )

    if not apply:
        if target is None or device.is_on == target:
            return f"Dry run ({battery.percent}%): no relay change needed"
        return f"Dry run ({battery.percent}%): would turn plug {'on' if target else 'off'}"

    if target and getattr(device, "overheated", False):
        raise ValueError("Plug reports overheating; check hardware before charging")

    changed = target is not None and device.is_on != target
    if changed and target is False:
        # Arm 15-minute failsafe BEFORE cutting power, then verify again afterwards.
        await device.ensure_recovery()
        try:
            await device.turn_off()
            await device.update()
            verify_plug(device, plug_cfg, require_mac=True)
            if device.is_on:
                raise RuntimeError("Plug did not confirm OFF state")
            await device.ensure_recovery()
        except Exception:
            await device.turn_on()
            await device.update()
            raise
    elif changed and target is True:
        await device.turn_on()
        await device.update()
        verify_plug(device, plug_cfg, require_mac=True)
        if not device.is_on:
            raise RuntimeError("Plug did not confirm ON state")

    if device.is_on:
        await device.cancel_recovery()
    else:
        try:
            await device.ensure_recovery()
        except Exception:
            # Restore power immediately if an already-OFF plug cannot maintain its failsafe.
            await device.turn_on()
            await device.update()
            raise

    if changed:
        LOG.info("Battery %s%%: verified plug %s", battery.percent, "ON" if target else "OFF")

    if target is True:
        await asyncio.sleep(3)
        rechecked = read_battery(bat_cfg.get("path", "auto"), str(bat_cfg.get("serial", "")), strict=True)
        if rechecked.status in {"Discharging", "Not charging"}:
            raise RuntimeError("Plug is ON but laptop battery is not charging; check AC adapter")

    return "Relay and failsafe verified" if changed else "Plug state and failsafe checked"


def network_due(
    battery: BatteryReading,
    config: dict[str, Any],
    cache: dict[str, Any],
    now: float,
) -> bool:
    """Return True when threshold action or periodic 5-minute health/timer renewal is needed."""
    auto_cfg = config.get("auto_charge", {})
    plug_cfg = config.get("plug", {})
    health_sec = max(60, int(auto_cfg.get("health_interval_sec", 300)))

    target = desired_power(
        battery.percent,
        int(auto_cfg.get("turn_on", 20)),
        int(auto_cfg.get("turn_off", 80)),
    )
    checked_at = cache.get("checked_at")
    if type(checked_at) not in (int, float):
        return True
    age = now - checked_at
    expected_mac = normalize_mac(str(plug_cfg.get("mac", "")))
    if expected_mac and cache.get("mac") != expected_mac:
        return True
    if not 0 <= age < health_sec:
        return True
    if type(cache.get("plug_on")) is not bool:
        return True
    if target is not None and target != cache["plug_on"]:
        return True
    if target is False and battery.status == "Charging":
        return True
    if target is True and battery.status != "Charging":
        return True
    return False


def setup_logging(sdir: Path | None = None) -> Path:
    target_dir = sdir or state_dir()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_file = target_dir / "history.log"
    if not LOG.handlers:
        handler = RotatingFileHandler(log_file, maxBytes=262_144, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.setLevel(logging.INFO)
        LOG.addHandler(handler)
    return log_file


def write_connection_cache(
    *,
    plug_on: bool,
    mac: str,
    model: str = "",
    countdown_remain: int | None = None,
    sdir: Path | None = None,
) -> dict[str, Any]:
    target_dir = sdir or state_dir()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload: dict[str, Any] = {
        "checked_at": time.time(),
        "plug_on": bool(plug_on),
        "mac": normalize_mac(mac),
    }
    if model:
        payload["model"] = model
    if countdown_remain is not None:
        payload["countdown_remain"] = countdown_remain
    save_json_atomic(target_dir / "connection.json", payload)
    return payload


def read_connection_cache(sdir: Path | None = None) -> dict[str, Any]:
    target_dir = sdir or state_dir()
    data = load_json(target_dir / "connection.json", default={})
    return data if isinstance(data, dict) else {}


def record_success(sdir: Path | None = None) -> None:
    target_dir = sdir or state_dir()
    try:
        (target_dir / "consecutive-failures").unlink(missing_ok=True)
    except OSError:
        pass


def record_failure(sdir: Path | None = None) -> int:
    target_dir = sdir or state_dir()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = target_dir / "consecutive-failures"
    count = 1
    try:
        if path.exists():
            count = int(path.read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        count = 1
    try:
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return count


def alert_failure(consecutive: int = FAILURE_THRESHOLD, sdir: Path | None = None) -> None:
    if consecutive < FAILURE_THRESHOLD:
        return
    target_dir = sdir or state_dir()
    stamp = target_dir / "last-alert"
    now = time.time()
    if stamp.exists() and 0 <= now - stamp.stat().st_mtime < 600:
        return
    try:
        res = subprocess.run(
            [
                "notify-send",
                "--urgency=critical",
                "-a",
                "TSPT",
                "TSPT Auto-Charge Needs Attention",
                "Check the AC adapter and Tapo plug. Automatic charge limits could not be verified.",
            ],
            timeout=5,
            check=False,
            capture_output=True,
        )
        if res.returncode == 0:
            stamp.touch()
    except (OSError, subprocess.TimeoutExpired):
        pass


async def manual_plug_action(action: str, config: dict[str, Any], sdir: Path | None = None) -> dict[str, Any]:
    """Perform a direct status/on/off plug operation and update the shared connection cache."""
    from .tpap import connect_plug

    plug_cfg = config.get("plug", {})
    email, password = load_credentials(config)
    plug = await connect_plug(
        str(plug_cfg.get("ip", "")).strip(),
        email,
        password,
        protocol=str(plug_cfg.get("protocol", "tpap")),
    )
    try:
        verify_plug(plug, plug_cfg, require_mac=False)
        if action == "status":
            timer = await plug.timer()
            remain = timer.get("remain", 0) if timer else None
            write_connection_cache(
                plug_on=plug.is_on,
                mac=plug.mac,
                model=plug.model,
                countdown_remain=remain,
                sdir=sdir,
            )
            return {
                "status": "ok",
                "is_on": plug.is_on,
                "mac": plug.mac,
                "model": plug.model,
                "countdown_remain": remain,
            }
        if action == "on":
            if getattr(plug, "overheated", False):
                raise ValueError("Plug reports overheating; refusing to turn ON")
            await plug.cancel_recovery()
            await plug.turn_on()
            await plug.update()
            write_connection_cache(plug_on=plug.is_on, mac=plug.mac, model=plug.model, countdown_remain=None, sdir=sdir)
            setup_logging(sdir)
            LOG.info("Manual override: plug turned ON")
            return {"status": "ok", "action": "on", "is_on": plug.is_on, "mac": plug.mac, "model": plug.model}
        if action == "off":
            await plug.ensure_recovery()
            await plug.turn_off()
            await plug.update()
            await plug.ensure_recovery()
            timer = await plug.timer()
            remain = timer.get("remain", 0) if timer else None
            write_connection_cache(plug_on=plug.is_on, mac=plug.mac, model=plug.model, countdown_remain=remain, sdir=sdir)
            setup_logging(sdir)
            LOG.info("Manual override: plug turned OFF (15m failsafe armed)")
            return {
                "status": "ok",
                "action": "off",
                "is_on": plug.is_on,
                "countdown_remain": remain,
                "mac": plug.mac,
                "model": plug.model,
            }
        raise ValueError(f"Unsupported action: {action}")
    finally:
        await plug.disconnect()


def run_guard_cycle(config: dict[str, Any], *, apply: bool, sdir: Path | None = None) -> str:
    """Execute one locked, bounded auto-charge guard tick."""
    from .tpap import connect_plug

    target_dir = sdir or state_dir()
    setup_logging(target_dir)

    auto_cfg = config.get("auto_charge", {})
    bat_cfg = config.get("battery", {})
    plug_cfg = config.get("plug", {})

    if apply and (
        auto_cfg.get("enabled") is not True
        or auto_cfg.get("charger_only_confirmed") is not True
    ):
        raise ValueError(
            "Auto-charge requires both 'auto_charge.enabled' and 'auto_charge.charger_only_confirmed' to be true"
        )

    lock_path = target_dir / "guard.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Another check is already in progress"

        battery = read_battery(bat_cfg.get("path", "auto"), str(bat_cfg.get("serial", "")), strict=True)
        cache = read_connection_cache(target_dir)
        if apply and not network_due(battery, config, cache, time.time()):
            return f"Battery {battery.percent}% ({battery.status}): within hysteresis band; next health check <5m"

        async def _bounded() -> str:
            async with asyncio.timeout(25):
                email, password = load_credentials(config)
                plug = await connect_plug(
                    str(plug_cfg.get("ip", "")).strip(),
                    email,
                    password,
                    protocol=str(plug_cfg.get("protocol", "tpap")),
                )
                try:
                    msg = await evaluate_and_control(plug, config, apply=apply)
                    if apply:
                        timer = await plug.timer()
                        write_connection_cache(
                            plug_on=plug.is_on,
                            mac=plug.mac,
                            model=plug.model,
                            countdown_remain=timer.get("remain", 0) if timer else None,
                            sdir=target_dir,
                        )
                    return msg
                finally:
                    await plug.disconnect()

        try:
            result = asyncio.run(_bounded())
            if apply:
                record_success(target_dir)
            return result
        except Exception as exc:
            # Never log raw third-party exceptions in case credentials appear in transport errors.
            consecutive = record_failure(target_dir) if apply else 1
            if apply:
                alert_failure(consecutive, target_dir)
            LOG.error(
                "Auto-charge check failed (%s, attempt %d/%d)",
                type(exc).__name__,
                consecutive,
                FAILURE_THRESHOLD,
            )
            raise
