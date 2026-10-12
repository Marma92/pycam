"""Settings that survive a restart, in ~/.config/pycam/settings.json.

The application is the source of truth: it pushes everything to the engine whenever it
(re)connects (docs/architecture.md §6).
"""

import copy
import json
import logging
import os
from dataclasses import dataclass, field, fields

from . import picture
from .exposure import Exposure
from .lenses import DEFAULT_LENSES, clean_lens

log = logging.getLogger(__name__)

DISPLAY_MODES = ("detailed", "simple", "clean")  # visée détaillée, simple, nature
STRATEGIES = ("switch", "zsl")
PEAKING_COLORS = ("red", "yellow", "green", "blue", "white")
PEAKING_SENSITIVITIES = ("low", "medium", "high")
REVIEW_SECONDS = (0, 1, 2, 4)  # 0: no review after the shot
HISTOGRAMS = ("rgb", "luma")
# What the Fn1 and Fn2 buttons can do: actions of Camera.on_action.
FN_ACTIONS = ("peaking", "zebra", "awb_lock", "grid", "strategy", "raw")


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pycam", "settings.json")


@dataclass
class Settings:
    exposure: Exposure = field(default_factory=Exposure)
    formats: list = field(default_factory=lambda: ["jpeg", "dng"])
    dng_compression: bool = True  # lossless: 12 MB instead of 25, ~1.5 s more of background writing
    jpeg_quality: int = 93
    jpeg_size: str = "L"  # picture.JPEG_SIZES
    sharpness: int = 0  # picture.py: levels -2..+2
    contrast: int = 0
    saturation: int = 0  # -3: black and white
    denoise: str = "hq"  # picture.DENOISE
    strategy: str = "switch"
    display: str = "detailed"
    grid: bool = True
    peaking: bool = False
    peaking_color: str = "red"
    peaking_sensitivity: str = "medium"
    zebra: bool = False
    review_seconds: int = 2
    histogram: str = "rgb"  # HISTOGRAMS, in the detailed display
    preset: str = "standard"  # the look of the JPEGs: a folder name (looks.py)
    grain: bool = True  # simulate the preset's film grain
    keep_base: bool = False  # also write the JPEG without the look (PYB_0001.JPG)
    lens_shading: bool = False  # correct the lens shading (another ISP tuning)
    fn1: str = "peaking"  # FN_ACTIONS
    fn2: str = "zebra"
    lenses: list = field(default_factory=lambda: copy.deepcopy(DEFAULT_LENSES))  # edit to add yours
    lens: int = -1  # index in lenses, -1: none chosen
    aperture: float | None = None  # f-number set on the ring, None: unknown
    meter_k: float = 13.8e6  # exposure meter constant, learnt in automatic (meter.py)
    dcim: str = "~/DCIM"
    folder_number: int = 100
    next_number: int = 1

    @property
    def peaking_mode(self):
        """Off or its colour: the peaking in one menu line."""
        return self.peaking_color if self.peaking else "off"

    @peaking_mode.setter
    def peaking_mode(self, value):
        self.peaking = value != "off"
        if self.peaking:
            self.peaking_color = value

    def to_dict(self):
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["exposure"] = self.exposure.to_dict()
        return data


def load_settings(path):
    """The saved settings, or the defaults (with a warning if the file is unreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as e:
        log.warning("unreadable settings %s (%s): using defaults", path, e)
        return Settings()

    settings = Settings()
    if not isinstance(data, dict):
        return settings
    defaults = Settings()
    for f in fields(Settings):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "exposure":
            if isinstance(value, dict):
                settings.exposure = Exposure.from_dict(value)
        elif f.name == "lenses":
            if isinstance(value, list):
                lenses = [clean_lens(entry) for entry in value]
                if None in lenses:
                    log.warning("ignoring invalid lens profiles in %s", path)
                settings.lenses = [lens for lens in lenses if lens]
        elif f.name == "aperture":
            if isinstance(value, (int, float)) and value > 0:
                settings.aperture = float(value)
        elif type(value) is type(getattr(defaults, f.name)):
            setattr(settings, f.name, value)
    if settings.display not in DISPLAY_MODES:
        settings.display = defaults.display
    if settings.strategy not in STRATEGIES:
        settings.strategy = defaults.strategy
    if settings.peaking_color not in PEAKING_COLORS:
        settings.peaking_color = defaults.peaking_color
    if settings.peaking_sensitivity not in PEAKING_SENSITIVITIES:
        settings.peaking_sensitivity = defaults.peaking_sensitivity
    if settings.review_seconds not in REVIEW_SECONDS:
        settings.review_seconds = defaults.review_seconds
    if settings.histogram not in HISTOGRAMS:
        settings.histogram = defaults.histogram
    for name in ("fn1", "fn2"):
        if getattr(settings, name) not in FN_ACTIONS:
            setattr(settings, name, getattr(defaults, name))
    for name, allowed in (
        ("jpeg_quality", picture.JPEG_QUALITIES),
        ("jpeg_size", picture.JPEG_SIZES),
        ("sharpness", picture.SHARPNESS),
        ("contrast", picture.CONTRAST),
        ("saturation", picture.SATURATION),
        ("denoise", picture.DENOISE),
    ):
        if getattr(settings, name) not in allowed:
            setattr(settings, name, getattr(defaults, name))
    if not set(settings.formats) <= {"jpeg", "dng"} or not settings.formats:
        settings.formats = defaults.formats
    if not 0 <= settings.lens < len(settings.lenses):
        settings.lens, settings.aperture = -1, None
    return settings


def save_settings(settings, path):
    """Atomic: a power cut leaves either the old or the new file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings.to_dict(), f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
