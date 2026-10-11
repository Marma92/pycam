import unittest

from pycam.lenses import aperture_stops, clean_lens, format_lens, lens_exif

LENS_16 = {"name": "Raspberry Pi 16 mm", "make": "Raspberry Pi", "focal_length": 16.0, "max_aperture": 1.4}


class LensesTest(unittest.TestCase):
    def test_exif_tags(self):
        tags = lens_exif(LENS_16, 2.8)
        self.assertIn("EXIF.LensModel=Raspberry Pi 16 mm", tags)
        self.assertIn("EXIF.LensMake=Raspberry Pi", tags)
        self.assertIn("EXIF.FocalLength=1600/100", tags)
        self.assertIn("EXIF.FocalLengthIn35mmFilm=88", tags)  # crop factor ~5.5
        self.assertIn("EXIF.FNumber=280/100", tags)
        self.assertIn("EXIF.MaxApertureValue=97/100", tags)  # APEX: 2 log2(1.4)
        self.assertIn("EXIF.LensSpecification=1600/100,1600/100,140/100,140/100", tags)

    def test_no_lens_no_tags_and_unknown_aperture_not_written(self):
        self.assertEqual(lens_exif(None, 2.8), [])
        self.assertFalse(any(t.startswith("EXIF.FNumber") for t in lens_exif(LENS_16, None)))

    def test_aperture_stops(self):
        self.assertEqual(aperture_stops(LENS_16), [1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0])
        six = {"name": "6 mm", "focal_length": 6.0, "max_aperture": 1.2}
        self.assertEqual(aperture_stops(six)[:3], [1.2, 1.4, 2.0])
        self.assertEqual(aperture_stops(dict(six, min_aperture=22))[-1], 22.0)

    def test_clean_lens(self):
        self.assertIsNone(clean_lens({"name": "", "focal_length": 16, "max_aperture": 1.4}))
        self.assertIsNone(clean_lens({"name": "x", "focal_length": "16", "max_aperture": 1.4}))
        self.assertIsNone(clean_lens("Canon"))
        lens = clean_lens({"name": " Fujinon 25mm ", "focal_length": 25, "max_aperture": 1.4, "colour": "black"})
        self.assertEqual(lens, {"name": "Fujinon 25mm", "focal_length": 25.0, "max_aperture": 1.4})

    def test_format(self):
        self.assertEqual(format_lens(LENS_16, None), "16 mm")
        self.assertEqual(format_lens(LENS_16, 2.8), "16 mm f/2.8")
        self.assertEqual(format_lens(None, 2.8), "")


if __name__ == "__main__":
    unittest.main()
