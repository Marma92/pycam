"""Physical controls, read through evdev (docs/architecture.md §5).

Today a USB keyboard stands in for the camera buttons; tomorrow the buttons and dials
wired to the GPIO will show up the same way, through the gpio-keys / rotary-encoder
kernel drivers. Devices we use are grabbed, so that key presses do not also reach the
console, and new devices are picked up within a couple of seconds.
"""

import asyncio
import logging

import evdev
from evdev import ecodes

log = logging.getLogger(__name__)

# Key -> action. One key per control of the camera (docs/hardware.md §2): the other
# settings are in the menus. Keys chosen to sit at the same place on QWERTY and AZERTY.
KEYMAP = {
    ecodes.KEY_SPACE: "shutter",
    ecodes.KEY_ENTER: "shutter",
    ecodes.KEY_KPENTER: "shutter",
    ecodes.KEY_RIGHT: "shutter_faster",  # front dial
    ecodes.KEY_LEFT: "shutter_slower",
    ecodes.KEY_UP: "iso_up",  # rear dial
    ecodes.KEY_DOWN: "iso_down",
    ecodes.KEY_S: "shutter_auto",  # front dial press: automatic speed, or manual from the measure
    ecodes.KEY_I: "iso_auto",  # rear dial press
    ecodes.KEY_PAGEUP: "ev_up",
    ecodes.KEY_KPPLUS: "ev_up",
    ecodes.KEY_PAGEDOWN: "ev_down",
    ecodes.KEY_KPMINUS: "ev_down",
    ecodes.KEY_TAB: "q",  # quick menu
    ecodes.KEY_ESC: "menu",  # full menu; closes a menu, leaves the loupe
    ecodes.KEY_D: "disp",
    ecodes.KEY_L: "loupe",  # x5, x10, off; in the loupe, the arrows move it
    ecodes.KEY_1: "fn1",  # what they do is chosen in the menu
    ecodes.KEY_2: "fn2",
}
# Actions that repeat while the key is held, like turning a dial.
REPEATABLE = {"shutter_faster", "shutter_slower", "iso_up", "iso_down", "ev_up", "ev_down"}

RESCAN_SECONDS = 2


class Inputs:
    def __init__(self, on_action):
        self.on_action = on_action
        self._devices = {}  # path -> reading task, or None for a device we do not use

    async def run(self):
        while True:
            present = set(evdev.list_devices())
            for path in list(self._devices):
                if path not in present and self._devices[path] is None:
                    del self._devices[path]
            for path in present - self._devices.keys():
                self._open(path)
            await asyncio.sleep(RESCAN_SECONDS)

    def _open(self, path):
        try:
            device = evdev.InputDevice(path)
        except OSError:
            return
        keys = device.capabilities().get(ecodes.EV_KEY, [])
        if not any(code in KEYMAP for code in keys):
            device.close()
            self._devices[path] = None
            return
        try:
            device.grab()
        except OSError as e:
            log.warning("cannot grab %s (%s): its keys also reach the console", device.name, e)
        log.info("input: %s (%s)", device.name, path)
        self._devices[path] = asyncio.create_task(self._read(path, device))

    async def _read(self, path, device):
        try:
            async for event in device.async_read_loop():
                if event.type != ecodes.EV_KEY or event.value == 0:  # 0: key released
                    continue
                action = KEYMAP.get(event.code)
                if action and (event.value == 1 or action in REPEATABLE):  # 2: auto-repeat
                    try:
                        self.on_action(action)
                    except Exception:
                        log.exception("action %s failed", action)
        except OSError:
            log.info("input gone: %s", device.name)
        finally:
            self._devices.pop(path, None)
            try:
                device.close()
            except OSError:
                pass
