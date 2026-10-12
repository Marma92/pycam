"""Partage's accounts and defaults, in ~/.config/pycam/share.json, readable by its owner
only: it holds the secrets (an app password, OAuth tokens, an Instagram session). They
never leave the camera except to their platform: the web interface only says whether they
are there.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

PLATFORMS = ("instagram", "flickr", "bluesky")
LABELS = {"instagram": "Instagram", "flickr": "Flickr", "bluesky": "Bluesky"}


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pycam", "share.json")


def data_dir():
    """The queue, Instagram's device, the pictures prepared."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "pycam", "share")


@dataclass
class ShareConfig:
    # Bluesky: the handle and an app password (never the account's).
    bluesky_handle: str = ""
    bluesky_password: str = field(default="", repr=False)
    # Flickr: the user's own API key, and the OAuth 1.0a token it was given.
    flickr_key: str = ""
    flickr_secret: str = field(default="", repr=False)
    flickr_token: str = field(default="", repr=False)
    flickr_token_secret: str = field(default="", repr=False)
    flickr_user: str = ""  # username, for show
    flickr_nsid: str = ""  # for the photo's address
    # Instagram (its private API, as SPP): a username, and a browser's sessionid (past the
    # verification Instagram asks of a new device) or the password.
    instagram_user: str = ""
    instagram_password: str = field(default="", repr=False)
    instagram_sessionid: str = field(default="", repr=False)
    # Defaults: the hashtags added to each post, its language, the platforms ticked.
    tags: str = "#pycam"
    lang: str = "fr"
    selected: list = field(default_factory=list)

    def ready(self, platform):
        if platform == "bluesky":
            return bool(self.bluesky_handle and self.bluesky_password)
        if platform == "flickr":
            return bool(self.flickr_key and self.flickr_secret and self.flickr_token and self.flickr_token_secret)
        if platform == "instagram":
            return bool(self.instagram_user and (self.instagram_sessionid or self.instagram_password))
        return False

    @property
    def configured(self):
        return [p for p in PLATFORMS if self.ready(p)]

    def account(self, platform):
        return {"bluesky": self.bluesky_handle, "flickr": self.flickr_user, "instagram": self.instagram_user}[platform]

    def forget(self, platform):
        names = {
            "bluesky": ("bluesky_handle", "bluesky_password"),
            "flickr": ("flickr_key", "flickr_secret", "flickr_token", "flickr_token_secret", "flickr_user", "flickr_nsid"),
            "instagram": ("instagram_user", "instagram_password", "instagram_sessionid"),
        }[platform]
        for name in names:
            setattr(self, name, "")

    def public(self):
        """For the web interface: everything but the secrets."""
        return {
            "accounts": {p: {"ready": self.ready(p), "account": self.account(p)} for p in PLATFORMS},
            "flickr_key": self.flickr_key,
            "instagram_sessionid": bool(self.instagram_sessionid),
            "tags": self.tags,
            "lang": self.lang,
            "selected": self.selected,
        }

    @classmethod
    def load(cls, path=None):
        path = path or default_path()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("unreadable share settings %s (%s)", path, e)
            return cls()
        config = cls()
        for name, value in asdict(config).items():
            if isinstance(data.get(name), type(value)):
                setattr(config, name, data[name])
        config.selected = [p for p in config.selected if p in PLATFORMS]
        return config

    def save(self, path=None):
        path = path or default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
