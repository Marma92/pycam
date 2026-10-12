"""The camera: settings, actions and HUD, driving the engine (docs/architecture.md §5)."""

import asyncio
import logging
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from . import drive as drives
from . import exposure as exp
from . import lenses, looks, meter, picture, timelapse
from .engine_client import EngineError, call_blocking
from .menu import Menu
from .playback import PIXEL_SCALES, Playback, describe, describe_video, poster
from .settings import DISPLAY_MODES, save_settings
from .storage import Storage
from .view import HudView

log = logging.getLogger(__name__)

HUD_MIN_INTERVAL = 0.2  # at most 5 HUD renders per second
MESSAGE_SECONDS = 1.5
SAVE_DELAY = 1.0  # settings are written once the dials stop turning

EXPOSURE_ACTIONS = {
    "front_dial_plus",
    "front_dial_minus",
    "rear_dial_plus",
    "rear_dial_minus",
    "shutter_auto",
    "iso_auto",
    "awb_lock",
}
FOCUS_AID_ACTIONS = {"peaking", "zebra"}
LOUPE_LEVELS = (1, 5, 10)
CAPTURED_TIMEOUT = 10.0  # seconds, beyond the exposure time
LONG_EXPOSURE = 1.0  # seconds: from there, a timer on the screen during the exposure
# Playback: the rear dial goes from one photo to the next, the front dial jumps ten.
PLAYBACK_STEPS = {"rear_dial_plus": 1, "rear_dial_minus": -1, "front_dial_plus": 10, "front_dial_minus": -10}
DELETE_CONFIRM_SECONDS = 3
# In the loupe, the dials move it (until the camera has a joystick): action -> (dx, dy).
LOUPE_MOVES = {
    "front_dial_plus": (1, 0),
    "front_dial_minus": (-1, 0),
    "rear_dial_plus": (0, -1),
    "rear_dial_minus": (0, 1),
}


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
class Exposing:
    """A long exposure or a bulb, from its start ("exposing") to its end ("captured")."""

    capture_id: int
    start: float  # time.monotonic()
    duration: float | None  # seconds; None: a bulb or a stack, as long as needed
    seconds: float = 0  # bulb: exposed so far, from its frames
    label: str = "POSE B"
    total: int | None = None  # a stack: its frames (0: until stopped); None: a bulb
    frames: int = 0


@dataclass
class Review:
    path: str
    info: str
    histogram: tuple
    histogram_rgb: tuple
    size: tuple | None = None  # of the photo, in pixels (playback: for the loupe)
    image: str = ""  # what the HUD shows, if not `path`: a video's first frame


@dataclass
class Video:
    """The video being played in playback (by the engine, on the video plane)."""

    path: str
    duration: float = 0
    position: float = 0
    paused: bool = False
    ended: bool = False


# Drive modes during which the screen may go to sleep (Camera.check_screen).
LONG_CAPTURES = ("interval", "timelapse", "bulb", "stack")


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
        self.menu = None  # Menu while open
        self.playback = None  # Playback while open (the LECTURE button)
        self._shown = None  # Review of the photo shown in playback, once loaded
        self._delete_armed_until = 0.0  # deleting takes two presses of Q
        self._look_version = 0  # the last look asked for: only that one is sent
        self._still_version = 0  # the last photo settings asked for (_configure)
        self._look = None  # looks.Compiled sent to the engine: its curves correct the histograms
        self._look_lock = asyncio.Lock()
        self.slowest_shutter = exp.SHUTTER_SPEEDS[-1]
        self._message = ("", False, 0.0)
        self._review_handle = None
        self._redraw = asyncio.Event()
        self._capture_lock = asyncio.Lock()
        self.capture_error = None  # why the last capture() was refused: "busy", "unavailable"...
        self._captured = OrderedDict()  # capture id -> taken (True) or failed (False), the last ones
        self._capture_waits = {}  # capture id -> future, for wait_captured
        self.drive = drives.Drive(self)
        self.exposing = None  # Exposing, during a long exposure or a bulb
        self.last_capture_path = ""  # without extension
        self.task_status = ""  # a long background task, on the HUD ("VIDÉO 40 %")
        self.video = None  # Video, while one is played in playback
        self.screen_on = True  # off during long captures, to save power (check_screen)
        self._last_input = time.monotonic()
        self._exposure_deferred = False  # an exposure change waits for the end of the exposure
        self._save_handle = None
        self._tasks = set()
        self._snapshots = {}  # path -> future, until the engine has written it

    async def run(self, inputs):
        await asyncio.gather(self.engine.run(), inputs.run(), self.hud_loop(), self.screen_loop())

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
        await self.engine.call("configure", strategy=self.settings.strategy, **picture.engine_config(self.settings))
        await self.apply_exposure()
        await self.apply_picture()
        await self.apply_look()
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
            e = self.settings.exposure
            if e.mode == "P" and not self.drive.freezes_exposure:
                # Fully automatic: learn the meter constant (saved with the settings).
                self.settings.meter_k = meter.calibrate(self.settings.meter_k, msg, e.ev + self.preset().exposure_bias)
            self.redraw()
        elif kind == "captured":
            shot = self.last_shot
            if shot and shot.capture_id == msg.get("capture_id"):
                shot.exposure_us = msg.get("exposure_us", 0)
                shot.iso = msg.get("analogue_gain", 0) * msg.get("digital_gain", 1) * 100
            self._capture_taken(msg.get("capture_id"), True)
            self._end_exposure(msg.get("capture_id"))
        elif kind == "exposing":
            if msg.get("stack") or msg.get("exposure_us", 0) >= LONG_EXPOSURE * 1e6:
                duration = None if msg.get("stack") else msg["exposure_us"] / 1e6
                self.exposing = Exposing(msg.get("capture_id"), time.monotonic(), duration)
                if msg.get("stack") and self.drive.running == "stack":
                    s = self.settings
                    self.exposing.label = "EMPILEMENT · " + drives.STACK_LABELS[s.stack_mode].upper()
                    self.exposing.total = s.stack_frames
                self.spawn(self._exposure_ticker())
                self.redraw()
        elif kind == "stack_frame":
            if self.exposing and self.exposing.capture_id == msg.get("capture_id"):
                self.exposing.seconds = msg.get("exposure_us", 0) / 1e6
                self.exposing.frames = msg.get("frames", 0)
                self.redraw()
        elif kind == "file_saved":
            # The JPEG is written first: review it without waiting for the DNG. Not during a
            # burst or a bracketing: the last photo is reviewed once it is over.
            path, shot = msg.get("path", ""), self.last_shot
            if (
                path.endswith(".JPG")
                and shot
                and shot.capture_id == msg.get("capture_id")
                and self.settings.review_seconds
                and not self.drive.quiet
            ):
                self.spawn(self.show_review(shot, path))
        elif kind == "capture_done":
            self.writing = msg.get("write_queue", 0)
            log.info("saved %s", " ".join(os.path.basename(f) for f in msg.get("files", [])))
            self.redraw()
        elif kind == "capture_error":
            self.writing = max(0, self.writing - 1)
            self._capture_taken(msg.get("capture_id"), False)
            self._end_exposure(msg.get("capture_id"))
            if msg.get("error") == "cancelled":
                log.info("capture %s cancelled", msg.get("capture_id"))
                self.show_message("Pose annulée")
            else:
                log.error("capture %s failed: %s", msg.get("capture_id"), msg.get("error"))
                self.show_message("Erreur d'enregistrement", warning=True)
        elif kind == "loupe":
            self.loupe = msg.get("loupe", {}).get("zoom", 1)
            self.redraw()
        elif kind in ("snapshot_done", "snapshot_error"):
            future = self._snapshots.pop(msg.get("path"), None)
            if future and not future.done():
                future.set_result(kind == "snapshot_done")
        elif kind == "tuning":
            log.info("ISP tuning %s (camera reopened in %.0f ms)", msg.get("tuning"), msg.get("reopen_ms", 0))
        elif kind == "video_progress":
            if self.video:
                self.video.position = msg.get("position", 0)
                self.video.duration = msg.get("duration") or self.video.duration
                self.redraw()
        elif kind == "video_ended":
            if self.video:
                self.video.position = msg.get("position", self.video.position)
                if msg.get("reason") == "end":
                    self.video.ended = True
                elif msg.get("reason") == "error":
                    log.error("video: %s", msg.get("error"))
                    self.video = None
                    self.spawn(self._video_call("video_stop"))
                    self.show_message("Lecture impossible", warning=True)
                self.redraw()
        elif kind == "display_ready":
            log.info("the engine got the screen")
            self.spawn(self.setup_hud())
        elif kind == "error":
            log.error("engine: %s", msg.get("error"))

    async def apply_exposure(self):
        if self.drive.freezes_exposure:
            return  # the bracketing sets it, and gives it back when it is over
        if self.exposing:
            self._exposure_deferred = True  # not in the middle of a bulb
            return
        try:
            preset = self.preset()
            controls = self.settings.exposure.controls(preset.exposure_bias, preset.white_balance)
            await self.engine.call("set_controls", controls=controls)
        except ConnectionError:
            pass  # pushed again on reconnection
        except EngineError as e:
            log.error("exposure refused: %s", e)

    async def apply_picture(self):
        try:
            await self.engine.call("set_controls", controls=picture.controls(self.settings))
        except ConnectionError:
            pass  # pushed again on reconnection
        except EngineError as e:
            log.error("picture settings refused: %s", e)

    async def apply_look(self):
        """Compiles the preset with the contrast and the saturation (off the event loop, and
        once: kept in a cache), then hands it to the engine. Turning a dial asks for several
        looks in a row: only the last one is sent."""
        self._look_version += 1
        version = self._look_version
        async with self._look_lock:
            if version != self._look_version:
                return  # a newer look is waiting for the lock
            s, preset = self.settings, self.preset()
            try:
                compiled = await asyncio.to_thread(looks.compiled_look, preset, s.contrast, s.saturation)
            except (looks.PresetError, OSError) as e:
                log.error("cannot compile the preset %s: %s", preset.id, e)
                self.show_message(f"Preset illisible : {preset.name}", warning=True)
                return
            if version != self._look_version:
                return
            try:
                # "Simuler le grain" off: the look without its grain.
                params = looks.engine_look(preset, grain=s.grain)
                await self.engine.call("look", lut=compiled.lut, preview=compiled.preview, **params)
            except ConnectionError:
                return  # pushed again on reconnection
            except EngineError as e:
                log.error("look refused: %s", e)
                return
            self._look = compiled
            self.redraw()

    async def configure_still(self, **config):
        """Photo settings of the engine for a while (a timelapse's smaller frames);
        apply_still_config() gives the user's back."""
        await self._configure(config)

    async def apply_still_config(self):
        await self._configure(picture.engine_config(self.settings))

    async def _configure(self, config):
        """The engine refuses photo settings while a photo is being taken ("busy"): they
        are sent again until it is over, unless newer ones replace them meanwhile."""
        self._still_version += 1
        version, waited = self._still_version, False
        while version == self._still_version:
            try:
                await self.engine.call("configure", **config)
                if waited:
                    log.info("still settings applied after the capture")
                return
            except ConnectionError:
                return  # pushed again on reconnection
            except EngineError as e:
                if e.code != "busy":
                    log.warning("still settings refused: %s", e)
                    self.show_message("Réglage refusé", warning=True)
                    return
            if not waited:
                log.info("still settings wait for the capture in progress")
                waited = True
            await asyncio.sleep(0.25)

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
        histogram, histogram_rgb = await asyncio.to_thread(hud.load_review, path)
        if shot is not self.last_shot or hud is not self.hud:
            return  # another photo was taken meanwhile, or the screen changed
        info = shot.name
        if shot.exposure_us:
            info += f"   {exp.format_shutter(shot.exposure_us / 1e6)}   ISO {round(shot.iso)}"
        self.review = Review(
            path=path, info=info, histogram=tuple(histogram), histogram_rgb=tuple(tuple(c) for c in histogram_rgb)
        )
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
        # The screen asleep (a long capture): a press only wakes it. The release of the
        # shutter still counts (it ends a bulb), and the web interface acts without waking it.
        if action not in ("shutter_release", "remote_shutter"):
            self._last_input = time.monotonic()
            if not self.screen_on:
                self.set_screen(True)
                return
        if action == "shutter_release":  # nothing else: it ends no review, closes no menu
            self.drive.release()
            return
        remote = action == "remote_shutter"  # the web interface's: pressed, never held
        if remote:
            action = "shutter"
        if self.review:
            # Any key ends the review; the shutter also shoots: never miss a photo.
            self.end_review()
            if action != "shutter":
                return
        if self.playback and self.on_playback_action(action):
            return
        if self.menu and self.on_menu_action(action):
            return
        if action == "play":
            self.open_playback()
            return
        if action in ("fn1", "fn2"):  # the action they are given in the menu
            action = getattr(s, action)
        if action in ("q", "menu") and not (action == "menu" and self.loupe > 1):
            looks.refresh()  # a preset copied to the camera shows up in the menu
        if action == "q":
            self.menu = Menu(s, quick=True)
            self.redraw()
            return
        if action == "menu":  # leaves the loupe first, as the camera's MENU button goes back
            if self.loupe > 1:
                self.loupe = 1
                self.spawn(self.apply_loupe())
            else:
                self.menu = Menu(s, quick=False)
                self.redraw()
            return
        if action == "shutter":
            self.drive.shutter(remote=remote)
            return
        if action in EXPOSURE_ACTIONS and (self.drive.freezes_exposure or self.exposing):
            self.show_message("Pose en cours" if self.exposing else "Bracketing en cours", warning=True)
            return
        if action == "drive":
            self.drive.stop()
            s.drive = _next(drives.MODES, s.drive) if s.drive in drives.MODES else drives.MODES[0]
            self.show_message(drives.LABELS[s.drive])
            self.save_soon()
            return
        if action == "strategy":
            self.spawn(self.set_strategy("zsl" if s.strategy == "switch" else "switch"))
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
        if action in ("front_dial_plus", "front_dial_minus"):
            # The speed when it is manual (S, M), else the exposure compensation (P, A), as
            # in docs/hardware.md §2. Turning a dial never changes the mode: pressing it does.
            step = +1 if action == "front_dial_plus" else -1
            if e.shutter is None:
                e.step_ev(step)
            else:
                e.step_shutter(-step, self.measured_shutter(), self.slowest_shutter)  # plus: faster
        elif action in ("rear_dial_plus", "rear_dial_minus"):
            # The ISO when it is manual (A, M), else the exposure compensation (P, S).
            step = +1 if action == "rear_dial_plus" else -1
            if e.iso is None:
                e.step_ev(step)
            else:
                e.step_iso(step, self.measured_iso())
        elif action == "shutter_auto":
            e.toggle_shutter_auto(self.measured_shutter())
        elif action == "iso_auto":
            e.toggle_iso_auto(self.measured_iso())
        elif action == "awb_lock":
            if not e.lock_awb(self.stats.get("colour_gains")):
                self.show_message("Pas de mesure de balance des blancs", warning=True)
                return
            self.show_message("Balance des blancs mémorisée")
        elif action == "disp":
            s.display = DISPLAY_MODES[(DISPLAY_MODES.index(s.display) + 1) % len(DISPLAY_MODES)]
        elif action == "grid":
            s.grid = not s.grid
            self.show_message("Grille" if s.grid else "Grille masquée")
        elif action == "raw":
            s.formats = ["jpeg"] if "dng" in s.formats else ["jpeg", "dng"]
            self.show_message("RAW + JPEG" if "dng" in s.formats else "JPEG seul")
        elif action == "peaking":
            s.peaking = not s.peaking
            self.show_message("Peaking" if s.peaking else "Peaking coupé")
        elif action == "zebra":
            s.zebra = not s.zebra
            self.show_message("Zébras" if s.zebra else "Zébras coupés")
        else:
            log.warning("unknown action %s", action)
            return
        if action in EXPOSURE_ACTIONS:
            self.spawn(self.apply_exposure())
        if action in FOCUS_AID_ACTIONS:
            self.spawn(self.apply_focus_aids())
        self.save_soon()
        self.redraw()

    def on_menu_action(self, action):
        """While a menu is open: the dials move in it. Returns True if handled."""
        if action in ("q", "menu"):
            # Each button closes its menu; Q goes from the full menu to the quick one.
            self.menu = Menu(self.settings, quick=True) if action == "q" and not self.menu.quick else None
        elif action == "shutter":
            self.menu = None
            return False  # and shoot
        elif action in ("rear_dial_plus", "rear_dial_minus"):  # rear dial: choose a line
            self.menu.move(-1 if action == "rear_dial_plus" else +1)
        elif action in ("front_dial_plus", "front_dial_minus"):  # front dial: change the value
            self.change_setting(+1 if action == "front_dial_plus" else -1)
        # Any other key does nothing while a menu is open.
        self.redraw()
        return True

    # --- Playback (playback.py)

    def open_playback(self):
        playback = Playback(self.storage.root)
        if not playback.photos:
            self.show_message("Aucune photo sur la carte")
            return
        self.playback, self._shown = playback, None
        self._delete_armed_until = 0.0
        self.spawn(self.load_shown())
        self.redraw()

    def close_playback(self):
        self.stop_video()
        self.playback = self._shown = None
        self.redraw()

    def on_playback_action(self, action):
        """While in playback. Returns True if handled (the shutter leaves and shoots)."""
        p = self.playback
        if action in ("play", "menu"):
            self.close_playback()
            return True
        if action == "shutter":
            self.close_playback()
            return False  # and shoot: never miss a photo
        if action == "q":
            self.delete_shown()
            return True
        self._delete_armed_until = 0.0
        if p.video and action == "loupe":  # play, pause, play again
            self.toggle_video()
        elif action == "loupe":
            p.cycle_zoom()
        elif action == "disp":
            s = self.settings
            s.display = DISPLAY_MODES[(DISPLAY_MODES.index(s.display) + 1) % len(DISPLAY_MODES)]
            self.save_soon()
        elif p.scale and action in LOUPE_MOVES:
            p.pan(*LOUPE_MOVES[action], self.playback_zoom())
        elif action in PLAYBACK_STEPS:
            if p.step(PLAYBACK_STEPS[action]):
                self.stop_video()
                self.spawn(self.load_shown())
        self.redraw()
        return True

    def delete_shown(self):
        """Q once asks, Q again within 3 s deletes the shot (JPEG, DNG, base copy)."""
        p = self.playback
        name = os.path.basename(p.current)[:-4]
        now = time.monotonic()
        if now > self._delete_armed_until:
            self._delete_armed_until = now + DELETE_CONFIRM_SECONDS
            self.show_message(f"Supprimer {name} ? Q pour confirmer", warning=True)
            return
        self._delete_armed_until = 0.0
        self.stop_video()
        try:
            removed = p.delete_current()
        except OSError as e:
            log.error("cannot delete %s: %s", name, e)
            self.show_message("Suppression impossible", warning=True)
            return
        log.info("deleted %s", " ".join(os.path.basename(f) for f in removed))
        if not p.photos:
            self.close_playback()
            self.show_message("Plus aucune photo sur la carte")
            return
        self.show_message(f"{name} supprimée")
        self.spawn(self.load_shown())

    async def load_shown(self):
        """Decodes the photo shown in playback, off the event loop (~0.3 s); for a video, its
        first frame (made once with ffmpeg, then cached) and what it is."""
        p, hud = self.playback, self.hud
        path = p.current if p else None
        if not path:
            return

        def load():
            if p.video:
                info, size, _ = describe_video(path)
                image = poster(path) or ""
            else:
                (info, size), image = describe(path), path
            luma, rgb = hud.load_review(image) if hud and image else ((), ())
            return Review(path, info, tuple(luma), tuple(tuple(c) for c in rgb), size, image)

        shown = await asyncio.to_thread(load)
        if self.playback is p and p.current == path:  # not moved on meanwhile
            self._shown = shown
            self.redraw()

    # --- Videos in playback (the engine plays them: video_player.cpp)

    def toggle_video(self):
        v = self.video
        if v is None or v.ended:
            self.video = Video(self.playback.current)
            self.spawn(self._play_video(self.video))
        else:
            v.paused = not v.paused
            self.spawn(self._video_call("video_pause", paused=v.paused))
        self.redraw()

    async def _play_video(self, video):
        try:
            reply = await self.engine.call("video_play", path=video.path)
            video.duration = reply.get("video", {}).get("duration", 0)
        except (EngineError, ConnectionError, TimeoutError) as e:
            log.error("cannot play %s: %s", video.path, e)
            if self.video is video:
                self.video = None
                self.show_message("Lecture impossible", warning=True)
        self.redraw()

    def stop_video(self):
        if self.video:
            self.video = None
            self.spawn(self._video_call("video_stop"))

    async def _video_call(self, command, **params):
        try:
            await self.engine.call(command, **params)
        except (EngineError, ConnectionError, TimeoutError) as e:
            log.warning("%s: %s", command, e)

    # --- The screen asleep during long captures

    @property
    def long_capture(self):
        """A capture that lasts (an interval, a timelapse, a bulb, a long exposure): no
        need for the screen meanwhile."""
        return (self.drive.active and self.drive.running in LONG_CAPTURES) or self.exposing is not None

    def check_screen(self, now=None):
        """Off after `screen_off` seconds without a press during a long capture; on again
        when it is over."""
        now = time.monotonic() if now is None else now
        delay = self.settings.screen_off
        if self.screen_on and delay and self.long_capture and now - self._last_input >= delay:
            self.set_screen(False)
        elif not self.screen_on and not self.long_capture:
            self.set_screen(True)

    def set_screen(self, on):
        if on == self.screen_on:
            return
        self.screen_on = on
        log.info("screen %s", "on" if on else "off (long capture)")
        self.spawn(self._video_call("display_power", on=on))
        if on:
            self._last_input = time.monotonic()
            self.redraw()

    async def screen_loop(self):
        while True:
            await asyncio.sleep(1.0)
            self.check_screen()

    def playback_zoom(self):
        """The playback loupe's magnification against the fitted photo."""
        p, shown = self.playback, self._shown
        if not p.scale or not shown or not shown.size:
            return 1.0
        _, _, w, h = getattr(self.hud, "rect", (0, 0, 1440, 1080))
        return p.zoom(min(w / shown.size[0], h / shown.size[1]))

    def change_setting(self, steps):
        """Turns the value of the chosen menu line, and tells the engine."""
        s, e, menu = self.settings, self.settings.exposure, self.menu
        item = menu.item
        if item.attr == "exposure.kelvin":  # from another balance, starts at the measured temperature
            e.step_kelvin(steps, self.stats.get("colour_temp"))
        else:
            value = menu.next_value(steps)
            if value == menu.get(item.attr):
                return
            if item.attr == "strategy":  # kept only if the engine accepts it
                self.spawn(self.set_strategy(value))
                return
            if item.attr == "exposure.awb" and value == "Locked":
                if not e.lock_awb(self.stats.get("colour_gains")):
                    self.show_message("Pas de mesure de balance des blancs", warning=True)
                    return
            else:
                menu.set(item.attr, value)
            if item.attr == "lens":
                s.aperture = None  # it belonged to the previous lens
            if item.attr == "drive":
                self.drive.stop()  # the sequence of the previous mode
        if item.attr == "preset":
            self.spawn(self.apply_exposure())  # its exposure shift
        if item.attr == "lens_shading":
            self.show_message("Correction de l'ombrage" if s.lens_shading else "Sans correction de l'ombrage")
        if item.push == "exposure":
            self.spawn(self.apply_exposure())
        elif item.push == "look":
            self.spawn(self.apply_look())
        elif item.push == "picture":
            self.spawn(self.apply_picture())
        elif item.push == "config":
            self.spawn(self.apply_still_config())
        elif item.push == "focus":
            self.spawn(self.apply_focus_aids())
        self.save_soon()

    async def capture(self, quiet=False, stack=None, formats=None, keep_base=None):
        """One photo: its capture id, or None (why in self.capture_error). quiet: no
        "busy" message (a sequence waits for the engine). stack: frames added up into
        one (the engine's "stack": the bulb). formats, keep_base: instead of the settings'
        (a timelapse's frames)."""
        async with self._capture_lock:  # two quick presses must not get the same name
            self.capture_error = None
            if not self.engine.connected:
                self.capture_error = "unavailable"
                self.show_message("Moteur indisponible", warning=True)
                return None
            base, folder, number = self.storage.next_photo()
            s, preset = self.settings, self.preset()
            formats = formats or s.formats
            keep_base = s.keep_base if keep_base is None else keep_base
            extra = {"base_jpeg": Storage.base_copy(base)} if keep_base and "jpeg" in formats else {}
            if stack:
                extra["stack"] = stack
            try:
                reply = await self.engine.call(
                    "capture",
                    path=base,
                    formats=formats,
                    dng_compression=s.dng_compression,
                    quality=s.jpeg_quality,
                    exif=lenses.lens_exif(self.lens(), s.aperture) + [f"IFD0.Software=pycam, {preset.name}"],
                    **extra,
                )
            except EngineError as e:
                self.capture_error = e.code
                if e.code != "busy":
                    log.error("capture refused: %s", e)
                    self.show_message("Déclenchement refusé", warning=True)
                elif not quiet:
                    self.show_message("Occupé…", warning=True)
                return None
            except ConnectionError:
                self.capture_error = "unavailable"
                self.show_message("Moteur indisponible", warning=True)
                return None
            self.storage.commit(folder, number)
            self.save_now()  # never give the same number twice, even after a crash
            self.writing += 1
            self.loupe = 1  # the engine leaves the loupe to shoot the full frame
            self.last_shot = Shot(reply.get("capture_id"), os.path.basename(base))
            self.last_capture_path = base
            self.show_message(os.path.basename(base))
            return self.last_shot.capture_id

    async def wait_captured(self, capture_id, timeout=None):
        """Until the engine has taken that photo ("captured"): True, or False if it failed
        or took too long. The event may have come already."""
        if capture_id in self._captured:
            return self._captured[capture_id]
        future = self._capture_waits.setdefault(capture_id, asyncio.get_running_loop().create_future())
        if timeout is None:
            timeout = CAPTURED_TIMEOUT + max(self.measured_shutter(), self.settings.exposure.shutter or 0)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            log.warning("capture %s: not taken after %.0f s", capture_id, timeout)
            return False
        finally:
            self._capture_waits.pop(capture_id, None)

    async def make_timelapse(self, frames):
        """The video of a timelapse's frames, next to them (timelapse.py), in the background."""
        s = self.settings
        out = os.path.join(os.path.dirname(frames[0]), timelapse.video_name(frames[0]))
        for _ in range(600):  # the last frames may still be being written
            if all(os.path.exists(f) for f in frames):
                break
            await asyncio.sleep(0.1)
        frames = [f for f in frames if os.path.exists(f)]

        def progress(fraction):
            self.task_status = f"VIDÉO {round(fraction * 100)} %"
            self.redraw()

        progress(0)
        try:
            ok = await timelapse.assemble(frames, out, s.timelapse_fps, s.timelapse_aspect, s.timelapse_deflicker, progress)
        finally:
            self.task_status = ""
            self.redraw()
        if ok:
            log.info("timelapse: %s, %d frames", out, len(frames))
            self.show_message(f"Vidéo : {os.path.basename(out)}")
        else:
            self.show_message("Vidéo impossible", warning=True)

    async def cancel_capture(self):
        """Abandons the exposure in progress (nothing is written)."""
        try:
            await self.engine.call("capture_cancel")
        except (EngineError, ConnectionError) as e:
            log.warning("cannot cancel the capture: %s", e)

    async def stop_capture(self):
        """Ends a bulb with the second being exposed."""
        try:
            await self.engine.call("capture_stop")
        except (EngineError, ConnectionError) as e:
            log.warning("cannot stop the capture: %s", e)

    def _end_exposure(self, capture_id):
        if self.exposing and self.exposing.capture_id == capture_id:
            self.exposing = None
            if self._exposure_deferred:
                self._exposure_deferred = False
                self.spawn(self.apply_exposure())
            self.redraw()

    async def _exposure_ticker(self):
        """No viewfinder frame, so no stats, during the exposure: the timer has its own beat."""
        exposing = self.exposing
        while self.exposing is exposing and exposing is not None:
            self.redraw()
            await asyncio.sleep(0.5)

    def _capture_taken(self, capture_id, ok):
        self._captured[capture_id] = ok
        while len(self._captured) > 32:
            self._captured.popitem(last=False)
        future = self._capture_waits.get(capture_id)
        if future and not future.done():
            future.set_result(ok)

    async def set_strategy(self, new):
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

    # --- For the web interface (web.py)

    async def snapshot(self, path, timeout=3.0):
        """The next viewfinder frame shown, as a JPEG at `path` (aids and look included, no
        HUD); False if the engine could not (no screen: it is taken on the way to it)."""
        future = self._snapshots.get(path)
        if future is None:
            future = self._snapshots[path] = asyncio.get_running_loop().create_future()
            try:
                await self.engine.call("snapshot", path=path)
            except (EngineError, ConnectionError):
                self._snapshots.pop(path, None)
                return False
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._snapshots.pop(path, None)
            return False

    def set_preset(self, preset_id):
        """As if chosen in the menu; False if there is no such preset."""
        if preset_id not in looks.registry():
            return False
        self.settings.preset = preset_id
        self.spawn(self.apply_exposure())  # its exposure shift, its film balance
        self.spawn(self.apply_look())
        self.save_soon()
        self.redraw()
        return True

    def set_grain(self, on):
        self.settings.grain = bool(on)
        self.spawn(self.apply_look())
        self.save_soon()
        self.redraw()

    def preset(self):
        """The chosen preset; the default one if it is gone (a deleted folder)."""
        presets = looks.registry()
        return presets.get(self.settings.preset) or presets.get(looks.DEFAULT) or next(iter(presets.values()))

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
        if self.playback:
            p, shown = self.playback, self._shown
            if self.video and self.video.path == p.current:
                v = self.video
                return HudView(
                    display=s.display,
                    review_info=os.path.basename(v.path)[:-4],  # the name: the bar has the time
                    playback=p.position,
                    video_playing=True,
                    video_paused=v.paused or v.ended,
                    video_time=f"{exp.format_duration(v.position)} / {exp.format_duration(math.ceil(v.duration))}",
                    video_progress=min(1.0, v.position / v.duration) if v.duration else 0.0,
                    message=message,
                    warning=warning,
                )
            if not shown or shown.path != p.current:  # being decoded
                return HudView(display=s.display, review=p.current, playback=p.position, message=message, warning=warning)
            if p.video:  # its first frame, until LOUPE plays it
                return HudView(
                    display=s.display,
                    review=shown.image,
                    review_info=shown.info,
                    playback=p.position,
                    video_hint="LOUPE : lire",
                    message=message,
                    warning=warning,
                )
            return HudView(
                display=s.display,
                review=shown.path,
                review_info=shown.info,
                playback=p.position,
                loupe=self.playback_zoom(),
                loupe_center=p.center,
                loupe_label=f"{PIXEL_SCALES[p.scale] * 100:g} %" if p.scale else "",
                **self._histograms(shown.histogram, shown.histogram_rgb, live=False),
                message=message,
                warning=warning,
            )
        if self.review:
            # Nothing live in it: it is drawn once, not at every stats event.
            r = self.review
            return HudView(
                display=s.display,
                review=r.path,
                review_info=r.info,
                **self._histograms(r.histogram, r.histogram_rgb, live=False),
                message=message,
                warning=warning,
            )
        shutter = e.shutter if e.shutter is not None else self.measured_shutter()
        iso = e.iso if e.iso is not None else round(self.measured_iso())
        # The meter: always in M (but B: no duration); in the other modes, only when the
        # automatic exposure cannot reach its aim (out of range), as a warning. In M, from
        # the settings: the viewfinder may not run at them (long exposures).
        meter_value, meter_warning = None, False
        if e.mode == "M":
            stops = None if e.bulb else meter.offset(s.meter_k, self.stats, e.shutter * 1e6, e.iso / 100)
        else:
            stops = meter.offset(s.meter_k, self.stats)
        bias = self.preset().exposure_bias  # the preset wants this much: that is the aim
        if stops is not None:
            if e.mode == "M":
                meter_value = meter.to_thirds(stops - bias)
            elif abs(stops - e.ev - bias) >= 2 / 3:
                meter_value, meter_warning = meter.to_thirds(stops - e.ev - bias), True
        timer, timer_label = "", ""
        if self.exposing:
            x = self.exposing
            if x.total is not None:  # a stack counts its frames
                timer, timer_label = f"{x.frames}/{x.total}" if x.total else str(x.frames), x.label
            elif x.duration is None:  # the bulb counts up, from its frames once they come
                timer, timer_label = exp.format_duration(max(x.seconds, time.monotonic() - x.start)), x.label
            else:
                left = x.duration - (time.monotonic() - x.start)
                timer, timer_label = exp.format_duration(math.ceil(max(left, 0))), "POSE"
        return HudView(
            display=s.display,
            grid=s.grid and self.loupe == 1,  # the rule of thirds means nothing in the loupe
            mode=e.mode,
            metering=e.metering,
            shutter="B" if e.bulb else exp.format_shutter(shutter),
            shutter_auto=e.shutter is None,
            iso=f"ISO {iso}" if iso else "ISO --",
            iso_auto=e.iso is None,
            ev=exp.format_ev(e.ev) if e.mode != "M" else "",
            meter=meter_value,
            meter_warning=meter_warning,
            awb=e.awb_label(self.preset().white_balance),
            formats="RAW+JPEG" if "dng" in s.formats else "JPEG",
            lens=lenses.format_lens(self.lens(), s.aperture),
            shots=self.storage.shots_remaining(s.formats, s.dng_compression, s.keep_base),
            look=self.preset().short,
            zsl=s.strategy == "zsl",
            peaking=s.peaking_color if s.peaking else "",
            zebra=s.zebra,
            loupe=self.loupe,
            loupe_center=self.loupe_center,
            menu=self.menu.rows(self.stats) if self.menu else (),
            menu_title=self.menu.title if self.menu else "",
            menu_index=self.menu.index if self.menu else 0,
            writing=self.writing,
            drive=self.drive.status if self.drive.active else drives.short_label(s),
            drive_running=self.drive.active,
            countdown=self.drive.countdown,
            task=self.task_status,
            exposure_timer=timer,
            exposure_label=timer_label,
            viewfinder_auto=self.stats.get("viewfinder") == "auto",
            **self._histograms(self.stats.get("histogram", ()), self.stats.get("histogram_rgb", ())),
            message=message,
            warning=warning,
        )

    def _histograms(self, luma, rgb, live=True):
        """The one histogram the HUD draws: in the detailed display, RGB when chosen and known.
        Live, the engine measures the base: it is corrected with the look's curves of the
        greys, to show roughly what the JPEG will be (the review measures the JPEG itself)."""
        if self.settings.display != "detailed":
            return {}
        curves = self._look.curves if live and self._look else None
        if curves and luma:
            luma = looks.remap_histogram(luma, curves["luma"])
        if self.settings.histogram == "rgb" and len(rgb) == 3:
            if curves:
                rgb = (
                    [luma] * 3
                    if self._look.monochrome
                    else [looks.remap_histogram(c, curves[name]) for c, name in zip(rgb, ("red", "green", "blue"))]
                )
            return {"histogram_rgb": tuple(tuple(c) for c in rgb)}
        return {"histogram": tuple(luma)}

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
            if not self.screen_on:  # asleep: nothing to draw (~0.1 s of CPU saved each time)
                shown = None
                continue
            if hud is not None:
                view = self.view()
                if view != shown:
                    try:
                        await asyncio.to_thread(hud.draw, view)  # ~0.1 s at 1080p: off the event loop
                        await self.engine.call("hud_commit")
                        shown = view
                    except (EngineError, ConnectionError, TimeoutError, ValueError) as e:  # ValueError: HUD closed meanwhile
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
