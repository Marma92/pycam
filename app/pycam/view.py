"""What the HUD shows, as a plain value: the camera builds it, the HUD draws it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HudView:
    """Two equal views make the same image: no need to redraw."""

    display: str = "detailed"  # detailed | simple | clean
    grid: bool = True
    mode: str = "P"
    shutter: str = "--"
    shutter_auto: bool = True
    iso: str = "--"
    iso_auto: bool = True
    ev: str = ""  # empty in M, where it does nothing
    awb: str = "AWB"
    formats: str = "RAW+JPEG"
    shots: int = 0
    zsl: bool = False
    peaking: str = ""  # its colour when on
    zebra: bool = False
    loupe: float = 1  # 1: off
    loupe_center: tuple = (0.5, 0.5)
    review: str = ""  # path of the photo being reviewed (decoded by Hud.load_review)
    review_info: str = ""
    writing: int = 0  # photos waiting to be written
    histogram: tuple = ()
    message: str = ""
    warning: bool = False
