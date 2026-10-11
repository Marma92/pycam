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
        self.assertEqual(Exposure.from_dict({"awb": "Locked"}).awb, "Auto")  # no gains: nothing to lock

    def test_white_balance_controls(self):
        self.assertEqual(Exposure(awb="Daylight").controls()["AwbEnable"], True)
        self.assertEqual(Exposure(awb="Daylight").controls()["AwbMode"], "Daylight")
        kelvin = Exposure(awb="Kelvin", kelvin=3200).controls()
        self.assertEqual((kelvin["AwbEnable"], kelvin["ColourTemperature"]), (False, 3200))
        self.assertNotIn("AwbMode", kelvin)
        locked = Exposure(awb="Locked", wb_gains=[2.1, 1.7]).controls()
        self.assertEqual((locked["AwbEnable"], locked["ColourGains"]), (False, [2.1, 1.7]))

    def test_kelvin_dial_starts_from_the_measure(self):
        e = Exposure()
        e.step_kelvin(+1, 4480)  # first press: the measured temperature, rounded
        self.assertEqual((e.awb, e.kelvin), ("Kelvin", 4500))
        e.step_kelvin(+1, 4480)
        self.assertEqual(e.kelvin, 4800)
        self.assertEqual(e.awb_label(), "4800 K")
        e.next_awb()  # Kelvin is last: back to Auto
        self.assertEqual(e.awb, "Auto")

    def test_lock_needs_a_measure(self):
        e = Exposure()
        self.assertFalse(e.lock_awb(None))
        self.assertTrue(e.lock_awb([2.72, 1.56]))
        self.assertEqual((e.awb, e.awb_label()), ("Locked", "Mémorisée"))
        e.next_awb()
        self.assertEqual(e.awb, "Auto")

    def test_metering(self):
        self.assertEqual(Exposure().controls()["AeMeteringMode"], "MeteringCentreWeighted")
        e = Exposure.from_dict({"metering": "spot"})
        self.assertEqual(e.controls()["AeMeteringMode"], "MeteringSpot")
        self.assertEqual(Exposure.from_dict(e.to_dict()).metering, "spot")
        self.assertEqual(Exposure.from_dict({"metering": "evaluative"}).metering, "centre")


if __name__ == "__main__":
    unittest.main()
