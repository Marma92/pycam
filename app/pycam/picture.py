"""Picture settings: what shapes the JPEG (and the viewfinder), never the RAW.

Sharpness, contrast and saturation are libcamera controls, applied by the ISP to the
viewfinder as to the JPEG: what you see is what you get. The denoise and the JPEG size
are engine settings for the stills ("configure").
"""

# Levels shown -2..+2 (saturation: -3 is black and white) -> libcamera values (1: neutral).
SHARPNESS = {-2: 0.0, -1: 0.5, 0: 1.0, 1: 1.5, 2: 2.0}
CONTRAST = {-2: 0.6, -1: 0.8, 0: 1.0, 1: 1.25, 2: 1.5}
SATURATION = {-3: 0.0, -2: 0.5, -1: 0.75, 0: 1.0, 1: 1.25, 2: 1.5}

# Denoise of the photos -> rpicam-apps modes. "hq" costs ~0.2 s of shutter lag in the
# switch strategy (ISP time, measured); in ZSL the engine uses "fast" instead.
DENOISE = {"off": "off", "light": "cdn_off", "fast": "cdn_fast", "hq": "cdn_hq"}

# JPEG sizes (the DNG is always the full sensor, 4056 x 3040).
JPEG_SIZES = {"L": None, "M": [2880, 2160], "S": [2028, 1520]}
JPEG_QUALITIES = (85, 93, 98)


def controls(settings):
    """libcamera controls, for set_controls."""
    return {
        "Sharpness": SHARPNESS[settings.sharpness],
        "Contrast": CONTRAST[settings.contrast],
        "Saturation": SATURATION[settings.saturation],
    }


def engine_config(settings):
    """Engine settings for the stills, for configure."""
    return {"still_denoise": DENOISE[settings.denoise], "still_size": JPEG_SIZES[settings.jpeg_size]}
