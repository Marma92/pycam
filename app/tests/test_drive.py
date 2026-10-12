import asyncio
import os
import tempfile
import unittest

from pycam import drive, looks
from pycam.camera import Camera
from pycam.engine_client import EngineError
from pycam.settings import Settings, load_settings
from tests.test_camera import FakeEngine, FakeHud

try:
    from evdev import ecodes

    from pycam.inputs import Inputs

    HAVE_EVDEV = True
except ImportError:  # the inputs are read on the camera
    HAVE_EVDEV = False


def manual(controls):
    return controls.get("ExposureTimeMode") == controls.get("AnalogueGainMode") == "Manual"


class Engine(FakeEngine):
    """Takes each photo at once ("captured"), refuses `busy` captures first ("busy"), and
    runs the sensor at the exposure asked in manual (a stats event)."""

    def __init__(self):
        super().__init__()
        self.busy = 0
        self.shot_times = []
        self.clock = lambda: 0.0

    async def call(self, cmd, timeout=10.0, **params):
        if cmd == "capture" and self.busy:
            self.busy -= 1
            self.calls.append((cmd, params))
            raise EngineError({"ok": False, "error": "busy"})
        reply = await super().call(cmd, timeout, **params)
        loop = asyncio.get_running_loop()
        if cmd == "capture":
            self.shot_times.append(self.clock())
            loop.call_soon(self.on_event, {"event": "captured", "capture_id": reply["capture_id"]})
        elif cmd == "set_controls" and manual(params["controls"]):
            c = params["controls"]
            loop.call_soon(
                self.on_event, {"event": "stats", "exposure_us": c["ExposureTime"], "analogue_gain": c["AnalogueGain"]}
            )
        return reply

    def captures(self):
        return [params for name, params in self.calls if name == "capture"]


class DriveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME")}
        os.environ["XDG_CACHE_HOME"] = os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        looks.refresh()
        self.settings = Settings(dcim=os.path.join(self.tmp.name, "DCIM"))
        self.engine = Engine()
        self.camera = Camera(self.settings, os.path.join(self.tmp.name, "settings.json"), self.engine)
        # Time passes only when the drive sleeps: sequences of minutes in an instant.
        self.now = 0.0

        async def sleep(seconds):
            self.now += seconds
            await asyncio.sleep(0)

        self.camera.drive.sleep = sleep
        self.camera.drive.clock = self.engine.clock = lambda: self.now

    async def asyncTearDown(self):
        self.camera.drive.stop()
        await asyncio.sleep(0)
        await asyncio.gather(*self.camera._tasks, return_exceptions=True)
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        looks.refresh()
        self.tmp.cleanup()

    async def finished(self):
        """Until the sequence is over."""
        for _ in range(10_000):
            if not self.camera.drive.active:
                return
            await asyncio.sleep(0)
        self.fail("the sequence never ends")

    async def test_single_is_one_photo_per_press(self):
        self.camera.on_action("shutter")
        self.camera.on_action("shutter_release")
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.engine.captures()), 1)
        self.assertFalse(self.camera.drive.active)
        self.assertEqual(self.camera.view().drive, "")

    async def test_burst_while_the_shutter_is_held(self):
        self.settings.drive = "burst"
        self.assertEqual(self.camera.view().drive, "RAFALE")
        self.camera.on_action("shutter")
        await self.finished()  # held all along: up to the limit
        self.assertEqual(len(self.engine.captures()), drive.BURST_MAX)
        self.assertIn("20 vues", self.camera.view().message)
        names = [os.path.basename(c["path"]) for c in self.engine.captures()]
        self.assertEqual(len(set(names)), drive.BURST_MAX)  # each its own number

    async def test_burst_released_at_once_takes_one(self):
        self.settings.drive = "burst"
        self.camera.on_action("shutter")
        self.camera.on_action("shutter_release")
        await self.finished()
        self.assertEqual(len(self.engine.captures()), 1)

    async def test_burst_stops_when_released(self):
        self.settings.drive = "burst"
        self.camera.on_action("shutter")
        while len(self.engine.captures()) < 3:
            await asyncio.sleep(0)
        self.camera.on_action("shutter_release")
        await self.finished()
        self.assertLess(len(self.engine.captures()), 6)

    async def test_burst_waits_for_the_writing_queue(self):
        self.settings.drive = "burst"
        self.engine.busy = 4
        self.camera.on_action("shutter")
        self.camera.on_action("shutter_release")
        await self.finished()
        self.assertEqual(len(self.engine.captures()), 5)  # 4 refused, then the photo
        self.assertEqual(self.settings.next_number, 2)
        self.assertNotIn("Occupé", self.camera.view().message)

    async def test_self_timer(self):
        self.settings.drive = "self10"
        self.camera.on_action("shutter")
        await asyncio.sleep(0)
        view = self.camera.view()
        self.assertEqual((view.countdown, view.drive, view.drive_running), (10, "RET 10 s", True))
        await self.finished()
        self.assertEqual(self.engine.shot_times, [10.0])
        self.assertEqual(self.camera.view().countdown, 0)

    async def test_shutter_stops_the_self_timer(self):
        self.settings.drive = "self2"
        self.camera.on_action("shutter")
        await asyncio.sleep(0)
        self.camera.on_action("shutter")
        await self.finished()
        self.assertEqual(self.engine.captures(), [])
        self.assertIn("arrêtée", self.camera.view().message)

    async def test_interval(self):
        self.settings.drive, self.settings.interval_seconds, self.settings.interval_count = "interval", 5, 3
        self.assertEqual(self.camera.view().drive, "INT 5 s")
        self.camera.on_action("shutter")
        self.camera.on_action("shutter_release")
        await self.finished()
        self.assertEqual(self.engine.shot_times, [0.0, 5.0, 10.0])
        self.assertIn("3 vues", self.camera.view().message)

    async def test_interval_late_does_not_catch_up(self):
        self.settings.drive, self.settings.interval_seconds, self.settings.interval_count = "interval", 2, 3
        self.engine.busy = 50  # 5 s of a full writing queue (a retry every 0.1 s) for the first one
        self.camera.on_action("shutter")
        await self.finished()
        self.assertEqual([round(t, 1) for t in self.engine.shot_times], [5.0, 7.0, 9.0])

    async def test_interval_until_stopped(self):
        self.settings.drive, self.settings.interval_seconds, self.settings.interval_count = "interval", 60, 0
        self.camera.on_action("shutter")
        while len(self.engine.captures()) < 30:
            await asyncio.sleep(0)
        self.assertEqual(self.camera.view().drive[:4], "INT ")
        self.camera.on_action("shutter")  # stops it
        await self.finished()
        self.assertGreaterEqual(len(self.engine.captures()), 30)

    async def test_interval_stops_when_the_engine_is_gone(self):
        self.settings.drive, self.settings.interval_seconds = "interval", 2
        self.camera.on_action("shutter")
        while len(self.engine.captures()) < 2:
            await asyncio.sleep(0)
        self.engine.connected = False
        await self.finished()
        self.assertEqual(len(self.engine.captures()), 2)

    def exposures(self):
        return [
            (c["controls"]["ExposureTime"], c["controls"]["AnalogueGain"])
            for name, c in self.engine.calls
            if name == "set_controls" and manual(c["controls"])
        ]

    async def test_bracket_varies_the_speed(self):
        self.settings.drive, self.settings.bracket_shots, self.settings.bracket_step = "bracket", 3, 3
        self.assertEqual(self.camera.view().drive, "BKT 3 × 1")
        self.camera.on_event({"event": "stats", "exposure_us": 10000, "analogue_gain": 2.0, "colour_gains": [2.1, 1.6]})
        self.camera.on_action("shutter")
        await asyncio.sleep(0)
        self.assertEqual(self.engine.last("set_controls")["controls"]["ColourGains"], [2.1, 1.6])  # white balance held
        self.camera.on_action("front_dial_plus")  # the exposure is held meanwhile
        self.assertIn("Bracketing", self.camera.view().message)
        await self.finished()
        self.assertEqual(self.exposures(), [(10000, 2.0), (5000, 2.0), (20000, 2.0)])
        self.assertEqual(len(self.engine.captures()), 3)
        self.assertEqual(self.settings.exposure.ev, 0)
        # And the user's exposure again, automatic.
        controls = self.engine.last("set_controls")["controls"]
        self.assertEqual((controls["ExposureTimeMode"], controls["AwbEnable"]), ("Auto", True))

    async def test_bracket_in_s_varies_the_gain(self):
        self.settings.drive, self.settings.bracket_shots, self.settings.bracket_step = "bracket", 5, 1
        self.settings.exposure.shutter = 1 / 100
        self.settings.exposure.awb, self.settings.exposure.kelvin = "Kelvin", 5000
        self.camera.on_event({"event": "stats", "exposure_us": 10000, "analogue_gain": 2.0, "colour_gains": [2.1, 1.6]})
        self.camera.on_action("shutter")
        await self.finished()
        times, gains = zip(*self.exposures())
        self.assertEqual(set(times), {10000})
        self.assertEqual(gains, tuple(round(2 * 2**k, 3) for k in (0, -1 / 3, 1 / 3, -2 / 3, 2 / 3)))
        self.assertEqual(self.engine.last("set_controls")["controls"]["ExposureTimeMode"], "Manual")  # S: the user's speed
        bracket = [c["controls"] for name, c in self.engine.calls if name == "set_controls" and manual(c["controls"])]
        self.assertNotIn("ColourGains", bracket[0])  # a fixed white balance stays as it is

    async def test_bracket_needs_a_measure(self):
        self.settings.drive = "bracket"
        self.camera.on_action("shutter")
        await self.finished()
        self.assertEqual(self.engine.captures(), [])
        self.assertIn("mesure", self.camera.view().message)

    async def test_no_review_during_a_burst(self):
        self.camera.hud = FakeHud()
        self.settings.drive = "burst"
        self.camera.on_action("shutter")
        while not self.camera.last_shot:
            await asyncio.sleep(0)
        self.camera.on_event({"event": "file_saved", "capture_id": self.camera.last_shot.capture_id, "path": "/x/PYC_0001.JPG"})
        await asyncio.sleep(0)
        self.assertEqual(self.camera.hud.reviewed, [])
        self.camera.on_action("shutter_release")
        await self.finished()
        self.camera.on_event({"event": "file_saved", "capture_id": self.camera.last_shot.capture_id, "path": "/x/PYC_0009.JPG"})
        await asyncio.sleep(0.01)
        self.assertEqual(self.camera.hud.reviewed, ["/x/PYC_0009.JPG"])  # the last one, once it is over

    async def test_fn_button_changes_the_mode(self):
        self.settings.fn1 = "drive"
        self.camera.on_action("fn1")
        self.assertEqual((self.settings.drive, self.camera.view().message), ("burst", "Rafale"))
        for _ in range(len(drive.MODES) - 1):
            self.camera.on_action("fn1")
        self.assertEqual(self.settings.drive, "single")

    async def test_mode_from_the_quick_menu(self):
        self.camera.on_action("q")
        for _ in range(len(self.camera.menu.items)):
            if self.camera.menu.item.label == "Mode de déclenchement":
                break
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")
        self.assertEqual(self.settings.drive, "burst")
        self.assertIn(("Mode de déclenchement", "Rafale"), self.camera.view().menu)


class HelpersTest(unittest.TestCase):
    def test_bracket_offsets(self):
        self.assertEqual(drive.bracket_offsets(3, 3), [0, -1, 1])
        self.assertEqual(drive.bracket_offsets(5, 2), [0, -2 / 3, 2 / 3, -4 / 3, 4 / 3])

    def test_bracket_exposure_moves_what_is_out_of_range(self):
        self.assertEqual(drive.bracket_exposure(10000, 2.0, 1, 1e6), (20000, 2.0))
        # The speed at its slowest: the gain does the rest.
        self.assertEqual(drive.bracket_exposure(800_000, 1.0, 1, 1e6), (1_000_000, 1.6))
        # The gain at its lowest: the speed does the rest.
        self.assertEqual(drive.bracket_exposure(10000, 1.0, -1, 1e6, vary="gain"), (5000, 1.0))

    def test_labels(self):
        s = Settings(drive="interval", interval_seconds=300)
        self.assertEqual(drive.short_label(s), "INT 5 min")
        s.drive, s.bracket_shots, s.bracket_step = "bracket", 5, 1
        self.assertEqual(drive.short_label(s), "BKT 5 × 1/3")

    def test_bad_saved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"drive": "rocket", "interval_seconds": 7, "bracket_shots": 5, "bracket_step": 9}')
            s = load_settings(path)
        self.assertEqual((s.drive, s.interval_seconds, s.bracket_shots, s.bracket_step), ("single", 10, 5, 3))


@unittest.skipUnless(HAVE_EVDEV, "evdev")
class InputsTest(unittest.TestCase):
    def event(self, code, value):
        class Event:
            type = ecodes.EV_KEY

        e = Event()
        e.code, e.value = code, value
        return e

    def test_shutter_release(self):
        self.assertEqual(Inputs._actions(self.event(ecodes.KEY_SPACE, 1), False), ["shutter"])
        self.assertEqual(Inputs._actions(self.event(ecodes.KEY_SPACE, 2), False), [])  # held: no repeat
        self.assertEqual(Inputs._actions(self.event(ecodes.KEY_SPACE, 0), False), ["shutter_release"])
        self.assertEqual(Inputs._actions(self.event(ecodes.KEY_TAB, 0), False), [])


if __name__ == "__main__":
    unittest.main()
