"""The video mode: a camera made for photos, with video as a backup (docs/architecture.md §5).

Few settings: the resolution, the frame rate, the quality (bitrate), the speed and the
ISO; the rest is automatic. 25 frames a second by default (50 Hz mains light does not
flicker at 1/50 s); 24 for cinema, 30, and 50 in 720p (the encoder of a Pi 3 does not go to
1080p50). The exposure is automatic within a frame, with the compensation on the front
dial; or the speed at 180° (half a frame: the motion blur of cinema), the gain following;
the ISO on the rear dial, or automatic. S locks the exposure as it is (AE-L), for a take.
A slow motion: 50 frames a second in 720p, played at 25 (the engine stretches the times).
No look, but the ISP does the contrast and the saturation of the Image settings, and black
and white for a black and white preset (picture.controls). The white balance and the metering are the photo's. No look: the video keeps the
pycam base (a film simulation at 25 frames a second is beyond a Pi 3). No sound: the Pi has
no microphone. H.264 in MP4, by the VideoCore's encoder (the engine's Recorder).
"""

from . import exposure as exp

FRAME_RATES = (24, 25, 30, 50)
FPS_LABELS = {24: "24 i/s · cinéma", 25: "25 i/s", 30: "30 i/s · scintille sous 50 Hz", 50: "50 i/s · 720p",
              "slow": "Ralenti ×2 · 50 i/s lus à 25"}  # fmt: skip
SHUTTERS = ("auto", "180")  # automatic, or half a frame
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
MAX_BITRATE = 25_000_000  # the engine's limit
# Left free on the card: the end of the MP4 (its index) is written when it stops, and a
# file that cannot be ended is lost whole. Warned a minute before, stopped 10 s before.
RESERVE_BYTES = 64 * 1024**2
WARN_SECONDS, STOP_SECONDS = 60, 10


def fps(settings):
    """The frame rate used: 50 only in 720p."""
    return 25 if settings.video_fps == 50 and settings.video_resolution != "720p" else settings.video_fps


def fps_options(settings):
    """For the menu's Cadence line (Settings.video_rate): the slow motion with them."""
    rates = FRAME_RATES + ("slow",) if settings.video_resolution == "720p" else FRAME_RATES[:-1]
    return tuple((f, FPS_LABELS[f]) for f in rates)


def slowdown(settings):
    """How many times slower the file plays than it is filmed: 2 in slow motion."""
    return 2 if settings.video_slowmo and fps(settings) == 50 else 1


def bitrate(settings, quality=None):
    """More frames, more bits: the table is for 25 frames a second."""
    base = BITRATES[(settings.video_resolution, quality or settings.video_quality)]
    return min(MAX_BITRATE, round(base * max(fps(settings), 25) / 25))


def shutter(settings):
    """The speed set, in seconds, or None (automatic)."""
    return 1 / (2 * fps(settings)) if settings.video_shutter == "180" else None


def engine_setup(settings):
    """The engine's video_mode command (protocol.md)."""
    width, height = RESOLUTIONS[settings.video_resolution]
    return {"width": width, "height": height, "fps": fps(settings), "bitrate": bitrate(settings), "slowdown": slowdown(settings)}


def controls(settings, lock=None):
    """The exposure of the video: the speed automatic or at 180°, the ISO set (or
    automatic), the photo's compensation, metering and white balance (not the film's: no
    look here). lock: (seconds, ISO), the exposure locked (AE-L)."""
    e = settings.exposure
    speed, iso = lock or (shutter(settings), settings.video_iso or None)
    video = exp.Exposure(
        shutter=speed,
        iso=iso,
        ev=e.ev,
        awb="Auto" if e.awb == "Film" else e.awb,
        kelvin=e.kelvin,
        wb_gains=e.wb_gains,
        metering=e.metering,
    )
    return video.controls()


def seconds_left(settings, free_bytes):
    """Of video that the card can still take, its reserve kept."""
    return max(0.0, (free_bytes - RESERVE_BYTES) * 8 / bitrate(settings))


def minutes_left(settings, free_bytes):
    return int(seconds_left(settings, free_bytes) / 60)


def label(settings):
    """"1080p25 · Normale" for the HUD (its bar is full in video), "720p ×2 · Normale" in
    slow motion."""
    rate = " ×2" if slowdown(settings) > 1 else str(fps(settings))
    return f"{settings.video_resolution}{rate} · {QUALITY_LABELS[settings.video_quality]}"


def step_iso(settings, steps, measured_iso):
    """The rear dial in video: from automatic, the first step starts at the measure."""
    values = exp.ISO_VALUES
    current = settings.video_iso or measured_iso or 100
    index = exp.nearest_index(values, current) + (steps if settings.video_iso else 0)
    settings.video_iso = values[max(0, min(len(values) - 1, index))]
