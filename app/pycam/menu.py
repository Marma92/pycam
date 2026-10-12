"""The menus: settings in a list over the viewfinder, changed with the dials.

Two menus, as on the camera (docs/hardware.md §2):
- Q (Tab) opens the quick menu: the dozen settings changed while shooting;
- MENU (Échap) opens the full menu: every setting, by section, the configuration included.

The rear dial (up / down) chooses a line, the front dial (left / right) changes its value.
Q, MENU or the shutter close them; the shutter also shoots.

Besides settings, a line may open another menu (the applications: apps/), run an action,
or only inform. MENU goes back from a menu opened in another one.
"""

from dataclasses import dataclass

from . import drive, exposure, lenses, looks, picture, video
from .settings import FN_ACTIONS, PEAKING_COLORS, PEAKING_SENSITIVITIES


@dataclass(frozen=True)
class Item:
    label: str
    attr: str = ""  # in Settings, dotted for nested ones ("exposure.metering"); empty: a section title
    values: tuple = ()  # or settings -> ((value, shown), ...) for lists that depend on other settings
    shown: tuple = ()  # how each value is shown
    push: str = ""  # what the engine must be told: "exposure", "picture", "look", "config", "focus" or nothing
    # Not a setting: "open" (target(context) gives the menu to open), "run" (target(context)
    # runs, with the front dial forward), "info" (only shown). text(context): what is shown
    # on the right.
    kind: str = ""
    target: object = None
    text: object = None

    @property
    def selectable(self):
        return bool(self.attr) or self.kind in ("open", "run")

    def options(self, settings):
        """((value, shown), ...)"""
        if callable(self.values):
            return tuple(self.values(settings))
        return tuple(zip(self.values, self.shown))


def _levels(values, black_and_white=False):
    return tuple("N&B" if black_and_white and v == -3 else ("0" if v == 0 else f"{v:+d}".replace("-", "−")) for v in values)


def _lens_options(settings):
    return ((-1, "Aucun"),) + tuple((i, lens["name"]) for i, lens in enumerate(settings.lenses))


def _preset_options(settings):
    return tuple((preset_id, preset.name) for preset_id, preset in looks.registry().items())


def _grain_options(settings):
    presets = looks.registry()
    preset = presets.get(settings.preset) or presets.get(looks.DEFAULT)
    label = looks.grain_label(preset) if preset else "aucun"
    return ((False, "Non"), (True, f"Oui · {label}"))


def _timelapse_count_options(settings):
    """The number of frames, and the length of the video they make."""
    fps = settings.timelapse_fps
    return tuple((n, f"{n} vues · {n / fps:.0f} s de vidéo" if n else "Jusqu'à l'arrêt") for n in drive.TL_COUNTS)


def _video_quality_options(settings):
    return tuple(
        (q, f"{video.QUALITY_LABELS[q]} · {video.BITRATES[(settings.video_resolution, q)] // 1_000_000} Mbit/s")
        for q in video.QUALITIES
    )


def _aperture_options(settings):
    if not 0 <= settings.lens < len(settings.lenses):
        return ((None, "Choisir l'objectif"),)
    stops = lenses.aperture_stops(settings.lenses[settings.lens])
    return ((None, "Non précisée"),) + tuple((f, lenses.format_aperture(f)) for f in stops)


COLOURS = ("Rouge", "Jaune", "Vert", "Bleu", "Blanc")  # PEAKING_COLORS
YES_NO = ((False, True), ("Non", "Oui"))
FN_LABELS = {
    "peaking": "Peaking",
    "zebra": "Zébras",
    "awb_lock": "Mémoriser la balance",
    "grid": "Grille",
    "strategy": "Bascule / ZSL",
    "raw": "RAW + JPEG / JPEG",
    "drive": "Mode de déclenchement",
    "video": "Photo / Vidéo",
}

METERING = Item(
    "Mesure de la lumière",
    "exposure.metering",
    tuple(exposure.METERING),
    tuple(exposure.METERING_LABELS[m] for m in exposure.METERING),
    "exposure",
)
# Film, next to Auto: the balance of the preset's film, fixed (looks.Preset). Locked, last
# (so that no dial turned on the way locks anything): it keeps the gains the automatic
# balance measures now (point at a grey card first).
_WB_VALUES = ("Auto", "Film") + exposure.AWB_MODES[1:] + ("Locked",)
WHITE_BALANCE = Item(
    "Balance des blancs",
    "exposure.awb",
    _WB_VALUES,
    tuple({"Auto": "Auto", "Film": "Pellicule", "Kelvin": "Température"}.get(m) or exposure.AWB_LABELS[m] for m in _WB_VALUES),
    "exposure",
)
# Turned from another balance, it starts from the measured temperature (Camera.change_setting).
KELVIN = Item("Température", "exposure.kelvin", exposure.KELVIN_STEPS, tuple(f"{k} K" for k in exposure.KELVIN_STEPS), "exposure")
PEAKING = Item("Peaking", "peaking_mode", ("off",) + PEAKING_COLORS, ("Coupé",) + COLOURS, "focus")
PEAKING_SENSITIVITY = Item(
    "Sensibilité du peaking", "peaking_sensitivity", PEAKING_SENSITIVITIES, ("Faible", "Moyenne", "Forte"), "focus"
)
ZEBRA = Item("Zébras", "zebra", *YES_NO, "focus")
SHARPNESS = Item("Netteté", "sharpness", tuple(picture.SHARPNESS), _levels(picture.SHARPNESS), "picture")
# Contrast and saturation are compiled into the look, after the preset.
CONTRAST = Item("Contraste", "contrast", tuple(picture.CONTRAST), _levels(picture.CONTRAST), "look")
SATURATION = Item("Saturation", "saturation", tuple(picture.SATURATION), _levels(picture.SATURATION, True), "look")
SIMULATION = Item("Simulation", "preset", _preset_options, push="look")
# The preset's film grain on the JPEG; shows how fine it is ("Oui · fin").
GRAIN = Item("Simuler le grain", "grain", _grain_options, push="look")
DENOISE = Item(
    "Réduction du bruit", "denoise", tuple(picture.DENOISE), ("Aucune", "Légère", "Rapide", "Haute qualité"), "config"
)
FORMAT = Item("Format", "formats", (["jpeg", "dng"], ["jpeg"]), ("RAW + JPEG", "JPEG"))
JPEG_SIZE = Item("Taille JPEG", "jpeg_size", tuple(picture.JPEG_SIZES), ("L · 12 Mpx", "M · 6 Mpx", "S · 3 Mpx"), "config")
JPEG_QUALITY = Item("Qualité JPEG", "jpeg_quality", picture.JPEG_QUALITIES, ("Normale", "Fine", "Extra fine"))
DNG_COMPRESSION = Item("RAW compressé", "dng_compression", (True, False), ("Oui · sans perte", "Non"))
# Applied once the engine accepts it (Camera.set_strategy).
STRATEGY = Item("Réactivité", "strategy", ("switch", "zsl"), ("Bascule · visée fluide", "ZSL · sans latence"))
DRIVE = Item("Mode de déclenchement", "drive", drive.MODES, tuple(drive.LABELS[m] for m in drive.MODES))
INTERVAL = Item("Intervalle", "interval_seconds", drive.INTERVALS, tuple(drive.format_interval(i) for i in drive.INTERVALS))
INTERVAL_COUNT = Item("Nombre de vues", "interval_count", drive.COUNTS, tuple(str(c) if c else "Jusqu'à l'arrêt" for c in drive.COUNTS))
BRACKET_SHOTS = Item("Vues du bracketing", "bracket_shots", drive.BRACKET_SHOTS, tuple(str(n) for n in drive.BRACKET_SHOTS))
BRACKET_STEP = Item(
    "Écart du bracketing", "bracket_step", drive.BRACKET_STEPS, tuple(f"±{drive.format_step(t)} IL" for t in drive.BRACKET_STEPS)
)
BULB = Item("Pose B", "bulb_mode", ("hold", "toggle"), ("Tenir le déclencheur", "Un appui, puis un autre"))
TL_INTERVAL = Item(
    "Une vue toutes les", "timelapse_interval", drive.TL_INTERVALS, tuple(drive.format_interval(i) for i in drive.TL_INTERVALS)
)
TL_COUNT = Item("Durée", "timelapse_frames", _timelapse_count_options)
TL_FPS = Item("Cadence vidéo", "timelapse_fps", drive.TL_FPS, tuple(f"{f} i/s" for f in drive.TL_FPS))
TL_ASPECT = Item("Format vidéo", "timelapse_aspect", drive.TL_ASPECTS, ("16:9 · 1920 × 1080", "4:3 · 1440 × 1080"))
TL_DEFLICKER = Item("Lissage du scintillement", "timelapse_deflicker", (True, False), ("Oui", "Non"))
TL_PHOTOS = Item("Vues", "timelapse_photos", drive.TL_PHOTOS, ("Pour la vidéo · JPEG 3 Mpx", "Photos complètes"))
STACK_MODE = Item(
    "Type d'empilement",
    "stack_mode",
    drive.STACK_MODES,
    ("Moyenne · moins de bruit, pose simulée", "Maximum · traînées de lumière"),
)
STACK_COUNT = Item(
    "Vues empilées", "stack_frames", drive.STACK_COUNTS, tuple(str(n) if n else "Jusqu'à l'arrêt" for n in drive.STACK_COUNTS)
)
CAMERA_MODE = Item("Appareil", "mode", ("photo", "video"), ("Photo", "Vidéo"), "mode")
VIDEO_RESOLUTION = Item(
    "Définition vidéo", "video_resolution", tuple(video.RESOLUTIONS), ("1080p · 1920 × 1080", "720p · 1280 × 720"), "video"
)
VIDEO_QUALITY = Item("Qualité vidéo", "video_quality", _video_quality_options, push="video")
VIDEO_ISO = Item("ISO vidéo", "video_iso", video.ISO_VALUES, ("Auto",) + tuple(str(i) for i in video.ISO_VALUES[1:]), "exposure")
REVIEW = Item("Revue après la prise", "review_seconds", (0, 1, 2, 4), ("Coupée", "1 s", "2 s", "4 s"))
# Applied at the next start of the application (web.py).
WEB = Item("Interface web", "web", (True, False), ("Oui · au prochain démarrage", "Non · au prochain démarrage"))
KEEP_BASE = Item("Garder la base", "keep_base", (False, True), ("Non", "Oui · JPEG neutre en plus"))
# Another ISP tuning: the camera is reopened (~0.2 s).
LENS_SHADING = Item("Correction de l'ombrage", "lens_shading", (False, True), ("Non", "Oui"), "config")
LENS = Item("Objectif", "lens", _lens_options)
APERTURE = Item("Ouverture", "aperture", _aperture_options)
GRID = Item("Grille des tiers", "grid", *YES_NO)
HISTOGRAM = Item("Histogramme", "histogram", ("rgb", "luma"), ("RVB", "Luminance"))
SCREEN_OFF = Item(
    "Écran en longue prise",
    "screen_off",
    (10, 30, 60, 0),
    ("Éteint après 10 s", "Éteint après 30 s", "Éteint après 1 min", "Toujours allumé"),
)
FN1 = Item("Bouton Fn1", "fn1", FN_ACTIONS, tuple(FN_LABELS[a] for a in FN_ACTIONS))
FN2 = Item("Bouton Fn2", "fn2", FN_ACTIONS, tuple(FN_LABELS[a] for a in FN_ACTIONS))

QUICK = (
    SIMULATION,
    GRAIN,
    METERING,
    WHITE_BALANCE,
    KELVIN,
    PEAKING,
    SHARPNESS,
    CONTRAST,
    SATURATION,
    FORMAT,
    DRIVE,
    APERTURE,
)
# In video mode, Q holds the video's settings instead.
QUICK_VIDEO = (
    CAMERA_MODE,
    VIDEO_RESOLUTION,
    VIDEO_QUALITY,
    VIDEO_ISO,
    METERING,
    WHITE_BALANCE,
    KELVIN,
    PEAKING,
    ZEBRA,
)
APPLICATIONS = Item("Applications", kind="open", target=lambda camera: camera.apps_menu(), text=lambda camera: "›")
FULL = (
    Item("Applications"),
    APPLICATIONS,
    Item("Exposition"),
    METERING,
    WHITE_BALANCE,
    KELVIN,
    Item("Simulation"),
    SIMULATION,
    GRAIN,
    Item("Mise au point"),
    PEAKING,
    PEAKING_SENSITIVITY,
    ZEBRA,
    Item("Image"),
    SHARPNESS,
    CONTRAST,
    SATURATION,
    DENOISE,
    Item("Fichiers"),
    FORMAT,
    JPEG_SIZE,
    JPEG_QUALITY,
    DNG_COMPRESSION,
    Item("Prise de vue"),
    DRIVE,
    INTERVAL,
    INTERVAL_COUNT,
    BRACKET_SHOTS,
    BRACKET_STEP,
    BULB,
    STRATEGY,
    REVIEW,
    KEEP_BASE,
    Item("Timelapse"),
    TL_INTERVAL,
    TL_COUNT,
    TL_FPS,
    TL_ASPECT,
    TL_DEFLICKER,
    TL_PHOTOS,
    Item("Empilement"),
    STACK_MODE,
    STACK_COUNT,
    Item("Vidéo"),
    CAMERA_MODE,
    VIDEO_RESOLUTION,
    VIDEO_QUALITY,
    VIDEO_ISO,
    Item("Objectif"),
    LENS,
    APERTURE,
    LENS_SHADING,
    Item("Réseau"),
    WEB,
    Item("Affichage"),
    GRID,
    HISTOGRAM,
    SCREEN_OFF,
    Item("Boutons"),
    FN1,
    FN2,
)


class Menu:
    """Q or MENU; or, with `items`, another menu (an application's). context: what the
    lines that are not settings need (the camera)."""

    def __init__(self, settings, quick=True, items=None, title="", context=None):
        self.settings = settings
        self.quick = quick and items is None
        self.context = context
        if items is not None:
            self.title, self.items = title, tuple(items)
        elif quick and settings.mode == "video":
            self.title, self.items = "Réglages vidéo", QUICK_VIDEO
        else:
            self.title = "Réglages rapides" if quick else "Menu"
            self.items = QUICK if quick else FULL
        self.index = 0
        self.move(0)  # past a section title

    @property
    def item(self):
        return self.items[self.index]

    def _owner(self, attr):
        """The object holding a (dotted) attribute, and the attribute's last name."""
        owner, *path = [self.settings] + attr.split(".")
        for name in path[:-1]:
            owner = getattr(owner, name)
        return owner, path[-1]

    def get(self, attr):
        owner, name = self._owner(attr)
        return getattr(owner, name)

    def set(self, attr, value):
        owner, name = self._owner(attr)
        setattr(owner, name, value)

    def move(self, steps):
        """steps lines down (up if negative), around the list, past the section titles."""
        if not any(item.selectable for item in self.items):
            return
        direction = 1 if steps >= 0 else -1
        for _ in range(abs(steps)):
            self.index = (self.index + direction) % len(self.items)
            while not self.item.selectable:
                self.index = (self.index + direction) % len(self.items)
        while not self.item.selectable:
            self.index = (self.index + direction) % len(self.items)

    def next_value(self, steps):
        """The value of the chosen line after turning the dial (stopping at the ends)."""
        values = [value for value, _ in self.item.options(self.settings)]
        current = self.get(self.item.attr)
        i = values.index(current) if current in values else 0
        return values[max(0, min(len(values) - 1, i + steps))]

    def rows(self, stats=None):
        """(label, value shown) for each line; the value is None for a section title.
        stats: the engine's last measures."""
        rows = []
        for item in self.items:
            if item.kind:
                rows.append((item.label, item.text(self.context) if item.text else ""))
                continue
            if not item.attr:
                rows.append((item.label, None))
                continue
            current = self.get(item.attr)
            shown = next((text for value, text in item.options(self.settings) if value == current), "?")
            if item is KELVIN and self.settings.exposure.awb != "Kelvin":
                # Not in use: the measured temperature, where the dial will start from.
                measured = (stats or {}).get("colour_temp")
                steps = exposure.KELVIN_STEPS
                shown = f"≈ {steps[exposure.nearest_index(steps, measured)]} K" if measured else "—"
            rows.append((item.label, shown))
        return tuple(rows)
