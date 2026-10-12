"""What the HUD shows, as a plain value: the camera builds it, the HUD draws it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HudView:
    """Two equal views make the same image: no need to redraw."""

    display: str = "detailed"  # detailed | simple | clean
    grid: bool = True
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
    menu: tuple = ()  # menu lines (label, value shown; None for a section title); empty: closed
    menu_index: int = 0
    menu_title: str = ""
    writing: int = 0  # photos waiting to be written
    drive: str = ""  # the drive mode ("RAFALE"), or where its sequence is ("INT 4/50 · 8 s")
    drive_running: bool = False  # a sequence is running
    countdown: int = 0  # the self-timer's seconds left, shown big
    exposure_timer: str = ""  # a long exposure's time left, a bulb's time so far ("0:23")
    exposure_label: str = ""  # "POSE", "POSE B"
    viewfinder_auto: bool = False  # the viewfinder does not show the manual exposure (too long)
    histogram: tuple = ()  # luminance, 64 classes
    histogram_rgb: tuple = ()  # (red, green, blue), 64 classes each; drawn instead
    message: str = ""
    warning: bool = False
