"""Exposure: shutter speed and ISO, each automatic or manual (docs/architecture.md §3).

The aperture is a ring on the lens, so the classic modes come from what is manual:
P (nothing), S (shutter speed), A (ISO: the camera picks the speed for the aperture set
on the lens), M (both).
"""

import math
from dataclasses import asdict, dataclass

# Third of a stop steps, in seconds, fastest first.
SHUTTER_SPEEDS = (
    1 / 4000, 1 / 3200, 1 / 2500, 1 / 2000, 1 / 1600, 1 / 1250, 1 / 1000, 1 / 800, 1 / 640,
    1 / 500, 1 / 400, 1 / 320, 1 / 250, 1 / 200, 1 / 160, 1 / 125, 1 / 100, 1 / 80, 1 / 60,
    1 / 50, 1 / 40, 1 / 30, 1 / 25, 1 / 20, 1 / 15, 1 / 13, 1 / 10, 1 / 8, 1 / 6, 1 / 5,
    1 / 4, 0.3, 0.4, 0.5, 0.6, 0.8, 1, 1.3, 1.6, 2, 2.5, 3.2, 4, 5, 6, 8, 10, 13, 15, 20,
    25, 30, 40, 50, 60,
)
ISO_VALUES = (100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600)
EV_MAX = 3.0
EV_STEP = 1 / 3
# White balance: libcamera presets, then a colour temperature ("Kelvin"), then the gains
# measured on a grey card ("Locked"). B goes through the first ones; Locked comes from H.
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


def nearest_index(values, x):
    """Index of the value closest to x, in stops."""
    if x <= 0:
        return 0
    return min(range(len(values)), key=lambda i: abs(math.log2(values[i] / x)))


def format_shutter(seconds):
    if seconds <= 0:
        return "--"
    if seconds >= 0.3 - 1e-6:
        return f'{seconds:g}"'
    return f"1/{round(1 / seconds)}"


def format_ev(ev):
    if abs(ev) < 0.05:
        return "±0"
    return f"{ev:+.1f}"


@dataclass
class Exposure:
    shutter: float | None = None  # seconds, None = automatic
    iso: int | None = None  # None = automatic
    ev: float = 0.0  # compensation, used by whatever is automatic
    awb: str = "Auto"  # AWB_MODES, or "Locked"
    kelvin: int = 5500  # for awb == "Kelvin"
    wb_gains: list | None = None  # [red, blue] for awb == "Locked"
    metering: str = "centre"  # METERING

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
        if awb in AWB_MODES or (awb == "Locked" and exposure.wb_gains):
            exposure.awb = awb
        if data.get("metering") in METERING:
            exposure.metering = data["metering"]
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

    def controls(self):
        """The libcamera controls for the engine (protocol.md, set_controls)."""
        controls = {"ExposureValue": self.ev, "AeMeteringMode": METERING[self.metering]}
        # libcamera applies a colour temperature or gains only with the automatic white
        # balance off; back to a preset, it must be turned on again.
        if self.awb == "Kelvin":
            controls.update(AwbEnable=False, ColourTemperature=self.kelvin)
        elif self.awb == "Locked" and self.wb_gains:
            controls.update(AwbEnable=False, ColourGains=list(self.wb_gains))
        else:
            controls.update(AwbEnable=True, AwbMode=self.awb if self.awb in AWB_PRESETS else "Auto")
        if self.shutter is None:
            controls["ExposureTimeMode"] = "Auto"
        else:
            controls["ExposureTime"] = round(self.shutter * 1e6)
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
        """steps > 0: slower (brighter)."""
        current = self.shutter if self.shutter is not None else (measured_seconds or 1 / 60)
        speeds = [s for s in SHUTTER_SPEEDS if s <= slowest * 1.001] or [SHUTTER_SPEEDS[0]]
        index = nearest_index(speeds, current) + steps
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
            self.shutter = None

    def toggle_iso_auto(self, measured_iso):
        if self.iso is None:
            self.step_iso(0, measured_iso)
        else:
            self.iso = None

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

    def awb_label(self):
        return f"{self.kelvin} K" if self.awb == "Kelvin" else AWB_LABELS.get(self.awb, self.awb)
