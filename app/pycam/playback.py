"""Playback, the LECTURE button: the photos and videos on the card, one at a time
(docs/architecture.md §5).

The rear dial goes from one to the next, the front dial jumps ten; on a photo, LOUPE
magnifies (fit, 100 %, 200 %) and the dials then move the magnified area; on a video
(a timelapse, after its frames), LOUPE plays it, pauses it, plays it again; Q deletes, in
two presses. LECTURE, MENU or the shutter leave (the shutter still shoots).
"""

import json
import os
import re
import subprocess

FOLDER = re.compile(r"^\d{3}PYCAM$")
PHOTO = re.compile(r"^PYC_\d{4}\.JPG$")
VIDEO = re.compile(r"^PYT_\d{4}\.MP4$")
# Magnifications: the photo fitted to the screen, then real pixels (100 %), then 200 %.
PIXEL_SCALES = (None, 1.0, 2.0)


def list_photos(root, videos=False):
    """The camera's JPEGs, oldest first (folder, then number). The base copies (PYB_) and
    the DNG go with them. videos: the timelapse videos too, each after its frames."""
    photos = []
    try:
        folders = sorted(f for f in os.listdir(root) if FOLDER.match(f))
    except OSError:
        return photos
    for folder in folders:
        path = os.path.join(root, folder)
        try:
            names = sorted(n for n in os.listdir(path) if PHOTO.match(n) or (videos and VIDEO.match(n)))
        except OSError:
            continue
        photos.extend(os.path.join(path, n) for n in names)
    return photos


def is_video(path):
    return bool(path) and path.upper().endswith(".MP4")


def companions(path):
    """The files of the same shot: the JPEG, its DNG, its base copy (a video: itself)."""
    if is_video(path):
        return [path]
    folder, name = os.path.split(path)
    stem = name[:-4]
    return [path, os.path.join(folder, stem + ".DNG"), os.path.join(folder, "PYB_" + stem[4:] + ".JPG")]


def posters_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "pycam", "posters")


def poster(video):
    """A JPEG of the video's first frame (cached), to show it before playing; None if
    ffmpeg cannot make one. Slow (~0.5 s the first time): off the event loop."""
    cache = os.path.join(posters_dir(), f"{os.path.basename(video)[:-4]}_{int(os.path.getmtime(video))}.jpg")
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        part = cache + ".part.jpg"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", video, "-frames:v", "1",
                   "-q:v", "3", part]  # fmt: skip
        try:
            subprocess.run(command, check=True, timeout=30, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            return None
        os.replace(part, cache)
    return cache


def describe_video(path):
    """(One line for the info bar, the video's size in pixels or None, its duration in
    seconds or 0.)"""
    name = os.path.basename(path)[:-4]
    command = ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,avg_frame_rate,nb_read_packets:format=duration", "-of", "json", path]  # fmt: skip
    try:
        data = json.loads(subprocess.run(command, check=True, timeout=10, capture_output=True, text=True).stdout)
        stream, duration = data["streams"][0], float(data["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return f"{name}   vidéo", None, 0
    num, _, den = stream.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    parts = [name, f"vidéo {duration:.0f} s", f"{stream['width']} × {stream['height']}", f"{fps:g} i/s"]
    if stream.get("nb_read_packets"):
        parts.append(f"{stream['nb_read_packets']} vues")
    return "   ".join(parts), (stream["width"], stream["height"]), duration


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
        self.photos = list_photos(root, videos=True)
        self.index = len(self.photos) - 1  # the last photo taken
        self.scale = 0  # index in PIXEL_SCALES
        self.center = (0.5, 0.5)

    @property
    def current(self):
        return self.photos[self.index] if self.photos else None

    @property
    def video(self):
        """The current item is a video."""
        return is_video(self.current)

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
