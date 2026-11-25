"""Unit tests for the Tapo P105 single-slot countdown recovery timer."""

import asyncio
import copy
import unittest
from types import SimpleNamespace

from tspt.tpap import KasaPlug


class CountdownProtocol:
    """Models the Tapo P105 single active countdown rule slot."""

    def __init__(self, timer=None, *, ignore_writes=False):
        self.rule = timer
        self.ignore_writes = ignore_writes
        self.writes = 0

    async def query(self, request):
        method, params = next(iter(request.items()))
        if method == "get_countdown_rules":
            return {
                method: {
                    "enable": bool(self.rule and self.rule["enable"]),
                    "rule_list": [copy.deepcopy(self.rule)] if self.rule else [],
                }
            }
        self.writes += 1
        if method == "add_countdown_rule" and self.rule and self.rule["enable"]:
            raise RuntimeError("P105 countdown slot already occupied")
        if method == "edit_countdown_rule" and params["id"] != self.rule["id"]:
            raise RuntimeError("Unknown countdown ID")
        if not self.ignore_writes:
            self.rule = {"id": "C1", **params, "remain": params["delay"]}
        return {method: {}}


def make_rule(remaining: int) -> dict:
    return {
        "id": "C1",
        "enable": True,
        "delay": 900,
        "remain": remaining,
        "desired_states": {"on": True},
    }


class TpapTimerTests(unittest.TestCase):
    def make_plug(self, protocol):
        return KasaPlug(SimpleNamespace(protocol=protocol))

    def test_renewal_uses_edit_on_occupied_single_slot(self):
        protocol = CountdownProtocol(make_rule(550))
        asyncio.run(self.make_plug(protocol).ensure_recovery())
        self.assertEqual(protocol.rule["remain"], 900)
        self.assertEqual(protocol.rule["desired_states"], {"on": True})

    def test_empty_slot_arms_new_countdown(self):
        protocol = CountdownProtocol()
        asyncio.run(self.make_plug(protocol).ensure_recovery())
        self.assertEqual(protocol.rule["remain"], 900)

    def test_healthy_countdown_skips_redundant_write(self):
        protocol = CountdownProtocol(make_rule(850))
        asyncio.run(self.make_plug(protocol).ensure_recovery())
        self.assertEqual(protocol.writes, 0)

    def test_unverified_write_raises(self):
        protocol = CountdownProtocol(make_rule(550), ignore_writes=True)
        with self.assertRaises(RuntimeError):
            asyncio.run(self.make_plug(protocol).ensure_recovery())

    def test_cancellation_disables_and_verifies(self):
        protocol = CountdownProtocol(make_rule(850))
        plug = self.make_plug(protocol)
        asyncio.run(plug.cancel_recovery())
        self.assertIsNone(asyncio.run(plug.timer()))


if __name__ == "__main__":
    unittest.main()
