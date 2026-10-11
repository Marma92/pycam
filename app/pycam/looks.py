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
import logging
import os
import struct
import tomllib
from dataclasses import dataclass

log = logging.getLogger(__name__)

BASE = "pycam-1"  # the ISP rendering presets are written for (engine/tuning)
FORMAT = 1  # of preset.toml
LUT_SIZE = 33
COMPILER = 1  # bump when compile() changes: the cached LUTs are then remade
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
    "split_toning",
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
    look: tuple = ()  # the [look] table, as sorted (key, value) pairs
    grain: float = 0.0
    grain_size: float = 1.0
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
        look=tuple(sorted((k, _freeze(v)) for k, v in look.items())),
        grain=_number(grain, "amount", 0.0, 0, 1, "grain."),
        grain_size=_number(grain, "size", 1.0, 0.5, 8, "grain."),
        vignette=_number(vignette, "amount", 0.0, 0, 1, "vignette."),
        digest=hasher.hexdigest()[:16],
    )


def _freeze(value):
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


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


def compile_look(preset, contrast=0, saturation=0, size=LUT_SIZE):
    """The preset and the picture settings as one LUT: size^3 RGB triplets 0..1 on the base,
    red varying fastest.

    Order, as in a film camera: the preset's LUT, the black and white mix (the emulsion's
    sensitivity), the tone curves (its characteristic curve), its saturation, the toning;
    then the strength (mix with the base); then the picture settings."""
    look = preset.look_table
    user_lut = read_lut_file(look["lut"]) if "lut" in look else None
    strength = look.get("strength", 1.0)
    mono = bool(look.get("monochrome", False)) or SATURATION[saturation] == 0
    mixer = look.get("mixer", (0.299, 0.587, 0.114))
    total = sum(mixer)
    mixer = [m / total for m in mixer]
    tone = curve_table(look["tone_curve"]) if "tone_curve" in look else None
    channel_curves = [curve_table(look[k]) if k in look else None for k in ("red_curve", "green_curve", "blue_curve")]
    look_saturation = look.get("saturation", 1.0)
    toning = dict(look.get("split_toning", ()))
    tints = []
    for key, weight in (("shadows", lambda y: (1 - y) ** 2), ("highlights", lambda y: y**2)):
        if key in toning:
            hue, amount = toning[key]
            rgb = colorsys.hsv_to_rgb(hue / 360, 1, 1)
            luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
            tints.append(([c - luma for c in rgb], amount, weight))
    k_contrast = CONTRAST[contrast]
    user_saturation = SATURATION[saturation] if SATURATION[saturation] > 0 else 1.0

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
                if look_saturation != 1.0 and not mono:
                    y = 0.299 * r + 0.587 * g + 0.114 * b
                    r, g, b = y + look_saturation * (r - y), y + look_saturation * (g - y), y + look_saturation * (b - y)
                for chroma, amount, weight in tints:
                    w = amount * weight(0.299 * r + 0.587 * g + 0.114 * b)
                    r, g, b = r + w * chroma[0], g + w * chroma[1], b + w * chroma[2]
                if strength != 1.0:
                    r, g, b = r0 + strength * (r - r0), g0 + strength * (g - g0), b0 + strength * (b - b0)
                if k_contrast:
                    r, g, b = (c + k_contrast * c * (1 - c) * (2 * c - 1) for c in (min(max(v, 0.0), 1.0) for v in (r, g, b)))
                if user_saturation != 1.0:
                    y = 0.299 * r + 0.587 * g + 0.114 * b
                    r, g, b = y + user_saturation * (r - y), y + user_saturation * (g - y), y + user_saturation * (b - y)
                table.append((min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0), min(max(b, 0.0), 1.0)))
    return table


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


def compiled_lut(preset, contrast=0, saturation=0, folder=None, keep=64):
    """Path of the engine LUT file for this preset and these settings, compiled once and
    kept in the cache (~0.2 MB each); None when it changes nothing (the base as it is)."""
    folder = folder or cache_dir()
    key = f"{preset.digest}-c{contrast}-s{saturation}-v{COMPILER}-{LUT_SIZE}"
    path = os.path.join(folder, f"{key}.lut")
    none_marker = path + ".identity"
    if os.path.exists(path):
        return path
    if os.path.exists(none_marker):
        return None
    os.makedirs(folder, exist_ok=True)
    table = compile_look(preset, contrast, saturation)
    if is_identity(table, LUT_SIZE):
        open(none_marker, "w").close()
        return None
    write_engine_lut(path, LUT_SIZE, table)
    _trim(folder, keep)
    return path


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
