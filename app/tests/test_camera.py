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
        return [0] * 64, [[1] * 64, [2] * 64, [3] * 64]


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

    def menu_line(self, label, quick=False):
        """Opens a menu (unless one is open) and turns the rear dial down to the line `label`."""
        if not self.camera.menu:
            self.camera.on_action("q" if quick else "menu")
        for _ in range(len(self.camera.menu.items)):
            if self.camera.menu.item.label == label:
                return
            self.camera.on_action("rear_dial_minus")
        self.fail(f"no line {label}")

    async def test_sync_pushes_the_whole_state(self):
        await self.camera.sync_engine()
        names = [name for name, _ in self.engine.calls]
        self.assertEqual(names, ["list_controls", "configure", "set_controls", "set_controls", "focus_aids", "loupe"])
        self.assertEqual(self.engine.last("loupe")["zoom"], 1)
        configure = self.engine.last("configure")
        self.assertEqual((configure["still_denoise"], configure["still_size"]), ("cdn_hq", None))
        self.assertEqual(self.engine.last("set_controls")["controls"]["Contrast"], 1.0)
        exposure = next(p["controls"] for n, p in self.engine.calls if n == "set_controls" and "ExposureValue" in p["controls"])
        self.assertEqual(exposure["AeMeteringMode"], "MeteringCentreWeighted")
        self.assertEqual(self.camera.slowest_shutter, 10)

    async def test_quick_menu(self):
        self.camera.on_action("q")
        view = self.camera.view()
        self.assertEqual((view.menu_title, view.menu_index), ("Réglages rapides", 0))
        self.camera.on_action("front_dial_plus")  # line 1, metering: spot
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["AeMeteringMode"], "MeteringSpot")
        self.assertEqual(self.camera.view().metering, "spot")
        self.assertEqual(self.camera.view().menu[0], ("Mesure de la lumière", "Spot"))
        self.menu_line("Netteté")
        self.camera.on_action("front_dial_plus")
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["Sharpness"], 1.5)
        self.camera.on_action("rear_dial_minus")  # contrast
        self.camera.on_action("rear_dial_minus")  # saturation
        for _ in range(5):
            self.camera.on_action("front_dial_minus")  # down to black and white, and stays there
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["Saturation"], 0.0)
        self.assertEqual(self.camera.view().menu[7], ("Saturation", "N&B"))
        self.camera.on_action("rear_dial_minus")  # denoise: an engine setting
        self.camera.on_action("front_dial_minus")
        await self.settle()
        self.assertEqual(self.engine.last("configure")["still_denoise"], "cdn_fast")
        self.camera.on_action("rear_dial_plus")  # the arrows stay in the menu: exposure untouched
        self.assertIsNone(self.settings.exposure.iso)
        self.camera.on_action("q")  # closes
        self.assertEqual(self.camera.view().menu, ())

    async def test_full_menu(self):
        self.camera.on_action("menu")
        view = self.camera.view()
        self.assertEqual((view.menu_title, view.menu[0]), ("Menu", ("Exposition", None)))
        self.assertEqual(view.menu_index, 1)  # the first setting, not its section title
        self.camera.on_action("rear_dial_plus")  # around, to the last line
        self.assertEqual(self.camera.menu.item.label, "Bouton Fn2")
        self.camera.on_action("rear_dial_minus")
        self.assertEqual(self.camera.menu.item.label, "Mesure de la lumière")
        self.menu_line("Zébras")
        self.camera.on_action("rear_dial_minus")  # past the "Image" title
        self.assertEqual(self.camera.menu.item.label, "Netteté")
        self.camera.on_action("q")  # from the full menu to the quick one
        self.assertTrue(self.camera.menu.quick)
        self.camera.on_action("menu")  # closes it
        self.assertIsNone(self.camera.menu)

    async def test_menu_button_leaves_the_loupe_first(self):
        self.camera.on_action("loupe")
        self.camera.on_action("menu")
        await self.settle()
        self.assertEqual((self.camera.loupe, self.camera.menu), (1, None))
        self.camera.on_action("menu")
        self.assertFalse(self.camera.menu.quick)

    async def test_fn_buttons(self):
        self.camera.on_action("fn1")  # peaking, by default
        await self.settle()
        self.assertTrue(self.engine.last("focus_aids")["peaking"])
        self.menu_line("Bouton Fn2")
        self.camera.on_action("front_dial_plus")  # zebras -> lock the white balance
        self.assertEqual(self.camera.view().menu[-1], ("Bouton Fn2", "Mémoriser la balance"))
        self.camera.on_action("menu")
        self.camera.on_event({"event": "stats", "colour_gains": [2.0, 1.5]})
        self.camera.on_action("fn2")
        await self.settle()
        self.assertEqual((self.settings.exposure.awb, self.settings.exposure.wb_gains), ("Locked", [2.0, 1.5]))
        self.assertEqual(self.engine.last("set_controls")["controls"]["ColourGains"], [2.0, 1.5])

    async def test_white_balance_in_the_menu(self):
        e = self.settings.exposure
        self.camera.on_event({"event": "stats", "colour_temp": 4480, "colour_gains": [2.5, 1.6]})
        self.menu_line("Température", quick=True)
        self.assertEqual(self.camera.view().menu[2], ("Température", "≈ 4500 K"))  # measured: not in use
        self.camera.on_action("front_dial_plus")  # from the auto balance: the measured temperature first
        await self.settle()
        self.assertEqual((e.awb, e.kelvin), ("Kelvin", 4500))
        self.assertEqual(self.engine.last("set_controls")["controls"]["ColourTemperature"], 4500)
        self.camera.on_action("front_dial_plus")
        self.assertEqual(e.kelvin, 4800)
        self.camera.on_action("rear_dial_plus")  # the white balance line
        self.assertEqual(self.camera.view().menu[1], ("Balance des blancs", "Température"))
        self.camera.on_action("front_dial_plus")  # Kelvin -> Locked: keeps the gains measured now
        await self.settle()
        self.assertEqual((e.awb, e.wb_gains), ("Locked", [2.5, 1.6]))
        self.assertEqual(self.camera.view().menu[1], ("Balance des blancs", "Mémorisée"))
        self.camera.on_action("front_dial_minus")
        self.camera.stats = {}
        self.camera.on_action("front_dial_plus")  # nothing measured: stays where it was
        self.assertEqual(e.awb, "Kelvin")
        self.assertIn("Pas de mesure", self.camera.view().message)

    async def test_shutter_closes_the_menu_and_shoots(self):
        self.camera.on_action("q")
        self.camera.on_action("shutter")
        await self.settle()
        self.assertIsNone(self.camera.menu)
        self.assertEqual(len([c for c, _ in self.engine.calls if c == "capture"]), 1)

    async def test_focus_aids(self):
        self.camera.on_action("peaking")
        await self.settle()
        self.assertTrue(self.engine.last("focus_aids")["peaking"])
        self.assertEqual(self.camera.view().peaking, "red")
        self.menu_line("Peaking")
        self.camera.on_action("front_dial_plus")  # red -> yellow
        self.camera.on_action("rear_dial_minus")  # sensitivity
        self.camera.on_action("front_dial_plus")  # medium -> high
        self.camera.on_action("rear_dial_minus")  # zebras
        self.camera.on_action("front_dial_plus")
        await self.settle()
        sent = self.engine.last("focus_aids")
        self.assertEqual((sent["peaking_color"], sent["peaking_sensitivity"], sent["zebra"]), ("yellow", "high", True))

    async def test_loupe_cycles_and_the_arrows_move_it(self):
        self.camera.on_action("loupe")
        await self.settle()
        self.assertEqual(self.engine.last("loupe")["zoom"], 5)
        self.camera.on_action("front_dial_plus")  # right arrow: moves the loupe, not the speed
        await self.settle()
        self.assertEqual(self.engine.last("loupe")["center"], [0.6, 0.5])
        self.assertIsNone(self.settings.exposure.shutter)
        for _ in range(10):  # never past the edge of the image
            self.camera.on_action("front_dial_plus")
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
        self.assertEqual([c[0] for c in view.histogram_rgb], [1, 2, 3])
        self.settings.histogram = "luma"
        self.assertEqual((len(self.camera.view().histogram), self.camera.view().histogram_rgb), (64, ()))

    async def test_histograms(self):
        rgb = [[i] * 64 for i in range(3)]
        self.camera.on_event({"event": "stats", "histogram": [9] * 64, "histogram_rgb": rgb})
        view = self.camera.view()
        self.assertEqual((view.histogram, view.histogram_rgb), ((), tuple(tuple(c) for c in rgb)))
        self.settings.histogram = "luma"
        view = self.camera.view()
        self.assertEqual((view.histogram, view.histogram_rgb), ((9,) * 64, ()))
        self.settings.histogram = "rgb"
        self.camera.on_event({"event": "stats", "histogram": [9] * 64})  # an older engine: luminance only
        self.assertEqual(self.camera.view().histogram, (9,) * 64)
        self.settings.display = "simple"
        view = self.camera.view()
        self.assertEqual((view.histogram, view.histogram_rgb), ((), ()))

    async def test_any_key_ends_the_review_but_the_shutter_still_shoots(self):
        self.camera.hud = FakeHud()
        await self.shoot_and_save()
        self.camera.on_action("rear_dial_plus")
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

    async def test_meter_in_manual_and_warning_out_of_range(self):
        self.settings.meter_k = 80 * 40000 * 4.0
        base = {"event": "stats", "lux": 80, "digital_gain": 1.0, "ae_converged": True}
        self.camera.on_event(dict(base, exposure_us=40000, analogue_gain=4.0))
        self.assertIsNone(self.camera.view().meter)  # P, on target: nothing to say
        self.settings.exposure.shutter, self.settings.exposure.iso = 1 / 50, 400  # M
        self.camera.on_event(dict(base, exposure_us=20000, analogue_gain=4.0))
        view = self.camera.view()
        self.assertAlmostEqual(view.meter, -1)
        self.assertFalse(view.meter_warning)
        self.settings.exposure.shutter = None  # A: the automatic exposure is stuck 2 stops under
        self.camera.on_event(dict(base, exposure_us=10000, analogue_gain=4.0))
        view = self.camera.view()
        self.assertAlmostEqual(view.meter, -2)
        self.assertTrue(view.meter_warning)

    async def test_lens_and_aperture_go_into_the_exif(self):
        self.camera.on_action("shutter")
        await self.settle()
        self.assertEqual(self.engine.last("capture")["exif"], [])  # no lens chosen
        self.menu_line("Objectif")
        self.camera.on_action("front_dial_plus")  # first default profile: 6 mm f/1.2
        self.camera.on_action("front_dial_plus")  # 16 mm f/1.4
        self.camera.on_action("rear_dial_minus")  # aperture
        for _ in range(3):  # f/1.4, f/2, f/2.8
            self.camera.on_action("front_dial_plus")
        self.camera.on_action("shutter")  # closes the menu and shoots
        await self.settle()
        exif = self.engine.last("capture")["exif"]
        self.assertIn("EXIF.FocalLength=1600/100", exif)
        self.assertIn("EXIF.FNumber=280/100", exif)
        self.assertEqual(self.camera.view().lens, "16 mm f/2.8")

    async def test_changing_lens_forgets_the_aperture(self):
        self.menu_line("Objectif")
        self.camera.on_action("front_dial_plus")
        self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")
        self.assertEqual(self.settings.aperture, 1.2)
        self.camera.on_action("rear_dial_plus")
        self.camera.on_action("front_dial_plus")  # another lens
        self.assertIsNone(self.settings.aperture)
        self.camera.on_action("front_dial_minus")
        self.camera.on_action("front_dial_minus")  # back to no lens
        self.assertEqual(self.camera.view().lens, "")
        self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")  # nothing to choose without a lens
        self.assertIsNone(self.settings.aperture)
        self.assertIn(("Ouverture", "Choisir l'objectif"), self.camera.view().menu)

    async def test_choosing_a_peaking_colour_turns_it_on(self):
        self.menu_line("Peaking", quick=True)
        self.camera.on_action("front_dial_plus")
        await self.settle()
        self.assertTrue(self.engine.last("focus_aids")["peaking"])
        self.assertEqual(self.engine.last("focus_aids")["peaking_color"], "red")

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

    async def test_dials_set_their_value_when_manual_else_the_compensation(self):
        self.camera.on_event({"event": "stats", "exposure_us": 16667, "analogue_gain": 2.0, "digital_gain": 1.0})
        self.camera.on_action("front_dial_plus")  # P: the compensation
        await self.settle()
        controls = self.engine.last("set_controls")["controls"]
        self.assertAlmostEqual(controls["ExposureValue"], 1 / 3)
        self.assertEqual(controls["ExposureTimeMode"], "Auto")
        self.assertEqual((self.camera.view().mode, self.camera.view().ev), ("P", "+0.3"))
        self.camera.on_action("shutter_auto")  # the dial press: manual speed, from the measure (1/60)
        self.camera.on_action("front_dial_plus")  # S: the speed, faster
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["ExposureTime"], 12500)  # 1/80
        self.assertEqual(self.camera.view().mode, "S")
        self.assertAlmostEqual(self.settings.exposure.ev, 1 / 3)  # kept for the automatic ISO
        self.camera.on_action("rear_dial_plus")  # S: the rear dial is the compensation
        self.assertAlmostEqual(self.settings.exposure.ev, 2 / 3)
        self.camera.on_action("iso_auto")  # its press: manual ISO, from the measure (200)
        self.camera.on_action("rear_dial_plus")  # M: the ISO
        await self.settle()
        self.assertEqual(self.engine.last("set_controls")["controls"]["AnalogueGain"], 2.5)  # ISO 250
        self.assertEqual(self.camera.view().mode, "M")
        self.camera.on_action("shutter_auto")  # A: the front dial is the compensation again
        self.camera.on_action("front_dial_minus")
        self.camera.on_action("front_dial_minus")
        self.assertEqual((self.camera.view().mode, self.settings.exposure.ev), ("A", 0))
        self.camera.on_action("iso_auto")  # P: both dials are the compensation
        self.camera.on_action("rear_dial_minus")
        self.assertEqual(self.camera.view().mode, "P")
        self.assertAlmostEqual(self.settings.exposure.ev, -1 / 3)

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

    async def test_dng_compression_is_on_by_default_and_can_be_switched_off(self):
        self.camera.on_action("shutter")
        await self.settle()
        self.assertTrue(self.engine.last("capture")["dng_compression"])
        shots_compressed = self.camera.view().shots
        self.menu_line("RAW compressé")
        self.camera.on_action("front_dial_plus")
        self.camera.on_action("shutter")
        await self.settle()
        self.assertFalse(self.engine.last("capture")["dng_compression"])
        self.assertLess(self.camera.view().shots, shots_compressed)  # bigger files, fewer shots

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

    async def test_strategy_from_the_menu(self):
        self.engine.refuse["configure"] = "busy"
        self.menu_line("Déclenchement", quick=True)
        self.camera.on_action("front_dial_plus")
        await self.settle()
        self.assertEqual(self.settings.strategy, "switch")
        del self.engine.refuse["configure"]
        self.camera.on_action("front_dial_plus")
        await self.settle()
        self.assertEqual(self.engine.last("configure")["strategy"], "zsl")
        self.assertIn(("Déclenchement", "ZSL · sans latence"), self.camera.view().menu)


if __name__ == "__main__":
    unittest.main()
