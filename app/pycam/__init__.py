"""pycam : l'application de l'appareil photo (docs/architecture.md §5)."""

import os


def _version():
    """The VERSION file at the root of the repository, or of the release."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "VERSION"), encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return "0.0.0"


__version__ = _version()
