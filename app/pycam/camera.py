"""The camera: settings, actions and HUD, driving the engine (docs/architecture.md §5)."""

import asyncio
import json
import logging
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from . import __version__
from . import drive as drives
from . import exposure as exp
from . import lenses, looks, meter, picture, timelapse, video
from .clock import Clock
from .engine_client import EngineError, call_blocking
from .menu import Item, Menu
from .playback import PIXEL_SCALES, Playback, describe, describe_video, extract_frame, list_photos, poster, prune_caches
from .settings import DISPLAY_MODES, save_settings
from .storage import Storage
from .view import HudView, visible

log = logging.getLogger(__name__)

HUD_MIN_INTERVAL = 0.2  # at most 5 HUD renders per second
APP_RESTART = 30  # seconds before an application that failed starts again
HISTOGRAM_LEVELS = 64  # the histograms as the HUD draws them: a change below redraws nothing
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


def app_label(app):
    """Its name in the Applications menu: "(expérimental)" while it is one."""
    return f"{app.title} (expérimental)" if getattr(app, "experimental", False) else app.title


@dataclass
class Recording:
    """A video being recorded (by the engine's Recorder)."""

    path: str
    start: float  # time.monotonic()
    duration: float = 0  # seconds recorded, from the engine
    bytes: int = 0
    seconds_left: float | None = None  # on the card, once it runs short (video.WARN_SECONDS)
    card_full: bool = False  # stopped before the card is full


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
        self.menu_stack = []  # the menus it was opened from (MENU goes back)
        self.apps = []  # the applications (apps/), started with the camera
        self.updates = None  # updates.Updates, on a camera installed from a release
        self.wifi = None  # wifi.Wifi, with NetworkManager
        self.web_access = None  # web_access.Access, set by the web interface
        self.clock = Clock()
        self.ready_path = None  # written once the camera works, for the updates (updater.py)
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
        self.recording = None  # Recording, in video mode
        self.video_lock = None  # (seconds, ISO): the video's exposure locked (AE-L, the S key)
        self._sent_picture = None  # the ISP controls last sent (picture.controls)
        self._video_setup_deferred = False  # video settings changed while recording
        self.screen_on = True  # off during long captures, to save power (check_screen)
        self._last_input = time.monotonic()
        self._exposure_deferred = False  # an exposure change waits for the end of the exposure
        self._save_handle = None
        self._tasks = set()
        self._snapshots = {}  # path -> future, until the engine has written it

    async def run(self, inputs):
        services = [s for s in (self.updates, self.wifi) if s]
        apps = [self._supervise(app) for app in self.apps + services]
        self.spawn(self._prune_caches())
        await asyncio.gather(self.engine.run(), inputs.run(), self.hud_loop(), self.screen_loop(), *apps)

    async def _supervise(self, app):
        """An application's background work: an error stops it, not the camera (a sequence
        running would be lost); it starts again a little later."""
        while True:
            try:
                await app.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("application %s failed; started again in %d s", app.title, APP_RESTART)
                await asyncio.sleep(APP_RESTART)

    async def _prune_caches(self):
        """Once the camera is up: the thumbnails and first frames of shots gone, removed."""
        await asyncio.sleep(60)
        removed = await asyncio.to_thread(prune_caches, self.storage.root)
        if removed:
            log.info("caches: %d stale thumbnails and first frames removed", removed)

    def files_in_use(self):
        """Files an application still needs (a photo waiting to be shared): kept on the card."""
        return set().union(*(app.files_in_use() for app in self.apps))

    def shutdown(self):
        """Settings saved, and the engine's default HUD back on the screen."""
        self.save_now()
        if self.ready_path:
            try:
                os.remove(self.ready_path)
            except OSError:
                pass
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
        self._sent_picture = None  # the engine may be a new one
        await self._take_over_recording()
        if self.settings.mode == "video" and not self.recording:
            await self.apply_mode()
        await self.apply_exposure()
        await self.apply_picture()
        await self.apply_look()
        await self.apply_focus_aids()
        self.loupe = 1
        if self.settings.mode != "video":  # no loupe in video
            await self.apply_loupe()
        await self.setup_hud()
        self._mark_ready()

    def _mark_ready(self):
        """The engine answered and everything was pushed: this version works. The updates
        wait for it after an installation, else they go back (updater.py)."""
        if not self.ready_path:
            return
        tmp = self.ready_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": __version__, "pid": os.getpid()}, f)
            os.replace(tmp, self.ready_path)
        except OSError as e:
            log.warning("cannot write %s: %s", self.ready_path, e)

    async def _take_over_recording(self):
        """The application restarted (an error) while the engine was recording: that
        recording is followed again (its time, the card's space, the shutter to stop it)
        rather than forgotten. A new engine records nothing."""
        try:
            running = (await self.engine.call("get_state")).get("state", {}).get("recording")
        except EngineError:
            running = None
        if not running or not running.get("path"):
            self.recording = None
            return
        duration = running.get("duration", 0)
        self.recording = Recording(running["path"], time.monotonic() - duration, duration, running.get("bytes", 0))
        self.settings.mode = "video"
        log.warning("recording %s still running in the engine: taken over", os.path.basename(running["path"]))

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
            if e.mode == "P" and self.settings.mode == "photo" and not self.drive.freezes_exposure:
                # Fully automatic: learn the meter constant (saved with the settings). Not
                # in video: its exposure has no preset's shift, and may be locked.
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
            self.wake_apps()
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
        elif kind == "record_progress":
            if self.recording and self.recording.path == msg.get("path"):
                self.recording.duration = msg.get("duration", 0)
                self.recording.bytes = msg.get("bytes", 0)
                self.check_card_space()
                self.redraw()
        elif kind == "record_done":
            self._recording_done(msg)
        elif kind == "record_error":
            log.error("recording %s: %s", msg.get("path"), msg.get("error"))
            self.show_message("Erreur d'enregistrement vidéo", warning=True)
            self.spawn(self.stop_recording())
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
            if self.settings.mode == "video":
                controls = video.controls(self.settings, self.video_lock)
            else:
                preset = self.preset()
                controls = self.settings.exposure.controls(preset.exposure_bias, preset.white_balance)
            await self.engine.call("set_controls", controls=controls)
        except ConnectionError:
            pass  # pushed again on reconnection
        except EngineError as e:
            log.error("exposure refused: %s", e)

    def _picture_controls(self):
        s = self.settings
        return picture.controls(s, video=s.mode == "video", monochrome=bool(self._look and self._look.monochrome))

    async def apply_picture(self):
        controls = self._picture_controls()
        self._sent_picture = controls
        try:
            await self.engine.call("set_controls", controls=controls)
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
        if self.settings.mode == "video":
            await self.refresh_video_picture()  # the video's contrast, saturation, black and white

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
            if self.recording:
                self.show_message("Enregistrement en cours", warning=True)
                return
            self.open_playback()
            return
        if action in ("fn1", "fn2"):  # the action they are given in the menu
            action = getattr(s, action)
        if action == "video":
            if self.set_mode("photo" if s.mode == "video" else "video"):
                self.show_message("Vidéo" if s.mode == "video" else "Photo")
            else:
                self.show_message("Enregistrement en cours", warning=True)
            return
        if action in ("q", "menu") and not (action == "menu" and self.loupe > 1):
            looks.refresh()  # a preset copied to the camera shows up in the menu
        if action == "q":
            self.menu, self.menu_stack = Menu(s, quick=True), []
            self.redraw()
            return
        if action == "menu":  # leaves the loupe first, as the camera's MENU button goes back
            if self.loupe > 1:
                self.loupe = 1
                self.spawn(self.apply_loupe())
            else:
                self.menu, self.menu_stack = Menu(s, quick=False, context=self), []
                self.redraw()
            return
        if s.mode == "video" and self.on_video_action(action):
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
        if action == "menu" and self.menu_stack:  # back to the menu this one was opened from
            self.menu = self.menu_stack.pop()
        elif action in ("q", "menu"):
            # Each button closes its menu; Q goes from the full menu to the quick one.
            top = self.menu_stack[0] if self.menu_stack else self.menu
            self.menu_stack = []
            self.menu = Menu(self.settings, quick=True) if action == "q" and not top.quick else None
        elif action == "shutter":
            self.menu, self.menu_stack = None, []
            return False  # and shoot
        elif action in ("rear_dial_plus", "rear_dial_minus"):  # rear dial: choose a line
            self.menu.move(-1 if action == "rear_dial_plus" else +1)
        elif action in ("front_dial_plus", "front_dial_minus"):  # front dial: change the value
            item = self.menu.item
            if item.kind == "open":
                if action == "front_dial_plus":
                    self.menu_stack.append(self.menu)
                    self.menu = item.target(self)
            elif item.kind in ("run", "toggle"):
                if action == "front_dial_plus" or item.kind == "toggle":
                    item.target(self)
            elif item.attr:  # not a line that only informs (a menu with nothing to choose)
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

    def photo_menu(self, position=0.0):
        """MENU in playback: what can be done with the shot shown: a video's image at
        `position` extracted as a photo; the applications' (share a photo...)."""
        path = self.playback.current
        items = [item for app in self.apps for item in app.photo_items(self, path)]
        if self.playback.video:
            at = f"à {exp.format_duration(int(position))}.{int(position * 10) % 10}"
            items.insert(0, Item("Extraire cette image", kind="run", target=lambda c: c.extract_frame(path, position), text=lambda c: at))
        if not items:
            items = [Item("Rien à faire de cette prise", kind="info")]
        return Menu(self.settings, items=items, title=os.path.basename(path)[:-4], context=self)

    def extract_frame(self, video_path, position):
        """The video's image at `position` as a photo (the next number), back in playback."""
        self.menu, self.menu_stack = None, []
        self.spawn(self._extract_frame(video_path, position))

    async def _extract_frame(self, video_path, position):
        base, folder, number = self.storage.next_photo()
        out = base + ".JPG"
        self.show_message("Extraction…")
        if not await asyncio.to_thread(extract_frame, video_path, position, out):
            self.show_message("Extraction impossible", warning=True)
            return
        self.storage.commit(folder, number)
        self.save_now()
        log.info("frame of %s at %.1f s: %s", os.path.basename(video_path), position, out)
        p = self.playback
        if p:  # in the list, the video still shown
            current = p.current
            p.photos = list_photos(self.storage.root, videos=True)
            p.index = p.photos.index(current) if current in p.photos else len(p.photos) - 1
        self.show_message(f"{os.path.basename(base)} extraite")
        self.wake_apps()

    def close_playback(self):
        self.stop_video()
        self.playback = self._shown = None
        self.redraw()

    def on_playback_action(self, action):
        """While in playback. Returns True if handled (the shutter leaves and shoots)."""
        p = self.playback
        if action == "play":
            self.close_playback()
            return True
        if action == "shutter":
            self.close_playback()
            return False  # and shoot: never miss a photo
        if self.menu:  # the photo's menu: MENU goes back, then closes it, as Q does
            if action == "q" or (action == "menu" and not self.menu_stack):
                self.menu, self.menu_stack = None, []
            else:
                self.on_menu_action(action)
            self.redraw()
            return True
        if action == "menu":
            v = self.video  # where a video is: its image can be extracted
            position = max(0.0, v.position - (0.1 if v.ended else 0)) if v and v.path == p.current else 0.0
            self.stop_video()
            self.menu, self.menu_stack = self.photo_menu(position), []
            self.redraw()
            return True
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

    # --- Applications (apps/)

    def apps_menu(self):
        """The Applications menu: one line per application, opening its own menu."""
        items = [Item(app_label(app), kind="open", target=app.menu, text=app.summary) for app in self.apps]
        if not items:
            items = [Item("Aucune application", kind="info")]
        return Menu(self.settings, items=items, title="Applications", context=self)

    # --- The web interface's access (web_access.py)

    def show_pairing_code(self, code, seconds):
        """A browser asks to be paired: the code, on the screen (woken up if asleep)."""
        self.set_screen(True)
        self.show_message(f"Code du site : {code[:3]} {code[3:]}", seconds=seconds)

    def forget_browsers(self):
        """MENU › Réseau: no browser paired any more."""
        if self.web_access:
            count = self.web_access.forget()
            self.show_message(f"{count} navigateur{'s' if count > 1 else ''} oublié{'s' if count > 1 else ''}" if count else "Aucun navigateur")

    def wake_apps(self):
        """New files on the card, or an application's setting changed."""
        for app in self.apps:
            app.wake()

    def apps_hud(self):
        """What the applications at work show on the HUD ("CLOUD 40 %")."""
        shown = self.apps + ([self.wifi] if self.wifi else [])  # the access point, while open
        return " · ".join(text for text in (app.hud() for app in shown) if text)

    # --- Video mode (video.py; the engine's video_mode and Recorder)

    def set_mode(self, mode):
        """Photo or video; False while recording (or for no such mode)."""
        if mode not in ("photo", "video") or (self.recording and mode != self.settings.mode):
            return False
        if mode != self.settings.mode:
            self.settings.mode = mode
            self.menu = None
            self.loupe = 1
            self.video_lock = None
            self.spawn(self.apply_mode())
            self.save_soon()
            self.redraw()
        return True

    async def apply_mode(self, restart=False):
        """Tells the engine the mode, the video's settings, and the exposure that goes with it.
        The ISP's contrast and saturation first (the video's, or neutral for the photos): the
        screen's stream takes them only when the camera starts. restart: in video, the camera
        starts again even with the same settings, for that."""
        s = self.settings
        await self.apply_picture()
        try:
            if s.mode == "video":
                await self.engine.call("video_mode", on=True, restart=restart, **video.engine_setup(s))
            else:
                await self.engine.call("video_mode", on=False)
        except (EngineError, ConnectionError, TimeoutError) as e:
            log.warning("video mode refused: %s", e)
            self.show_message("Impossible pour l'instant", warning=True)
        await self.apply_exposure()

    async def refresh_video_picture(self):
        """In video, the picture changed (contrast, saturation, sharpness, a black and white
        preset): the recording would take it at once, the screen only when the camera starts
        again (~0.3 s). During a recording: after it, so that the take stays as it began."""
        if self._picture_controls() == self._sent_picture:
            return  # the grain, or a preset of the same colours: nothing for the ISP
        if self.recording:
            self._video_setup_deferred = True
            self.show_message("Appliqué après l'enregistrement")
            return
        await self.apply_mode(restart=True)

    def on_video_action(self, action):
        """The controls in video mode; True if handled (the others act as in photo)."""
        s, e = self.settings, self.settings.exposure
        if action == "shutter":
            self.spawn(self.stop_recording() if self.recording else self.start_recording())
            return True
        if action == "shutter_auto" or (self.video_lock and action == "iso_auto"):
            self.toggle_video_lock()
        elif self.video_lock and action in ("front_dial_plus", "front_dial_minus"):
            self.show_message("Exposition verrouillée : S pour la libérer", warning=True)
            return True
        elif self.video_lock and action in ("rear_dial_plus", "rear_dial_minus"):
            speed, iso = self.video_lock  # the locked exposure, brighter or darker
            index = exp.nearest_index(exp.ISO_VALUES, iso) + (1 if action == "rear_dial_plus" else -1)
            self.video_lock = (speed, exp.ISO_VALUES[max(0, min(len(exp.ISO_VALUES) - 1, index))])
        elif action in ("front_dial_plus", "front_dial_minus"):
            e.step_ev(+1 if action == "front_dial_plus" else -1)
        elif action in ("rear_dial_plus", "rear_dial_minus"):
            video.step_iso(s, +1 if action == "rear_dial_plus" else -1, self.measured_iso())
        elif action == "iso_auto":
            if s.video_iso:
                s.video_iso = 0
            else:
                video.step_iso(s, 0, self.measured_iso())
        elif action in ("loupe", "strategy", "raw", "drive"):
            self.show_message("Pas en vidéo", warning=True)
            return True
        else:
            return False
        self.spawn(self.apply_exposure())
        self.save_soon()
        self.redraw()
        return True

    def toggle_video_lock(self):
        """S in video: the exposure kept as it is now (speed and gain), for a take that
        must not change in brightness when the scene does; S again, automatic."""
        if self.video_lock:
            self.video_lock = None
            self.show_message("Exposition automatique")
            return
        speed = video.shutter(self.settings) or self.measured_shutter()
        iso = round(self.measured_iso())
        if not speed or not iso:
            self.show_message("Pas de mesure", warning=True)
            return
        self.video_lock = (speed, iso)
        self.show_message("Exposition verrouillée")

    def check_card_space(self):
        """While recording: warned a minute before the card is full, stopped 10 s before,
        so that the video can still be ended (a video that cannot be is lost whole)."""
        r = self.recording
        if not r or r.card_full:
            return
        left = video.seconds_left(self.settings, self.storage.free_bytes())
        if left <= video.STOP_SECONDS:
            r.card_full = True
            log.warning("card full: recording %s stopped", os.path.basename(r.path))
            self.show_message("Carte pleine : enregistrement arrêté", warning=True)
            self.spawn(self.stop_recording())
        elif left <= video.WARN_SECONDS:
            if r.seconds_left is None:
                self.show_message("Carte presque pleine", warning=True)
            r.seconds_left = left

    async def start_recording(self):
        async with self._capture_lock:
            if self.recording:
                return
            if video.seconds_left(self.settings, self.storage.free_bytes()) <= 2 * video.STOP_SECONDS:
                self.show_message("Carte pleine", warning=True)
                return
            base, folder, number = self.storage.next_photo()
            path = os.path.join(os.path.dirname(base), f"PYV_{number:04d}.MP4")
            try:
                await self.engine.call("record_start", path=path)
            except (EngineError, ConnectionError, TimeoutError) as e:
                log.error("cannot record: %s", e)
                self.show_message("Enregistrement impossible", warning=True)
                return
            self.storage.commit(folder, number)
            self.save_now()  # never give the same number twice
            self.recording = Recording(path, time.monotonic())
            log.info("recording %s", path)
            self.redraw()

    async def stop_recording(self):
        try:
            await self.engine.call("record_stop")  # then "record_done"
        except (EngineError, ConnectionError, TimeoutError) as e:
            log.error("cannot stop recording: %s", e)
            self.recording = None
            self.redraw()

    def _recording_done(self, msg):
        name = os.path.basename(msg.get("path", ""))[:-4]
        card_full = bool(self.recording and self.recording.card_full)
        self.recording = None
        if msg.get("error"):
            log.error("video %s: %s", name, msg["error"])
            self.show_message("Vidéo non enregistrée", warning=True)
        else:
            log.info("video %s: %.1f s, %d frames, %d bytes", name, msg.get("duration", 0), msg.get("frames", 0), msg.get("bytes", 0))
            text = f"{name} · {exp.format_duration(msg.get('duration', 0))}"
            if msg.get("video_duration", 0) > msg.get("duration", 0) + 0.5:  # a slow motion: as long as it plays
                text = f"{name} · {exp.format_duration(msg['video_duration'])} au ralenti"
            self.show_message(text + " · carte pleine" if card_full else text, warning=card_full)
        if self._video_setup_deferred:
            self._video_setup_deferred = False
            self.spawn(self.apply_mode(restart=True))
        self.wake_apps()
        self.redraw()

    def _video_view(self, message, warning):
        s, e = self.settings, self.settings.exposure
        recording, task = "", self.task_status or self.apps_hud()
        if self.recording:
            r = self.recording
            recording = exp.format_duration(r.duration or time.monotonic() - r.start)
            if r.seconds_left is not None:  # the card runs short: in sight
                task = f"CARTE PLEINE DANS {exp.format_duration(max(0, r.seconds_left - video.STOP_SECONDS))}"
        fixed = video.shutter(s)
        if self.video_lock:
            speed, iso = self.video_lock
        else:
            speed, iso = fixed or self.measured_shutter(), s.video_iso or exp.nominal_iso(self.measured_iso())
        # The meter, as a warning only: the exposure cannot reach the right one (at 180° in
        # full sun: an ND filter is wanted), or, locked, the scene has gone away from it.
        meter_value, meter_warning = None, False
        stops = meter.offset(s.meter_k, self.stats)
        if stops is not None:
            stops -= 0 if self.video_lock else e.ev
            if abs(stops) >= 2 / 3:
                meter_value, meter_warning = meter.to_thirds(stops), True
        return HudView(
            display=s.display,
            grid=s.grid,
            guide="" if s.guide == "none" else s.guide,
            video_mode=True,
            recording=recording,
            mode="V",
            metering=e.metering,
            shutter=exp.format_shutter(speed),
            shutter_auto=not (fixed or self.video_lock),
            iso=f"ISO {iso}" if iso else "ISO --",
            iso_auto=not (s.video_iso or self.video_lock),
            ev="AE-L" if self.video_lock else exp.format_ev(e.ev),
            meter=meter_value,
            meter_warning=meter_warning,
            awb=e.awb_label(),
            formats=video.label(s),
            shots=f"{video.minutes_left(s, self.storage.free_bytes())} min",
            peaking=s.peaking_color if s.peaking else "",
            zebra=s.zebra,
            menu=self.menu.rows(self.stats) if self.menu else (),
            menu_title=self.menu.title if self.menu else "",
            menu_index=self.menu.index if self.menu else 0,
            writing=self.writing,
            task=task,
            **self._histograms(self.stats.get("histogram", ()), self.stats.get("histogram_rgb", ()), live=False),
            message=message,
            warning=warning,
        )

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
            if item.attr in ("video_rate", "video_shutter", "video_resolution"):
                self.video_lock = None  # another speed, or another frame
            if item.attr == "video_resolution" and s.video_fps == 50 and s.video_resolution != "720p":
                s.video_fps, s.video_slowmo = 25, False  # 50 frames a second: 720p only
        if item.attr == "preset":
            self.spawn(self.apply_exposure())  # its exposure shift
        if item.attr == "lens_shading":
            self.show_message("Correction de l'ombrage" if s.lens_shading else "Sans correction de l'ombrage")
        if item.push == "exposure":
            self.spawn(self.apply_exposure())
        elif item.push == "look":
            self.spawn(self.apply_look())
        elif item.push == "picture":
            self.spawn(self.refresh_video_picture() if s.mode == "video" else self.apply_picture())
        elif item.push == "config":
            self.spawn(self.apply_still_config())
        elif item.push == "focus":
            self.spawn(self.apply_focus_aids())
        elif item.push == "mode":
            self.show_message("Vidéo" if s.mode == "video" else "Photo")
            self.spawn(self.apply_mode())
        elif item.push == "apps":
            self.wake_apps()
        elif item.push == "video":
            if self.recording:
                self._video_setup_deferred = True
                self.show_message("Appliqué après l'enregistrement")
            elif s.mode == "video":
                self.spawn(self.apply_mode())
        self.save_soon()

    async def capture(self, quiet=False, stack=None, formats=None, keep_base=None):
        """One photo: its capture id, or None (why in self.capture_error). quiet: no
        "busy" message (a sequence waits for the engine). stack: frames added up into
        one (the engine's "stack": the bulb). formats, keep_base: instead of the settings'
        (a timelapse's frames)."""
        async with self._capture_lock:  # two quick presses must not get the same name
            self.capture_error = None
            keep_base = self.settings.keep_base if keep_base is None else keep_base
            if not self.engine.connected:
                self.capture_error = "unavailable"
                self.show_message("Moteur indisponible", warning=True)
                return None
            s, preset = self.settings, self.preset()
            formats = formats or s.formats
            if self.storage.shots_remaining(formats, s.dng_compression, keep_base) <= 0:
                self.capture_error = "full"
                self.show_message("Carte pleine", warning=True)
                return None
            base, folder, number = self.storage.next_photo()
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
            self.wake_apps()
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
                    **self._menu_fields(),
                    video_hint="LOUPE : lire",
                    message=message,
                    warning=warning,
                )
            return HudView(
                display=s.display,
                review=shown.path,
                review_info=shown.info,
                playback=p.position,
                **self._menu_fields(),
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
        if s.mode == "video":
            return self._video_view(message, warning)
        shutter = e.shutter if e.shutter is not None else self.measured_shutter()
        iso = e.iso if e.iso is not None else exp.nominal_iso(self.measured_iso())
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
            guide="" if s.guide == "none" or self.loupe > 1 else s.guide,
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
            task=self.task_status or self.apps_hud(),
            exposure_timer=timer,
            exposure_label=timer_label,
            viewfinder_auto=self.stats.get("viewfinder") == "auto",
            **self._histograms(self.stats.get("histogram", ()), self.stats.get("histogram_rgb", ())),
            message=message,
            warning=warning,
        )

    def _menu_fields(self):
        if not self.menu:
            return {}
        return {"menu": self.menu.rows(self.stats), "menu_title": self.menu.title, "menu_index": self.menu.index}

    def _histograms(self, luma, rgb, live=True):
        """The one histogram the HUD draws: in the detailed display, RGB when chosen and known.
        Live, the engine measures the base: it is corrected with the look's curves of the
        greys, to show roughly what the JPEG will be (the review measures the JPEG itself).
        Scaled as the HUD scales it, in HISTOGRAM_LEVELS steps: the noise of each measure
        does not redraw it."""
        histograms = self._raw_histograms(luma, rgb, live)
        channels = histograms.get("histogram_rgb") or ((histograms["histogram"],) if histograms.get("histogram") else ())
        if not channels:
            return histograms
        peak = max((max(c[1:-1]) for c in channels), default=0) or max(max(c) for c in channels) or 1
        scaled = tuple(tuple(min(HISTOGRAM_LEVELS, round(HISTOGRAM_LEVELS * v / peak)) for v in c) for c in channels)
        return {"histogram_rgb": scaled} if "histogram_rgb" in histograms else {"histogram": scaled[0]}

    def _raw_histograms(self, luma, rgb, live):
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

    def show_message(self, text, warning=False, seconds=MESSAGE_SECONDS):
        self._message = (text, warning, time.monotonic() + seconds)
        asyncio.get_running_loop().call_later(seconds + 0.05, self.redraw)
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
                view = visible(self.view())
                if view != shown:
                    try:
                        await asyncio.to_thread(hud.draw, view)  # ~0.015 s at 1080p: off the event loop
                        await self.engine.call("hud_commit")
                        shown = view
                    except (EngineError, ConnectionError, TimeoutError, ValueError) as e:  # ValueError: HUD closed meanwhile
                        log.debug("HUD not shown: %s", e)
                    except Exception:  # a drawing that fails: the next view may not
                        log.exception("HUD not drawn")
                        await asyncio.sleep(1)
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
