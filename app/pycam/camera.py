"""The camera: settings, actions and HUD, driving the engine (docs/architecture.md §5)."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from . import exposure as exp
from . import lenses
from .engine_client import EngineError, call_blocking
from .settings import DISPLAY_MODES, PEAKING_COLORS, PEAKING_SENSITIVITIES, REVIEW_SECONDS, save_settings
from .storage import Storage
from .view import HudView

log = logging.getLogger(__name__)

HUD_MIN_INTERVAL = 0.2  # at most 5 HUD renders per second
MESSAGE_SECONDS = 1.5
SAVE_DELAY = 1.0  # settings are written once the dials stop turning

EXPOSURE_ACTIONS = {
    "shutter_faster",
    "shutter_slower",
    "iso_up",
    "iso_down",
    "ev_up",
    "ev_down",
    "shutter_auto",
    "iso_auto",
    "awb",
    "kelvin_up",
    "kelvin_down",
    "awb_lock",
}
FOCUS_AID_ACTIONS = {"peaking", "peaking_color", "peaking_sensitivity", "zebra"}
LOUPE_LEVELS = (1, 5, 10)
# In the loupe, the dials move it (until the camera has a joystick): action -> (dx, dy).
LOUPE_MOVES = {
    "shutter_faster": (1, 0),
    "shutter_slower": (-1, 0),
    "iso_up": (0, -1),
    "iso_down": (0, 1),
}

COLOR_LABELS = {"red": "rouge", "yellow": "jaune", "green": "vert", "blue": "bleu", "white": "blanc"}
SENSITIVITY_LABELS = {"low": "faible", "medium": "moyenne", "high": "forte"}


def _next(values, current):
    return values[(values.index(current) + 1) % len(values)]


@dataclass
class Shot:
    """The last photo taken, followed until it can be reviewed."""

    capture_id: int
    name: str
    exposure_us: float = 0
    iso: float = 0


@dataclass
class Review:
    path: str
    info: str
    histogram: tuple


class Camera:
    """hud_factory(info) makes the HUD from the engine's hud_info; None: no HUD (tests)."""

    def __init__(self, settings, settings_path, engine, hud_factory=None):
        self.settings = settings
        self.settings_path = settings_path
        self.storage = Storage(settings)
        self.engine = engine
        engine.on_event = self.on_event
        engine.on_connect = self.sync_engine
        engine.on_disconnect = self.on_engine_lost
        self.hud_factory = hud_factory
        self.hud = None
        self.stats = {}
        self.writing = 0
        self.loupe = 1  # not a setting: the camera always starts unmagnified
        self.loupe_center = (0.5, 0.5)
        self.last_shot = None
        self.review = None  # photo shown after the shot
        self.slowest_shutter = exp.SHUTTER_SPEEDS[-1]
        self._message = ("", False, 0.0)
        self._review_handle = None
        self._redraw = asyncio.Event()
        self._capture_lock = asyncio.Lock()
        self._save_handle = None
        self._tasks = set()

    async def run(self, inputs):
        await asyncio.gather(self.engine.run(), inputs.run(), self.hud_loop())

    def shutdown(self):
        """Settings saved, and the engine's default HUD back on the screen."""
        self.save_now()
        if self.hud:
            try:
                self.hud.draw_default()
                call_blocking(self.engine.path, "hud_commit")
            except (OSError, ValueError, EngineError) as e:
                log.debug("cannot restore the default HUD: %s", e)
            self.close_hud()

    # --- Engine

    async def sync_engine(self):
        """After each (re)connection: push the whole state, then set the HUD up."""
        controls = (await self.engine.call("list_controls"))["controls"]
        exposure_max = controls.get("ExposureTime", {}).get("max")
        if isinstance(exposure_max, (int, float)) and exposure_max > 0:
            self.slowest_shutter = exposure_max / 1e6
        await self.engine.call("configure", strategy=self.settings.strategy)
        await self.apply_exposure()
        await self.apply_focus_aids()
        self.loupe = 1
        await self.apply_loupe()
        await self.setup_hud()

    async def setup_hud(self):
        """At connection, or when the engine gets the screen later ("display_ready")."""
        if not self.hud_factory:
            return
        try:
            info = (await self.engine.call("hud_info"))["display"]
        except EngineError as e:
            log.warning("no HUD for now: %s", e)
            return
        self.close_hud()
        self.hud = self.hud_factory(info)
        self.redraw()

    def on_engine_lost(self):
        self.end_review()
        self.close_hud()
        self.stats = {}

    def on_event(self, msg):
        kind = msg.get("event")
        if kind == "stats":
            self.stats = msg
            self.redraw()
        elif kind == "captured":
            shot = self.last_shot
            if shot and shot.capture_id == msg.get("capture_id"):
                shot.exposure_us = msg.get("exposure_us", 0)
                shot.iso = msg.get("analogue_gain", 0) * msg.get("digital_gain", 1) * 100
        elif kind == "file_saved":
            # The JPEG is written first: review it without waiting for the DNG.
            path, shot = msg.get("path", ""), self.last_shot
            if path.endswith(".JPG") and shot and shot.capture_id == msg.get("capture_id") and self.settings.review_seconds:
                self.spawn(self.show_review(shot, path))
        elif kind == "capture_done":
            self.writing = msg.get("write_queue", 0)
            log.info("saved %s", " ".join(os.path.basename(f) for f in msg.get("files", [])))
            self.redraw()
        elif kind == "capture_error":
            self.writing = max(0, self.writing - 1)
            log.error("capture %s failed: %s", msg.get("capture_id"), msg.get("error"))
            self.show_message("Erreur d'enregistrement", warning=True)
        elif kind == "loupe":
            self.loupe = msg.get("loupe", {}).get("zoom", 1)
            self.redraw()
        elif kind == "display_ready":
            log.info("the engine got the screen")
            self.spawn(self.setup_hud())
        elif kind == "error":
            log.error("engine: %s", msg.get("error"))

    async def apply_exposure(self):
        try:
            await self.engine.call("set_controls", controls=self.settings.exposure.controls())
        except ConnectionError:
            pass  # pushed again on reconnection
        except EngineError as e:
            log.error("exposure refused: %s", e)

    async def apply_focus_aids(self):
        s = self.settings
        try:
            await self.engine.call(
                "focus_aids",
                peaking=s.peaking,
                peaking_color=s.peaking_color,
                peaking_sensitivity=s.peaking_sensitivity,
                zebra=s.zebra,
            )
        except ConnectionError:
            pass  # pushed again on reconnection
        except EngineError as e:
            log.error("focus aids refused: %s", e)

    async def apply_loupe(self):
        try:
            await self.engine.call("loupe", zoom=self.loupe, center=list(self.loupe_center))
        except ConnectionError:
            pass
        except EngineError as e:
            log.warning("loupe refused: %s", e)
            self.show_message("Impossible pour l'instant", warning=True)

    def move_loupe(self, dx, dy):
        """One step = half of what the loupe shows; the centre stays where it can be shown."""
        half = 0.5 / self.loupe
        x, y = self.loupe_center
        x = min(1 - half, max(half, x + dx * half))
        y = min(1 - half, max(half, y + dy * half))
        self.loupe_center = (x, y)

    # --- Review after the shot

    async def show_review(self, shot, path):
        hud = self.hud
        if hud is None:
            return
        histogram = await asyncio.to_thread(hud.load_review, path)
        if shot is not self.last_shot or hud is not self.hud:
            return  # another photo was taken meanwhile, or the screen changed
        info = shot.name
        if shot.exposure_us:
            info += f"   {exp.format_shutter(shot.exposure_us / 1e6)}   ISO {round(shot.iso)}"
        self.review = Review(path=path, info=info, histogram=tuple(histogram))
        if self._review_handle:
            self._review_handle.cancel()
        self._review_handle = asyncio.get_running_loop().call_later(self.settings.review_seconds, self.end_review)
        self.redraw()

    def end_review(self):
        if self._review_handle:
            self._review_handle.cancel()
            self._review_handle = None
        if self.review:
            self.review = None
            self.redraw()

    # --- Actions (inputs.py)

    def on_action(self, action):
        log.debug("action %s", action)
        s, e = self.settings, self.settings.exposure
        if self.review:
            # Any key ends the review; the shutter also shoots: never miss a photo.
            self.end_review()
            if action != "shutter":
                return
        if action == "shutter":
            self.spawn(self.capture())
            return
        if action == "strategy":
            self.spawn(self.toggle_strategy())
            return
        if action == "loupe":
            self.loupe = _next(LOUPE_LEVELS, self.loupe) if self.loupe in LOUPE_LEVELS else 1
            self.show_message(f"Loupe ×{self.loupe}" if self.loupe > 1 else "Loupe coupée")
            self.spawn(self.apply_loupe())
            return
        if self.loupe > 1 and action in LOUPE_MOVES:
            self.move_loupe(*LOUPE_MOVES[action])
            self.spawn(self.apply_loupe())
            self.redraw()
            return
        if action == "shutter_faster":
            e.step_shutter(-1, self.measured_shutter(), self.slowest_shutter)
        elif action == "shutter_slower":
            e.step_shutter(+1, self.measured_shutter(), self.slowest_shutter)
        elif action == "iso_up":
            e.step_iso(+1, self.measured_iso())
        elif action == "iso_down":
            e.step_iso(-1, self.measured_iso())
        elif action == "ev_up":
            e.step_ev(+1)
        elif action == "ev_down":
            e.step_ev(-1)
        elif action == "shutter_auto":
            e.toggle_shutter_auto(self.measured_shutter())
        elif action == "iso_auto":
            e.toggle_iso_auto(self.measured_iso())
        elif action == "awb":
            e.next_awb()
            self.show_message(f"Balance des blancs : {e.awb_label()}")
        elif action in ("kelvin_up", "kelvin_down"):
            e.step_kelvin(+1 if action == "kelvin_up" else -1, self.stats.get("colour_temp"))
        elif action == "awb_lock":
            if not e.lock_awb(self.stats.get("colour_gains")):
                self.show_message("Pas de mesure de balance des blancs", warning=True)
                return
            self.show_message("Balance des blancs mémorisée")
        elif action == "disp":
            s.display = DISPLAY_MODES[(DISPLAY_MODES.index(s.display) + 1) % len(DISPLAY_MODES)]
        elif action == "grid":
            s.grid = not s.grid
        elif action == "raw":
            s.formats = ["jpeg"] if "dng" in s.formats else ["jpeg", "dng"]
            self.show_message("RAW + JPEG" if "dng" in s.formats else "JPEG seul")
        elif action == "dng_compression":
            s.dng_compression = not s.dng_compression
            self.show_message("RAW compressé (sans perte)" if s.dng_compression else "RAW non compressé")
        elif action == "peaking":
            s.peaking = not s.peaking
            self.show_message("Peaking" if s.peaking else "Peaking coupé")
        elif action == "peaking_color":
            s.peaking = True
            s.peaking_color = _next(PEAKING_COLORS, s.peaking_color)
            self.show_message(f"Peaking {COLOR_LABELS[s.peaking_color]}")
        elif action == "peaking_sensitivity":
            s.peaking = True
            s.peaking_sensitivity = _next(PEAKING_SENSITIVITIES, s.peaking_sensitivity)
            self.show_message(f"Peaking : sensibilité {SENSITIVITY_LABELS[s.peaking_sensitivity]}")
        elif action == "zebra":
            s.zebra = not s.zebra
            self.show_message("Zébras" if s.zebra else "Zébras coupés")
        elif action == "review":
            s.review_seconds = _next(REVIEW_SECONDS, s.review_seconds)
            self.show_message(f"Revue : {s.review_seconds} s" if s.review_seconds else "Revue coupée")
        elif action == "lens":
            # none -> each profile -> none; the aperture belongs to the previous lens
            s.lens = s.lens + 1 if s.lens + 1 < len(s.lenses) else -1
            s.aperture = None
            lens = self.lens()
            self.show_message(lens["name"] if lens else "Objectif non précisé")
        elif action == "aperture":
            lens = self.lens()
            if not lens:
                self.show_message("Choisir d'abord l'objectif (N)", warning=True)
                return
            stops = [None] + lenses.aperture_stops(lens)  # None: not told
            current = s.aperture if s.aperture in stops else None
            s.aperture = _next(stops, current)
            self.show_message(lenses.format_aperture(s.aperture) if s.aperture else "Ouverture non précisée")
        else:
            log.warning("unknown action %s", action)
            return
        if action in EXPOSURE_ACTIONS:
            self.spawn(self.apply_exposure())
        if action in FOCUS_AID_ACTIONS:
            self.spawn(self.apply_focus_aids())
        self.save_soon()
        self.redraw()

    async def capture(self):
        async with self._capture_lock:  # two quick presses must not get the same name
            if not self.engine.connected:
                self.show_message("Moteur indisponible", warning=True)
                return
            base, folder, number = self.storage.next_photo()
            try:
                reply = await self.engine.call(
                    "capture",
                    path=base,
                    formats=self.settings.formats,
                    dng_compression=self.settings.dng_compression,
                    quality=self.settings.jpeg_quality,
                    exif=lenses.lens_exif(self.lens(), self.settings.aperture),
                )
            except EngineError as e:
                if e.code != "busy":
                    log.error("capture refused: %s", e)
                self.show_message("Occupé…" if e.code == "busy" else "Déclenchement refusé", warning=True)
                return
            except ConnectionError:
                self.show_message("Moteur indisponible", warning=True)
                return
            self.storage.commit(folder, number)
            self.save_now()  # never give the same number twice, even after a crash
            self.writing += 1
            self.loupe = 1  # the engine leaves the loupe to shoot the full frame
            self.last_shot = Shot(reply.get("capture_id"), os.path.basename(base))
            self.show_message(os.path.basename(base))

    async def toggle_strategy(self):
        new = "zsl" if self.settings.strategy == "switch" else "switch"
        try:
            await self.engine.call("configure", strategy=new)
        except (EngineError, ConnectionError) as e:
            log.warning("cannot change the capture strategy: %s", e)
            self.show_message("Impossible pour l'instant", warning=True)
            return
        self.settings.strategy = new
        self.save_soon()
        self.show_message("Réactivité (ZSL)" if new == "zsl" else "Fluidité (bascule)")

    # --- Measures

    def lens(self):
        """The chosen lens profile, or None."""
        s = self.settings
        return s.lenses[s.lens] if 0 <= s.lens < len(s.lenses) else None

    def measured_shutter(self):
        return self.stats.get("exposure_us", 0) / 1e6

    def measured_iso(self):
        return self.stats.get("analogue_gain", 0) * self.stats.get("digital_gain", 1) * 100

    # --- HUD

    def view(self):
        s, e = self.settings, self.settings.exposure
        text, warning, until = self._message
        message = text if time.monotonic() < until else ""
        if self.review:
            # Nothing live in it: it is drawn once, not at every stats event.
            r = self.review
            histogram = r.histogram if s.display == "detailed" else ()
            return HudView(
                display=s.display, review=r.path, review_info=r.info, histogram=histogram, message=message, warning=warning
            )
        shutter = e.shutter if e.shutter is not None else self.measured_shutter()
        iso = e.iso if e.iso is not None else round(self.measured_iso())
        return HudView(
            display=s.display,
            grid=s.grid and self.loupe == 1,  # the rule of thirds means nothing in the loupe
            mode=e.mode,
            shutter=exp.format_shutter(shutter),
            shutter_auto=e.shutter is None,
            iso=f"ISO {iso}" if iso else "ISO --",
            iso_auto=e.iso is None,
            ev=exp.format_ev(e.ev) if e.mode != "M" else "",
            awb=e.awb_label(),
            formats="RAW+JPEG" if "dng" in s.formats else "JPEG",
            lens=lenses.format_lens(self.lens(), s.aperture),
            shots=self.storage.shots_remaining(s.formats, s.dng_compression),
            zsl=s.strategy == "zsl",
            peaking=s.peaking_color if s.peaking else "",
            zebra=s.zebra,
            loupe=self.loupe,
            loupe_center=self.loupe_center,
            writing=self.writing,
            histogram=tuple(self.stats.get("histogram", ())) if s.display == "detailed" else (),
            message=message,
            warning=warning,
        )

    def redraw(self):
        self._redraw.set()

    def show_message(self, text, warning=False):
        self._message = (text, warning, time.monotonic() + MESSAGE_SECONDS)
        asyncio.get_running_loop().call_later(MESSAGE_SECONDS + 0.05, self.redraw)
        self.redraw()

    async def hud_loop(self):
        hud, shown = None, None
        while True:
            await self._redraw.wait()
            self._redraw.clear()
            if self.hud is not hud:  # new HUD after a reconnection: draw everything again
                hud, shown = self.hud, None
            if hud is not None:
                view = self.view()
                if view != shown:
                    try:
                        await asyncio.to_thread(hud.draw, view)  # ~0.1 s at 1080p: off the event loop
                        await self.engine.call("hud_commit")
                        shown = view
                    except (EngineError, ConnectionError, ValueError) as e:  # ValueError: HUD closed meanwhile
                        log.debug("HUD not shown: %s", e)
            await asyncio.sleep(HUD_MIN_INTERVAL)

    def close_hud(self):
        if self.hud:
            self.hud.close()
            self.hud = None

    # --- Plumbing

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("task failed", exc_info=task.exception())

    def save_soon(self):
        if self._save_handle:
            self._save_handle.cancel()
        self._save_handle = asyncio.get_running_loop().call_later(SAVE_DELAY, self.save_now)

    def save_now(self):
        if self._save_handle:
            self._save_handle.cancel()
            self._save_handle = None
        try:
            save_settings(self.settings, self.settings_path)
        except OSError as e:
            log.error("cannot save the settings: %s", e)
