"""Exposure meter, for the manual mode (docs/features.md, M1).

The lens is manual, so the camera cannot reason in apertures. It reasons on the light
reaching the sensor instead: the ISP's "Lux" estimate, computed from linear raw
statistics, does not depend on the exposure settings. The automatic exposure, when
settled, gives the constant linking the two:

    lux x exposure time x gain = K (for a correct exposure)

so in manual, the exposure set is log2(exposure x gain / (K / lux)) stops away from the
one the automatic exposure would pick. K is learnt continuously while the camera is fully
automatic. Measured on a Pi 3B (2026-10-11): K ~ 13.8e6 lux.µs, and the meter reads
-0.9 / +0.8 for settings 1.1 stops under / 0.9 stop over (highlights clipping shift it a
little): good enough for a meter shown in thirds of a stop.
"""

import math

DEFAULT_K = 13.8e6  # lux x µs x gain, measured
LEARNING_RATE = 0.05  # per stats event (5 per second): settles in a few seconds
MAX_STOPS = 3.0


def calibrate(k, stats, ev):
    """The constant updated from a settled automatic exposure (or unchanged)."""
    lux, exposure, gain = stats.get("lux"), stats.get("exposure_us"), _gain(stats)
    if not stats.get("ae_converged") or not lux or not exposure or not gain:
        return k
    # With a compensation, the automatic exposure aims at 2^ev times the neutral one.
    measured = lux * exposure * gain / 2**ev
    return math.exp((1 - LEARNING_RATE) * math.log(k) + LEARNING_RATE * math.log(measured))


def offset(k, stats, exposure_us=None, gain=None):
    """How many stops an exposure is above the neutral one (None: no measure): the one the
    sensor runs at, or the one given (in M: the viewfinder may not run at it)."""
    lux = stats.get("lux")
    exposure = exposure_us if exposure_us is not None else stats.get("exposure_us")
    gain = gain if gain is not None else _gain(stats)
    if not lux or not exposure or not gain:
        return None
    return math.log2(exposure * gain * lux / k)


def to_thirds(stops):
    """Rounded to a third of a stop and bounded, as shown."""
    return max(-MAX_STOPS - 1 / 3, min(MAX_STOPS + 1 / 3, round(stops * 3) / 3))


def _gain(stats):
    return stats.get("analogue_gain", 0) * stats.get("digital_gain", 1)
