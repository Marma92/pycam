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
    ecodes.KEY_RIGHT: "front_dial_plus",  # front dial
    ecodes.KEY_LEFT: "front_dial_minus",
    ecodes.KEY_UP: "rear_dial_plus",  # rear dial
    ecodes.KEY_DOWN: "rear_dial_minus",
    ecodes.KEY_S: "shutter_auto",  # front dial press: automatic speed, or manual from the measure
    ecodes.KEY_I: "iso_auto",  # rear dial press: the same for the ISO
    ecodes.KEY_TAB: "q",  # quick menu
    ecodes.KEY_ESC: "menu",  # full menu; closes a menu, leaves the loupe
    ecodes.KEY_D: "disp",
    ecodes.KEY_L: "loupe",  # x5, x10, off; in the loupe, the arrows move it
    ecodes.KEY_P: "play",  # LECTURE: the photos on the card
    ecodes.KEY_1: "fn1",  # what they do is chosen in the menu
    ecodes.KEY_2: "fn2",
    ecodes.KEY_V: "video",  # PHOTO / VIDÉO (a switch on the camera, or a Fn button)
}
# The camera's dials (docs/hardware.md, "Câblage"): the kernel's rotary-encoder driver, one
# relative axis each; one step per detent. Only devices named "rotary...": a mouse moves
# the same axes and must not turn the dials.
DIALS = {
    ecodes.REL_X: ("front_dial_plus", "front_dial_minus"),
    ecodes.REL_Y: ("rear_dial_plus", "rear_dial_minus"),
}
# Actions that repeat while the key is held, like turning a dial.
REPEATABLE = {"front_dial_plus", "front_dial_minus", "rear_dial_plus", "rear_dial_minus"}

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
        capabilities = device.capabilities()
        keys = capabilities.get(ecodes.EV_KEY, [])
        dial = "rotary" in device.name.lower() and any(axis in DIALS for axis in capabilities.get(ecodes.EV_REL, []))
        if not dial and not any(code in KEYMAP for code in keys):
            device.close()
            self._devices[path] = None
            return
        try:
            device.grab()
        except OSError as e:
            log.warning("cannot grab %s (%s): its keys also reach the console", device.name, e)
        log.info("input: %s (%s)", device.name, path)
        self._devices[path] = asyncio.create_task(self._read(path, device))

    @staticmethod
    def _actions(event, dial):
        """The actions an input event stands for: a key pressed (or repeating), or the
        detents a dial turned."""
        if event.type == ecodes.EV_KEY:
            action = KEYMAP.get(event.code)
            if action == "shutter" and event.value == 0:  # released: ends a burst
                return ["shutter_release"]
            if action and (event.value == 1 or action in REPEATABLE and event.value == 2):  # 2: auto-repeat
                return [action]
        elif dial and event.type == ecodes.EV_REL and event.code in DIALS:
            plus, minus = DIALS[event.code]
            return [plus if event.value > 0 else minus] * min(abs(event.value), 10)
        return []

    async def _read(self, path, device):
        try:
            dial = "rotary" in device.name.lower()
            async for event in device.async_read_loop():
                for action in self._actions(event, dial):
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
