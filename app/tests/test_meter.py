import math
import unittest

from pycam.meter import DEFAULT_K, calibrate, offset, to_thirds


def stats(lux, exposure_us, gain, converged=True):
    return {"lux": lux, "exposure_us": exposure_us, "analogue_gain": gain, "digital_gain": 1.0, "ae_converged": converged}


class MeterTest(unittest.TestCase):
    def test_offset_in_stops(self):
        k = 80 * 40000 * 4.0  # the automatic exposure picked 40 ms x4 at 80 lux
        self.assertAlmostEqual(offset(k, stats(80, 40000, 4.0)), 0)
        self.assertAlmostEqual(offset(k, stats(80, 20000, 4.0)), -1)
        self.assertAlmostEqual(offset(k, stats(80, 40000, 16.0)), 2)
        self.assertIsNone(offset(k, {}))

    def test_calibration_learns_and_ignores_unsettled_exposures(self):
        target = 80 * 40000 * 4.0
        k = DEFAULT_K
        self.assertEqual(calibrate(k, stats(80, 40000, 4.0, converged=False), 0), k)
        for _ in range(200):
            k = calibrate(k, stats(80, 40000, 4.0), 0)
        self.assertAlmostEqual(math.log2(k / target), 0, places=2)

    def test_calibration_takes_the_compensation_out(self):
        k = 80 * 40000 * 4.0
        # +1 stop of compensation: the automatic exposure doubles the time; K must not move.
        self.assertAlmostEqual(calibrate(k, stats(80, 80000, 4.0), 1.0), k)

    def test_thirds(self):
        self.assertAlmostEqual(to_thirds(0.71), 2 / 3)
        self.assertAlmostEqual(to_thirds(-7), -10 / 3)  # bounded just past the scale


if __name__ == "__main__":
    unittest.main()
