"""Drive modes: what the shutter does (docs/architecture.md §5).

- single: one photo per press;
- burst: photos while the shutter is held, each as soon as the engine takes one (then the
  writing queue sets the pace), at least one and at most BURST_MAX;
- self2, self10: a countdown, then one photo;
- interval: one photo every `interval_seconds`, `interval_count` times (0: until stopped);
- bracket: 3 or 5 photos around the exposure measured, `bracket_step` thirds of a stop
  apart. The exposure is frozen (manual speed and gain) and the speed varies (the gain
  in S, where the speed is the user's): immediate and exact, where moving the
  compensation would wait for the automatic exposure. An automatic white balance is
  frozen too: the photos of a bracketing are to be merged.

The shutter pressed during a sequence stops it; the camera stays usable meanwhile.
"""

import asyncio
import logging
import math
import time

from . import exposure as exp

log = logging.getLogger(__name__)

MODES = ("single", "burst", "self2", "self10", "interval", "bracket")
LABELS = {
    "single": "Vue par vue",
    "burst": "Rafale",
    "self2": "Retardateur 2 s",
    "self10": "Retardateur 10 s",
    "interval": "Intervallomètre",
    "bracket": "Bracketing",
}
SELF_TIMERS = {"self2": 2, "self10": 10}
INTERVALS = (2, 5, 10, 30, 60, 300)  # seconds
COUNTS = (10, 25, 50, 100, 250, 0)  # 0: until stopped
BRACKET_SHOTS = (3, 5)
BRACKET_STEPS = (1, 2, 3)  # thirds of a stop
BURST_MAX = 20
BUSY_WAIT = 15.0  # seconds a sequence waits for the writing queue before giving up
MIN_TIME_US = 60  # the sensor's shortest exposure
MAX_GAIN = 16.0  # ISO 1600, the highest offered


def format_interval(seconds):
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def format_step(thirds):
    return {1: "1/3", 2: "2/3", 3: "1"}[thirds]


def short_label(settings):
    """The drive mode on the HUD; empty for one photo at a time."""
    mode = settings.drive
    if mode == "burst":
        return "RAFALE"
    if mode in SELF_TIMERS:
        return f"RET {SELF_TIMERS[mode]} s"
    if mode == "interval":
        return f"INT {format_interval(settings.interval_seconds)}"
    if mode == "bracket":
        return f"BKT {settings.bracket_shots} × {format_step(settings.bracket_step)}"
    return ""


def bracket_offsets(shots, thirds):
    """Stops from the measure, in the usual order: 0, -1, +1 (, -2, +2) steps."""
    steps = (0, -1, 1) if shots == 3 else (0, -1, 1, -2, 2)
    return [k * thirds / 3 for k in steps]


def bracket_exposure(time_us, gain, stops, slowest_us, vary="time"):
    """(exposure time, analogue gain) `stops` away from (time_us, gain): `vary` changes,
    what it cannot do (out of its range) goes to the other one."""
    factor = 2**stops
    if vary == "gain":
        new_gain = min(max(gain * factor, 1.0), MAX_GAIN)
        new_time = min(max(time_us * factor * gain / new_gain, MIN_TIME_US), slowest_us)
    else:
        new_time = min(max(time_us * factor, MIN_TIME_US), slowest_us)
        new_gain = min(max(gain * factor * time_us / new_time, 1.0), MAX_GAIN)
    return round(new_time), round(new_gain, 3)


class Drive:
    def __init__(self, camera):
        self.camera = camera
        self.task = None
        self.running = ""  # the mode of the sequence running
        self.frozen = False  # the bracketing holds the exposure
        self.status = ""  # shown on the HUD during a sequence
        self.countdown = 0  # seconds left on the self-timer
        self.held = False  # the shutter, for the burst
        self.sleep = asyncio.sleep  # the tests make time pass faster
        self.clock = time.monotonic

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    @property
    def freezes_exposure(self):
        """A bracketing is running: nothing else may set the exposure."""
        return self.frozen

    @property
    def quiet(self):
        """No review after each photo: it would hide the burst or the bracketing."""
        return self.active and self.running in ("burst", "bracket")

    def shutter(self):
        """The shutter pressed: one photo, or a sequence started, or the one running stopped."""
        if self.active:
            if self.running == "burst":  # pressed again before its last photo: it goes on
                self.held = True
                return
            self.stop()
            self.camera.show_message("Séquence arrêtée")
            return
        mode = self.camera.settings.drive
        if mode not in MODES or mode == "single":
            self.camera.spawn(self.camera.capture())
            return
        self.held = True
        self.running = mode
        sequence = {
            "burst": self._burst,
            "self2": self._self_timer,
            "self10": self._self_timer,
            "interval": self._interval,
            "bracket": self._bracket,
        }[mode]
        self.task = asyncio.get_running_loop().create_task(self._run(sequence(mode)))

    def release(self):
        self.held = False

    def stop(self):
        if self.active:
            self.task.cancel()

    async def _run(self, sequence):
        try:
            await sequence
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("drive sequence failed")
            self.camera.show_message("Séquence interrompue", warning=True)
        finally:
            self.status, self.countdown = "", 0
            self.camera.redraw()

    def _show(self, status, countdown=0):
        self.status, self.countdown = status, countdown
        self.camera.redraw()

    async def _shoot(self):
        """One photo, once the engine can take it (the writing queue may be full): its
        capture id, or None."""
        deadline = self.clock() + BUSY_WAIT
        while True:
            capture_id = await self.camera.capture(quiet=True)
            if capture_id is not None or self.camera.capture_error != "busy" or self.clock() > deadline:
                return capture_id
            await self.sleep(0.1)

    async def _burst(self, mode):
        count = 0
        while count == 0 or (self.held and count < BURST_MAX):  # at least one
            self._show(f"RAFALE {count + 1}")
            capture_id = await self._shoot()
            if capture_id is None:
                break
            count += 1
            await self.camera.wait_captured(capture_id)  # the engine takes one at a time
        if count > 1:
            self.camera.show_message(f"Rafale : {count} vues")

    async def _self_timer(self, mode):
        end = self.clock() + SELF_TIMERS[mode]
        while (left := end - self.clock()) > 0:
            self._show(f"RET {SELF_TIMERS[mode]} s", math.ceil(left))
            await self.sleep(left - math.ceil(left) + 1)  # until the next whole second
        self._show("")
        await self._shoot()

    async def _interval(self, mode):
        s = self.camera.settings
        every, total = s.interval_seconds, s.interval_count
        of = f"/{total}" if total else ""
        due, taken = self.clock(), 0
        while total == 0 or taken < total:
            while (left := due - self.clock()) > 0:
                self._show(f"INT {taken}{of} · {math.ceil(left)} s")
                await self.sleep(min(left, 1.0))
            self._show(f"INT {taken + 1}{of}")
            if await self._shoot() is None:
                self.camera.show_message("Intervallomètre arrêté", warning=True)
                return
            taken += 1
            # A whole interval late (the writing queue was full): the next one an interval
            # from now, rather than catching up the ones missed in a burst.
            due += every
            if due < self.clock():
                due = self.clock() + every
        self.camera.show_message(f"Intervallomètre terminé : {taken} vues")

    async def _bracket(self, mode):
        cam, s = self.camera, self.camera.settings
        time_us, gain = cam.stats.get("exposure_us") or 0, cam.stats.get("analogue_gain") or 0
        if time_us <= 0 or gain <= 0:
            cam.show_message("Pas de mesure d'exposition", warning=True)
            return
        offsets = bracket_offsets(s.bracket_shots, s.bracket_step)
        vary = "gain" if s.exposure.mode == "S" else "time"
        frozen = {"ExposureTimeMode": "Manual", "AnalogueGainMode": "Manual"}
        preset = cam.preset()
        gains = cam.stats.get("colour_gains")
        if s.exposure.controls(preset.exposure_bias, preset.white_balance).get("AwbEnable") and gains:
            frozen.update(AwbEnable=False, ColourGains=list(gains))
        self.frozen = True
        try:
            for i, stops in enumerate(offsets):
                self._show(f"BKT {i + 1}/{len(offsets)} · {exp.format_ev(stops)}")
                target = bracket_exposure(time_us, gain, stops, cam.slowest_shutter * 1e6, vary)
                await cam.engine.call(
                    "set_controls", controls={"ExposureTime": target[0], "AnalogueGain": target[1], **frozen}
                )
                await self._settle(*target)
                capture_id = await self._shoot()
                if capture_id is None:
                    cam.show_message("Bracketing interrompu", warning=True)
                    return
                await cam.wait_captured(capture_id)  # taken before the exposure changes again
            cam.show_message(f"Bracketing : {len(offsets)} vues")
        finally:
            self.frozen = False
            await cam.apply_exposure()  # the user's exposure again

    async def _settle(self, time_us, gain):
        """Until the sensor runs at this exposure (a few frames), as the stats tell."""
        deadline = self.clock() + 1.0 + 4 * time_us / 1e6
        while self.clock() < deadline:
            stats = self.camera.stats
            measured_time, measured_gain = stats.get("exposure_us") or 0, stats.get("analogue_gain") or 0
            if abs(measured_time - time_us) <= max(0.03 * time_us, 40) and abs(measured_gain - gain) <= 0.03 * gain:
                return
            await self.sleep(0.05)
        log.warning("bracketing: exposure %d us × %.2f not reached", time_us, gain)
