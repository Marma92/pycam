"""The updates, the application's side (docs/architecture.md, « Les mises à jour »).

The work is done as root by updater.py; here: when to ask for it, and what the camera
shows of it. Only on a camera installed from a release (tools/install.sh, /opt/pycam): a
development tree is updated by tools/deploy.sh.

- A release is looked for when the camera finds the Internet (not on a metered network:
  a phone's hotspot), at most every six hours, unless the updates are off. A new one is
  downloaded straight away (a few MB).
- "auto": installed at the next start of the camera, before anything is shot (it starts
  again on it, ~20 s); "notify": only proposed; "off": only when asked for.
- MENU › Système, or the web page: look for one now, install the one downloaded.
"""

import asyncio
import json
import logging
import os
import time

from . import __version__
from .apps.network import network_state

log = logging.getLogger(__name__)

STATUS = "/var/lib/pycam-update/status.json"
RELEASES = "/opt/pycam/releases"
CHECK_EVERY = 6 * 3600
POLL = 30  # seconds between two looks, while nothing happens
FAST_POLL = 2  # while root works on a request
FAST_FOR = 15 * 60
START_GRACE = 20  # seconds, at most, waiting for the screen to say what happens
MESSAGE_SECONDS = 4  # a result: longer than the camera's usual messages
WORKING = ("checking", "downloading", "preparing", "installing")
STALE = 3600  # a "working" status this old: the updater stopped on the way
MODE_LABELS = {"auto": "Automatiques", "notify": "Proposées", "off": "Désactivées"}


def from_release(path=__file__):
    """Installed from a release (/opt/pycam/releases), not run from a development tree."""
    return os.path.realpath(path).startswith(os.path.realpath(RELEASES) + os.sep)


class Updates:
    title = "Mises à jour"

    def __init__(self, camera, status_path=STATUS, managed=None):
        self.camera = camera
        self.status_path = status_path
        # Next to the engine's socket: /run/pycam, the camera user's (pycam.tmpfiles).
        self.request_path = os.path.join(os.path.dirname(camera.engine.path), "update-request")
        self.managed = from_release() if managed is None else managed
        self.status = {}
        self._wake = asyncio.Event()
        self._fast_until = 0.0
        self._asked = False  # by the user: the result is shown, whatever it is

    # --- The root side's status, and requests to it

    def read(self):
        try:
            with open(self.status_path, encoding="utf-8") as f:
                status = json.load(f)
            return status if isinstance(status, dict) else {}
        except (OSError, ValueError):
            return {}

    def request(self, action):
        try:
            with open(self.request_path, "w", encoding="ascii") as f:
                f.write(action + "\n")
        except OSError as e:
            log.warning("update request %s not sent: %s", action, e)
            return False
        log.info("update: %s requested", action)
        self._fast_until = time.monotonic() + FAST_FOR
        self.wake()
        return True

    def wake(self):
        self._wake.set()

    @property
    def state(self):
        state = self.status.get("state", "")
        if state in WORKING and time.time() - self.status.get("time", 0) > STALE:
            return "error"
        return state

    @property
    def available(self):
        return (self.status.get("available") or {}).get("version", "") if self.state == "ready" else ""

    # --- The background work

    async def run(self):
        if not self.managed:
            return
        self.status = self.read()
        await self._screen_ready()  # what happened before the restart: said once it shows
        said = self._announce({}, self.status)
        if self.available and self.camera.settings.update_mode == "auto":
            await self._install_at_start(said)
        while True:
            await self.step()
            # Root at work (after a restart too: the installation is checked once this
            # version says it works): its result is followed closely.
            delay = FAST_POLL if time.monotonic() < self._fast_until or self.state in WORKING else POLL
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _screen_ready(self):
        deadline = time.monotonic() + START_GRACE
        while self.camera.hud is None and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

    async def _install_at_start(self, said):
        """Downloaded during an earlier session: installed now, before anything is shot.
        said: a result is on the screen (the last installation's), left to be read."""
        if not self._busy():
            if said:
                await asyncio.sleep(MESSAGE_SECONDS)
            self.camera.show_message(f"Mise à jour {self.available}…", seconds=MESSAGE_SECONDS)
            await asyncio.sleep(2)
            if not self._busy():
                self.request("install")

    async def step(self):
        status = self.read()
        if status != self.status:
            old, self.status = self.status, status
            self._announce(old, status)
            self.camera.redraw()
        if self.state in WORKING or os.path.exists(self.request_path):  # one request at a time
            return
        s = self.camera.settings
        if s.update_mode != "off" and abs(time.time() - s.update_checked) > CHECK_EVERY:
            online, metered = await network_state()
            if online and not metered:
                s.update_checked = time.time()
                self.camera.save_soon()
                self.request("check")

    def _busy(self):
        c = self.camera
        return bool(c.recording or c.drive.active or c.exposing or c.task_status or c.writing)

    def _announce(self, old, new):
        """Each result once on the screen: an installation (after the restart), a failure,
        an update ready; the rest only when the user asked. True if one was shown."""
        s, state = self.camera.settings, self.state
        if new.get("time", 0) <= s.update_seen or state in WORKING:
            return False
        text, warning = "", state in ("failed", "error")
        if state == "installed" and new.get("current") == __version__:
            text = f"pycam {__version__} installé"
        elif state == "failed":
            text = new.get("message", "")
        elif state == "ready" and (old.get("state") != "ready" or self._asked):
            text = f"Mise à jour {self.available} prête" + (" · au prochain démarrage" if s.update_mode == "auto" else "")
        elif self._asked:
            text = new.get("message", "")
        if not text:
            return False
        log.info("update: %s", text)
        self._asked = False
        s.update_seen = new["time"]
        self.camera.save_soon()
        self.camera.show_message(text, warning, seconds=MESSAGE_SECONDS)
        return True

    # --- The menu (MENU › Système) and the web page

    def act(self, action=None):
        """action: "check" (look for an update now), "install" (the one downloaded), or None:
        whichever fits (the menu's line). What happens, in a few words."""
        if not self.managed:
            return "Installation de développement : tools/deploy.sh"
        self.status = self.read()  # root may have finished since the last look
        if self.state in WORKING:
            return self.state_text()
        self._asked = True
        if action == "install" and not self.available:
            return "Aucune mise à jour à installer"
        if self.available and action != "check":
            if self._busy():
                return "Pas pendant une prise"
            version = self.available
            return f"Installation de {version}…" if self.request("install") else "Mise à jour indisponible"
        return "Recherche…" if self.request("check") else "Mise à jour indisponible"

    def version_text(self):
        return __version__ if self.managed else f"{__version__} · développement"

    def action_text(self):
        if not self.managed:
            return "—"
        return f"Installer {self.available}" if self.available else "Rechercher"

    def state_text(self):
        """On one menu line."""
        if not self.managed:
            return "Par tools/deploy.sh"
        st, state = self.status, self.state
        if state == "checking":
            return "Recherche…"
        if state == "downloading":
            return f"Téléchargement {round(100 * st.get('progress', 0))} %"
        if state == "preparing":
            return "Préparation…"
        if state == "installing":
            return "Installation…"
        if state == "ready":
            return f"{self.available} prête"
        if state in ("up_to_date", "installed"):
            return "À jour"
        if state in ("failed", "error"):
            return st.get("message", "Erreur").split(" : ")[0]
        return "Jamais vérifié"

    def to_dict(self):
        """For the web page."""
        st = self.status
        return {
            "version": __version__,
            "managed": self.managed,
            "mode": self.camera.settings.update_mode,
            "state": self.state,
            "state_text": self.state_text(),
            "message": st.get("message", ""),
            "available": self.available,
            "notes": (st.get("available") or {}).get("notes", "") if self.available else "",
            "checked": st.get("checked", 0),
        }
