import os
import tempfile
import unittest

from pycam.playback import Playback, companions, list_photos


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


class PlaybackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for path in (
            "101PYCAM/PYC_0001.JPG",
            "100PYCAM/PYC_9998.JPG",
            "100PYCAM/PYC_9999.JPG",
            "100PYCAM/PYC_9999.DNG",
            "100PYCAM/PYB_9999.JPG",  # a base copy: goes with its photo
            "100PYCAM/notes.txt",
            "misc/PYC_0005.JPG",  # not a camera folder
        ):
            touch(os.path.join(self.root, path))

    def test_photos_in_order(self):
        names = [os.path.relpath(p, self.root).replace(os.sep, "/") for p in list_photos(self.root)]
        self.assertEqual(names, ["100PYCAM/PYC_9998.JPG", "100PYCAM/PYC_9999.JPG", "101PYCAM/PYC_0001.JPG"])
        self.assertEqual(list_photos(os.path.join(self.root, "missing")), [])

    def test_browsing(self):
        p = Playback(self.root)
        self.assertEqual((os.path.basename(p.current), p.position), ("PYC_0001.JPG", "3/3"))  # the last one
        self.assertFalse(p.step(+1))  # already the last
        self.assertTrue(p.step(-10))  # stops at the first
        self.assertEqual(p.position, "1/3")

    def test_zoom_and_pan(self):
        p = Playback(self.root)
        self.assertEqual(p.zoom(0.355), 1.0)  # fitted
        p.cycle_zoom()
        self.assertAlmostEqual(p.zoom(0.355), 1 / 0.355)  # 100 %: one photo pixel per screen pixel
        for _ in range(10):
            p.pan(1, 0, p.zoom(0.355))
        self.assertAlmostEqual(p.center[0], 1 - 0.5 * 0.355)  # never past the edge
        p.cycle_zoom()
        p.cycle_zoom()  # back to fitted, and to the centre
        self.assertEqual((p.scale, p.center), (0, (0.5, 0.5)))

    def test_delete_takes_the_whole_shot(self):
        p = Playback(self.root)
        p.step(-1)  # PYC_9999, with its DNG and its base copy
        jpeg = p.current
        self.assertEqual(len(p.delete_current()), 3)
        self.assertFalse(any(os.path.exists(f) for f in companions(jpeg)))
        self.assertEqual(os.path.basename(p.current), "PYC_0001.JPG")  # the next one
        self.assertEqual(p.position, "2/2")


if __name__ == "__main__":
    unittest.main()
