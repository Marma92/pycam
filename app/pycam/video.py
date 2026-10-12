"""The video mode: a camera made for photos, with video as a backup (docs/architecture.md §5).

Few settings: the resolution, the quality (bitrate) and the ISO; the rest is automatic.
25 frames a second (50 Hz mains light does not flicker at 1/50 s), the exposure automatic
within a frame, with the compensation on the front dial; the ISO on the rear dial, or
automatic. The white balance and the metering are the photo's. No look: the video keeps the
pycam base (a film simulation at 25 frames a second is beyond a Pi 3). No sound: the Pi has
no microphone. H.264 in MP4, by the VideoCore's encoder (the engine's Recorder).
"""

from . import exposure as exp

FPS = 25
RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720)}
QUALITIES = ("high", "normal", "light")
QUALITY_LABELS = {"high": "Haute", "normal": "Normale", "light": "Légère"}
BITRATES = {  # bit/s
    ("1080p", "high"): 20_000_000,
    ("1080p", "normal"): 12_000_000,
    ("1080p", "light"): 6_000_000,
    ("720p", "high"): 10_000_000,
    ("720p", "normal"): 6_000_000,
    ("720p", "light"): 3_000_000,
}
ISO_VALUES = (0,) + exp.ISO_VALUES  # 0: automatic


def bitrate(settings):
    return BITRATES[(settings.video_resolution, settings.video_quality)]


def engine_setup(settings):
    """The engine's video_mode command (protocol.md)."""
    width, height = RESOLUTIONS[settings.video_resolution]
    return {"width": width, "height": height, "fps": FPS, "bitrate": bitrate(settings)}


def controls(settings):
    """The exposure of the video: automatic speed, the ISO set (or automatic), the
    photo's compensation, metering and white balance (not the film's: no look here)."""
    e = settings.exposure
    video = exp.Exposure(
        shutter=None,
        iso=settings.video_iso or None,
        ev=e.ev,
        awb="Auto" if e.awb == "Film" else e.awb,
        kelvin=e.kelvin,
        wb_gains=e.wb_gains,
        metering=e.metering,
    )
    return video.controls()


def minutes_left(settings, free_bytes):
    return int(free_bytes * 8 / bitrate(settings) / 60)


def label(settings):
    """"1080p · 25 i/s · Normale" for the HUD."""
    return f"{settings.video_resolution} {FPS} i/s · {QUALITY_LABELS[settings.video_quality]}"


def step_iso(settings, steps, measured_iso):
    """The rear dial in video: from automatic, the first step starts at the measure."""
    values = exp.ISO_VALUES
    current = settings.video_iso or measured_iso or 100
    index = exp.nearest_index(values, current) + (steps if settings.video_iso else 0)
    settings.video_iso = values[max(0, min(len(values) - 1, index))]
