"""Cloud sync: the card's photos and videos sent to a cloud (docs/architecture.md §5).

Three modes (MENU › Applications › Cloud sync, or the web interface):
- sync: as soon as the camera reaches the Internet, then as the shots come, it sends what
  the cloud does not have yet; the card keeps everything;
- offload: the same, then frees the card: a shot (its JPEG, DNG and base copy) is deleted
  once all its files are in the cloud, checked, and unchanged since;
- manual: nothing on its own; "Synchroniser maintenant" and "Déverser maintenant" do it once.

On its own, never on a metered network (a phone's hotspot), and never during a recording, a
sequence (interval, timelapse, stack...) or a long exposure: the card and the processor are
theirs. One file at a time, the oldest first; the files still being written (the last few
seconds) wait for the next look.
"""

import asyncio
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

import aiohttp

from ...menu import Item, Menu
from ...settings import CLOUD_MODES
from ..network import local_address, network_state
from . import providers
from .config import PROVIDER_LABELS, CloudConfig
from .state import SentFiles

log = logging.getLogger(__name__)

MODE_LABELS = {"manual": "Manuel", "sync": "Synchroniser · garder", "offload": "Déverser · libérer la carte"}
NETWORK_SECONDS = 20  # how often NetworkManager is asked
SCAN_SECONDS = 60  # online, how often the card is looked at (and after each shot)
SETTLE_SECONDS = 10  # a file more recent may still be written
MODE_DELAY = 10  # a mode is acted on once it has stayed that long: the dial may only go through it
RETRY_SECONDS = (60, 300, 900)  # after an error, then the next ones
BUSY_POLL = 2.0
CONFIRM_SECONDS = 3

FOLDER = re.compile(r"^\d{3}PYCAM$")
NAME = re.compile(r"^PY([CBTV])_(\d{4})\.(JPG|DNG|MP4)$")


@dataclass(frozen=True)
class LocalFile:
    path: str
    remote: str  # in the cloud's folder: "DCIM/100PYCAM/PYC_0001.JPG"
    size: int
    mtime: float


def scan(root):
    """The shots on the card, oldest first: lists of the files that go together (a photo's
    JPEG, DNG and base copy; a video alone). Slow on a full card: in a thread."""
    shots = {}
    try:
        folders = sorted(f for f in os.listdir(root) if FOLDER.match(f))
    except OSError:
        return []
    for folder in folders:
        try:
            names = os.listdir(os.path.join(root, folder))
        except OSError:
            continue
        for name in names:
            m = NAME.match(name)
            if not m or (m.group(1) in "TV") != (m.group(3) == "MP4"):
                continue
            path = os.path.join(root, folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (folder, m.group(2), m.group(1) if m.group(1) in "TV" else "C")
            shots.setdefault(key, []).append(LocalFile(path, f"DCIM/{folder}/{name}", st.st_size, st.st_mtime))
    return [sorted(files, key=lambda f: f.remote) for _, files in sorted(shots.items())]


def is_sent(sent, f):
    """sent: SentFiles.sent_files()."""
    row = sent.get(f.path)
    return row is not None and row[0] == f.size and abs(row[1] - f.mtime) < 1e-3


def format_size(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f} Go".replace(".", ",")
    if n >= 1e6:
        return f"{n / 1e6:.0f} Mo"
    return f"{max(1, round(n / 1e3))} ko" if n else "0"


def owncloud_url(url):
    """The server's address as typed (cloud.example.org, .../remote.php/webdav...): its base."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    path = re.split(r"/(remote\.php|index\.php|apps)(/|$)", parts.path, maxsplit=1)[0]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path.rstrip("/"), "", ""))


class CloudSync:
    id = "cloudsync"
    title = "Cloud sync"

    def __init__(self, camera, config_path=None, db_path=None):
        self.camera = camera
        self.config_path = config_path
        self.db_path = db_path
        self.config = CloudConfig.load(config_path)
        self.sent = None  # SentFiles, opened by run()
        self.online = False
        self.metered = False
        self.state = "idle"  # idle | sending | paused | stopped | error | auth
        self.error = ""
        self.pending = (0, 0)  # files and bytes not in the cloud yet (at the last look)
        self.progress = None  # percent of the pass, while sending
        self.current = ""  # the file being sent
        self.last_sync = 0.0  # time.time() of the last pass that sent everything
        self.connecting = {}  # a connection being made: Dropbox's page, Google's code
        self._session = None
        self._provider = None
        self._job = None  # the pass running
        self._job_action = ""  # "auto", or "sync" / "offload" asked by hand
        self._request = None  # asked by hand, for when the pass running is over
        self._held = False  # stopped by hand: nothing on its own until the next connection
        self._wake = asyncio.Event()
        self._recount = True
        self._next_network = 0.0
        self._next_scan = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._offload_armed_until = 0.0
        self._seen_mode = camera.settings.cloud_mode
        self._mode_since = 0.0  # when it was chosen; the one at start is settled
        self._dropbox = None  # (app key, PKCE verifier) while connecting
        self._connect_task = None

    # --- What the camera shows

    def summary(self, camera=None):
        """On the right of its line in the Applications menu."""
        if not self.config.ready:
            return "Non configuré"
        if self.state in ("sending", "paused") and self.progress is not None:
            return f"Envoi {self.progress} %"
        if self.state == "auth":
            return "Connexion à refaire"
        if self.state == "error":
            return "Erreur"
        if not self.online:
            return "Hors ligne"
        return f"{self.pending[0]} à envoyer" if self.pending[0] else "À jour"

    def hud(self):
        return f"CLOUD {self.progress} %" if self.state == "sending" and self.progress is not None else ""

    def state_text(self):
        mode = self.camera.settings.cloud_mode
        if not self.config.ready:
            return "Non configuré"
        if self.state == "sending":
            return f"Envoi {self.progress or 0} % · {self.current}"
        if self.state == "paused":
            return "En pause · l'appareil travaille"
        if self.state == "auth":
            return "Connexion à refaire (page web)"
        if self.state == "error":
            return f"Erreur · nouvel essai dans {max(0, round((self._retry_at - time.monotonic()) / 60))} min"
        if not self.online:
            return "Hors ligne"
        if self.state == "stopped" or self._held:
            return "Arrêté jusqu'à la prochaine connexion"
        if self.metered and mode != "manual":
            return "Réseau facturé · envoi à la main"
        if self.pending[0]:
            return f"En ligne · {self.pending[0]} à envoyer"
        return "À jour" if self.last_sync else "En ligne"

    def menu(self, camera):
        self.wake(scan=False)  # what is left to send, counted again
        address = local_address()
        items = (
            Item("Compte"),
            Item("Service", kind="info", text=lambda c: PROVIDER_LABELS.get(self.config.provider, "Aucun")),
            Item("Identité", kind="info", text=lambda c: self.config.account or "—"),
            Item("Configurer", kind="info", text=lambda c: f"page web {address}:8080" if address else "page web, onglet Cloud"),
            Item("Envoi"),
            Item("État", kind="info", text=lambda c: self.state_text()),
            Item("À envoyer", kind="info", text=lambda c: self.pending_text()),
            Item("Mode", "cloud_mode", CLOUD_MODES, tuple(MODE_LABELS[m] for m in CLOUD_MODES), "apps"),
            Item("Synchroniser maintenant", kind="run", target=lambda c: c.show_message(self.request("sync"))),
            Item("Déverser maintenant", kind="run", target=self._offload_pressed),
            Item("Arrêter l'envoi", kind="run", target=lambda c: c.show_message(self.stop())),
        )
        return Menu(camera.settings, items=items, title="Cloud sync", context=camera)

    def pending_text(self):
        files, size = self.pending
        return f"{files} fichiers · {format_size(size)}" if files else "Rien"

    def photo_items(self, camera, path):
        return []

    def files_in_use(self):
        return set()

    def _offload_pressed(self, camera):
        """Two presses within 3 s: it deletes from the card."""
        now = time.monotonic()
        if now > self._offload_armed_until:
            self._offload_armed_until = now + CONFIRM_SECONDS
            camera.show_message("Effacer de la carte ce qui est envoyé ? Encore pour confirmer", warning=True)
            return
        self._offload_armed_until = 0.0
        camera.show_message(self.request("offload"))

    def status(self):
        """For the web interface (no secret in it)."""
        return {
            "config": self.config.public(),
            "service": PROVIDER_LABELS.get(self.config.provider, ""),
            "mode": self.camera.settings.cloud_mode,
            "modes": [{"id": m, "label": MODE_LABELS[m]} for m in CLOUD_MODES],
            "online": self.online,
            "metered": self.metered,
            "state": self.state,
            "text": self.state_text(),
            "summary": self.summary(),
            "error": self.error,
            "pending": self.pending[0],
            "pending_bytes": self.pending[1],
            "pending_text": self.pending_text(),
            "progress": self.progress,
            "current": self.current,
            "last_sync": int(self.last_sync),
            "connecting": self.connecting,
        }

    def _changed(self):
        self.camera.redraw()

    # --- Asked by hand, or by the camera

    def wake(self, scan=True):
        """New files, or the mode changed: look again soon (once the files are written; a
        mode turned through on the dial is not kept that long)."""
        self._recount = True
        if scan:
            self._next_scan = min(self._next_scan, time.monotonic() + SETTLE_SECONDS + 1)
        if self._check_mode() and self._seen_mode == "offload":
            self.camera.show_message("Déverser : ce qui est envoyé sera effacé de la carte", warning=True)
        self._wake.set()

    def _check_mode(self):
        """True if the mode just changed."""
        mode = self.camera.settings.cloud_mode
        if mode == self._seen_mode:
            return False
        self._seen_mode, self._mode_since = mode, time.monotonic()
        return True

    def _mode_settled(self):
        self._check_mode()
        return time.monotonic() >= self._mode_since + MODE_DELAY

    def request(self, action):
        """"sync" or "offload", now (after the pass running). The message to show."""
        if not self.config.ready:
            return "Cloud sync : aucun compte (page web, onglet Cloud)"
        self._request, self._held = action, False
        if self.state in ("error", "auth"):
            self._retry_at = 0.0
        self._wake.set()
        if not self.online:
            return "Hors ligne : à la prochaine connexion"
        return "Déversement demandé" if action == "offload" else "Synchronisation demandée"

    def stop(self):
        """Stops the pass running; nothing more on its own until the next connection."""
        self._request = None
        self._held = True
        if self._job:
            self._job.cancel()
            return "Envoi arrêté"
        self.state = "stopped"
        self._changed()
        return "Cloud sync arrêté jusqu'à la prochaine connexion"

    # --- The background work

    async def run(self):
        self.sent = SentFiles(self.db_path)
        try:
            while True:
                await self._tick()
                try:
                    await asyncio.wait_for(self._wake.wait(), self._sleep())
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            for task in (self._job, self._connect_task):
                if task:
                    task.cancel()
            if self._session:
                await self._session.close()
            self.sent.close()

    def _sleep(self):
        if not self.config.ready:
            return 300
        now = time.monotonic()
        return max(1.0, min(self._next_network, max(self._next_scan, self._retry_at, self._mode_since + MODE_DELAY)) - now)

    async def _tick(self):
        now = time.monotonic()
        if self._recount and not self._job:
            self._recount = False
            await self._count()
        if not self.config.ready:
            return
        if now >= self._next_network:
            online, metered = await network_state()
            self._next_network = now + NETWORK_SECONDS
            if online and not self.online:
                log.info("online%s", " (metered network)" if metered else "")
                self._held, self._failures, self._retry_at, self._next_scan = False, 0, 0.0, now
                if self.state == "stopped":
                    self.state = "idle"
            elif self.online and not online:
                log.info("offline")
            if (online, metered) != (self.online, self.metered):
                self.online, self.metered = online, metered
                self._changed()
        mode = self.camera.settings.cloud_mode
        if self._job:
            if self._job_action == "auto" and mode == "manual":
                self._job.cancel()
            return
        if self._request and self.online:
            action, self._request = self._request, None
            self._start(action)
            return
        if mode == "manual" or self._held or self.state == "auth" or not self.online or self.metered or self._busy():
            return
        if now >= self._retry_at and now >= self._next_scan and self._mode_settled():
            self._next_scan = now + SCAN_SECONDS
            self._start("auto")

    def _start(self, action):
        self._job_action = action
        self._job = asyncio.create_task(self._pass(action))
        self._job.add_done_callback(self._job_done)

    def _job_done(self, task):
        self._job = None
        if not task.cancelled() and task.exception():
            log.error("cloud sync failed", exc_info=task.exception())
        self._recount = True
        self._wake.set()

    async def _count(self):
        if self.sent is None:
            return
        shots = await asyncio.to_thread(scan, self.camera.storage.root)
        sent = self.sent.sent_files(self.config.identity) if self.config.ready else {}
        files = [f for shot in shots for f in shot if not is_sent(sent, f)]
        pending = (len(files), sum(f.size for f in files))
        if pending != self.pending:
            self.pending = pending
            self._changed()

    def _busy(self):
        """The camera needs its card and its processor."""
        c = self.camera
        return bool(c.recording or c.drive.active or c.exposing or c.task_status or c.writing)

    async def _wait_idle(self, playback=False):
        """Until the camera has finished (and, before deleting, playback is closed)."""
        while self._busy() or (playback and self.camera.playback):
            if self.state != "paused":
                self.state = "paused"
                self._changed()
            await asyncio.sleep(BUSY_POLL)
        if self.state == "paused":
            self.state = "sending"
            self._changed()

    def _offloading(self, action):
        """Read before each deletion: the dial may have left offload meanwhile, or be going
        through it."""
        if action == "offload":
            return True
        return action == "auto" and self.camera.settings.cloud_mode == "offload" and self._mode_settled()

    async def _pass(self, action):
        """Sends what the cloud does not have; then, offloading, frees each shot sent."""
        identity, sent_count, freed = self.config.identity, 0, 0
        touched = set()
        try:
            await self._wait_idle()
            shots = await asyncio.to_thread(scan, self.camera.storage.root)
            sent = self.sent.sent_files(identity)
            now = time.time()
            ready = [shot for shot in shots if all(now - f.mtime >= SETTLE_SECONDS for f in shot)]
            todo = [f for shot in ready for f in shot if not is_sent(sent, f)]
            if len(ready) < len(shots):
                self._next_scan = min(self._next_scan, time.monotonic() + SETTLE_SECONDS + 1)
            if not todo and not (self._offloading(action) and ready):
                self.last_sync = self.last_sync or time.time()
                if action != "auto":
                    self.camera.show_message("Cloud : rien à envoyer")
                return
            total, done = sum(f.size for f in todo) or 1, 0
            left = [len(todo), sum(f.size for f in todo)]
            self.state, self.error, self.progress = "sending", "", 0
            self._changed()
            if todo:
                log.info("cloud sync (%s): %d files, %s", action, len(todo), format_size(total))

            def progress(sent_bytes):
                percent = min(100, (done + sent_bytes) * 100 // total)
                if percent != self.progress:
                    self.progress = percent
                    self._changed()

            for shot in ready:
                for f in shot:
                    if is_sent(sent, f):
                        continue
                    await self._wait_idle()
                    self.current = os.path.basename(f.path)
                    try:
                        checksum = await self._get_provider().upload(f.path, f.remote, progress)
                    except FileNotFoundError:
                        log.info("%s deleted before it was sent", f.path)
                    else:
                        self.sent.mark(f.path, identity, f.size, f.mtime, checksum)
                        sent[f.path] = (f.size, f.mtime)
                        sent_count += 1
                    done += f.size
                    left[0] -= 1
                    left[1] -= f.size
                    self.pending = (max(0, left[0]), max(0, left[1]))
                    progress(0)
                if self._offloading(action):
                    await self._wait_idle(playback=True)
                    if self._offloading(action):
                        freed += self._free(shot)
                        touched.add(os.path.dirname(shot[0].path))
            self.last_sync = time.time()
            self._failures = 0
            self.state = "idle"
            if sent_count or freed:
                log.info("cloud sync: %d files sent, %s freed", sent_count, format_size(freed))
            if action != "auto":  # on its own, the HUD's badge is enough
                text = f"Cloud : {sent_count} fichiers envoyés"
                self.camera.show_message(text + (f", {format_size(freed)} libérés" if freed else ""))
        except asyncio.CancelledError:
            self.state = "stopped" if self._held else "idle"
            log.info("cloud sync stopped")
            raise
        except providers.AuthError as e:
            self.state, self.error = "auth", str(e)
            log.warning("cloud sync: %s", e)
        except (providers.CloudError, aiohttp.ClientError, TimeoutError, OSError) as e:
            self._failures += 1
            delay = RETRY_SECONDS[min(self._failures, len(RETRY_SECONDS)) - 1]
            self._retry_at = time.monotonic() + delay
            self.state, self.error = "error", str(e) or type(e).__name__
            log.warning("cloud sync: %s (again in %d s)", self.error, delay)
        finally:
            if self.state in ("sending", "paused"):
                self.state = "idle"
            self.current, self.progress = "", None
            self._remove_empty(touched)
            self._changed()

    def _free(self, shot):
        """Deletes a shot from the card if each of its files is in the cloud as it is now.
        Returns the bytes freed."""
        identity = self.config.identity
        if any(f.path in self.camera.files_in_use() for f in shot):
            return 0  # another application still needs it (a photo waiting to be shared)
        for f in shot:
            try:
                st = os.stat(f.path)
            except FileNotFoundError:
                continue
            if not self.sent.is_sent(f.path, identity, st.st_size, st.st_mtime):
                return 0
        freed = 0
        for f in shot:
            try:
                os.remove(f.path)
                freed += f.size
            except FileNotFoundError:
                pass
        log.info("offloaded %s", " ".join(os.path.basename(f.path) for f in shot))
        return freed

    def _remove_empty(self, folders):
        """The folders emptied, but the one the next photos go to."""
        current = f"{self.camera.settings.folder_number}PYCAM"
        for folder in folders:
            if os.path.basename(folder) != current:
                try:
                    os.rmdir(folder)
                except OSError:
                    pass  # not empty

    def _get_provider(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=providers.TIMEOUT)
            self._provider = None
        if self._provider is None:
            self._provider = providers.make_provider(self.config, self._session)
        return self._provider

    # --- Accounts (from the web interface: the camera has no keyboard)

    async def _set_config(self, config):
        if self._job:
            self._job.cancel()
        session, self._session, self._provider = self._session, None, None
        if session:
            await session.close()
        config.save(self.config_path)
        self.config = config
        self.state, self.error, self._failures, self._retry_at, self._held = "idle", "", 0, 0.0, False
        self.connecting, self.last_sync = {}, 0.0
        self._next_network = self._next_scan = 0.0
        self.online = False  # looked at again: coming online starts the first pass
        log.info("cloud account: %s %s", config.provider or "none", config.account)
        self.wake(scan=False)
        self._changed()

    async def connect_owncloud(self, url, user, password, folder="pycam"):
        config = CloudConfig(provider="owncloud", url=owncloud_url(url), user=user.strip(), password=password)
        config.folder = clean_folder(folder)
        async with aiohttp.ClientSession(timeout=providers.TIMEOUT) as session:
            config.account = await providers.WebDav(config, session).account()
        await self._set_config(config)
        return config.account

    def dropbox_start(self, app_key, folder="pycam"):
        """The page where the user lets the app in; Dropbox then shows a code to paste."""
        app_key = app_key.strip()
        url, verifier = providers.Dropbox.authorize_url(app_key)
        self._dropbox = (app_key, verifier, clean_folder(folder))
        self.connecting = {"provider": "dropbox", "url": url}
        return url

    async def dropbox_finish(self, code):
        if not self._dropbox:
            raise providers.AuthError("Recommencer la connexion à Dropbox")
        app_key, verifier, folder = self._dropbox
        async with aiohttp.ClientSession(timeout=providers.TIMEOUT) as session:
            token = await providers.Dropbox.finish(session, app_key, code, verifier)
            config = CloudConfig(provider="dropbox", client_id=app_key, refresh_token=token, folder=folder)
            config.account = await providers.Dropbox(config, session).account()
        self._dropbox = None
        await self._set_config(config)
        return config.account

    async def google_start(self, client_id, client_secret, folder="pycam"):
        """The code to type at Google's address; the camera waits for the answer meanwhile."""
        client_id, client_secret = client_id.strip(), client_secret.strip()
        async with aiohttp.ClientSession(timeout=providers.TIMEOUT) as session:
            info = await providers.GoogleDrive.device_start(session, client_id)
        if self._connect_task:
            self._connect_task.cancel()
        self.connecting = {
            "provider": "gdrive",
            "user_code": info["user_code"],
            "url": info.get("verification_url") or info.get("verification_uri", ""),
            "expires": int(time.time() + info.get("expires_in", 1800)),
        }
        config = CloudConfig(provider="gdrive", client_id=client_id, client_secret=client_secret, folder=clean_folder(folder))
        self._connect_task = asyncio.create_task(self._google_wait(config, info))
        return self.connecting

    async def _google_wait(self, config, info):
        interval = max(1, info.get("interval", 5))
        deadline = time.monotonic() + info.get("expires_in", 1800)
        try:
            async with aiohttp.ClientSession(timeout=providers.TIMEOUT) as session:
                while time.monotonic() < deadline:
                    await asyncio.sleep(interval)
                    token = await providers.GoogleDrive.device_poll(
                        session, config.client_id, config.client_secret, info["device_code"]
                    )
                    if token:
                        config.refresh_token = token
                        config.account = await providers.GoogleDrive(config, session).account()
                        self._connect_task = None
                        await self._set_config(config)
                        return
            raise providers.AuthError("code expiré : recommencer")
        except (providers.CloudError, aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warning("Google Drive connection: %s", e)
            self.connecting = {"provider": "gdrive", "error": str(e)}
        finally:
            if self._connect_task is asyncio.current_task():
                self._connect_task = None

    async def disconnect(self):
        """Forgets the account (the files sent stay in the cloud)."""
        if self._connect_task:
            self._connect_task.cancel()
        self._dropbox = None
        await self._set_config(CloudConfig(folder=self.config.folder))


def clean_folder(folder):
    """A folder in the cloud, relative: "pycam", "Photos/pycam"."""
    parts = [p for p in str(folder or "").replace("\\", "/").split("/") if p.strip() and p not in (".", "..")]
    return "/".join(p.strip() for p in parts) or "pycam"
