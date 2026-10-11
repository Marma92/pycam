import unittest

from pycam.exposure import SHUTTER_SPEEDS, Exposure, format_ev, format_shutter


class ExposureTest(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(Exposure().mode, "P")
        self.assertEqual(Exposure(shutter=1 / 60).mode, "S")
        self.assertEqual(Exposure(iso=400).mode, "A")
        self.assertEqual(Exposure(shutter=1 / 60, iso=400).mode, "M")

    def test_controls_auto(self):
        controls = Exposure().controls()
        self.assertEqual(controls["ExposureTimeMode"], "Auto")
        self.assertEqual(controls["AnalogueGainMode"], "Auto")
        self.assertNotIn("ExposureTime", controls)

    def test_controls_manual(self):
        controls = Exposure(shutter=1 / 250, iso=800, awb="Daylight").controls()
        self.assertEqual(controls["ExposureTime"], 4000)
        self.assertEqual(controls["ExposureTimeMode"], "Manual")
        self.assertEqual(controls["AnalogueGain"], 8.0)
        self.assertEqual(controls["AnalogueGainMode"], "Manual")
        self.assertEqual(controls["AwbMode"], "Daylight")

    def test_first_dial_step_starts_from_the_measure(self):
        e = Exposure()
        e.step_shutter(-1, 1 / 60)  # measured 1/60, one step faster
        self.assertAlmostEqual(e.shutter, 1 / 80)
        e.step_shutter(+2, None)
        self.assertAlmostEqual(e.shutter, 1 / 50)

    def test_dial_stops_at_the_ends(self):
        e = Exposure(shutter=SHUTTER_SPEEDS[0])
        e.step_shutter(-5, None)
        self.assertEqual(e.shutter, SHUTTER_SPEEDS[0])
        e = Exposure(iso=1600)
        e.step_iso(+3, None)
        self.assertEqual(e.iso, 1600)

    def test_slowest_shutter_is_honoured(self):
        e = Exposure(shutter=1)
        e.step_shutter(+50, None, slowest=2)
        self.assertEqual(e.shutter, 2)

    def test_auto_toggles(self):
        e = Exposure()
        e.toggle_iso_auto(390)  # measured ~ISO 390: manual at the nearest value
        self.assertEqual(e.iso, 400)
        e.toggle_iso_auto(390)
        self.assertIsNone(e.iso)

    def test_ev_steps_and_limits(self):
        e = Exposure()
        e.step_ev(+1)
        self.assertAlmostEqual(e.ev, 1 / 3)
        for _ in range(20):
            e.step_ev(-1)
        self.assertAlmostEqual(e.ev, -3)

    def test_formats(self):
        self.assertEqual(format_shutter(1 / 250), "1/250")
        self.assertEqual(format_shutter(0.5), '0.5"')
        self.assertEqual(format_shutter(2), '2"')
        self.assertEqual(format_ev(0), "±0")
        self.assertEqual(format_ev(-2 / 3), "-0.7")

    def test_from_dict_ignores_nonsense(self):
        e = Exposure.from_dict({"shutter": "fast", "iso": 410, "ev": 12, "awb": "Disco"})
        self.assertIsNone(e.shutter)
        self.assertEqual(e.iso, 400)
        self.assertEqual(e.ev, 3)
        self.assertEqual(e.awb, "Auto")


if __name__ == "__main__":
    unittest.main()
