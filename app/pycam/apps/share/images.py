"""The picture each platform receives, as SPP (Simple Photo Poster) prepares it:
- Instagram: a 1440 × 1440 square, the photo centred on white (the white bands look);
- Bluesky: 2000 px on the long side, under its 1 MB limit;
- Flickr: the original, untouched (the archive).
Slow on a Pi 3B (~1 s for a 12 Mpx JPEG): in a thread.
"""

import os

INSTAGRAM_SIDE = 1440
BLUESKY_EDGE = 2000
BLUESKY_MAX_BYTES = 976 * 1024  # Bluesky refuses any blob over 1,000,000 bytes
QUALITY_STEPS = (95, 90, 85, 78, 70, 60)  # walked down until the JPEG fits


def _load(path, width, height):
    """The photo, decoded at the smallest size still covering width × height once fitted
    (the JPEG decoder shrinks by 2, 4 or 8 for almost nothing), upright."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        ratio = min(width / im.width, height / im.height)
        im.draft("RGB", (round(im.width * ratio), round(im.height * ratio)))
        return ImageOps.exif_transpose(im).convert("RGB")


def _fit(image, width, height):
    from PIL import Image

    ratio = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _save(image, target, max_bytes=None):
    """JPEG, giving up quality then size to fit under max_bytes."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    for _ in range(6):
        for quality in QUALITY_STEPS:
            image.save(part, "JPEG", quality=quality, subsampling=0, optimize=True)
            if max_bytes is None or os.path.getsize(part) <= max_bytes:
                os.replace(part, target)
                return target
        image = _fit(image, round(image.width * 0.8), round(image.height * 0.8))
    os.remove(part)
    raise ValueError(f"impossible de faire tenir {os.path.basename(target)} sous {max_bytes} octets")


def prepare(platform, path, target):
    """The file to send to `platform` for the photo `path`; written at `target` (.jpg) if
    it must be made."""
    from PIL import Image

    if platform == "flickr":
        return path
    if platform == "instagram":
        image = _fit(_load(path, INSTAGRAM_SIDE, INSTAGRAM_SIDE), INSTAGRAM_SIDE, INSTAGRAM_SIDE)
        canvas = Image.new("RGB", (INSTAGRAM_SIDE, INSTAGRAM_SIDE), (255, 255, 255))
        canvas.paste(image, ((INSTAGRAM_SIDE - image.width) // 2, (INSTAGRAM_SIDE - image.height) // 2))
        return _save(canvas, target)
    if platform == "bluesky":
        image = _load(path, BLUESKY_EDGE, BLUESKY_EDGE)
        if max(image.size) > BLUESKY_EDGE:
            image = _fit(image, BLUESKY_EDGE, BLUESKY_EDGE)
        return _save(image, target, BLUESKY_MAX_BYTES)
    raise ValueError(f"unknown platform {platform}")
