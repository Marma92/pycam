"""Playback, the LECTURE button: the photos on the card, one at a time (docs/architecture.md §5).

The rear dial goes from one photo to the next, the front dial jumps ten; LOUPE magnifies
(fit, 100 %, 200 %) and the dials then move the magnified area; Q deletes the photo, in
two presses. LECTURE, MENU or the shutter leave (the shutter still shoots).
"""

import os
import re

FOLDER = re.compile(r"^\d{3}PYCAM$")
PHOTO = re.compile(r"^PYC_\d{4}\.JPG$")
# Magnifications: the photo fitted to the screen, then real pixels (100 %), then 200 %.
PIXEL_SCALES = (None, 1.0, 2.0)


def list_photos(root):
    """The camera's JPEGs, oldest first (folder, then number). The base copies (PYB_) and
    the DNG go with them."""
    photos = []
    try:
        folders = sorted(f for f in os.listdir(root) if FOLDER.match(f))
    except OSError:
        return photos
    for folder in folders:
        path = os.path.join(root, folder)
        try:
            names = sorted(n for n in os.listdir(path) if PHOTO.match(n))
        except OSError:
            continue
        photos.extend(os.path.join(path, n) for n in names)
    return photos


def companions(jpeg):
    """The files of the same shot: the JPEG, its DNG, its base copy."""
    folder, name = os.path.split(jpeg)
    stem = name[:-4]
    return [jpeg, os.path.join(folder, stem + ".DNG"), os.path.join(folder, "PYB_" + stem[4:] + ".JPG")]


def describe(path):
    """(One line for the info bar: what the photo was shot at, from its EXIF; its size in
    pixels, or None if it cannot be read.)"""
    parts = [os.path.basename(path)[:-4]]
    try:
        from PIL import Image

        with Image.open(path) as im:
            size = im.size
            exif = im.getexif()
            details = exif.get_ifd(0x8769)  # the EXIF sub-IFD
    except (ImportError, OSError):
        return parts[0], None
    exposure, iso, f_number = details.get(33434), details.get(34855), details.get(33437)
    if exposure:
        exposure = float(exposure)
        parts.append(f"1/{round(1 / exposure)}" if exposure < 0.3 else f'{exposure:g}"')
    if iso:
        parts.append(f"ISO {iso}")
    if f_number:
        parts.append(f"f/{float(f_number):g}")
    software = exif.get(305, "")  # "pycam, Portrait 400"
    if software.startswith("pycam, "):
        parts.append(software[len("pycam, ") :])
    return "   ".join(parts), size


class Playback:
    def __init__(self, root):
        self.photos = list_photos(root)
        self.index = len(self.photos) - 1  # the last photo taken
        self.scale = 0  # index in PIXEL_SCALES
        self.center = (0.5, 0.5)

    @property
    def current(self):
        return self.photos[self.index] if self.photos else None

    @property
    def position(self):
        """"12/57": where we are in the card."""
        return f"{self.index + 1}/{len(self.photos)}" if self.photos else ""

    def step(self, steps):
        """To another photo (stopping at the first and the last one); False if none."""
        if not self.photos:
            return False
        index = max(0, min(len(self.photos) - 1, self.index + steps))
        if index == self.index:
            return False
        self.index = index
        self.scale, self.center = 0, (0.5, 0.5)
        return True

    def cycle_zoom(self):
        self.scale = (self.scale + 1) % len(PIXEL_SCALES)
        if not self.scale:
            self.center = (0.5, 0.5)

    def zoom(self, fit_scale):
        """The magnification against the fitted photo, for a fit scale (screen pixels per
        photo pixel): 1 when fitted."""
        pixel_scale = PIXEL_SCALES[self.scale]
        return 1.0 if pixel_scale is None else max(1.0, pixel_scale / fit_scale)

    def pan(self, dx, dy, zoom):
        """One step = half of what is shown; the centre stays where it can be shown."""
        half = 0.5 / zoom
        x, y = self.center
        self.center = (min(1 - half, max(half, x + dx * half)), min(1 - half, max(half, y + dy * half)))

    def delete_current(self):
        """Deletes the shot (JPEG, DNG, base copy); returns the files removed."""
        removed = []
        for path in companions(self.current):
            try:
                os.remove(path)
                removed.append(path)
            except FileNotFoundError:
                pass
        del self.photos[self.index]
        self.index = min(self.index, len(self.photos) - 1)
        self.scale, self.center = 0, (0.5, 0.5)
        return removed
