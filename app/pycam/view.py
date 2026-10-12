"""What the HUD shows, as a plain value: the camera builds it, the HUD draws it."""

from dataclasses import dataclass, replace

# Framing guides: the crop of another format, in the image (stories, Instagram, cinema).
GUIDES = ("none", "9:16", "4:5", "1:1", "2.39:1")


def guide_rect(rect, guide):
    """(x, y, w, h): the largest frame of the guide's format centred in rect, or rect."""
    x, y, w, h = rect
    if guide in ("", "none"):
        return rect
    a, b = (float(v) for v in guide.split(":"))
    ratio = a / b
    if w / h > ratio:  # narrower than the image: full height
        gw = round(h * ratio)
        return x + (w - gw) // 2, y, gw, h
    gh = round(w / ratio)
    return x, y + (h - gh) // 2, w, gh


@dataclass(frozen=True)
class HudView:
    """Two equal views make the same image: no need to redraw."""

    display: str = "detailed"  # detailed | simple | clean
    grid: bool = True
    guide: str = ""  # GUIDES: a framing guide, shaded outside (the grid then divides it)
    mode: str = "P"
    metering: str = "centre"  # centre | spot | matrix
    shutter: str = "--"
    shutter_auto: bool = True
    iso: str = "--"
    iso_auto: bool = True
    ev: str = ""  # empty in M, where it does nothing
    meter: float | None = None  # stops from the neutral exposure, in thirds; None: hidden
    meter_warning: bool = False  # the automatic exposure cannot reach it (out of range)
    awb: str = "AWB"
    formats: str = "RAW+JPEG"
    look: str = ""  # short name of the preset
    lens: str = ""  # "16 mm f/2.8"
    shots: int = 0
    zsl: bool = False
    peaking: str = ""  # its colour when on
    zebra: bool = False
    loupe: float = 1  # 1: off; in playback, the magnification against the fitted photo
    loupe_center: tuple = (0.5, 0.5)
    loupe_label: str = ""  # instead of "×5": "100 %" in playback
    review: str = ""  # path of the photo being reviewed (decoded by Hud.load_review)
    review_info: str = ""
    playback: str = ""  # in playback, where we are: "12/57"
    video_hint: str = ""  # on a video's first frame: how to play it
    video_playing: bool = False  # a video on the video plane: the HUD only overlays it
    video_paused: bool = False
    video_time: str = ""  # "0:02 / 0:04"
    video_progress: float = 0.0  # 0..1
    menu: tuple = ()  # menu lines (label, value shown; None for a section title); empty: closed
    menu_index: int = 0
    menu_title: str = ""
    writing: int = 0  # photos waiting to be written
    drive: str = ""  # the drive mode ("RAFALE"), or where its sequence is ("INT 4/50 · 8 s")
    drive_running: bool = False  # a sequence is running
    countdown: int = 0  # the self-timer's seconds left, shown big
    task: str = ""  # a background task and where it is ("VIDÉO 40 %")
    video_mode: bool = False  # the video mode: a 16:9 image, the info bar for the video
    recording: str = ""  # while recording, for how long ("0:12")
    exposure_timer: str = ""  # a long exposure's time left, a bulb's time so far ("0:23")
    exposure_label: str = ""  # "POSE", "POSE B"
    viewfinder_auto: bool = False  # the viewfinder does not show the manual exposure (too long)
    histogram: tuple = ()  # luminance, 64 classes
    histogram_rgb: tuple = ()  # (red, green, blue), 64 classes each; drawn instead
    message: str = ""
    warning: bool = False


# What the info bar and the badges show: nothing of it in the "clean" display.
INFO_FIELDS = ("mode", "metering", "shutter", "shutter_auto", "iso", "iso_auto", "ev", "meter", "meter_warning", "awb",
               "formats", "look", "lens", "shots", "drive", "drive_running", "task", "zsl", "peaking", "zebra",
               "writing", "viewfinder_auto")  # fmt: skip
_DEFAULTS = HudView()


def visible(view):
    """The view without what its display does not show: a value changing there does not
    redraw the HUD (~0.015 s of CPU each time)."""
    if view.display != "clean" or view.review or view.video_playing:
        return view
    return replace(view, **{name: getattr(_DEFAULTS, name) for name in INFO_FIELDS})
