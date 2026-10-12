import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from pycam import timelapse
from pycam.menu import Menu
from pycam.settings import Settings
from tests.test_drive import DriveCase

try:
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and os.name == "posix"


class CommandTest(unittest.TestCase):
    def test_filters(self):
        self.assertEqual(
            timelapse.video_filters("16:9", True),
            "crop=iw:iw*9/16,scale=1920:1080,deflicker=size=9:mode=pm,format=yuv420p",
        )
        self.assertEqual(timelapse.video_filters("4:3", False), "crop=ih*4/3:ih,scale=1440:1080,format=yuv420p")

    def test_command(self):
        command = timelapse.ffmpeg_command("/t/frame_%06d.jpg", "/d/PYT_0001.MP4.part", 25, "16:9", True, timelapse.ENCODERS[0])
        self.assertEqual(command[:4], ["nice", "-n", "10", "ffmpeg"])
        self.assertIn("h264_v4l2m2m", command)
        self.assertEqual(command[command.index("-framerate") + 1], "25")
        self.assertEqual(command[command.index("-f") + 1], "mp4")  # whatever the extension (.part)
        self.assertEqual(command[-1], "/d/PYT_0001.MP4.part")

    def test_video_name(self):
        self.assertEqual(timelapse.video_name("/dcim/101PYCAM/PYC_0042.JPG"), "PYT_0042.MP4")


@unittest.skipUnless(HAVE_FFMPEG and HAVE_PILLOW, "ffmpeg and Pillow (on the camera)")
class AssembleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frames = []
        for i in range(12):
            path = os.path.join(self.tmp.name, f"PYC_{i + 1:04d}.JPG")
            Image.new("RGB", (400, 300), (40 + 10 * i, 80, 120)).save(path)
            self.frames.append(path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def probe(self, path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_name,width,height,nb_read_frames",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        return json.loads(out)["streams"][0]

    async def test_a_video_with_progress(self):
        out = os.path.join(self.tmp.name, "PYT_0001.MP4")
        seen = []
        self.assertTrue(await timelapse.assemble(self.frames, out, 25, "16:9", True, seen.append))
        stream = self.probe(out)
        self.assertEqual((stream["codec_name"], stream["width"], stream["height"]), ("h264", 1920, 1080))
        self.assertEqual(int(stream["nb_read_frames"]), 12)
        self.assertEqual(seen[-1], 1.0)
        self.assertFalse(os.path.exists(out + ".part"))

    async def test_another_encoder_if_one_fails(self):
        encoders = timelapse.ENCODERS
        timelapse.ENCODERS = (["-c:v", "no_such_encoder"], encoders[1])
        try:
            out = os.path.join(self.tmp.name, "PYT_0002.MP4")
            self.assertTrue(await timelapse.assemble(self.frames, out, 24, "4:3", False))
            self.assertEqual(self.probe(out)["width"], 1440)
        finally:
            timelapse.ENCODERS = encoders

    async def test_too_few_frames(self):
        self.assertFalse(await timelapse.assemble(self.frames[:1], os.path.join(self.tmp.name, "x.MP4")))


class TimelapseDriveTest(DriveCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.videos = []

        async def make_timelapse(frames):
            self.videos.append(list(frames))

        self.camera.make_timelapse = make_timelapse
        # The engine writes the JPEGs: the folders fill up as on the camera.
        engine_call = self.engine.call

        async def call(cmd, timeout=10.0, **params):
            reply = await engine_call(cmd, timeout, **params)
            if cmd == "capture":
                os.makedirs(os.path.dirname(params["path"]), exist_ok=True)
                open(params["path"] + ".JPG", "w").close()
            return reply

        self.engine.call = call

    def timelapse(self, frames=3, photos="video"):
        s = self.settings
        s.drive, s.timelapse_interval, s.timelapse_frames, s.timelapse_photos = "timelapse", 2, frames, photos
        s.formats, s.keep_base = ["jpeg", "dng"], True

    async def test_frames_folder_and_video(self):
        folder = os.path.join(self.settings.dcim, "100PYCAM")
        os.makedirs(folder)
        open(os.path.join(folder, "PYC_0007.JPG"), "w").close()  # a photo from before
        self.settings.next_number = 8
        self.timelapse()
        self.assertEqual(self.camera.view().drive, "TL 2 s")
        self.camera.on_action("shutter")
        await self.finished()
        await asyncio.sleep(0.01)
        captures = self.engine.captures()
        self.assertEqual([c["formats"] for c in captures], [["jpeg"]] * 3)  # light frames, no base copy
        self.assertNotIn("base_jpeg", captures[0])
        self.assertEqual(self.engine.shot_times, [0.0, 2.0, 4.0])
        configures = [p for name, p in self.engine.calls if name == "configure"]
        self.assertEqual(configures[0], {"still_size": [2028, 1520]})
        self.assertIsNone(configures[-1]["still_size"])  # the photos' size again
        # Its own folder; the video gets the frames in order.
        self.assertEqual(len(self.videos), 1)
        self.assertEqual([os.path.relpath(f, self.settings.dcim) for f in self.videos[0]],
                         [os.path.join("101PYCAM", f"PYC_{n:04d}.JPG") for n in (8, 9, 10)])  # fmt: skip
        # And the next photo in another one.
        self.settings.drive = "single"
        self.camera.on_action("shutter")
        await asyncio.sleep(0.01)
        self.assertIn("102PYCAM", self.engine.last("capture")["path"])

    async def test_photo_size_back_after_the_last_frame(self):
        self.timelapse(frames=2)
        self.camera.on_action("shutter")
        await self.finished()
        self.engine.refuse["configure"] = "busy"  # the last frame still being taken
        await asyncio.sleep(0.6)
        del self.engine.refuse["configure"]
        await asyncio.sleep(0.4)
        self.assertIsNone(self.engine.last("configure")["still_size"])  # the photos' size again

    async def test_stopped_still_makes_its_video(self):
        self.timelapse(frames=0)  # until stopped
        self.camera.on_action("shutter")
        while len(self.engine.captures()) < 4:
            await asyncio.sleep(0)
        self.camera.on_action("shutter")  # stops it
        await self.finished()
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.videos[0]), 4)

    async def test_full_photos(self):
        self.timelapse(frames=2, photos="full")
        self.settings.formats = ["dng"]  # a JPEG is needed for the video
        self.camera.on_action("shutter")
        await self.finished()
        self.assertEqual(self.engine.captures()[0]["formats"], ["jpeg", "dng"])
        self.assertNotIn("configure", [name for name, _ in self.engine.calls])

    async def test_one_frame_no_video(self):
        self.timelapse(frames=0)
        self.camera.on_action("shutter")
        while not self.engine.captures():
            await asyncio.sleep(0)
        self.camera.on_action("shutter")
        await self.finished()
        await asyncio.sleep(0.01)
        self.assertEqual(self.videos, [])
        self.assertIn("pas de vidéo", self.camera.view().message)


class MenuTest(unittest.TestCase):
    def test_count_shows_the_video_length(self):
        settings = Settings(timelapse_frames=250, timelapse_fps=25)
        rows = dict(Menu(settings, quick=False).rows())
        self.assertEqual(rows["Durée"], "250 vues · 10 s de vidéo")
        settings.timelapse_fps = 30
        self.assertEqual(dict(Menu(settings, quick=False).rows())["Durée"], "250 vues · 8 s de vidéo")


if __name__ == "__main__":
    unittest.main()
