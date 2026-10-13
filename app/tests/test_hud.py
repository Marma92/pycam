"""The HUD drawn in place, in a file standing for the engine's shared memory."""

import os
import tempfile
import unittest

try:
    from pycam.hud import FONT, Hud

    HAVE_HUD = os.path.exists(FONT)
except ImportError:  # Pillow: on the camera
    HAVE_HUD = False

from pycam.view import HudView

W, H = 320, 240


@unittest.skipUnless(HAVE_HUD, "Pillow and the DejaVu fonts")
class HudTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.write(fd, bytes(W * H * 4))
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        info = {"hud": {"format": "RGBA", "width": W, "height": H, "stride": W * 4, "path": self.path}, "image_rect": [40, 0, 240, 240]}
        self.hud = Hud(info)
        self.addCleanup(self.hud.close)

    def memory(self):
        with open(self.path, "rb") as f:
            return f.read()

    def pixel(self, x, y):
        data = self.memory()
        return tuple(data[(y * W + x) * 4 : (y * W + x) * 4 + 4])

    def test_drawn_in_place(self):
        self.hud.draw(HudView(display="clean", grid=False, message="Bonjour"))
        self.assertEqual(self.pixel(5, 120), (0, 0, 0, 255))  # the margins: opaque black
        drawn = self.memory()
        self.hud.draw(HudView(display="clean", grid=False))  # each drawing starts again from the background
        self.assertNotEqual(self.memory(), drawn)
        self.assertEqual(self.memory(), self.hud._background(False).tobytes())
        self.hud.draw(HudView(display="detailed", shutter="1/60", iso="ISO 200", histogram=(10,) * 64))
        self.hud.draw_default()
        self.assertEqual(self.memory(), self.hud._background(False).tobytes())

    def test_recording_shown_in_the_clean_display(self):
        self.hud.draw(HudView(display="clean", grid=False, video_mode=True))
        quiet = self.memory()
        self.hud.draw(HudView(display="clean", grid=False, video_mode=True, recording="0:05"))
        self.assertNotEqual(self.memory(), quiet)  # "● REC 0:05"
