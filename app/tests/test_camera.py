import asyncio
import os
import tempfile
import unittest

from pycam.camera import Camera
from pycam.engine_client import EngineError
from pycam.settings import Settings, load_settings


class FakeEngine:
    """Records commands; `refuse` maps a command to the error code to answer."""

    def __init__(self):
        self.path = "/nonexistent.sock"
        self.connected = True
        self.calls = []
        self.refuse = {}
        self.on_event = self.on_connect = self.on_disconnect = None

    async def call(self, cmd, timeout=10.0, **params):
        self.calls.append((cmd, params))
        if cmd in self.refuse:
            raise EngineError({"ok": False, "error": self.refuse[cmd]})
        if cmd == "list_controls":
            return {"ok": True, "controls": {"ExposureTime": {"min": 60, "max": 10_000_000}}}
        if cmd == "capture":
            return {"ok": True, "capture_id": len(self.calls)}
        if cmd == "hud_info":
            return {"ok": True, "display": {}}
        return {"ok": True}

    def last(self, cmd):
        return next(params for name, params in reversed(self.calls) if name == cmd)


class FakeHud:
    def close(self):
        pass

    def draw(self, view):
        pass


class CameraTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self.tmp.name, "settings.json")
        self.settings = Settings(dcim=os.path.join(self.tmp.name, "DCIM"))
        self.engine = FakeEngine()
        self.camera = Camera(self.settings, self.settings_path, self.engine)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_sync_pushes_the_whole_state(self):
        await self.camera.sync_engine()
        names = [name for name, _ in self.engine.calls]
        self.assertEqual(names, ["list_controls", "configure", "set_controls"])
        self.assertEqual(self.camera.slowest_shutter, 10)

    async def test_hud_set_up_when_the_engine_gets_the_screen_later(self):
        made = []
        self.camera.hud_factory = lambda info: made.append(info) or FakeHud()
        self.engine.refuse["hud_info"] = "unsupported"
        await self.camera.sync_engine()
        self.assertIsNone(self.camera.hud)
        del self.engine.refuse["hud_info"]
        self.camera.on_event({"event": "display_ready"})
        await self.settle()
        self.assertIsNotNone(self.camera.hud)
        self.assertEqual(len(made), 1)

    async def test_dial_goes_manual_from_the_measure(self):
        self.camera.on_event({"event": "stats", "exposure_us": 16667, "analogue_gain": 2.0, "digital_gain": 1.0})
        self.camera.on_action("shutter_faster")
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["ExposureTime"], 12500)  # 1/80
        self.assertEqual(self.camera.view().mode, "S")
        self.camera.on_action("iso_up")
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["AnalogueGain"], 2.5)  # ISO 250
        self.assertEqual(self.camera.view().mode, "M")

    async def test_captures_are_numbered_and_the_number_is_saved(self):
        self.camera.on_action("shutter")
        self.camera.on_action("shutter")
        await self.settle()
        paths = [params["path"] for name, params in self.engine.calls if name == "capture"]
        self.assertEqual([os.path.basename(p) for p in paths], ["PYC_0001", "PYC_0002"])
        self.assertEqual(load_settings(self.settings_path).next_number, 3)
        self.assertEqual(self.camera.writing, 2)

    async def test_busy_does_not_use_a_number(self):
        self.engine.refuse["capture"] = "busy"
        self.camera.on_action("shutter")
        await self.settle()
        self.assertEqual(self.settings.next_number, 1)
        self.assertIn("Occupé", self.camera.view().message)

    async def test_raw_toggle_changes_the_formats(self):
        self.camera.on_action("raw")
        self.camera.on_action("shutter")
        await self.settle()
        self.assertEqual(self.engine.last("capture")["formats"], ["jpeg"])

    async def test_strategy_is_kept_only_if_the_engine_accepts(self):
        self.engine.refuse["configure"] = "busy"
        self.camera.on_action("strategy")
        await self.settle()
        self.assertEqual(self.settings.strategy, "switch")
        del self.engine.refuse["configure"]
        self.camera.on_action("strategy")
        await self.settle()
        self.assertEqual(self.settings.strategy, "zsl")
        self.assertTrue(self.camera.view().zsl)


if __name__ == "__main__":
    unittest.main()
