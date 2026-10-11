"""Lens profiles: the HQ Camera lenses are manual, the camera cannot know them.

A profile gives what goes into the EXIF of the photos (docs/features.md, M1): name,
make, focal length, widest aperture. The aperture actually set on the ring is told by the
user (or stays unknown).
"""

import math

# IMX477: 4056 x 3040 pixels of 1.55 µm, 6.29 x 4.71 mm, diagonal 7.86 mm; 24x36: 43.27 mm.
CROP_FACTOR = 43.27 / 7.857

# The official Raspberry Pi lenses for the HQ Camera; edit "lenses" in settings.json for others.
DEFAULT_LENSES = [
    {"name": "Raspberry Pi 6 mm", "make": "Raspberry Pi", "focal_length": 6.0, "max_aperture": 1.2},
    {"name": "Raspberry Pi 16 mm", "make": "Raspberry Pi", "focal_length": 16.0, "max_aperture": 1.4},
]

# Full stops, as engraved on lens rings.
APERTURES = (1.0, 1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0)
SMALLEST_APERTURE = 16.0  # unless the profile says otherwise ("min_aperture")


def clean_lens(data):
    """A valid profile from a settings entry, or None."""
    if not isinstance(data, dict):
        return None
    name, focal, widest = data.get("name"), data.get("focal_length"), data.get("max_aperture")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(focal, (int, float)) or focal <= 0 or not isinstance(widest, (int, float)) or widest <= 0:
        return None
    lens = {"name": name.strip(), "focal_length": float(focal), "max_aperture": float(widest)}
    if isinstance(data.get("make"), str) and data["make"].strip():
        lens["make"] = data["make"].strip()
    smallest = data.get("min_aperture")
    if isinstance(smallest, (int, float)) and smallest > widest:
        lens["min_aperture"] = float(smallest)
    return lens


def aperture_stops(lens):
    """The apertures of the lens, widest first: its own widest, then the full stops."""
    widest = lens["max_aperture"]
    smallest = lens.get("min_aperture", SMALLEST_APERTURE)
    return [widest] + [a for a in APERTURES if widest * 1.05 < a <= smallest * 1.001]


def format_aperture(f_number):
    return f"f/{f_number:g}"


def format_lens(lens, aperture):
    if not lens:
        return ""
    text = f"{lens['focal_length']:g} mm"
    if aperture:
        text += " " + format_aperture(aperture)
    return text


def _rational(value):
    return f"{round(value * 100)}/100"


def lens_exif(lens, aperture):
    """EXIF tags for the engine's capture command ("IFD.Tag=value", rpicam-apps syntax)."""
    if not lens:
        return []
    focal, widest = lens["focal_length"], lens["max_aperture"]
    tags = [
        f"EXIF.LensModel={lens['name']}",
        f"EXIF.FocalLength={_rational(focal)}",
        f"EXIF.FocalLengthIn35mmFilm={round(focal * CROP_FACTOR)}",
        # APEX aperture value: 2 log2(N).
        f"EXIF.MaxApertureValue={_rational(2 * math.log2(widest))}",
        f"EXIF.LensSpecification={_rational(focal)},{_rational(focal)},{_rational(widest)},{_rational(widest)}",
    ]
    if lens.get("make"):
        tags.append(f"EXIF.LensMake={lens['make']}")
    if aperture:
        tags.append(f"EXIF.FNumber={_rational(aperture)}")
    return tags
