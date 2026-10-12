"""What a shared photo says, composed as SPP (Simple Photo Poster) does it: a title, the gear
read from the photo, a description, hashtags, in blocks separated by a lone dot (how
Instagram shows a paragraph break). Then cut, for each platform, where its limit falls,
counted its way (Bluesky in graphemes: an emoji weighs one).
"""

import datetime
import re
import unicodedata
from dataclasses import asdict, dataclass, fields

from ...playback import exif_text

BLOCK_SEPARATOR = "\n.\n"
ELLIPSIS = "…"
DETAIL_ICONS = (("camera", "📷"), ("lens", "👁️"), ("film", "🎞️"), ("date", "🗓️"))
TAG = re.compile(r"#(\w+)")
# Presets that are not a film: no "🎞️" line for them.
NOT_FILMS = ("Standard", "Base")
# Combining marks and format characters (variation selectors, zero-width joiners) do not
# start a grapheme: "🎞️" is one.
ZERO_WIDTH = {"Mn", "Me", "Cf"}


def hashtagify(tags):
    """Loose words into hashtags, the ones already there left alone."""
    return re.sub(r"#?(\w+)", r"#\1", tags)


def _terminated(text):
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


@dataclass
class Post:
    title: str = ""
    description: str = ""
    alt: str = ""  # the image described, for those who do not see it
    tags: str = ""
    camera: str = ""
    lens: str = ""
    film: str = ""
    date: str = ""

    @property
    def hashtags(self):
        return hashtagify(self.tags)

    @property
    def tag_words(self):
        """The hashtags without their #: Flickr's tags."""
        return TAG.findall(self.hashtags)

    @property
    def gear(self):
        return "\n".join(
            f"{icon} {_terminated(getattr(self, name))}" for name, icon in DETAIL_ICONS if getattr(self, name).strip()
        )

    @property
    def caption(self):
        blocks = [
            _terminated(self.title) if self.title.strip() else "",
            self.gear,
            _terminated(self.description) if self.description.strip() else "",
            self.hashtags.strip(),
        ]
        return BLOCK_SEPARATOR.join(block for block in blocks if block)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{f.name: str(data.get(f.name) or "") for f in fields(cls)})


def from_photo(path, tags=""):
    """A post pre-filled from the photo's EXIF (camera, lens, the preset's film, date) and
    the default hashtags."""
    post = Post(tags=tags)
    try:
        from PIL import Image

        with Image.open(path) as im:
            ifd0 = im.getexif()
            exif = ifd0.get_ifd(0x8769)
    except (ImportError, OSError):
        return post
    make = exif_text(ifd0.get(271) or exif.get(271, ""))
    model = exif_text(ifd0.get(272) or exif.get(272, ""))
    if model.lower() == "imx477":
        model = "HQ Camera"
    post.camera = f"{make} {model}".strip()
    post.lens = exif_text(exif.get(0xA434, ""))  # LensModel
    software = exif_text(ifd0.get(305, ""))  # "pycam, Portrait 400"
    if software.startswith("pycam, ") and software[7:] not in NOT_FILMS:
        post.film = software[7:]
    taken = exif_text(exif.get(36867) or ifd0.get(306, ""))
    try:
        post.date = datetime.datetime.strptime(taken, "%Y:%m:%d %H:%M:%S").date().isoformat()
    except ValueError:
        post.date = ""
    return post


def graphemes(text):
    return sum(1 for char in text if unicodedata.category(char) not in ZERO_WIDTH)


def split_text(text, limit, measure=len):
    """(kept, dropped): what a platform publishes of the text, and what it cuts. measure:
    the platform's ruler."""
    if limit is None or measure(text) <= limit:
        return text, ""
    budget = limit - measure(ELLIPSIS)
    cut = used = 0
    for index, char in enumerate(text):
        used += measure(char)
        if used > budget:
            break
        cut = index + 1
    return text[:cut].rstrip() + ELLIPSIS, text[cut:]
