import os
import struct
import tempfile
import unittest

from pycam import looks

try:
    import PIL  # noqa: F401

    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False


def write_preset(folder, text, **files):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "preset.toml"), "w", encoding="utf-8") as f:
        f.write(text)
    for name, content in files.items():
        with open(os.path.join(folder, name.replace("_", ".")), "w", encoding="utf-8") as f:
            f.write(content)
    return folder


class LooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def preset(self, text, name="test", **files):
        return looks.load_preset(write_preset(os.path.join(self.tmp.name, name), "format = 1\nname = \"Test\"\n" + text, **files))

    def test_builtin_presets(self):
        presets = looks.find_presets([looks.BUILTIN_DIR])
        self.assertEqual(list(presets)[:2], ["standard", "base"])  # the default one first
        self.assertTrue(looks.is_identity(looks.compile_look(presets["base"], size=9), 9))
        standard = looks.compile_look(presets["standard"], size=9)
        grey = standard[(4 * 9 + 4) * 9 + 4]  # the middle grey, 0.5
        self.assertAlmostEqual(grey[0], 0.638, places=2)  # the standard curve, as computed
        self.assertEqual(grey[0], grey[2])

    def test_monochrome_and_mixer(self):
        preset = self.preset("[look]\nmonochrome = true\nmixer = [1, 0, 0]\n")  # a red filter, all the way
        table = looks.compile_look(preset, size=5)
        self.assertTrue(all(r == g == b for r, g, b in table))
        self.assertAlmostEqual(table[4][0], 1.0)  # pure red -> white
        self.assertAlmostEqual(table[4 * 5][0], 0.0)  # pure green -> black
        # Saturation at -3 (black and white) works with any preset.
        self.assertTrue(all(r == g == b for r, g, b in looks.compile_look(self.preset("", "plain"), saturation=-3, size=5)))

    def test_curve_goes_through_its_points_without_overshoot(self):
        points = [[0, 0.05], [0.25, 0.2], [0.5, 0.55], [0.75, 0.85], [1, 0.95]]
        table = looks.curve_table(points, samples=1001)
        for x, y in points:
            self.assertAlmostEqual(table[round(x * 1000)], y, places=6)
        self.assertTrue(all(b >= a for a, b in zip(table, table[1:])))  # monotone

    def test_colour_ranges(self):
        preset = self.preset("[look.colours]\ngreen = [-60, 1, 0]\nblue = [0, 0.5, 0]\norange = [10, 1, 0]\n")
        table = looks.compile_look(preset, size=5)
        at = lambda r, g, b: table[(b * 5 + g) * 5 + r]  # noqa: E731
        self.assertTrue(all(abs(c - 0.5) < 1e-9 for c in at(2, 2, 2)))  # the greys stay put
        self.assertEqual([round(c, 6) for c in at(0, 4, 0)], [1, 1, 0])  # green turned yellow
        # Pure blue is 240 degrees: a third of the way to purple, so two thirds of the change.
        self.assertEqual([round(c, 6) for c in at(0, 0, 4)], [0.333333, 0.333333, 1])
        # Between two ranges, a mix: hue 45 is halfway from orange (30) to yellow (60).
        ranges = sorted((h, (10, 1, 0) if n == "orange" else (0, 1, 0)) for n, h in looks.COLOUR_RANGES.items())
        self.assertAlmostEqual(looks._colour_adjustment(ranges, 45)[0], 5)
        self.assertAlmostEqual(looks._colour_adjustment(ranges, 350)[0], 0)  # magenta -> red: nothing
        for bad in ("teal = [0, 1, 0]", "green = [90, 1, 0]", "green = [0, 1]"):
            with self.subTest(bad=bad), self.assertRaises(looks.PresetError):
                self.preset(f"[look.colours]\n{bad}\n", "bad")

    def test_contrast_and_strength(self):
        plain = self.preset("", "plain")
        dark = looks.compile_look(plain, contrast=2, size=5)[1 * 31]  # grey 0.25: darker
        self.assertLess(dark[0], 0.25)
        flat = looks.compile_look(plain, contrast=-2, size=5)[1 * 31]
        self.assertGreater(flat[0], 0.25)
        half = self.preset("[look]\nmonochrome = true\nstrength = 0.5\n", "half")
        r, g, b = looks.compile_look(half, size=5)[4]  # pure red, half black and white
        self.assertAlmostEqual(r, (1 + 0.299) / 2)

    def test_cube_lut(self):
        size, table = 3, [(r / 2, g / 2, b / 2) for b in range(3) for g in range(3) for r in range(3)]
        inverted = [(1 - r, 1 - g, 1 - b) for r, g, b in table]
        folder = os.path.join(self.tmp.name, "negative")
        os.makedirs(folder)
        looks.write_cube(os.path.join(folder, "look.cube"), size, inverted)
        preset = looks.load_preset(write_preset(folder, 'format = 1\nname = "Négatif"\n[look]\nlut = "look.cube"\n'))
        out = looks.compile_look(preset, size=5)
        self.assertAlmostEqual(out[0][0], 1.0)  # black -> white
        self.assertAlmostEqual(out[(2 * 5 + 2) * 5 + 2][1], 0.5)  # the middle stays
        self.assertNotEqual(preset.digest, self.preset("", "plain").digest)

    @unittest.skipUnless(HAVE_PILLOW, "Pillow")
    def test_hald_identity_round_trip(self):
        path = os.path.join(self.tmp.name, "hald.png")
        looks.hald_identity(level=4).save(path)
        size, table = looks.read_lut_file(path)
        self.assertEqual(size, 16)
        self.assertTrue(looks.is_identity(table, size, tolerance=1 / 255))

    def test_broken_presets_are_refused(self):
        for text in (
            "[look]\ntone_curve = [[0, 0], [0, 1]]\n",  # x not increasing
            "[look]\nsaturation = 9\n",
            "[look]\nlut = \"missing.cube\"\n",
            "[grain]\namount = \"lots\"\n",
            "[look]\nsplit_toning = { shadows = [400, 0.1] }\n",
        ):
            with self.subTest(text=text), self.assertRaises(looks.PresetError):
                self.preset(text)
        with self.assertRaises(looks.PresetError):
            looks.load_preset(write_preset(os.path.join(self.tmp.name, "v2"), 'format = 2\nname = "Futur"\n'))

    def test_bad_presets_are_skipped(self):
        write_preset(os.path.join(self.tmp.name, "good"), 'format = 1\nname = "Bon"\n')
        write_preset(os.path.join(self.tmp.name, "bad"), "format = 1\n")  # no name
        with self.assertLogs("pycam.looks", "WARNING"):
            presets = looks.find_presets([self.tmp.name])
        self.assertEqual(list(presets), ["good"])

    def test_viewfinder_preview(self):
        presets = looks.find_presets([looks.BUILTIN_DIR])
        self.assertIsNone(looks.fit_preview(looks.compile_look(presets["base"], size=9), 9))
        standard = looks.fit_preview(looks.compile_look(presets["standard"], size=17), 17)
        self.assertAlmostEqual(standard["luma"][128] / 255, 0.638, delta=0.01)  # the standard curve
        self.assertLess(max(abs(v) for v in standard["luma_mix"]), 0.1)  # small: the curve, channel by channel
        red_filter = self.preset("[look]\nmonochrome = true\nmixer = [1, 0, 0]\n")
        preview = looks.fit_preview(looks.compile_look(red_filter, size=17), 17)
        self.assertAlmostEqual(preview["luma_mix"][1], 1.402, delta=0.05)  # R = Y + 1.402 Cr
        self.assertLess(max(abs(v) for v in preview["chroma"]), 0.01)  # no colour left
        vivid = looks.fit_preview(looks.compile_look(self.preset("", "plain"), saturation=2, size=17), 17)
        self.assertAlmostEqual(vivid["chroma"][0], 1.5, delta=0.1)  # a little less: some colours clip
        self.assertAlmostEqual(vivid["chroma"][1], 0.0, delta=0.05)

    def test_histogram_remap(self):
        histogram = list(range(64))
        identity = list(range(256))
        self.assertEqual(looks.remap_histogram(histogram, identity), histogram)
        # A curve that stretches the tones: each class covers two, without gaps between
        # them (they showed as black bands in the HUD).
        brighter = [min(255, 2 * i) for i in range(256)]
        remapped = looks.remap_histogram([10] * 64, brighter)
        self.assertAlmostEqual(sum(remapped), 640)  # nothing lost
        self.assertTrue(all(remapped[k] > 0 for k in range(63)))
        self.assertAlmostEqual(remapped[63], 10 * 32 + 10 * 3 / 7)  # the upper half clips to white
        inverted = looks.remap_histogram(histogram, [255 - i for i in range(256)])
        self.assertEqual(inverted, histogram[::-1])

    def test_compiled_lut_is_cached(self):
        cache = os.path.join(self.tmp.name, "cache")
        standard = looks.find_presets([looks.BUILTIN_DIR])["standard"]
        path = looks.compiled_lut(standard, folder=cache)
        with open(path, "rb") as f:
            magic, version, size = f.read(8), *struct.unpack("<II", f.read(8))
        self.assertEqual((magic, version, size), (b"PYCAMLUT", 1, looks.LUT_SIZE))
        self.assertEqual(os.path.getsize(path), 16 + looks.LUT_SIZE**3 * 6)
        mtime = os.path.getmtime(path)
        self.assertEqual(looks.compiled_lut(standard, folder=cache), path)
        self.assertEqual(os.path.getmtime(path), mtime)  # not compiled again
        self.assertNotEqual(looks.compiled_lut(standard, contrast=1, folder=cache), path)
        base = looks.find_presets([looks.BUILTIN_DIR])["base"]
        self.assertIsNone(looks.compiled_lut(base, folder=cache))  # nothing to apply


if __name__ == "__main__":
    unittest.main()
