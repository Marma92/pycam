import json
import os
import tempfile
import unittest

from pycam.exposure import Exposure
from pycam.settings import Settings, load_settings, save_settings
from pycam.storage import Storage


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(dcim=self.tmp.name)
        self.storage = Storage(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbering(self):
        base, folder, number = self.storage.next_photo()
        self.assertEqual(base, os.path.join(self.tmp.name, "100PYCAM", "PYC_0001"))
        self.storage.commit(folder, number)
        base, _, _ = self.storage.next_photo()
        self.assertTrue(base.endswith("PYC_0002"))

    def test_names_already_taken_are_skipped(self):
        os.makedirs(os.path.join(self.tmp.name, "100PYCAM"))
        open(os.path.join(self.tmp.name, "100PYCAM", "PYC_0001.DNG"), "w").close()
        base, folder, number = self.storage.next_photo()
        self.assertEqual((folder, number), (100, 2))
        self.assertTrue(base.endswith("PYC_0002"))

    def test_next_folder_after_9999(self):
        self.storage.commit(100, 9999)
        self.assertEqual((self.settings.folder_number, self.settings.next_number), (101, 1))

    def test_shots_remaining_without_dcim_folder(self):
        self.settings.dcim = os.path.join(self.tmp.name, "not", "yet")
        self.assertGreater(self.storage.shots_remaining(["jpeg", "dng"]), 0)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "pycam", "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        settings = Settings(exposure=Exposure(shutter=1 / 125, iso=200, ev=1 / 3), display="clean", next_number=42)
        save_settings(settings, self.path)
        loaded = load_settings(self.path)
        self.assertEqual(loaded.exposure.mode, "M")
        self.assertAlmostEqual(loaded.exposure.shutter, 1 / 125)
        self.assertEqual(loaded.display, "clean")
        self.assertEqual(loaded.next_number, 42)

    def test_missing_or_corrupt_file_gives_defaults(self):
        self.assertEqual(load_settings(self.path), Settings())
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(load_settings(self.path), Settings())

    def test_lens_profiles(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump(
                {
                    "lenses": [{"name": "Fujinon 25mm", "focal_length": 25, "max_aperture": 1.4}, {"name": "broken"}],
                    "lens": 0,
                    "aperture": 4,
                },
                f,
            )
        loaded = load_settings(self.path)
        self.assertEqual([lens["name"] for lens in loaded.lenses], ["Fujinon 25mm"])
        self.assertEqual((loaded.lens, loaded.aperture), (0, 4.0))

    def test_lens_index_out_of_range_means_no_lens(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"lenses": [], "lens": 3, "aperture": 2.8}, f)
        loaded = load_settings(self.path)
        self.assertEqual((loaded.lens, loaded.aperture), (-1, None))

    def test_bad_values_are_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump(
                {
                    "display": "sparkles",
                    "grid": "yes",
                    "formats": ["gif"],
                    "jpeg_quality": 85,
                    "peaking_color": "pink",
                    "sharpness": 7,
                    "jpeg_size": "XXL",
                    "histogram": "cmyk",
                },
                f,
            )
        loaded = load_settings(self.path)
        self.assertEqual(loaded.display, "detailed")
        self.assertEqual((loaded.peaking_color, loaded.histogram), ("red", "rgb"))
        self.assertEqual((loaded.sharpness, loaded.jpeg_size), (0, "L"))
        self.assertTrue(loaded.grid)
        self.assertEqual(loaded.formats, ["jpeg", "dng"])
        self.assertEqual(loaded.jpeg_quality, 85)


if __name__ == "__main__":
    unittest.main()
