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
    def __init__(self):
        self.reviewed = []

    def close(self):
        pass

    def draw(self, view):
        pass

    def load_review(self, path):
        self.reviewed.append(path)
        return [0] * 64


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
        self.assertEqual(names, ["list_controls", "configure", "set_controls", "focus_aids", "loupe"])
        self.assertEqual(self.engine.last("loupe")["zoom"], 1)
        self.assertEqual(self.camera.slowest_shutter, 10)

    async def test_focus_aids(self):
        self.camera.on_action("peaking")
        await self.settle()
        self.assertTrue(self.engine.last("focus_aids")["peaking"])
        self.assertEqual(self.camera.view().peaking, "red")
        self.camera.on_action("peaking_color")
        self.camera.on_action("peaking_sensitivity")
        self.camera.on_action("zebra")
        await self.settle()
        sent = self.engine.last("focus_aids")
        self.assertEqual((sent["peaking_color"], sent["peaking_sensitivity"], sent["zebra"]), ("yellow", "high", True))

    async def test_loupe_cycles_and_the_arrows_move_it(self):
        self.camera.on_action("loupe")
        await self.settle()
        self.assertEqual(self.engine.last("loupe")["zoom"], 5)
        self.camera.on_action("shutter_faster")  # right arrow: moves the loupe, not the speed
        await self.settle()
        self.assertEqual(self.engine.last("loupe")["center"], [0.6, 0.5])
        self.assertIsNone(self.settings.exposure.shutter)
        for _ in range(10):  # never past the edge of the image
            self.camera.on_action("shutter_faster")
        await self.settle()
        self.assertAlmostEqual(self.engine.last("loupe")["center"][0], 0.9)
        self.camera.on_action("loupe")
        self.camera.on_action("loupe")
        await self.settle()
        self.assertEqual(self.engine.last("loupe")["zoom"], 1)

    async def test_shutter_leaves_the_loupe(self):
        self.camera.on_action("loupe")
        self.camera.on_action("shutter")
        await self.settle()
        self.assertEqual(self.camera.loupe, 1)
        self.assertTrue(self.camera.view().grid)

    async def shoot_and_save(self):
        """A photo goes through the whole engine cycle: accepted, captured, written."""
        self.camera.on_action("shutter")
        await self.settle()
        capture_id = self.camera.last_shot.capture_id
        base = self.engine.last("capture")["path"]
        self.camera.on_event({"event": "captured", "capture_id": capture_id, "exposure_us": 8000, "analogue_gain": 4.0})
        self.camera.on_event({"event": "file_saved", "capture_id": capture_id, "path": base + ".JPG"})
        self.camera.on_event({"event": "file_saved", "capture_id": capture_id, "path": base + ".DNG"})
        self.camera.on_event({"event": "capture_done", "capture_id": capture_id, "files": [base + ".JPG", base + ".DNG"]})
        for _ in range(3):  # load_review runs in a thread
            await asyncio.sleep(0.05)
        return base

    async def test_review_after_the_shot(self):
        self.camera.hud = FakeHud()
        base = await self.shoot_and_save()
        self.assertEqual(self.camera.hud.reviewed, [base + ".JPG"])
        view = self.camera.view()
        self.assertEqual(view.review, base + ".JPG")
        self.assertIn("1/125", view.review_info)
        self.assertIn("ISO 400", view.review_info)

    async def test_any_key_ends_the_review_but_the_shutter_still_shoots(self):
        self.camera.hud = FakeHud()
        await self.shoot_and_save()
        self.camera.on_action("iso_up")
        self.assertIsNone(self.camera.review)
        self.assertIsNone(self.settings.exposure.iso)  # the key only ended the review
        await self.shoot_and_save()
        self.camera.on_action("shutter")
        await self.settle()
        self.assertIsNone(self.camera.review)
        self.assertEqual(len([c for c, _ in self.engine.calls if c == "capture"]), 3)

    async def test_no_review_when_switched_off(self):
        self.camera.hud = FakeHud()
        self.settings.review_seconds = 0
        await self.shoot_and_save()
        self.assertEqual(self.camera.hud.reviewed, [])
        self.assertIsNone(self.camera.review)

    async def test_lens_and_aperture_go_into_the_exif(self):
        self.camera.on_action("shutter")
        await self.settle()
        self.assertEqual(self.engine.last("capture")["exif"], [])  # no lens chosen
        self.camera.on_action("lens")  # first default profile: 6 mm f/1.2
        self.camera.on_action("lens")  # 16 mm f/1.4
        self.camera.on_action("aperture")  # f/1.4
        self.camera.on_action("aperture")  # f/2
        self.camera.on_action("aperture")  # f/2.8
        self.camera.on_action("shutter")
        await self.settle()
        exif = self.engine.last("capture")["exif"]
        self.assertIn("EXIF.FocalLength=1600/100", exif)
        self.assertIn("EXIF.FNumber=280/100", exif)
        self.assertEqual(self.camera.view().lens, "16 mm f/2.8")

    async def test_changing_lens_forgets_the_aperture(self):
        self.camera.on_action("lens")
        self.camera.on_action("aperture")
        self.camera.on_action("lens")
        self.assertIsNone(self.settings.aperture)
        self.camera.on_action("lens")  # back to no lens
        self.assertEqual(self.camera.view().lens, "")
        self.camera.on_action("aperture")  # refused without a lens
        self.assertIsNone(self.settings.aperture)

    async def test_changing_the_peaking_colour_turns_it_on(self):
        self.camera.on_action("peaking_color")
        await self.settle()
        self.assertTrue(self.engine.last("focus_aids")["peaking"])

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
