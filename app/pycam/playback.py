"""Playback, the LECTURE button: the photos and videos on the card, one at a time
(docs/architecture.md §5).

The rear dial goes from one to the next, the front dial jumps ten; on a photo, LOUPE
magnifies (fit, 100 %, 200 %) and the dials then move the magnified area; on a video
(a timelapse, after its frames), LOUPE plays it, pauses it, plays it again; Q deletes, in
two presses. LECTURE, MENU or the shutter leave (the shutter still shoots).
"""

import datetime
import json
import os
import re
import subprocess

FOLDER = re.compile(r"^\d{3}PYCAM$")
PHOTO = re.compile(r"^PYC_\d{4}\.JPG$")
VIDEO = re.compile(r"^PY[TV]_\d{4}\.MP4$")  # timelapses (PYT) and videos (PYV)
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


def thumbs_dir():
    """The web interface's thumbnails (web.py)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "pycam", "thumbs")


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


def prune_caches(root):
    """The first frames and the thumbnails of shots gone or changed (deleted, offloaded to a
    cloud) removed from the caches: they would grow for ever. Returns how many. Slow on a
    full card: off the event loop."""
    posters, thumbs = set(), set()
    for path in list_photos(root, videos=True):
        folder = os.path.basename(os.path.dirname(path))
        for f in companions(path):
            try:
                stem, mtime = os.path.basename(f)[:-4], int(os.path.getmtime(f))
            except OSError:
                continue
            (posters if is_video(f) else thumbs).add(f"{stem}_{mtime}.jpg" if is_video(f) else f"{folder}_{stem}_{mtime}.jpg")
    return _prune(posters_dir(), posters) + _prune(thumbs_dir(), thumbs)


def _prune(folder, keep):
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if name not in keep:
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


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
    if name.startswith("PYT_") and stream.get("nb_read_packets"):  # a timelapse: its frames
        parts.append(f"{stream['nb_read_packets']} vues")
    return "   ".join(parts), (stream["width"], stream["height"]), duration


def exif_text(value):
    """An EXIF text as written: the engine writes UTF-8 in ASCII fields, which Pillow reads
    as Latin-1 ("CinÃ©ma 800T")."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).replace("\x00", "").strip()
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def extract_frame(video, position, out):
    """The video's image at `position` seconds as a JPEG photo at `out`, with when it was
    shot (the video's start, plus the position) in its EXIF; True if made. Slow (~1 s):
    off the event loop."""
    from PIL import Image

    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time", "-of", "json", video]
    try:
        info = json.loads(subprocess.run(command, check=True, timeout=10, capture_output=True, text=True).stdout)["format"]
        duration = float(info.get("duration", 0))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return False
    start = os.path.getmtime(video) - duration  # the file is ended when the recording stops
    created = info.get("tags", {}).get("creation_time", "")
    try:
        start = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass  # a video made before the date was written: its file's date
    taken = datetime.datetime.fromtimestamp(start + position)  # EXIF: local time
    png = out + ".part.png"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{position:.3f}", "-i", video,
               "-frames:v", "1", png]  # fmt: skip
    try:
        subprocess.run(command, check=True, timeout=60, capture_output=True)
        exif = Image.Exif()
        exif[271], exif[272], exif[305] = "Raspberry Pi", "imx477", "pycam"
        exif[306] = taken.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x8769] = {36867: exif[306], 36868: exif[306]}  # DateTimeOriginal, DateTimeDigitized
        part = out + ".part"
        with Image.open(png) as im:
            im.convert("RGB").save(part, "JPEG", quality=95, exif=exif)
        os.replace(part, out)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        for leftover in (png, out + ".part"):
            if os.path.exists(leftover):
                os.remove(leftover)
    return True


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
    software = exif_text(exif.get(305, ""))  # "pycam, Portrait 400"
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
