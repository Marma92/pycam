"""The camera's clock, without a real-time clock (docs/architecture.md §8).

The Pi has no battery-backed clock: off the network, it starts again at the time of its
last synchronisation, and its photos are dated wrong. With the network, NTP sets it
(systemd-timesyncd). Without, the phone or the computer that opens the web page gives
its own time: the page sends it, and the camera takes it if it is off by more than a
minute. Never over NTP's.

Setting the clock takes CAP_SYS_TIME, which pycam.service gives the application.
"""

import logging
import os
import time

log = logging.getLogger(__name__)

SYNCED = "/run/systemd/timesync/synchronized"  # written by systemd-timesyncd once in sync
TOLERANCE = 60  # seconds: closer than this, left alone


def synchronized(path=SYNCED):
    return os.path.exists(path)


def set_clock(seconds):
    time.clock_settime(time.CLOCK_REALTIME, seconds)


class Clock:
    def __init__(self, synced_path=SYNCED, setter=set_clock):
        self.synced_path = synced_path
        self.setter = setter
        self.source = ""  # "phone" once a browser has set it

    @property
    def synchronized(self):
        return synchronized(self.synced_path)

    def from_browser(self, browser_ms):
        """The time of a browser (ms since 1970): "network" (NTP has it, nothing done),
        "ok" (close enough), "set", or "denied" (no CAP_SYS_TIME: run by hand)."""
        if self.synchronized:
            return "network"
        browser = float(browser_ms) / 1000
        if abs(browser - time.time()) < TOLERANCE:
            return "ok"
        try:
            self.setter(browser)
        except (PermissionError, OSError) as e:
            log.warning("cannot set the clock: %s", e)
            return "denied"
        log.info("clock set from a browser: %s", time.strftime("%Y-%m-%d %H:%M:%S"))
        self.source = "phone"
        return "set"

    def text(self):
        """For the menu: the time, and where it comes from."""
        now = time.strftime("%d/%m/%Y %H:%M")
        if self.synchronized:
            return f"{now} · réseau"
        return f"{now} · téléphone" if self.source == "phone" else f"{now} · à régler (site)"
