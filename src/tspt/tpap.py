"""Tapo TPAP smart plug transport and verified 15-minute failsafe countdown."""

from __future__ import annotations

import asyncio
from typing import Any

RECOVERY_DELAY_SEC = 900  # 15 minutes


class KasaPlug:
    """Wrapper around a python-kasa Device with verified single-slot countdown recovery."""

    def __init__(self, device: Any) -> None:
        self.device = device

    @property
    def mac(self) -> str:
        return str(self.device.mac)

    @property
    def model(self) -> str:
        return str(self.device.model)

    @property
    def is_on(self) -> bool:
        return bool(self.device.is_on)

    @property
    def is_strip(self) -> bool:
        from kasa.device_type import DeviceType

        return self.device.device_type != DeviceType.Plug

    @property
    def overheated(self) -> bool:
        features = getattr(self.device, "features", {}) or {}
        feature = features.get("overheated")
        return bool(feature.value) if feature else False

    async def update(self) -> None:
        await self.device.update()

    async def turn_on(self) -> None:
        await self.device.turn_on()

    async def turn_off(self) -> None:
        await self.device.turn_off()

    async def disconnect(self) -> None:
        await self.device.disconnect()

    async def timer(self) -> dict[str, Any] | None:
        """Return the active countdown rule if enabled and ticking."""
        response = await self.device.protocol.query(
            {"get_countdown_rules": {"start_index": 0}}
        )
        data = response["get_countdown_rules"]
        rules = data.get("rule_list", [])
        active = [
            rule
            for rule in rules
            if data.get("enable") and rule.get("enable") and rule.get("remain", 0) > 0
        ]
        return active[0] if active else None

    async def ensure_recovery(self) -> None:
        """Arm or renew a 15-minute ON failsafe timer, respecting P105's single-slot limit."""
        timer = await self.timer()
        if (
            timer
            and timer.get("desired_states", {}).get("on") is True
            and 600 < timer["remain"] <= RECOVERY_DELAY_SEC
        ):
            return

        params: dict[str, Any] = {
            "delay": RECOVERY_DELAY_SEC,
            "desired_states": {"on": True},
            "enable": True,
        }
        method = "add_countdown_rule"
        if timer:
            method = "edit_countdown_rule"
            params["id"] = timer["id"]

        await self.device.protocol.query({method: params})
        verified = await self.timer()
        if (
            not verified
            or verified.get("desired_states", {}).get("on") is not True
            or not (RECOVERY_DELAY_SEC - 30) <= verified["remain"] <= RECOVERY_DELAY_SEC
        ):
            raise RuntimeError("Failsafe recovery timer did not verify on plug")

    async def cancel_recovery(self) -> None:
        """Disable the active recovery countdown rule when charger power is ON."""
        timer = await self.timer()
        if timer:
            await self.device.protocol.query(
                {
                    "edit_countdown_rule": {
                        "id": timer["id"],
                        "delay": timer["delay"],
                        "desired_states": timer["desired_states"],
                        "enable": False,
                    }
                }
            )
            if await self.timer():
                raise RuntimeError("Failsafe recovery timer cancellation did not verify")


async def connect_plug(
    host: str,
    username: str,
    password: str,
    *,
    protocol: str = "tpap",
    timeout: int = 8,
) -> KasaPlug:
    """Connect directly to a Tapo smart plug over HTTP/TPAP (or auto-negotiate)."""
    if not host:
        raise ValueError("Plug IP/hostname is not configured")

    from kasa import Credentials, Device, KasaException
    from kasa.deviceconfig import DeviceConfig, DeviceConnectionParameters

    if protocol.lower() == "tpap":
        dev_config = DeviceConfig(
            host=host,
            timeout=timeout,
            credentials=Credentials(username, password),
            connection_type=DeviceConnectionParameters.from_values(
                "SMART.TAPOPLUG",
                "TPAP",
                login_version=2,
                http_port=80,
            ),
        )
    else:
        dev_config = DeviceConfig(
            host=host,
            timeout=timeout,
            credentials=Credentials(username, password),
        )

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            return KasaPlug(await Device.connect(config=dev_config))
        except (KasaException, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(1)
    assert last_exc is not None
    raise last_exc
