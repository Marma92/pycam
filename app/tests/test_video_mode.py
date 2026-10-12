import asyncio
import datetime
import os
import shutil
import subprocess
import tempfile
import types
import unittest

from pycam import video
from pycam.camera import Video
from pycam.settings import Settings, load_settings
from pycam.view import guide_rect
from tests.test_drive import DriveCase


class VideoModeTest(DriveCase):
    def calls(self, name):
        return [params for cmd, params in self.engine.calls if cmd == name]

    def picture_controls(self):
        """The last contrast and saturation sent to the ISP."""
        return [c["controls"] for c in self.calls("set_controls") if "Saturation" in c["controls"]][-1]

    def exposure_controls(self):
        """The last exposure sent (the picture's controls follow a change of mode)."""
        return [c["controls"] for c in self.calls("set_controls") if "ExposureTimeMode" in c["controls"]][-1]

    async def video_mode(self):
        self.camera.on_action("video")
        await asyncio.sleep(0.01)

    async def test_mode_and_its_exposure(self):
        self.settings.exposure.shutter, self.settings.exposure.iso = 4, 100  # a photo in M
        await self.video_mode()
        self.assertEqual(self.settings.mode, "video")
        self.assertEqual(self.calls("video_mode")[-1], {"on": True, "restart": False, "width": 1920, "height": 1080, "fps": 25, "bitrate": 12_000_000, "slowdown": 1})
        controls = self.exposure_controls()
        self.assertEqual((controls["ExposureTimeMode"], controls["AnalogueGainMode"]), ("Auto", "Auto"))  # not the photo's M
        view = self.camera.view()
        self.assertEqual((view.video_mode, view.mode, view.formats), (True, "V", "1080p25 · Normale"))
        self.assertTrue(view.shots.endswith(" min"))
        await self.video_mode()  # back to photo
        self.assertEqual(self.calls("video_mode")[-1], {"on": False})
        self.assertEqual(self.exposure_controls()["ExposureTime"], 4_000_000)

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

    async def test_exposure_lock(self):
        await self.video_mode()
        self.camera.on_event({"event": "stats", "analogue_gain": 2.0, "exposure_us": 10000})
        self.camera.on_action("shutter_auto")  # S: AE-L
        self.assertEqual(self.camera.video_lock, (0.01, 200))
        await asyncio.sleep(0.01)
        controls = self.engine.last("set_controls")["controls"]
        self.assertEqual((controls["ExposureTime"], controls["AnalogueGain"]), (10000, 2.0))
        view = self.camera.view()
        self.assertEqual((view.ev, view.shutter, view.iso, view.shutter_auto, view.iso_auto), ("AE-L", "1/100", "ISO 200", False, False))
        self.assertIsNone(view.meter)  # the meter only when something is wrong
        self.camera.on_event({"event": "stats", "analogue_gain": 2.0, "exposure_us": 10000, "lux": 2 * 13.8e6 / 20000})
        view = self.camera.view()
        self.assertEqual((view.meter, view.meter_warning), (1.0, True))  # the scene a stop brighter than locked
        self.camera.on_action("front_dial_plus")  # no compensation while locked
        self.assertIn("verrouillée", self.camera.view().message)
        self.assertEqual(self.settings.exposure.ev, 0)
        self.camera.on_action("rear_dial_plus")  # the locked exposure, brighter
        self.assertEqual(self.camera.video_lock, (0.01, 250))
        self.camera.on_action("shutter_auto")
        self.assertIsNone(self.camera.video_lock)
        await asyncio.sleep(0.01)
        self.assertEqual(self.engine.last("set_controls")["controls"]["ExposureTimeMode"], "Auto")
        self.settings.video_shutter = "180"  # locked at 180°: the speed stays half a frame
        self.camera.on_action("shutter_auto")
        self.assertEqual(self.camera.video_lock, (1 / 50, 200))
        self.camera.on_action("video")  # leaving the video mode frees it
        self.assertIsNone(self.camera.video_lock)

    async def test_stops_before_the_card_is_full(self):
        s = self.settings
        free = [10**12]
        self.camera.storage.free_bytes = lambda: free[0]
        per_second = video.bitrate(s) / 8
        await self.video_mode()
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        path = self.calls("record_start")[0]["path"]
        free[0] = video.RESERVE_BYTES + 50 * per_second  # 50 s left
        self.camera.on_event({"event": "record_progress", "path": path, "duration": 60, "bytes": 1})
        view = self.camera.view()
        self.assertEqual((view.message, view.task), ("Carte presque pleine", "CARTE PLEINE DANS 0:40"))
        self.assertEqual(self.calls("record_stop"), [])
        free[0] = video.RESERVE_BYTES + 8 * per_second
        self.camera.on_event({"event": "record_progress", "path": path, "duration": 102, "bytes": 1})
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("record_stop")), 1)
        self.camera.on_event({"event": "record_progress", "path": path, "duration": 103, "bytes": 1})
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("record_stop")), 1)  # once
        self.camera.on_event({"event": "record_done", "path": path, "duration": 103, "frames": 2575, "bytes": 1})
        self.assertEqual(self.camera.view().message, "PYV_0001 · 1:43 · carte pleine")
        self.camera.on_action("shutter")  # too little left to start another
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("record_start")), 1)
        self.assertEqual(self.camera.view().message, "Carte pleine")

    async def test_frame_rate_in_the_menu(self):
        self.settings.video_resolution, self.settings.video_fps = "720p", 50
        await self.video_mode()
        self.assertEqual(self.calls("video_mode")[-1]["fps"], 50)
        self.camera.on_action("q")
        while self.camera.menu.item.label != "Définition vidéo":
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_minus")  # 1080p: 50 frames a second no more
        await asyncio.sleep(0.01)
        self.assertEqual((self.settings.video_resolution, self.settings.video_fps), ("1080p", 25))
        self.assertEqual(self.calls("video_mode")[-1]["fps"], 25)
        rows = dict(self.camera.menu.rows())
        self.assertEqual((rows["Cadence"], rows["Vitesse vidéo"]), ("25 i/s", "Automatique"))

    async def test_slow_motion_and_black_and_white(self):
        s = self.settings
        s.video_resolution, s.contrast = "720p", 2
        self.camera._look = types.SimpleNamespace(monochrome=True, curves=None)  # a black and white preset
        await self.video_mode()
        controls = self.picture_controls()
        self.assertEqual((controls["Saturation"], controls["Contrast"]), (0.0, 1.5))
        commands = [cmd for cmd, _ in self.engine.calls]
        first_picture = next(i for i, (cmd, p) in enumerate(self.engine.calls) if cmd == "set_controls" and "Saturation" in p["controls"])
        self.assertLess(first_picture, commands.index("video_mode"))  # before the camera starts: the screen's stream takes it then
        self.camera.on_action("q")
        while self.camera.menu.item.label != "Cadence":
            self.camera.on_action("rear_dial_minus")
        for _ in range(4):  # 25, 30, 50, slow motion
            self.camera.on_action("front_dial_plus")
        await asyncio.sleep(0.01)
        self.assertEqual(dict(self.camera.menu.rows())["Cadence"], "Ralenti ×2 · 50 i/s lus à 25")
        self.assertEqual(self.calls("video_mode")[-1]["slowdown"], 2)
        self.camera.on_action("q")
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        path = self.calls("record_start")[0]["path"]
        self.camera.on_event({"event": "record_done", "path": path, "duration": 6.0, "video_duration": 12.0, "frames": 300, "bytes": 1})
        self.assertEqual(self.camera.view().message, "PYV_0001 · 0:12 au ralenti")
        await self.video_mode()  # back to photo: the ISP neutral again, the look does the rest
        await asyncio.sleep(0.01)
        controls = self.picture_controls()
        self.assertEqual((controls["Saturation"], controls["Contrast"]), (1.0, 1.0))

    async def test_the_video_mode_starts_without_loupe_nor_restart(self):
        self.settings.mode = "video"
        await self.camera.sync_engine()
        names = [name for name, _ in self.engine.calls]
        self.assertNotIn("loupe", names)  # the engine refuses it in video
        self.assertEqual([p.get("restart") for p in self.calls("video_mode")], [False])  # once
        self.camera.set_grain(not self.settings.grain)  # nothing for the ISP: no restart
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.calls("video_mode")), 1)

    async def test_a_picture_change_in_video_starts_the_camera_again(self):
        await self.video_mode()
        self.camera.on_action("menu")
        while self.camera.menu.item.label != "Netteté":
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls("video_mode")[-1]["restart"], True)  # for the screen to show it
        self.assertEqual(self.picture_controls()["Sharpness"], 1.5)
        self.camera.on_action("menu")
        self.camera.on_action("shutter")  # recording: after it
        await asyncio.sleep(0.01)
        path = self.calls("record_start")[0]["path"]
        self.camera.on_action("menu")
        while self.camera.menu.item.label != "Netteté":
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")
        await asyncio.sleep(0.01)
        self.assertEqual(self.camera.view().message, "Appliqué après l'enregistrement")
        self.assertEqual(len(self.calls("video_mode")), 2)
        self.camera.on_event({"event": "record_done", "path": path, "duration": 3, "frames": 75, "bytes": 1})
        await asyncio.sleep(0.01)
        self.assertEqual((len(self.calls("video_mode")), self.calls("video_mode")[-1]["restart"]), (3, True))
        self.assertEqual(self.picture_controls()["Sharpness"], 2.0)

    async def test_failed_recording(self):
        await self.video_mode()
        self.engine.refuse["record_start"] = "busy"
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        self.assertIsNone(self.camera.recording)
        self.assertIn("impossible", self.camera.view().message)


    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg")
    async def test_extract_a_frame(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow")
        folder = os.path.join(self.settings.dcim, "100PYCAM")
        os.makedirs(folder)
        path = os.path.join(folder, "PYV_0001.MP4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "2", "-c:v", "mpeg4",
             "-metadata", "creation_time=2026-10-12T10:00:00.000000Z", path],
            check=True,
        )  # fmt: skip
        self.settings.next_number = 2
        self.camera.on_action("play")
        self.assertEqual(self.camera.playback.current, path)
        self.camera.video = Video(path, duration=2, position=1.04, paused=True)  # paused at 1 s
        self.camera.on_action("menu")
        menu = self.camera.menu
        self.assertEqual((menu.item.label, dict(menu.rows())["Extraire cette image"]), ("Extraire cette image", "à 0:01.0"))
        self.assertIsNone(self.camera.video)  # stopped to show the menu
        self.camera.on_action("front_dial_plus")
        self.assertIsNone(self.camera.menu)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if "extraite" in self.camera.view().message:
                break
        frame = os.path.join(folder, "PYC_0002.JPG")
        self.assertEqual(self.camera.view().message, "PYC_0002 extraite")
        with Image.open(frame) as im:
            self.assertEqual(im.size, (320, 240))
            taken = im.getexif().get_ifd(0x8769)[36867]
        expected = datetime.datetime.fromtimestamp(datetime.datetime(2026, 10, 12, 10, 0, 1, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(taken, expected.strftime("%Y:%m:%d %H:%M:%S"))  # the video's start, plus 1 s
        self.assertEqual(self.settings.next_number, 3)
        self.assertEqual(self.camera.playback.photos, [frame, path])  # in the list, the video still shown
        self.assertEqual(self.camera.playback.current, path)


class HelpersTest(unittest.TestCase):
    def test_slow_motion(self):
        s = Settings(video_resolution="720p")
        self.assertIn("slow", [f for f, _ in video.fps_options(s)])
        s.video_rate = "slow"
        self.assertEqual((s.video_fps, s.video_slowmo, s.video_rate), (50, True, "slow"))
        self.assertEqual(video.engine_setup(s), {"width": 1280, "height": 720, "fps": 50, "bitrate": 12_000_000, "slowdown": 2})
        self.assertEqual(video.label(s), "720p ×2 · Normale")
        s.video_shutter = "180"
        self.assertAlmostEqual(video.shutter(s), 1 / 100)  # half a frame as filmed
        s.video_rate = 25
        self.assertEqual((s.video_fps, s.video_slowmo, video.slowdown(s)), (25, False, 1))
        s.video_resolution = "1080p"
        self.assertNotIn("slow", [f for f, _ in video.fps_options(s)])

    def test_the_isp_does_the_videos_picture(self):
        from pycam import picture

        s = Settings(contrast=1, saturation=-1, sharpness=2)
        self.assertEqual(picture.controls(s), {"Sharpness": 2.0, "Contrast": 1.0, "Saturation": 1.0})  # photos: the look does it
        self.assertEqual(picture.controls(s, video=True), {"Sharpness": 2.0, "Contrast": 1.25, "Saturation": 0.75})
        self.assertEqual(picture.controls(s, video=True, monochrome=True)["Saturation"], 0.0)

    def test_framing_guides(self):
        rect = (240, 0, 1440, 1080)  # a 4:3 image
        self.assertEqual(guide_rect(rect, "none"), rect)
        self.assertEqual(guide_rect(rect, "9:16"), (656, 0, 608, 1080))
        self.assertEqual(guide_rect(rect, "1:1"), (420, 0, 1080, 1080))
        self.assertEqual(guide_rect((0, 0, 1920, 1080), "2.39:1"), (0, 138, 1920, 803))
        s = Settings()
        self.assertEqual(s.guide, "none")

    def test_bitrates_and_time_left(self):
        s = Settings(video_resolution="720p", video_quality="high")
        self.assertEqual(video.engine_setup(s), {"width": 1280, "height": 720, "fps": 25, "bitrate": 10_000_000, "slowdown": 1})
        self.assertEqual(video.minutes_left(s, 75 * 10**8 + video.RESERVE_BYTES), 100)  # 7.5 GB at 10 Mbit/s, the reserve kept

    def test_frame_rates_and_speed(self):
        s = Settings(video_fps=24)
        self.assertEqual(video.engine_setup(s), {"width": 1920, "height": 1080, "fps": 24, "bitrate": 12_000_000, "slowdown": 1})
        self.assertEqual([f for f, _ in video.fps_options(s)], [24, 25, 30])  # no 1080p50 on a Pi 3
        s.video_fps, s.video_quality = 30, "high"
        self.assertEqual(video.bitrate(s), 24_000_000)  # more frames, more bits
        s.video_resolution, s.video_fps = "720p", 50
        self.assertEqual(video.engine_setup(s), {"width": 1280, "height": 720, "fps": 50, "bitrate": 20_000_000, "slowdown": 1})
        self.assertEqual(video.label(s), "720p50 · Haute")
        s.video_resolution = "1080p"
        self.assertEqual(video.fps(s), 25)
        s.video_fps, s.video_shutter = 25, "180"
        self.assertAlmostEqual(video.shutter(s), 1 / 50)
        controls = video.controls(s)
        self.assertEqual((controls["ExposureTime"], controls["ExposureTimeMode"], controls["AnalogueGainMode"]), (20000, "Manual", "Auto"))
        controls = video.controls(s, lock=(1 / 100, 400))
        self.assertEqual((controls["ExposureTime"], controls["AnalogueGain"], controls["AnalogueGainMode"]), (10000, 4.0, "Manual"))

    def test_bad_saved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"mode": "cinema", "video_resolution": "4k", "video_quality": "ultra", "video_iso": 123, "video_fps": 60, "video_shutter": "90"}')
            s = load_settings(path)
            self.assertEqual((s.mode, s.video_resolution, s.video_quality, s.video_iso), ("photo", "1080p", "normal", 0))
            self.assertEqual((s.video_fps, s.video_shutter), (25, "auto"))
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"video_resolution": "1080p", "video_fps": 50}')
            self.assertEqual(load_settings(path).video_fps, 25)


if __name__ == "__main__":
    unittest.main()
