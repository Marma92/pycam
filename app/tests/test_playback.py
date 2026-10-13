import os
import tempfile
import unittest

from pycam.playback import Playback, companions, list_photos, posters_dir, prune_caches, thumbs_dir


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

    def test_caches_pruned(self):
        old = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.root, "cache")
        try:
            touch(os.path.join(self.root, "101PYCAM", "PYV_0002.MP4"))
            mtime = int(os.path.getmtime(os.path.join(self.root, "101PYCAM", "PYV_0002.MP4")))
            jpeg_mtime = int(os.path.getmtime(os.path.join(self.root, "101PYCAM", "PYC_0001.JPG")))
            keep = [os.path.join(posters_dir(), f"PYV_0002_{mtime}.jpg"), os.path.join(thumbs_dir(), f"101PYCAM_PYC_0001_{jpeg_mtime}.jpg")]
            gone = [os.path.join(posters_dir(), "PYV_0042_1.jpg"), os.path.join(thumbs_dir(), "101PYCAM_PYC_0001_1.jpg"),
                    os.path.join(thumbs_dir(), "100PYCAM_PYC_0007_5.jpg")]  # fmt: skip
            for path in keep + gone:
                touch(path)
            self.assertEqual(prune_caches(self.root), 3)
            self.assertEqual([os.path.exists(p) for p in keep + gone], [True, True, False, False, False])
        finally:
            if old is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old

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
