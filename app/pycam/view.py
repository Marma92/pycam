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
    lens: str = ""  # "16 mm f/2.8"
    shots: int = 0
    zsl: bool = False
    peaking: str = ""  # its colour when on
    zebra: bool = False
    loupe: float = 1  # 1: off
    loupe_center: tuple = (0.5, 0.5)
    review: str = ""  # path of the photo being reviewed (decoded by Hud.load_review)
    review_info: str = ""
    menu: tuple = ()  # quick menu lines (label, value); empty: closed
    menu_index: int = 0
    writing: int = 0  # photos waiting to be written
    histogram: tuple = ()
    message: str = ""
    warning: bool = False
