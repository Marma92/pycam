"""Videos in playback, and the screen asleep during long captures."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from pycam import camera as camera_module
from pycam.playback import Playback, companions, list_photos
from tests.test_camera import FakeHud
from tests.test_drive import DriveCase


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


class ListTest(unittest.TestCase):
    def test_videos_after_their_frames(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("100PYCAM/PYC_0001.JPG", "101PYCAM/PYC_0002.JPG", "101PYCAM/PYC_0003.JPG",
                         "101PYCAM/PYT_0002.MP4", "102PYCAM/PYC_0004.JPG"):  # fmt: skip
                touch(os.path.join(root, name))
            names = [os.path.relpath(p, root).replace(os.sep, "/") for p in list_photos(root, videos=True)]
            self.assertEqual(names[2:4], ["101PYCAM/PYC_0003.JPG", "101PYCAM/PYT_0002.MP4"])
            self.assertNotIn("101PYCAM/PYT_0002.MP4", [os.path.relpath(p, root) for p in list_photos(root)])
            p = Playback(root)
            p.step(-1)
            self.assertTrue(p.video)
            self.assertEqual(companions(p.current), [p.current])  # a video: itself only


class VideoPlaybackTest(DriveCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        folder = os.path.join(self.settings.dcim, "101PYCAM")
        touch(os.path.join(folder, "PYC_0002.JPG"))
        self.video = os.path.join(folder, "PYT_0002.MP4")
        touch(self.video)
        touch(os.path.join(self.settings.dcim, "102PYCAM", "PYC_0010.JPG"))
        self.camera.hud = FakeHud()
        patches = (
            mock.patch.object(camera_module, "describe_video", lambda path: ("PYT_0002   vidéo 4 s", (1920, 1080), 4.0)),
            mock.patch.object(camera_module, "poster", lambda path: "/posters/PYT_0002.jpg"),
            mock.patch.object(camera_module, "describe", lambda path: (os.path.basename(path), (4056, 3040))),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.camera.on_action("play")  # on the last photo (102PYCAM)
        self.camera.on_action("rear_dial_minus")  # back to the video
        await asyncio.sleep(0.05)

    def calls(self, name):
        return [params for cmd, params in self.engine.calls if cmd == name]

    async def test_first_frame_then_play_pause(self):
        view = self.camera.view()
        self.assertEqual((view.review, view.video_hint), ("/posters/PYT_0002.jpg", "LOUPE : lire"))
        self.camera.on_action("loupe")  # plays
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls("video_play"), [{"path": self.video}])
        self.camera.on_event({"event": "video_progress", "position": 2.04, "duration": 4.0})
        view = self.camera.view()
        self.assertTrue(view.video_playing)
        self.assertEqual((view.video_time, round(view.video_progress, 2), view.video_paused), ("0:02 / 0:04", 0.51, False))
        self.camera.on_action("loupe")  # pauses
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls("video_pause"), [{"paused": True}])
        self.assertTrue(self.camera.view().video_paused)
        self.camera.on_event({"event": "video_ended", "reason": "end", "position": 4.0})
        self.camera.on_action("loupe")  # ended: plays again
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("video_play")), 2)

    async def test_dials_and_leaving_stop_it(self):
        self.camera.on_action("loupe")
        await asyncio.sleep(0.01)
        self.camera.on_action("rear_dial_plus")  # the next photo
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("video_stop")), 1)
        self.assertFalse(self.camera.view().video_playing)
        self.camera.on_action("rear_dial_minus")
        await asyncio.sleep(0.05)
        self.camera.on_action("loupe")
        await asyncio.sleep(0.01)
        self.camera.on_action("shutter")  # leaves playback, stops the video, and shoots
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.calls("video_stop")), 2)
        self.assertIsNone(self.camera.playback)
        self.assertEqual(len(self.engine.captures()), 1)

    async def test_delete_a_video(self):
        self.camera.on_action("q")
        self.camera.on_action("q")
        self.assertFalse(os.path.exists(self.video))
        self.assertTrue(os.path.exists(os.path.join(self.settings.dcim, "101PYCAM", "PYC_0002.JPG")))  # its frames stay

    async def test_unplayable(self):
        self.engine.refuse["video_play"] = "bad_request"
        self.camera.on_action("loupe")
        await asyncio.sleep(0.01)
        self.assertIn("Lecture impossible", self.camera.view().message)
        self.assertIsNone(self.camera.video)


class ScreenTest(DriveCase):
    def powers(self):
        return [p["on"] for cmd, p in self.engine.calls if cmd == "display_power"]

    async def interval(self):
        self.settings.drive, self.settings.interval_seconds, self.settings.interval_count = "interval", 60, 0
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)

    async def test_asleep_during_a_long_capture(self):
        self.settings.screen_off = 30
        await self.interval()
        start = self.camera._last_input
        self.camera.check_screen(start + 29)
        self.assertTrue(self.camera.screen_on)
        self.camera.check_screen(start + 31)
        await asyncio.sleep(0.01)
        self.assertEqual((self.camera.screen_on, self.powers()), (False, [False]))
        self.assertEqual(self.camera.view().drive[:4], "INT ")

        # A press only wakes it: the interval goes on.
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        self.assertEqual((self.camera.screen_on, self.powers()), (True, [False, True]))
        self.assertTrue(self.camera.drive.active)

        # Asleep again; the web interface's shutter acts without waking it (and stops it).
        self.camera.check_screen(self.camera._last_input + 31)
        self.camera.on_action("remote_shutter")
        await asyncio.sleep(0.01)
        self.assertFalse(self.camera.drive.active)
        self.camera.check_screen()  # the long capture is over: awake
        self.assertTrue(self.camera.screen_on)

    async def test_never_outside_long_captures_or_when_asked(self):
        self.camera.check_screen(self.camera._last_input + 3600)  # no capture
        self.assertTrue(self.camera.screen_on)
        self.settings.screen_off = 0  # always on
        await self.interval()
        self.camera.check_screen(self.camera._last_input + 3600)
        self.assertTrue(self.camera.screen_on)

    async def test_the_release_ends_a_bulb_held_in_the_dark(self):
        e = self.settings.exposure
        e.shutter, e.iso, e.bulb = 640, 400, True
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        self.camera.check_screen(self.camera._last_input + 31)
        self.assertFalse(self.camera.screen_on)
        self.camera.on_action("shutter_release")
        await self.finished()
        self.assertIn("capture_stop", [name for name, _ in self.engine.calls])

    async def test_no_drawing_asleep(self):
        drawn = []
        self.camera.hud = mock.Mock(draw=drawn.append)
        self.camera.screen_on = False
        loop = asyncio.ensure_future(self.camera.hud_loop())
        self.camera.redraw()
        await asyncio.sleep(0.05)
        loop.cancel()
        self.assertEqual(drawn, [])


if __name__ == "__main__":
    unittest.main()
