"""Partage: a photo shared on Instagram, Flickr and Bluesky from playback (app.py)."""


def __getattr__(name):  # app.py needs aiohttp; the caption alone does not (its tests)
    if name == "Share":
        from .app import Share

        return Share
    raise AttributeError(name)
