"""Picture settings: what shapes the JPEG (and the viewfinder), never the RAW.

The ISP keeps a fixed rendering, the "pycam base" (docs/presets.md §3): only the
sharpness is an ISP control. The contrast and the saturation are compiled into the look
with the preset (looks.py). The denoise, the JPEG size and the tuning (with or without
the lens shading correction) are engine settings ("configure").
"""

from .looks import CONTRAST, SATURATION  # noqa: F401 (levels, for the menus and the settings)

# Levels shown -2..+2 -> libcamera values (1: neutral).
SHARPNESS = {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.5, 2: 2.0}

# Denoise of the photos -> rpicam-apps modes. "hq" costs ~0.2 s of shutter lag in the
# switch strategy (ISP time, measured); in ZSL the engine uses "fast" instead.
DENOISE = {"off": "off", "light": "cdn_off", "fast": "cdn_fast", "hq": "cdn_hq"}

# JPEG sizes (the DNG is always the full sensor, 4056 x 3040).
JPEG_SIZES = {"L": None, "M": [2880, 2160], "S": [2028, 1520]}
JPEG_QUALITIES = (85, 93, 98)

# engine/tuning: the base, with or without the correction of the lens shading.
TUNINGS = {False: "pycam-imx477", True: "pycam-imx477-alsc"}


def controls(settings):
    """libcamera controls, for set_controls. Contrast and saturation stay neutral."""
    return {"Sharpness": SHARPNESS[settings.sharpness], "Contrast": 1.0, "Saturation": 1.0}


def engine_config(settings):
    """Engine settings for the stills and the ISP, for configure."""
    return {
        "still_denoise": DENOISE[settings.denoise],
        "still_size": JPEG_SIZES[settings.jpeg_size],
        "tuning": TUNINGS[settings.lens_shading],
    }
