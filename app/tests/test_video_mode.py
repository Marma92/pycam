import asyncio
import os
import tempfile
import unittest

from pycam import video
from pycam.settings import Settings, load_settings
from tests.test_drive import DriveCase


class VideoModeTest(DriveCase):
    def calls(self, name):
        return [params for cmd, params in self.engine.calls if cmd == name]

    async def video_mode(self):
        self.camera.on_action("video")
        await asyncio.sleep(0.01)

    async def test_mode_and_its_exposure(self):
        self.settings.exposure.shutter, self.settings.exposure.iso = 4, 100  # a photo in M
        await self.video_mode()
        self.assertEqual(self.settings.mode, "video")
        self.assertEqual(self.calls("video_mode")[-1], {"on": True, "width": 1920, "height": 1080, "fps": 25, "bitrate": 12_000_000})
        controls = self.engine.last("set_controls")["controls"]
        self.assertEqual((controls["ExposureTimeMode"], controls["AnalogueGainMode"]), ("Auto", "Auto"))  # not the photo's M
        view = self.camera.view()
        self.assertEqual((view.video_mode, view.mode, view.formats), (True, "V", "1080p 25 i/s · Normale"))
        self.assertTrue(view.shots.endswith(" min"))
        await self.video_mode()  # back to photo
        self.assertEqual(self.calls("video_mode")[-1], {"on": False})
        self.assertEqual(self.engine.last("set_controls")["controls"]["ExposureTime"], 4_000_000)

    async def test_recording(self):
        await self.video_mode()
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        path = self.calls("record_start")[0]["path"]
        self.assertTrue(path.endswith(os.path.join("100PYCAM", "PYV_0001.MP4")))
        self.assertEqual(load_settings(os.path.join(self.tmp.name, "settings.json")).next_number, 2)  # the number is taken
        self.assertEqual(self.camera.view().recording, "0:00")
        self.camera.on_event({"event": "record_progress", "path": path, "duration": 12.3, "bytes": 18_000_000})
        self.assertEqual(self.camera.view().recording, "0:12")
        self.camera.on_action("play")  # no playback while recording
        self.assertIsNone(self.camera.playback)
        self.camera.on_action("video")  # nor back to photo
        self.assertEqual(self.settings.mode, "video")
        self.camera.on_action("shutter")  # stops
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("record_stop")), 1)
        self.camera.on_event({"event": "record_done", "path": path, "duration": 12.8, "frames": 320, "bytes": 19_000_000})
        self.assertIsNone(self.camera.recording)
        self.assertEqual(self.camera.view().message, "PYV_0001 · 0:12")

    async def test_dials(self):
        await self.video_mode()
        self.camera.on_action("front_dial_plus")  # the compensation
        self.assertAlmostEqual(self.settings.exposure.ev, 1 / 3)
        self.camera.on_event({"event": "stats", "analogue_gain": 3.9, "exposure_us": 20000})
        self.camera.on_action("rear_dial_plus")  # the ISO, from the measure
        self.assertEqual(self.settings.video_iso, 400)
        self.camera.on_action("rear_dial_plus")
        self.assertEqual(self.settings.video_iso, 500)
        await asyncio.sleep(0.01)
        self.assertEqual(self.engine.last("set_controls")["controls"]["AnalogueGain"], 5.0)
        self.camera.on_action("iso_auto")
        self.assertEqual(self.settings.video_iso, 0)
        self.camera.on_action("loupe")
        self.assertIn("Pas en vidéo", self.camera.view().message)

    async def test_settings_wait_for_the_end_of_a_recording(self):
        await self.video_mode()
        self.camera.on_action("q")
        self.assertEqual(self.camera.menu.title, "Réglages vidéo")
        self.camera.on_action("shutter")  # closes the menu and records
        await asyncio.sleep(0.01)
        path = self.calls("record_start")[0]["path"]
        self.camera.on_action("q")
        for _ in range(len(self.camera.menu.items)):
            if self.camera.menu.item.label == "Définition vidéo":
                break
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")  # 720p
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("video_mode")), 1)  # not now
        self.camera.on_event({"event": "record_done", "path": path, "duration": 3, "frames": 75, "bytes": 1})
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls("video_mode")[-1]["width"], 1280)

    async def test_failed_recording(self):
        await self.video_mode()
        self.engine.refuse["record_start"] = "busy"
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        self.assertIsNone(self.camera.recording)
        self.assertIn("impossible", self.camera.view().message)


class HelpersTest(unittest.TestCase):
    def test_bitrates_and_time_left(self):
        s = Settings(video_resolution="720p", video_quality="high")
        self.assertEqual(video.engine_setup(s), {"width": 1280, "height": 720, "fps": 25, "bitrate": 10_000_000})
        self.assertEqual(video.minutes_left(s, 75 * 10**8), 100)  # 7.5 GB at 10 Mbit/s

    def test_bad_saved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"mode": "cinema", "video_resolution": "4k", "video_quality": "ultra", "video_iso": 123}')
            s = load_settings(path)
        self.assertEqual((s.mode, s.video_resolution, s.video_quality, s.video_iso), ("photo", "1080p", "normal", 0))


if __name__ == "__main__":
    unittest.main()
