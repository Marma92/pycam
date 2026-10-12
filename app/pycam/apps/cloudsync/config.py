"""Cloud sync's account: the service, and how to reach it (docs/architecture.md §5).

Kept in ~/.config/pycam/cloud.json, readable by its owner only: it holds the secrets
(an app password, refresh tokens). They never leave the camera except to their service:
the web interface only says whether they are there.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

PROVIDERS = ("owncloud", "dropbox", "gdrive")
PROVIDER_LABELS = {"owncloud": "ownCloud / Nextcloud", "dropbox": "Dropbox", "gdrive": "Google Drive"}


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pycam", "cloud.json")


@dataclass
class CloudConfig:
    provider: str = ""  # PROVIDERS; empty: none
    account: str = ""  # who, as the service says (shown)
    # ownCloud / Nextcloud: the server, the user, an app password (WebDAV).
    url: str = ""
    user: str = ""
    password: str = field(default="", repr=False)
    # Dropbox and Google Drive: the user's own app (its key, or client), and the token
    # that lasts.
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    folder: str = "pycam"  # where the files go (Dropbox: in the app's folder, if it has one)

    @property
    def ready(self):
        if self.provider == "owncloud":
            return bool(self.url and self.user and self.password)
        if self.provider in ("dropbox", "gdrive"):
            return bool(self.client_id and self.refresh_token)
        return False

    @property
    def identity(self):
        """What the uploads are recorded against: another account sends everything again."""
        return f"{self.provider}:{self.account or self.user}:{self.url}:{self.folder}"

    def public(self):
        """For the web interface: everything but the secrets."""
        return {
            "provider": self.provider,
            "account": self.account,
            "url": self.url,
            "user": self.user,
            "client_id": self.client_id,
            "folder": self.folder,
            "ready": self.ready,
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
            log.warning("unreadable cloud settings %s (%s)", path, e)
            return cls()
        config = cls()
        for name in asdict(config):
            if isinstance(data.get(name), str):
                setattr(config, name, data[name])
        if config.provider not in PROVIDERS:
            config.provider = ""
        return config

    def save(self, path=None):
        path = path or default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
