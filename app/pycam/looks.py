"""Presets: what the JPEGs look like (docs/presets.md).

A preset is a folder holding a `preset.toml`, and maybe a LUT (`.cube`, or a Hald CLUT
`.png`). The application compiles it, with the picture settings (contrast, saturation),
into one 3D LUT on the "pycam base"; the engine applies that LUT, the grain and the
vignetting to each JPEG ("look" command, docs/protocol.md).

Everything here is plain Python, so that the same code runs on the camera and on a
computer (tools/pycam_preset.py): a preset gives the same result on both.
"""

import bisect
import colorsys
import hashlib
import json
import logging
import os
import struct
import tomllib
from dataclasses import dataclass

log = logging.getLogger(__name__)

BASE = "pycam-1"  # the ISP rendering presets are written for (engine/tuning)
FORMAT = 1  # of preset.toml
LUT_SIZE = 33
COMPILER = 5  # bump when compile() changes: the cached LUTs are then remade
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
DEFAULT = "standard"

# Picture settings, levels as in the menus -> look adjustments.
CONTRAST = {-2: -0.7, -1: -0.35, 0: 0.0, 1: 0.35, 2: 0.7}  # strength of the S curve
SATURATION = {-3: 0.0, -2: 0.5, -1: 0.75, 0: 1.0, 1: 1.25, 2: 1.5}  # -3: black and white

LOOK_KEYS = {
    "lut",
    "strength",
    "monochrome",
    "mixer",
    "tone_curve",
    "red_curve",
    "green_curve",
    "blue_curve",
    "saturation",
    "colours",
    "split_toning",
}
# Colour ranges for look.colours, as in the HSL panel of photo editors: their hue
# (degrees). A colour between two of them takes a mix of both adjustments.
COLOUR_RANGES = {
    "red": 0,
    "orange": 30,
    "yellow": 60,
    "green": 120,
    "aqua": 180,
    "blue": 225,
    "purple": 270,
    "magenta": 315,
}


class PresetError(ValueError):
    pass


def user_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pycam", "presets")


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "pycam", "looks")


@dataclass(frozen=True)
class Preset:
    id: str  # its folder's name
    name: str
    short: str  # for the info bar
    folder: str
    description: str = ""
    base: str = BASE
    exposure_bias: float = 0.0  # stops, added to the compensation (protects the highlights)
    white_balance: int | None = None  # Kelvin, the film's own (balance "Pellicule"); None: auto
    look: tuple = ()  # the [look] table, as sorted (key, value) pairs
    grain: float = 0.0  # strength, 0..1
    grain_size: float = 1.0  # in pixels of the full sensor image: the finer the grain, the smaller
    grain_colour: float = 0.0  # chroma grain against the luma grain (colour films: three dye layers)
    grain_shadows: float = 0.0  # 0: strongest in the mid-tones (slides), 1: in the shadows (negatives)
    grain_clumps: float = 0.0  # share of coarser clumps
    # Halation: the red glow of films without an anti-halation layer (CineStill).
    halation: float = 0.0  # 0..1
    halation_radius: float = 24.0  # spread, in pixels of the full sensor image
    halation_threshold: float = 0.8  # base level (0..1) from which light spreads
    halation_colour: tuple = (1.0, 0.25, 0.05)
    vignette: float = 0.0
    digest: str = ""  # of the files that make the preset: the compiled LUT's cache key

    @property
    def look_table(self):
        return dict(self.look)


def _number(table, key, default, low, high, where):
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise PresetError(f"{where}{key} : un nombre entre {low} et {high}")
    return float(value)


def _curve(points, where):
    if (
        not isinstance(points, list)
        or len(points) < 2
        or not all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p) for p in points)
    ):
        raise PresetError(f"{where} : une liste de points [x, y], au moins deux")
    xs = [p[0] for p in points]
    if not all(0 <= v <= 1 for p in points for v in p) or any(b <= a for a, b in zip(xs, xs[1:])):
        raise PresetError(f"{where} : des valeurs entre 0 et 1, et des x croissants")
    return [[float(x), float(y)] for x, y in points]


def load_preset(folder):
    """The preset in `folder`; raises PresetError if it cannot be used."""
    preset_id = os.path.basename(os.path.normpath(folder))
    path = os.path.join(folder, "preset.toml")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PresetError(f"{path} : {e}") from e
    if data.get("format") != FORMAT:
        raise PresetError(f"{path} : format = {FORMAT} attendu")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PresetError(f"{path} : il faut un nom (name)")
    short = data.get("short", name[:5])
    base = data.get("base", BASE)
    if base != BASE:
        log.warning("%s: written for the base %s, the camera has %s", path, base, BASE)

    shooting = data.get("shooting", {})
    look = dict(data.get("look", {}))
    grain = data.get("grain", {})
    halation = data.get("halation", {})
    vignette = data.get("vignette", {})
    unknown = set(look) - LOOK_KEYS
    if unknown:
        log.warning("%s: unknown keys in [look]: %s", path, ", ".join(sorted(unknown)))

    # Check everything now: a broken preset is refused when loaded, not when shooting.
    hasher = hashlib.sha256(raw)
    _number(look, "strength", 1.0, 0, 1, "look.")
    _number(look, "saturation", 1.0, 0, 3, "look.")
    for key in ("tone_curve", "red_curve", "green_curve", "blue_curve"):
        if key in look:
            look[key] = _curve(look[key], f"look.{key}")
    if "mixer" in look:
        mixer = look["mixer"]
        if not (isinstance(mixer, list) and len(mixer) == 3 and all(isinstance(v, (int, float)) for v in mixer) and sum(mixer) > 0):
            raise PresetError("look.mixer : trois poids [rouge, vert, bleu], de somme positive")
    colours = look.get("colours", {})
    if not isinstance(colours, dict) or set(colours) - set(COLOUR_RANGES):
        raise PresetError("look.colours : des plages parmi " + ", ".join(COLOUR_RANGES))
    for key, value in colours.items():
        if not (
            isinstance(value, list)
            and len(value) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
            and -60 <= value[0] <= 60
            and 0 <= value[1] <= 3
            and -1 <= value[2] <= 1
        ):
            raise PresetError(f"look.colours.{key} : [teinte -60..60 degrés, saturation 0..3, luminosité -1..1]")
    toning = look.get("split_toning", {})
    for key in ("shadows", "highlights"):
        if key in toning:
            value = toning[key]
            if not (isinstance(value, list) and len(value) == 2 and 0 <= value[0] <= 360 and 0 <= value[1] <= 1):
                raise PresetError(f"look.split_toning.{key} : [teinte en degrés, intensité 0-1]")
    if "lut" in look:
        lut_path = os.path.join(folder, look["lut"])
        read_lut_file(lut_path)  # raises PresetError
        with open(lut_path, "rb") as f:
            hasher.update(f.read())
        look["lut"] = lut_path

    return Preset(
        id=preset_id,
        name=name.strip(),
        short=str(short)[:6],
        folder=folder,
        description=str(data.get("description", "")),
        base=base,
        exposure_bias=_number(shooting, "exposure_bias", 0.0, -2, 2, "shooting."),
        white_balance=_white_balance(shooting.get("white_balance", "auto")),
        look=tuple(sorted((k, _freeze(v)) for k, v in look.items())),
        grain=_number(grain, "amount", 0.0, 0, 1, "grain."),
        grain_size=_number(grain, "size", 1.0, 0.5, 8, "grain."),
        grain_colour=_number(grain, "colour", 0.0, 0, 1, "grain."),
        grain_shadows=_number(grain, "shadows", 0.0, 0, 1, "grain."),
        grain_clumps=_number(grain, "clumps", 0.0, 0, 1, "grain."),
        halation=_number(halation, "amount", 0.0, 0, 1, "halation."),
        halation_radius=_number(halation, "radius", 24.0, 2, 200, "halation."),
        halation_threshold=_number(halation, "threshold", 0.8, 0, 0.99, "halation."),
        halation_colour=tuple(
            round(c, 4) for c in colorsys.hsv_to_rgb(_number(halation, "hue", 10.0, 0, 360, "halation.") / 360, 0.95, 1)
        ),
        vignette=_number(vignette, "amount", 0.0, 0, 1, "vignette."),
        digest=hasher.hexdigest()[:16],
    )


def _white_balance(value):
    if value == "auto":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 10000:
        raise PresetError('shooting.white_balance : des Kelvin entre 2000 et 10000, ou "auto"')
    return value


def _freeze(value):
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


def engine_look(preset, grain=True):
    """The engine "look" parameters of a preset (docs/protocol.md), but the LUT and the
    preview (compiled_look). grain: False to leave the film grain out."""
    params = {
        "name": preset.name,
        "vignette": preset.vignette,
        "grain": preset.grain if grain else 0.0,
        "grain_size": preset.grain_size,
        "grain_colour": preset.grain_colour,
        "grain_shadows": preset.grain_shadows,
        "grain_clumps": preset.grain_clumps,
    }
    if preset.halation > 0:
        params.update(
            halation=preset.halation,
            halation_radius=preset.halation_radius,
            halation_threshold=preset.halation_threshold,
            halation_colour=list(preset.halation_colour),
        )
    return params


def grain_label(preset):
    """How fine the preset's grain is, in words: its visibility grows with the strength
    and, more slowly, with the size."""
    index = preset.grain * preset.grain_size**0.5 * 100
    if index <= 0:
        return "aucun"
    return "très fin" if index < 8 else "fin" if index < 18 else "moyen" if index < 35 else "marqué"


def find_presets(folders=None):
    """{id: Preset}: the built-in ones first, then the user's (which may replace them)."""
    presets = {}
    for root in folders or (BUILTIN_DIR, user_dir()):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            folder = os.path.join(root, name)
            if not os.path.isfile(os.path.join(folder, "preset.toml")):
                continue
            try:
                presets[name] = load_preset(folder)
            except PresetError as e:
                log.warning("preset ignored: %s", e)
    order = [DEFAULT, "base"]
    return dict(sorted(presets.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 2, kv[1].name.lower())))


_registry = {}


def registry():
    """The presets found by the last refresh() (the menus read it at each redraw)."""
    if not _registry:
        refresh()
    return _registry


def refresh():
    """Looks for presets again: one copied to the camera shows up in the next menu."""
    _registry.clear()
    _registry.update(find_presets())
    return _registry


# --- LUT files


def read_lut_file(path):
    """(size, table) of a .cube or Hald CLUT .png file; table: size^3 RGB triplets 0..1,
    red varying fastest."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".cube":
            return read_cube(path)
        if ext == ".png":
            return read_hald(path)
    except OSError as e:
        raise PresetError(f"{path} : {e}") from e
    raise PresetError(f"{path} : LUT en .cube ou image Hald en .png attendue")


def read_cube(path):
    size, rows, low, high = None, [], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            words = line.split("#", 1)[0].split()
            if not words:
                continue
            key = words[0].upper()
            if key == "LUT_3D_SIZE":
                size = int(words[1])
            elif key == "DOMAIN_MIN":
                low = [float(v) for v in words[1:4]]
            elif key == "DOMAIN_MAX":
                high = [float(v) for v in words[1:4]]
            elif key in ("TITLE", "LUT_1D_SIZE", "LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"):
                if key == "LUT_1D_SIZE":
                    raise PresetError(f"{path} : LUT 1D non gérée, il faut une LUT 3D")
            else:
                try:
                    rows.append(tuple(float(v) for v in words[:3]))
                except ValueError as e:
                    raise PresetError(f"{path} : ligne illisible « {line.strip()} »") from e
    if not size or len(rows) != size**3 or not 2 <= size <= 129:
        raise PresetError(f"{path} : LUT_3D_SIZE absent, ou nombre de lignes faux")
    if low != [0.0, 0.0, 0.0] or high != [1.0, 1.0, 1.0]:
        rows = [tuple((v - lo) / (hi - lo) for v, lo, hi in zip(row, low, high)) for row in rows]
    return size, rows


def read_hald(path):
    try:
        from PIL import Image
    except ImportError as e:
        raise PresetError("Pillow est nécessaire pour les images Hald") from e
    with Image.open(path) as im:
        im = im.convert("RGB")
        width, height = im.size
        level = round(width ** (1 / 3))
        if width != height or level**3 != width:
            raise PresetError(f"{path} : image Hald carrée de côté niveau³ attendue (512 pour le niveau 8)")
        pixels = list(im.getdata())
    size = level * level
    return size, [(r / 255, g / 255, b / 255) for r, g, b in pixels]


def hald_identity(level=8):
    """The identity Hald CLUT image, to edit in any photo editor (needs Pillow)."""
    from PIL import Image

    size, side = level * level, level**3
    im = Image.new("RGB", (side, side))
    scale = 255 / (size - 1)
    im.putdata(
        [(round(r * scale), round(g * scale), round(b * scale)) for b in range(size) for g in range(size) for r in range(size)]
    )
    return im


def write_cube(path, size, table, title="pycam"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\n')
        for r, g, b in table:
            f.write(f"{r:.6f} {g:.6f} {b:.6f}\n")


def write_engine_lut(path, size, table):
    """The engine's format (docs/protocol.md, "look"), written atomically."""
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(b"PYCAMLUT" + struct.pack("<II", 1, size))
        f.write(
            struct.pack(
                f"<{len(table) * 3}H",
                *(min(65535, max(0, round(v * 65535))) for rgb in table for v in rgb),
            )
        )
    os.replace(tmp, path)


def _sample(size, table, r, g, b):
    """Trilinear interpolation in a LUT table, inputs 0..1."""
    n = size - 1
    fr, fg, fb = (min(max(v, 0.0), 1.0) * n for v in (r, g, b))
    r0, g0, b0 = (min(int(v), n - 1) for v in (fr, fg, fb))
    tr, tg, tb = fr - r0, fg - g0, fb - b0
    out = [0.0, 0.0, 0.0]
    for db, wb in ((0, 1 - tb), (1, tb)):
        for dg, wg in ((0, 1 - tg), (1, tg)):
            w = wb * wg
            if w == 0:
                continue
            base = ((b0 + db) * size + g0 + dg) * size + r0
            c0, c1 = table[base], table[base + 1]
            for k in range(3):
                out[k] += w * (c0[k] + (c1[k] - c0[k]) * tr)
    return out


# --- Curves


def curve_table(points, samples=1024):
    """A smooth curve through the points (monotone cubic: no overshoot), as a table."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if xs[0] > 0:
        xs, ys = [0.0] + xs, [ys[0]] + ys
    if xs[-1] < 1:
        xs, ys = xs + [1.0], ys + [ys[-1]]
    n = len(xs)
    slopes = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    tangents = [slopes[0]] + [0.0 if slopes[i - 1] * slopes[i] <= 0 else (slopes[i - 1] + slopes[i]) / 2 for i in range(1, n - 1)] + [slopes[-1]]
    for i, s in enumerate(slopes):  # Fritsch-Carlson
        if s == 0:
            tangents[i] = tangents[i + 1] = 0.0
            continue
        a, b = tangents[i] / s, tangents[i + 1] / s
        if a * a + b * b > 9:
            t = 3 / (a * a + b * b) ** 0.5
            tangents[i], tangents[i + 1] = t * a * s, t * b * s
    table = []
    for k in range(samples):
        x = k / (samples - 1)
        i = min(max(bisect.bisect_right(xs, x) - 1, 0), n - 2)
        h = xs[i + 1] - xs[i]
        t = (x - xs[i]) / h
        h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
        y = h00 * ys[i] + h10 * h * tangents[i] + h01 * ys[i + 1] + h11 * h * tangents[i + 1]
        table.append(min(max(y, 0.0), 1.0))
    return table


def _lookup(table, x):
    pos = min(max(x, 0.0), 1.0) * (len(table) - 1)
    i = min(int(pos), len(table) - 2)
    return table[i] + (table[i + 1] - table[i]) * (pos - i)


# --- Compilation


def _colour_adjustment(ranges, hue):
    """(hue shift, saturation factor, lightness) for a hue in degrees: a linear mix of the
    two ranges around it. ranges: [(range hue, (shift, saturation, lightness))], sorted."""
    for i, (h0, a0) in enumerate(ranges):
        h1, a1 = ranges[(i + 1) % len(ranges)]
        span = (h1 - h0) % 360 or 360
        offset = (hue - h0) % 360
        if offset <= span:
            t = offset / span
            return tuple(x0 + (x1 - x0) * t for x0, x1 in zip(a0, a1))
    return 0.0, 1.0, 0.0


_preset_tables = {}  # (digest, size) -> table: changing the contrast or the saturation reuses it


def compile_look(preset, contrast=0, saturation=0, size=LUT_SIZE):
    """The preset and the picture settings as one LUT: size^3 RGB triplets 0..1 on the base,
    red varying fastest.

    In two steps: the preset (slow, up to ~3 s on the Pi: kept in memory), then the
    picture settings on its result (fast: a dial turned in the menu applies quickly)."""
    key = (preset.digest, size)
    table = _preset_tables.get(key)
    if table is None:
        table = _preset_table(preset, size)
        if len(_preset_tables) >= 4:
            _preset_tables.pop(next(iter(_preset_tables)))
        _preset_tables[key] = table
    return _adjust(table, contrast, saturation)


def _preset_table(preset, size):
    """Order, as in a film camera: the preset's LUT, the black and white mix (the emulsion's
    sensitivity), the tone curves (its characteristic curve), the colour ranges and the
    saturation (its dyes), the toning; then the strength (mix with the base)."""
    look = preset.look_table
    user_lut = read_lut_file(look["lut"]) if "lut" in look else None
    strength = look.get("strength", 1.0)
    mono = bool(look.get("monochrome", False))
    mixer = look.get("mixer", (0.299, 0.587, 0.114))
    total = sum(mixer)
    mixer = [m / total for m in mixer]
    tone = curve_table(look["tone_curve"]) if "tone_curve" in look else None
    channel_curves = [curve_table(look[k]) if k in look else None for k in ("red_curve", "green_curve", "blue_curve")]
    look_saturation = look.get("saturation", 1.0)
    colour_table = dict(look.get("colours", ()))
    colour_ranges = sorted((hue, tuple(colour_table.get(name, (0, 1, 0)))) for name, hue in COLOUR_RANGES.items())
    toning = dict(look.get("split_toning", ()))
    tints = []
    for key, weight in (("shadows", lambda y: (1 - y) ** 2), ("highlights", lambda y: y**2)):
        if key in toning:
            hue, amount = toning[key]
            rgb = colorsys.hsv_to_rgb(hue / 360, 1, 1)
            luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
            tints.append(([c - luma for c in rgb], amount, weight))

    step = 1 / (size - 1)
    table = []
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                r0, g0, b0 = ri * step, gi * step, bi * step
                r, g, b = _sample(*user_lut, r0, g0, b0) if user_lut else (r0, g0, b0)
                if mono:
                    r = g = b = mixer[0] * r + mixer[1] * g + mixer[2] * b
                if tone:
                    r, g, b = _lookup(tone, r), _lookup(tone, g), _lookup(tone, b)
                if channel_curves[0]:
                    r = _lookup(channel_curves[0], r)
                if channel_curves[1]:
                    g = _lookup(channel_curves[1], g)
                if channel_curves[2]:
                    b = _lookup(channel_curves[2], b)
                if colour_table and not mono:
                    h, s, v = colorsys.rgb_to_hsv(*(min(max(c, 0.0), 1.0) for c in (r, g, b)))
                    if s > 0:
                        shift, k_sat, light = _colour_adjustment(colour_ranges, h * 360)
                        # The greys stay put: the changes grow with the saturation.
                        v = min(1.0, v * (1 + light * s))
                        s = min(1.0, s * k_sat)
                        r, g, b = colorsys.hsv_to_rgb(((h * 360 + shift * min(1.0, 4 * s)) % 360) / 360, s, v)
                if look_saturation != 1.0 and not mono:
                    y = 0.299 * r + 0.587 * g + 0.114 * b
                    r, g, b = y + look_saturation * (r - y), y + look_saturation * (g - y), y + look_saturation * (b - y)
                for chroma, amount, weight in tints:
                    w = amount * weight(0.299 * r + 0.587 * g + 0.114 * b)
                    r, g, b = r + w * chroma[0], g + w * chroma[1], b + w * chroma[2]
                if strength != 1.0:
                    r, g, b = r0 + strength * (r - r0), g0 + strength * (g - g0), b0 + strength * (b - b0)
                table.append((min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0), min(max(b, 0.0), 1.0)))
    return table


def _adjust(table, contrast, saturation):
    """The picture settings of the menu, on the preset's result: an S curve for the
    contrast, then the saturation around the luma (-3: black and white)."""
    k_contrast = CONTRAST[contrast]
    k_saturation = SATURATION[saturation]
    if not k_contrast and k_saturation == 1.0:
        return table
    out = []
    for r, g, b in table:
        if k_contrast:
            r, g, b = (c + k_contrast * c * (1 - c) * (2 * c - 1) for c in (r, g, b))
        if k_saturation != 1.0:
            y = 0.299 * r + 0.587 * g + 0.114 * b
            r, g, b = y + k_saturation * (r - y), y + k_saturation * (g - y), y + k_saturation * (b - y)
        out.append((min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0), min(max(b, 0.0), 1.0)))
    return out


def is_identity(table, size, tolerance=0.5 / 255):
    step = 1 / (size - 1)
    i = 0
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                r, g, b = table[i]
                i += 1
                if abs(r - ri * step) > tolerance or abs(g - gi * step) > tolerance or abs(b - bi * step) > tolerance:
                    return False
    return True


def _yuv(r, g, b):
    """Full range BT.601, 0..255, as the engine."""
    return (
        255 * (0.299 * r + 0.587 * g + 0.114 * b),
        255 * (-0.168736 * r - 0.331264 * g + 0.5 * b),
        255 * (0.5 * r - 0.418688 * g - 0.081312 * b),
    )


def _solve2(sxx, sxy, syy, tx, ty):
    """[a, b] minimising the squared error of a*x + b*y against t, from the sums."""
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        return 0.0, 0.0
    return (tx * syy - ty * sxy) / det, (ty * sxx - tx * sxy) / det


def neutral_curves(table, size):
    """What the look does to the greys: 256-entry tables (0..255) for the luma and each
    channel. They also correct the histograms, measured on the base."""
    step = 1 / (size - 1)
    diagonal = [table[(k * size + k) * size + k] for k in range(size)]
    curves = {}
    for name, f in (
        ("luma", lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]),
        ("red", lambda c: c[0]),
        ("green", lambda c: c[1]),
        ("blue", lambda c: c[2]),
    ):
        values = [f(c) for c in diagonal]
        curve = []
        for i in range(256):
            pos = i / 255 / step
            k = min(int(pos), size - 2)
            curve.append(round(255 * (values[k] + (values[k + 1] - values[k]) * (pos - k))))
        curves[name] = curve
    return curves


def fit_preview(table, size):
    """The viewfinder preview (engine "look" command): a luma curve, a mix of the chroma
    into the luma, a chroma matrix, fitted on the LUT by least squares. None: no change."""
    luma = neutral_curves(table, size)["luma"]
    inverse = [0] * 256  # the curve is monotone: first input reaching each output level
    j = 0
    for level in range(256):
        while j < 255 and luma[j] < level:
            j += 1
        inverse[level] = j
    sums = [0.0] * 5  # cb*cb, cb*cr, cr*cr, cb*d, cr*d
    m = [0.0] * 7  # cb*cb, cb*cr, cr*cr, cb'*cb, cb'*cr, cr'*cb, cr'*cr
    for bi in range(0, size, 2):  # every other point: plenty for 6 numbers
        for gi in range(0, size, 2):
            for ri in range(0, size, 2):
                y, cb, cr = _yuv(ri / (size - 1), gi / (size - 1), bi / (size - 1))
                # The colours of real scenes: moderate. The extreme ones clip whatever the
                # look, and would drag the fit down.
                if not 24 < y < 232 or cb * cb + cr * cr > 48 * 48:
                    continue
                y2, cb2, cr2 = _yuv(*table[(bi * size + gi) * size + ri])
                d = inverse[min(255, max(0, round(y2)))] - y
                sums[0] += cb * cb
                sums[1] += cb * cr
                sums[2] += cr * cr
                sums[3] += cb * d
                sums[4] += cr * d
                m[3] += cb2 * cb
                m[4] += cb2 * cr
                m[5] += cr2 * cb
                m[6] += cr2 * cr
    mix = _solve2(sums[0], sums[1], sums[2], sums[3], sums[4])
    # (cb', cr') = M (cb, cr): each row is a two-variable least squares problem.
    row_cb = _solve2(sums[0], sums[1], sums[2], m[3], m[4])
    row_cr = _solve2(sums[0], sums[1], sums[2], m[5], m[6])
    chroma = [row_cb[0], row_cb[1], row_cr[0], row_cr[1]]
    if (
        max(abs(v - i) for i, v in enumerate(luma)) <= 1
        and max(abs(v) for v in mix) < 0.01
        and max(abs(a - b) for a, b in zip(chroma, (1, 0, 0, 1))) < 0.02
    ):
        return None
    return {
        "luma": luma,
        "luma_mix": [round(v, 4) for v in mix],
        "chroma": [round(v, 4) for v in chroma],
    }


@dataclass(frozen=True)
class Compiled:
    lut: str | None  # the engine LUT file; None: nothing to apply
    preview: dict | None  # for the viewfinder (engine "look" command)
    curves: dict | None  # neutral_curves(), for the histograms; None: unchanged
    monochrome: bool = False


def compiled_look(preset, contrast=0, saturation=0, folder=None, keep=96):
    """The look for this preset and these settings, compiled once and kept in the cache
    (~0.2 MB per LUT): the LUT file, the viewfinder preview, the curves of the greys."""
    folder = folder or cache_dir()
    key = f"{preset.digest}-c{contrast}-s{saturation}-v{COMPILER}-{LUT_SIZE}"
    path = os.path.join(folder, f"{key}.lut")
    info_path = os.path.join(folder, f"{key}.json")
    try:
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
        if info["lut"] is None or os.path.exists(info["lut"]):
            return Compiled(info["lut"], info["preview"], info["curves"], info["monochrome"])
    except (OSError, ValueError, KeyError):
        pass
    os.makedirs(folder, exist_ok=True)
    table = compile_look(preset, contrast, saturation)
    identity = is_identity(table, LUT_SIZE)
    if not identity:
        write_engine_lut(path, LUT_SIZE, table)
    preview = None if identity else fit_preview(table, LUT_SIZE)
    monochrome = all(abs(r - g) < 1e-6 and abs(g - b) < 1e-6 for r, g, b in table)
    compiled = Compiled(None if identity else path, preview, None if identity else neutral_curves(table, LUT_SIZE), monochrome)
    tmp = info_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(compiled.__dict__, f)
    os.replace(tmp, info_path)
    _trim(folder, keep)
    return compiled


def compiled_lut(preset, contrast=0, saturation=0, folder=None):
    """Path of the engine LUT file for this preset and these settings; None: nothing to do."""
    return compiled_look(preset, contrast, saturation, folder).lut


def remap_histogram(histogram, curve):
    """A histogram of the base (64 classes) as it will be after the look, for the greys.

    Each class is spread over all the levels the curve sends it to: where the curve
    stretches the tones, one class covers several, and moving it whole would leave empty
    classes in between (black bands in the HUD)."""
    n = len(histogram)
    width = 256 // n
    out = [0.0] * n
    for i, count in enumerate(histogram):
        if not count:
            continue
        a, b = curve[i * width], curve[min(255, (i + 1) * width - 1)]
        low, high = min(a, b), max(a, b) + 1  # levels [low, high)
        for k in range(low // width, min(n - 1, (high - 1) // width) + 1):
            overlap = min(high, (k + 1) * width) - max(low, k * width)
            if overlap > 0:
                out[k] += count * overlap / (high - low)
    return out


def _trim(folder, keep):
    try:
        files = sorted((os.path.join(folder, f) for f in os.listdir(folder)), key=os.path.getmtime)
    except OSError:
        return
    for old in files[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
