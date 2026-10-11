"""The quick menu: the settings changed now and then, in a list over the viewfinder.

Tab opens and closes it (the Q button of the camera, docs/hardware.md §2); the rear dial
(up / down) chooses a line, the front dial (left / right) changes its value. The shutter
closes it and shoots.
"""

from dataclasses import dataclass

from . import picture


@dataclass(frozen=True)
class Item:
    label: str
    attr: str  # in Settings
    values: tuple
    shown: tuple  # how each value is shown
    push: str = ""  # what the engine must be told: "picture", "config" or nothing


def _levels(values, black_and_white=False):
    return tuple("N&B" if black_and_white and v == -3 else ("0" if v == 0 else f"{v:+d}".replace("-", "−")) for v in values)


ITEMS = (
    Item("Netteté", "sharpness", tuple(picture.SHARPNESS), _levels(picture.SHARPNESS), "picture"),
    Item("Contraste", "contrast", tuple(picture.CONTRAST), _levels(picture.CONTRAST), "picture"),
    Item("Saturation", "saturation", tuple(picture.SATURATION), _levels(picture.SATURATION, True), "picture"),
    Item(
        "Réduction du bruit",
        "denoise",
        tuple(picture.DENOISE),
        ("Aucune", "Légère", "Rapide", "Haute qualité"),
        "config",
    ),
    Item("Taille JPEG", "jpeg_size", tuple(picture.JPEG_SIZES), ("L · 12 Mpx", "M · 6 Mpx", "S · 3 Mpx"), "config"),
    Item("Qualité JPEG", "jpeg_quality", picture.JPEG_QUALITIES, ("Normale", "Fine", "Extra fine")),
    Item("Format", "formats", (["jpeg", "dng"], ["jpeg"]), ("RAW + JPEG", "JPEG")),
    Item("RAW compressé", "dng_compression", (True, False), ("Oui · sans perte", "Non")),
    Item("Revue après la prise", "review_seconds", (0, 1, 2, 4), ("Coupée", "1 s", "2 s", "4 s")),
)


class QuickMenu:
    def __init__(self, settings):
        self.settings = settings
        self.index = 0

    def move(self, steps):
        self.index = (self.index + steps) % len(ITEMS)

    def change(self, steps):
        """Moves the chosen setting along its values (stopping at the ends); returns its item."""
        item = ITEMS[self.index]
        current = getattr(self.settings, item.attr)
        i = item.values.index(current) if current in item.values else 0
        setattr(self.settings, item.attr, item.values[max(0, min(len(item.values) - 1, i + steps))])
        return item

    def rows(self):
        """(label, value shown) for each line."""
        rows = []
        for item in ITEMS:
            current = getattr(self.settings, item.attr)
            shown = item.shown[item.values.index(current)] if current in item.values else "?"
            rows.append((item.label, shown))
        return tuple(rows)
