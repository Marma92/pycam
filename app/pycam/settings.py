"""Settings that survive a restart, in ~/.config/pycam/settings.json.

The application is the source of truth: it pushes everything to the engine whenever it
(re)connects (docs/architecture.md §6).
"""

import json
import logging
import os
from dataclasses import dataclass, field, fields

from .exposure import Exposure

log = logging.getLogger(__name__)

DISPLAY_MODES = ("detailed", "simple", "clean")  # visée détaillée, simple, nature
STRATEGIES = ("switch", "zsl")


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pycam", "settings.json")


@dataclass
class Settings:
    exposure: Exposure = field(default_factory=Exposure)
    formats: list = field(default_factory=lambda: ["jpeg", "dng"])
    jpeg_quality: int = 93
    strategy: str = "switch"
    display: str = "detailed"
    grid: bool = True
    dcim: str = "~/DCIM"
    folder_number: int = 100
    next_number: int = 1

    def to_dict(self):
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["exposure"] = self.exposure.to_dict()
        return data


def load_settings(path):
    """The saved settings, or the defaults (with a warning if the file is unreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as e:
        log.warning("unreadable settings %s (%s): using defaults", path, e)
        return Settings()

    settings = Settings()
    if not isinstance(data, dict):
        return settings
    defaults = Settings()
    for f in fields(Settings):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "exposure":
            if isinstance(value, dict):
                settings.exposure = Exposure.from_dict(value)
        elif type(value) is type(getattr(defaults, f.name)):
            setattr(settings, f.name, value)
    if settings.display not in DISPLAY_MODES:
        settings.display = defaults.display
    if settings.strategy not in STRATEGIES:
        settings.strategy = defaults.strategy
    if not set(settings.formats) <= {"jpeg", "dng"} or not settings.formats:
        settings.formats = defaults.formats
    return settings


def save_settings(settings, path):
    """Atomic: a power cut leaves either the old or the new file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings.to_dict(), f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
