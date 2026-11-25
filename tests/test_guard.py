"""Unit tests for battery detection, charge hysteresis, safety checks, and config."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tspt.config import ensure_config, load_credentials, save_json_atomic
from tspt.guard import (
    BatteryReading,
    alert_failure,
    desired_power,
    detect_battery_path,
    evaluate_and_control,
    network_due,
    read_battery,
    read_connection_cache,
    record_failure,
    record_success,
    verify_plug,
    write_connection_cache,
)


class FakePlug:
    mac = "AA-BB-CC-DD-EE-FF"
    model = "P105"
    is_strip = False
    overheated = False

    def __init__(self, *, on=True, fail=False):
        self.is_on = on
        self.fail = fail
        self.calls = []

    async def turn_on(self):
        self.calls.append(True)
        if not self.fail:
            self.is_on = True

    async def turn_off(self):
        self.calls.append(False)
        if not self.fail:
            self.is_on = False

    async def update(self):
        pass

    async def ensure_recovery(self):
        pass

    async def cancel_recovery(self):
        pass


class GuardAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bat_dir = self.root / "BAT0"
        self.bat_dir.mkdir()
        for name, value in {
            "type": "Battery",
            "present": "1",
            "serial_number": "SN-001",
            "capacity": "80",
            "status": "Charging",
            "uevent": "POWER_SUPPLY_PRESENT=1\nPOWER_SUPPLY_CAPACITY=80\nPOWER_SUPPLY_STATUS=Charging\n",
        }.items():
            (self.bat_dir / name).write_text(value, encoding="utf-8")

        self.config = {
            "plug": {"ip": "192.0.2.10", "mac": "aa:bb:cc:dd:ee:ff", "model": "P105"},
            "battery": {"path": str(self.bat_dir), "serial": "SN-001"},
            "auto_charge": {
                "enabled": True,
                "charger_only_confirmed": True,
                "turn_on": 20,
                "turn_off": 80,
                "health_interval_sec": 300,
            },
        }

    def test_auto_detect_battery_path(self):
        ac_dir = self.root / "AC"
        ac_dir.mkdir()
        (ac_dir / "type").write_text("Mains\n", encoding="utf-8")
        self.assertEqual(detect_battery_path(self.root), self.bat_dir)

    def test_optional_serial_allows_blank_default(self):
        reading = read_battery(self.bat_dir, expected_serial="", strict=True)
        self.assertEqual(reading.percent, 80)

    def test_thresholds_and_hysteresis(self):
        for pct in range(101):
            expected = True if pct <= 20 else False if pct >= 80 else None
            self.assertIs(desired_power(pct, 20, 80), expected)

    def test_invalid_thresholds_rejected(self):
        for low, high in [(80, 20), (20, 20), (5, 80), (20, 99)]:
            with self.assertRaises(ValueError):
                desired_power(50, low, high)

    def test_invalid_battery_readings_rejected(self):
        for field, bad in [
            ("capacity", "105"),
            ("capacity", "-1"),
            ("status", "Unknown"),
            ("serial_number", "OTHER"),
            ("present", "0"),
            ("type", "Mains"),
        ]:
            original = (self.bat_dir / field).read_text(encoding="utf-8")
            (self.bat_dir / field).write_text(bad, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_battery(self.bat_dir, "SN-001", strict=True)
            (self.bat_dir / field).write_text(original, encoding="utf-8")

    def test_wrong_device_refused(self):
        for attr, bad in [("mac", "11:22:33:44:55:66"), ("model", "KP303"), ("is_strip", True)]:
            plug = FakePlug()
            setattr(plug, attr, bad)
            with self.assertRaises(ValueError):
                verify_plug(plug, self.config["plug"], require_mac=True)

    def test_cutoff_and_idempotency(self):
        plug = FakePlug(on=True)
        asyncio.run(evaluate_and_control(plug, self.config, apply=True))
        asyncio.run(evaluate_and_control(plug, self.config, apply=True))
        self.assertEqual(plug.calls, [False])

    def test_low_battery_turns_plug_on(self):
        plug = FakePlug(on=False)
        (self.bat_dir / "capacity").write_text("20", encoding="utf-8")
        with patch("tspt.guard.asyncio.sleep", return_value=None):
            asyncio.run(evaluate_and_control(plug, self.config, apply=True))
        self.assertEqual(plug.calls, [True])

    def test_timer_failure_prevents_power_cut(self):
        plug = FakePlug(on=True)

        async def fail():
            raise RuntimeError("Timer failed")

        plug.ensure_recovery = fail
        with self.assertRaises(RuntimeError):
            asyncio.run(evaluate_and_control(plug, self.config, apply=True))
        self.assertEqual(plug.calls, [])
        self.assertTrue(plug.is_on)

    def test_timer_failure_after_cut_restores_power(self):
        plug = FakePlug(on=True)
        calls = 0

        async def fail_second():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Lost timer after cut")

        plug.ensure_recovery = fail_second
        with self.assertRaises(RuntimeError):
            asyncio.run(evaluate_and_control(plug, self.config, apply=True))
        self.assertEqual(plug.calls, [False, True])
        self.assertTrue(plug.is_on)

    def test_wall_clock_cache_compatible_with_network_due(self):
        sdir = self.root / "state"
        write_connection_cache(plug_on=False, mac="AA:BB:CC:DD:EE:FF", model="P105", sdir=sdir)
        cache = read_connection_cache(sdir)
        self.assertFalse(
            network_due(BatteryReading(50, "Discharging"), self.config, cache, time.time())
        )
        self.assertTrue(
            network_due(BatteryReading(20, "Discharging"), self.config, cache, time.time())
        )

    def test_credentials_permission_enforced(self):
        cred_file = self.root / "credentials.json"
        cred_file.write_text(json.dumps({"tapo_email": "a@b.com", "tapo_password": "secret"}), encoding="utf-8")
        os.chmod(cred_file, 0o644)
        cfg = ensure_config(self.root / "config.json")
        cfg["credentials_file"] = str(cred_file)
        with self.assertRaises(PermissionError):
            load_credentials(cfg)
        save_json_atomic(cred_file, {"tapo_email": "a@b.com", "tapo_password": "secret"}, mode=0o600)
        self.assertEqual(load_credentials(cfg), ("a@b.com", "secret"))

    def test_failure_tracking_and_alert_threshold(self):
        sdir = self.root / "state_alerts"
        sdir.mkdir()
        with patch("tspt.guard.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            self.assertEqual(record_failure(sdir), 1)
            alert_failure(1, sdir)
            self.assertEqual(record_failure(sdir), 2)
            alert_failure(2, sdir)
            self.assertEqual(mock_run.call_count, 0)
            self.assertEqual(record_failure(sdir), 3)
            alert_failure(3, sdir)
            self.assertEqual(mock_run.call_count, 1)
            record_success(sdir)
            self.assertFalse((sdir / "consecutive-failures").exists())


if __name__ == "__main__":
    unittest.main()
