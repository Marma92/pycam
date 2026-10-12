"""Exposure: shutter speed and ISO, each automatic or manual (docs/architecture.md §3).

The aperture is a ring on the lens, so the classic modes come from what is manual:
P (nothing), S (shutter speed), A (ISO: the camera picks the speed for the aperture set
on the lens), M (both).

Long exposures: up to 1 s in S (the automatic gain needs a few frames of the exposure to
settle), up to the sensor's limit in M (10'40"), then B, the bulb: as long as the
shutter is held, made of 1 s exposures added up (drive.py, the engine's stacks).
"""

import math
from dataclasses import asdict, dataclass

# Third of a stop steps, in seconds, fastest first.
SHUTTER_SPEEDS = (
    1 / 4000, 1 / 3200, 1 / 2500, 1 / 2000, 1 / 1600, 1 / 1250, 1 / 1000, 1 / 800, 1 / 640,
    1 / 500, 1 / 400, 1 / 320, 1 / 250, 1 / 200, 1 / 160, 1 / 125, 1 / 100, 1 / 80, 1 / 60,
    1 / 50, 1 / 40, 1 / 30, 1 / 25, 1 / 20, 1 / 15, 1 / 13, 1 / 10, 1 / 8, 1 / 6, 1 / 5,
    1 / 4, 0.3, 0.4, 0.5, 0.6, 0.8, 1, 1.3, 1.6, 2, 2.5, 3.2, 4, 5, 6, 8, 10, 13, 15, 20,
    25, 30, 40, 50, 60, 80, 100, 120, 160, 200, 240, 320, 400, 480, 640,
)
S_SLOWEST = 1.0  # seconds: the slowest speed in S
BULB_FRAME = 1.0  # seconds: the exposures a bulb is made of
ISO_VALUES = (100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600)
EV_MAX = 3.0
EV_STEP = 1 / 3
# White balance: libcamera presets, then a colour temperature ("Kelvin"), then the gains
# measured on a grey card ("Locked"), then the film's own ("Film": the preset's balance,
# fixed like a film's: 5500 K for daylight films, 3200 K for tungsten ones).
AWB_PRESETS = ("Auto", "Daylight", "Cloudy", "Tungsten", "Fluorescent", "Indoor")
AWB_MODES = AWB_PRESETS + ("Kelvin",)
AWB_LABELS = {
    "Auto": "AWB",
    "Daylight": "Soleil",
    "Cloudy": "Nuageux",
    "Tungsten": "Tungstène",
    "Fluorescent": "Fluo",
    "Indoor": "Intérieur",
    "Locked": "Mémorisée",
}
# Metering: which part of the image the automatic exposure looks at (weights of the 15
# statistics zones of the ISP, in the IMX477 tuning file). Measured on the same scene:
# spot -1.6 stops, matrix +0.4 stop against centre-weighted.
METERING = {"centre": "MeteringCentreWeighted", "spot": "MeteringSpot", "matrix": "MeteringMatrix"}
METERING_LABELS = {"centre": "Pondérée centrale", "spot": "Spot", "matrix": "Matricielle"}

KELVIN_STEPS = (
    2000, 2200, 2500, 2700, 3000, 3200, 3500, 3800, 4000, 4300, 4500, 4800, 5000, 5300, 5500, 5800,
    6000, 6500, 7000, 7500, 8000, 9000, 10000,
)


# The ISO shown when automatic: the nearest third of a stop, as cameras do (the measured one
# moves by a few units at each measure, which would redraw the HUD each time).
NOMINAL_ISO = ISO_VALUES + (2000, 2500, 3200)


def nominal_iso(measured):
    return NOMINAL_ISO[nearest_index(NOMINAL_ISO, measured)] if measured > 0 else 0


def nearest_index(values, x):
    """Index of the value closest to x, in stops."""
    if x <= 0:
        return 0
    return min(range(len(values)), key=lambda i: abs(math.log2(values[i] / x)))


def format_shutter(seconds):
    if seconds <= 0:
        return "--"
    if seconds >= 60:
        minutes, rest = divmod(round(seconds), 60)
        return f"{minutes}'{rest:02d}\"" if rest else f"{minutes}'"
    if seconds >= 0.3 - 1e-6:
        return f'{seconds:g}"'
    return f"1/{round(1 / seconds)}"


def format_duration(seconds):
    """A long exposure's time, as on a timer: 0:07, 2:30."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_ev(ev):
    if abs(ev) < 0.05:
        return "±0"
    return f"{ev:+.1f}"


@dataclass
class Exposure:
    shutter: float | None = None  # seconds, None = automatic
    iso: int | None = None  # None = automatic
    ev: float = 0.0  # compensation, used by whatever is automatic
    awb: str = "Auto"  # AWB_MODES, "Locked" or "Film"
    kelvin: int = 5500  # for awb == "Kelvin"
    wb_gains: list | None = None  # [red, blue] for awb == "Locked"
    metering: str = "centre"  # METERING
    bulb: bool = False  # B, past the slowest speed in M: as long as the shutter is held

    @classmethod
    def from_dict(cls, data):
        exposure = cls()
        shutter, iso = data.get("shutter"), data.get("iso")
        if isinstance(shutter, (int, float)) and shutter > 0:
            exposure.shutter = SHUTTER_SPEEDS[nearest_index(SHUTTER_SPEEDS, shutter)]
        if isinstance(iso, (int, float)) and iso > 0:
            exposure.iso = ISO_VALUES[nearest_index(ISO_VALUES, iso)]
        if isinstance(data.get("ev"), (int, float)):
            exposure.ev = max(-EV_MAX, min(EV_MAX, float(data["ev"])))
        if isinstance(data.get("kelvin"), (int, float)) and data["kelvin"] > 0:
            exposure.kelvin = KELVIN_STEPS[nearest_index(KELVIN_STEPS, data["kelvin"])]
        gains = data.get("wb_gains")
        if isinstance(gains, list) and len(gains) == 2 and all(isinstance(g, (int, float)) and 0 < g < 32 for g in gains):
            exposure.wb_gains = [float(g) for g in gains]
        awb = data.get("awb")
        if awb in AWB_MODES or awb == "Film" or (awb == "Locked" and exposure.wb_gains):
            exposure.awb = awb
        if data.get("metering") in METERING:
            exposure.metering = data["metering"]
        exposure.bulb = data.get("bulb") is True and exposure.mode == "M"
        if exposure.mode == "S" and exposure.shutter > S_SLOWEST:
            exposure.shutter = S_SLOWEST
        return exposure

    def to_dict(self):
        return asdict(self)

    @property
    def mode(self):
        return {
            (False, False): "P",
            (True, False): "S",
            (False, True): "A",
            (True, True): "M",
        }[(self.shutter is not None, self.iso is not None)]

    def controls(self, bias=0.0, film_kelvin=None):
        """The libcamera controls for the engine (protocol.md, set_controls). bias: the
        preset's exposure shift, added to the compensation; film_kelvin: the preset's own
        white balance, used when the balance is "Film" (None: the film has none, auto)."""
        controls = {"ExposureValue": self.ev + bias, "AeMeteringMode": METERING[self.metering]}
        # libcamera applies a colour temperature or gains only with the automatic white
        # balance off; back to a preset, it must be turned on again.
        if self.awb == "Film" and film_kelvin:
            controls.update(AwbEnable=False, ColourTemperature=film_kelvin)
        elif self.awb == "Kelvin":
            controls.update(AwbEnable=False, ColourTemperature=self.kelvin)
        elif self.awb == "Locked" and self.wb_gains:
            controls.update(AwbEnable=False, ColourGains=list(self.wb_gains))
        else:
            controls.update(AwbEnable=True, AwbMode=self.awb if self.awb in AWB_PRESETS else "Auto")
        if self.shutter is None:
            controls["ExposureTimeMode"] = "Auto"
        else:
            # B: the engine adds up exposures of BULB_FRAME; the viewfinder shows one.
            controls["ExposureTime"] = round((BULB_FRAME if self.bulb else self.shutter) * 1e6)
            controls["ExposureTimeMode"] = "Manual"
        if self.iso is None:
            controls["AnalogueGainMode"] = "Auto"
        else:
            controls["AnalogueGain"] = self.iso / 100
            controls["AnalogueGainMode"] = "Manual"
        return controls

    # The dials. From automatic, the first step starts from the measured value, so that
    # the picture does not jump when going manual.

    def step_shutter(self, steps, measured_seconds, slowest=SHUTTER_SPEEDS[-1]):
        """steps > 0: slower (brighter). In M, one step past the slowest speed is B."""
        manual_iso = self.iso is not None
        limit = slowest if manual_iso else min(slowest, S_SLOWEST)
        speeds = [s for s in SHUTTER_SPEEDS if s <= limit * 1.001] or [SHUTTER_SPEEDS[0]]
        if self.bulb:
            if steps >= 0:
                return
            self.bulb, self.shutter, steps = False, speeds[-1], steps + 1
        current = self.shutter if self.shutter is not None else (measured_seconds or 1 / 60)
        index = nearest_index(speeds, current) + steps
        if manual_iso and index >= len(speeds):
            self.bulb, self.shutter = True, speeds[-1]
            return
        self.shutter = speeds[max(0, min(len(speeds) - 1, index))]

    def step_iso(self, steps, measured_iso):
        current = self.iso if self.iso is not None else (measured_iso or 100)
        index = nearest_index(ISO_VALUES, current) + steps
        self.iso = ISO_VALUES[max(0, min(len(ISO_VALUES) - 1, index))]

    def step_ev(self, steps):
        self.ev = round(max(-EV_MAX, min(EV_MAX, self.ev + steps * EV_STEP)) * 3) / 3

    def toggle_shutter_auto(self, measured_seconds):
        if self.shutter is None:
            self.step_shutter(0, measured_seconds)
        else:
            self.shutter, self.bulb = None, False

    def toggle_iso_auto(self, measured_iso):
        if self.iso is None:
            self.step_iso(0, measured_iso)
        else:
            self.iso, self.bulb = None, False
            if self.shutter is not None and self.shutter > S_SLOWEST:  # S stops at 1 s
                self.shutter = S_SLOWEST

    def next_awb(self):
        """Presets, then Kelvin, then back to Auto (a locked balance is left for Auto)."""
        index = AWB_MODES.index(self.awb) if self.awb in AWB_MODES else -1
        self.awb = AWB_MODES[(index + 1) % len(AWB_MODES)]

    def step_kelvin(self, steps, measured_kelvin):
        """Turns the white balance dial; from another mode, starts at the measured temperature."""
        current = self.kelvin if self.awb == "Kelvin" else (measured_kelvin or self.kelvin)
        index = nearest_index(KELVIN_STEPS, current) + (steps if self.awb == "Kelvin" else 0)
        self.kelvin = KELVIN_STEPS[max(0, min(len(KELVIN_STEPS) - 1, index))]
        self.awb = "Kelvin"

    def lock_awb(self, measured_gains):
        """Keeps the gains the automatic balance found (point at a grey card first)."""
        if not measured_gains or len(measured_gains) != 2 or not all(g > 0 for g in measured_gains):
            return False
        self.wb_gains = [round(float(g), 4) for g in measured_gains]
        self.awb = "Locked"
        return True

    def awb_label(self, film_kelvin=None):
        if self.awb == "Film":
            return f"Film {film_kelvin} K" if film_kelvin else "Film AWB"
        return f"{self.kelvin} K" if self.awb == "Kelvin" else AWB_LABELS.get(self.awb, self.awb)
