"""Settings that survive a restart, in ~/.config/pycam/settings.json.

The application is the source of truth: it pushes everything to the engine whenever it
(re)connects (docs/architecture.md §6).
"""

import copy
import json
import logging
import os
from dataclasses import dataclass, field, fields

from . import drive, picture, video
from .view import GUIDES
from .exposure import Exposure
from .lenses import DEFAULT_LENSES, clean_lens

log = logging.getLogger(__name__)

DISPLAY_MODES = ("detailed", "simple", "clean")  # visée détaillée, simple, nature
STRATEGIES = ("switch", "zsl")
PEAKING_COLORS = ("red", "yellow", "green", "blue", "white")
PEAKING_SENSITIVITIES = ("low", "medium", "high")
REVIEW_SECONDS = (0, 1, 2, 4)  # 0: no review after the shot
HISTOGRAMS = ("rgb", "luma")
BULB_MODES = ("hold", "toggle")
SCREEN_OFF = (10, 30, 60, 0)  # B: held shutter, or one press to start and one to stop
# What the Fn1 and Fn2 buttons can do: actions of Camera.on_action.
FN_ACTIONS = ("peaking", "zebra", "awb_lock", "grid", "strategy", "raw", "drive", "video")
CAMERA_MODES = ("photo", "video")
# Cloud sync (apps/cloudsync): only by hand, send and keep, send and free the card. Freeing
# last, so that turning the dial from one to the other never goes through it.
CLOUD_MODES = ("manual", "sync", "offload")
# Updates (updates.py): found and installed at the next start, found and proposed, or only
# when asked for.
UPDATE_MODES = ("auto", "notify", "off")


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
    drive: str = "single"  # drive.MODES
    interval_seconds: int = 10  # drive.INTERVALS
    interval_count: int = 0  # drive.COUNTS, 0: until stopped
    bracket_shots: int = 3  # drive.BRACKET_SHOTS
    bracket_step: int = 3  # thirds of a stop, drive.BRACKET_STEPS
    bulb_mode: str = "hold"  # BULB_MODES
    timelapse_interval: int = 5  # drive.TL_INTERVALS, seconds
    timelapse_frames: int = 250  # drive.TL_COUNTS, 0: until stopped
    timelapse_fps: int = 25  # drive.TL_FPS
    timelapse_aspect: str = "16:9"  # drive.TL_ASPECTS
    timelapse_deflicker: bool = True  # smooth the brightness of the frames in the video
    timelapse_photos: str = "video"  # drive.TL_PHOTOS
    stack_mode: str = "mean"  # drive.STACK_MODES
    stack_frames: int = 8  # drive.STACK_COUNTS, 0: until stopped
    mode: str = "photo"  # CAMERA_MODES
    video_resolution: str = "1080p"  # video.RESOLUTIONS
    video_quality: str = "normal"  # video.QUALITIES
    video_iso: int = 0  # video.ISO_VALUES, 0: automatic
    video_fps: int = 25  # video.FRAME_RATES (50: 720p only)
    video_slowmo: bool = False  # 50 frames a second played at 25 (720p)
    video_shutter: str = "auto"  # video.SHUTTERS: automatic, or 180° (half a frame)
    screen_off: int = 30  # SCREEN_OFF: seconds without a press before the screen sleeps in a long capture, 0: never
    display: str = "detailed"
    grid: bool = True
    guide: str = "none"  # view.GUIDES: a framing guide on the viewfinder
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
    web: bool = True  # the web interface, on the local network (web.py)
    web_code: bool = True  # only the browsers paired by a code shown on the screen (web_access.py)
    hotspot_password: str = ""  # the camera's access point (wifi.py), made at its first use
    cloud_mode: str = "sync"  # CLOUD_MODES; nothing happens until an account is connected
    update_mode: str = "auto"  # UPDATE_MODES
    update_checked: float = 0.0  # when the last release was looked for (time.time())
    update_seen: float = 0.0  # the updates' last result shown on the screen (its time)
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
    def video_rate(self):
        """The frame rate, or "slow" (slow motion): the menu's Cadence line."""
        return "slow" if self.video_slowmo and self.video_fps == 50 else self.video_fps

    @video_rate.setter
    def video_rate(self, value):
        self.video_slowmo = value == "slow"
        self.video_fps = 50 if self.video_slowmo else value

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
    if settings.bulb_mode not in BULB_MODES:
        settings.bulb_mode = defaults.bulb_mode
    if settings.screen_off not in SCREEN_OFF:
        settings.screen_off = defaults.screen_off
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
        ("drive", drive.MODES),
        ("interval_seconds", drive.INTERVALS),
        ("interval_count", drive.COUNTS),
        ("bracket_shots", drive.BRACKET_SHOTS),
        ("bracket_step", drive.BRACKET_STEPS),
        ("timelapse_interval", drive.TL_INTERVALS),
        ("timelapse_frames", drive.TL_COUNTS),
        ("timelapse_fps", drive.TL_FPS),
        ("timelapse_aspect", drive.TL_ASPECTS),
        ("timelapse_photos", drive.TL_PHOTOS),
        ("stack_mode", drive.STACK_MODES),
        ("mode", CAMERA_MODES),
        ("cloud_mode", CLOUD_MODES),
        ("update_mode", UPDATE_MODES),
        ("video_resolution", tuple(video.RESOLUTIONS)),
        ("video_quality", video.QUALITIES),
        ("video_iso", video.ISO_VALUES),
        ("video_fps", video.FRAME_RATES),
        ("video_shutter", video.SHUTTERS),
        ("guide", GUIDES),
        ("stack_frames", drive.STACK_COUNTS),
    ):
        if getattr(settings, name) not in allowed:
            setattr(settings, name, getattr(defaults, name))
    if settings.video_fps == 50 and settings.video_resolution != "720p":
        settings.video_fps = defaults.video_fps
    if settings.video_fps != 50:
        settings.video_slowmo = False
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
