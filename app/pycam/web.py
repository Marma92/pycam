"""The web interface: the camera from a phone or a computer on the same network
(docs/architecture.md §5).

- the viewfinder, roughly once a second, and a remote shutter;
- the preset and its grain;
- the photos: thumbnails, download (JPEG, DNG), deletion;
- the presets: install one made on a computer, remove it;
- Cloud sync: its account (connected from here: the camera has no keyboard), its mode;
- Partage: a photo shared on Instagram, Flickr, Bluesky, with the preview of each; the accounts.

Local network only: any other address is refused, IPv6 included (the camera may have a
public IPv6 address, and nothing here asks for a password).
"""

import asyncio
import io
import ipaddress
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile

import aiohttp
from aiohttp import web

from . import looks
from .playback import companions, describe, list_photos
from .settings import CLOUD_MODES

log = logging.getLogger(__name__)

PORT = 8080
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
PREVIEW = "/run/pycam/web-preview.jpg"  # tmpfs, written by the engine
PREVIEW_MAX_AGE = 0.5  # seconds: two browsers watching share the same frame
MAX_UPLOAD = 32 * 1024**2
FOLDER = re.compile(r"^\d{3}PYCAM$")
SHOT = re.compile(r"^(PY[CB]_\d{4}\.(JPG|DNG)|PY[TV]_\d{4}\.MP4)$")
VIDEO = re.compile(r"^PY[TV]_\d{4}\.MP4$")
SHARED = re.compile(r"^PY[CB]_\d{4}\.JPG$")
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def local_client(address):
    """Private, link-local or loopback addresses only."""
    try:
        ip = ipaddress.ip_address((address or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


@web.middleware
async def local_only(request, handler):
    if not local_client(request.remote):
        log.warning("refused %s: not on the local network", request.remote)
        raise web.HTTPForbidden(text="Réservé au réseau local.")
    return await handler(request)


def thumbs_dir():
    return os.path.join(os.path.dirname(looks.cache_dir()), "thumbs")


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc"))).strip("-")
    return s[:48] or "preset"


class WebServer:
    def __init__(self, camera, port=PORT):
        self.camera = camera
        self.port = port
        self.runner = None
        self._preview_lock = asyncio.Lock()
        self._preview_time = 0.0
        app = web.Application(middlewares=[local_only], client_max_size=MAX_UPLOAD)
        app.add_routes(
            [
                web.get("/", self.page),
                web.get("/api/state", self.state),
                web.post("/api/shutter", self.shutter),
                web.post("/api/preset", self.choose_preset),
                web.post("/api/grain", self.grain),
                web.post("/api/mode", self.mode),
                web.get("/api/preview.jpg", self.preview),
                web.get("/api/photos", self.photos),
                web.get("/api/videos", self.videos),
                web.get("/api/thumb/{folder}/{name}", self.thumb),
                web.get("/api/photo/{folder}/{name}", self.photo),
                web.delete("/api/photo/{folder}/{name}", self.delete_photo),
                web.get("/api/presets", self.presets),
                web.post("/api/presets", self.install_preset),
                web.delete("/api/presets/{id}", self.remove_preset),
                web.get("/api/cloud", self.cloud_state),
                web.post("/api/cloud/owncloud", self.cloud_owncloud),
                web.post("/api/cloud/dropbox/start", self.cloud_dropbox_start),
                web.post("/api/cloud/dropbox/finish", self.cloud_dropbox_finish),
                web.post("/api/cloud/google/start", self.cloud_google_start),
                web.post("/api/cloud/mode", self.cloud_mode),
                web.post("/api/cloud/action", self.cloud_action),
                web.delete("/api/cloud", self.cloud_disconnect),
                web.get("/api/share", self.share_state),
                web.post("/api/share/preview", self.share_preview),
                web.post("/api/share/post", self.share_post),
                web.post("/api/share/queue/{id}/retry", self.share_retry),
                web.delete("/api/share/queue/{id}", self.share_remove),
                web.post("/api/share/bluesky", self.share_bluesky),
                web.post("/api/share/flickr/start", self.share_flickr_start),
                web.post("/api/share/flickr/finish", self.share_flickr_finish),
                web.post("/api/share/instagram", self.share_instagram),
                web.post("/api/share/defaults", self.share_defaults),
                web.delete("/api/share/account/{platform}", self.share_disconnect),
            ]
        )
        self.app = app

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, port=self.port).start()
        log.info("web interface on port %d", self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    # --- Page and state

    async def page(self, request):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def state(self, request):
        view, s = self.camera.view(), self.camera.settings
        preset = self.camera.preset()
        cloud = self._cloud(required=False)
        return web.json_response(
            {
                "connected": self.camera.engine.connected,
                "mode": view.mode,
                "shutter": view.shutter,
                "iso": view.iso,
                "ev": view.ev,
                "awb": view.awb,
                "formats": view.formats,
                "shots": view.shots,
                "writing": view.writing,
                "drive": view.drive,
                "task": view.task,
                "screen": self.camera.screen_on,
                "camera_mode": s.mode,
                "recording": view.recording,
                "drive_running": view.drive_running,
                "countdown": view.countdown,
                "exposure_timer": view.exposure_timer,
                "exposure_label": view.exposure_label,
                "preset": preset.id,
                "grain": s.grain,
                "grain_label": looks.grain_label(preset),
                "presets": [{"id": p.id, "name": p.name} for p in looks.registry().values()],
                "cloud": cloud.summary() if cloud else "",
            }
        )

    async def shutter(self, request):
        """A press without a release: one photo, a self-timer or an interval started (or
        the one running stopped), the first photo of a burst only; a bulb starts, and a
        second press ends it; a long exposure is cancelled."""
        self.camera.on_action("remote_shutter")
        return web.json_response({"ok": True})

    async def choose_preset(self, request):
        data = await request.json()
        if not self.camera.set_preset(str(data.get("id", ""))):
            raise web.HTTPNotFound(text="Preset inconnu.")
        return web.json_response({"ok": True})

    async def mode(self, request):
        data = await request.json()
        if not self.camera.set_mode(str(data.get("mode", ""))):
            raise web.HTTPConflict(text="Impossible pendant un enregistrement.")
        return web.json_response({"ok": True})

    async def grain(self, request):
        data = await request.json()
        self.camera.set_grain(bool(data.get("on")))
        return web.json_response({"ok": True})

    async def preview(self, request):
        """The viewfinder; one engine snapshot at a time, shared for a moment."""
        async with self._preview_lock:
            if time.monotonic() - self._preview_time > PREVIEW_MAX_AGE:
                if not await self.camera.snapshot(PREVIEW):
                    raise web.HTTPServiceUnavailable(text="Visée indisponible (moteur ou écran absent).")
                self._preview_time = time.monotonic()
            with open(PREVIEW, "rb") as f:
                data = f.read()
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # --- Photos

    def _path(self, request):
        folder, name = request.match_info["folder"], request.match_info["name"]
        if not FOLDER.match(folder) or not SHOT.match(name):
            raise web.HTTPNotFound()
        path = os.path.join(self.camera.storage.root, folder, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return path

    async def photos(self, request):
        root = self.camera.storage.root
        out = []
        for jpeg in reversed(list_photos(root)):  # the last one first
            folder, name = jpeg.split(os.sep)[-2:]
            stem = name[:-4]
            dng, base = (os.path.exists(p) for p in companions(jpeg)[1:])
            st = os.stat(jpeg)
            out.append(
                {
                    "folder": folder,
                    "name": stem,
                    "jpeg_size": st.st_size,
                    "dng": dng,
                    "base": base,
                    "time": int(st.st_mtime),
                }
            )
        return web.json_response(out)

    async def videos(self, request):
        """The timelapse videos, the last one first."""
        root = self.camera.storage.root
        out = []
        try:
            folders = sorted(f for f in os.listdir(root) if FOLDER.match(f))
        except OSError:
            folders = []
        for folder in folders:
            try:
                names = sorted(n for n in os.listdir(os.path.join(root, folder)) if VIDEO.match(n))
            except OSError:
                continue
            for name in names:
                st = os.stat(os.path.join(root, folder, name))
                out.append({"folder": folder, "name": name, "size": st.st_size, "time": int(st.st_mtime)})
        out.sort(key=lambda v: v["time"], reverse=True)
        return web.json_response(out)

    async def thumb(self, request):
        path = self._path(request)
        folder, name = request.match_info["folder"], request.match_info["name"]
        cache = os.path.join(thumbs_dir(), f"{folder}_{name[:-4]}_{int(os.path.getmtime(path))}.jpg")
        if not os.path.exists(cache):
            await asyncio.to_thread(make_thumb, path, cache)
        return web.FileResponse(cache, headers={"Cache-Control": "max-age=86400"})

    async def photo(self, request):
        path = self._path(request)
        headers = {}
        if request.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
        if request.query.get("info"):
            info, size = await asyncio.to_thread(describe, path)
            return web.json_response({"info": info, "size": size})
        return web.FileResponse(path, headers=headers)

    async def delete_photo(self, request):
        path = self._path(request)
        video = path.endswith(".MP4")
        if not video and (not path.endswith(".JPG") or os.path.basename(path).startswith("PYB_")):
            raise web.HTTPBadRequest(text="On supprime une prise par son JPEG.")
        removed = []
        for f in [path] if video else companions(path):
            try:
                os.remove(f)
                removed.append(os.path.basename(f))
            except FileNotFoundError:
                pass
        log.info("deleted from the web interface: %s", " ".join(removed))
        return web.json_response({"ok": True, "removed": removed})

    # --- Presets

    async def presets(self, request):
        looks.refresh()
        builtin = os.path.normpath(looks.BUILTIN_DIR)
        return web.json_response(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "short": p.short,
                    "description": p.description,
                    "grain": looks.grain_label(p),
                    "white_balance": p.white_balance,
                    "builtin": os.path.normpath(os.path.dirname(p.folder)) == builtin,
                }
                for p in looks.registry().values()
            ]
        )

    async def install_preset(self, request):
        """A .zip of a preset folder, or its files (preset.toml and its LUT)."""
        files = {}
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                files[os.path.basename(part.filename)] = await part.read(decode=False)
        try:
            preset = await asyncio.to_thread(install_preset, files)
        except looks.PresetError as e:
            raise web.HTTPBadRequest(text=f"Preset refusé : {e}")
        looks.refresh()
        log.info("preset installed from the web interface: %s", preset.id)
        return web.json_response({"ok": True, "id": preset.id, "name": preset.name})

    async def remove_preset(self, request):
        preset_id = request.match_info["id"]
        folder = os.path.join(looks.user_dir(), preset_id)
        if not PRESET_ID.match(preset_id) or not os.path.isdir(folder):
            raise web.HTTPNotFound(text="Seuls les presets ajoutés peuvent être supprimés.")
        shutil.rmtree(folder)
        looks.refresh()
        if self.camera.settings.preset == preset_id:  # back to the default one
            self.camera.set_preset(looks.DEFAULT)
        log.info("preset removed from the web interface: %s", preset_id)
        return web.json_response({"ok": True})


    # --- Cloud sync (apps/cloudsync): the secrets come in, never go out

    def _cloud(self, required=True):
        app = next((a for a in self.camera.apps if getattr(a, "id", "") == "cloudsync"), None)
        if app is None and required:
            raise web.HTTPNotFound(text="Cloud sync n'est pas disponible.")
        return app

    async def _connect(self, coro):
        from .apps.cloudsync import providers

        try:
            account = await coro
        except providers.AuthError as e:
            raise web.HTTPBadRequest(text=f"Refusé : {e}")
        except (providers.CloudError, aiohttp.ClientError, TimeoutError, OSError) as e:
            raise web.HTTPBadGateway(text=f"Service injoignable : {e or type(e).__name__}")
        return account

    @staticmethod
    async def _fields(request, *names):
        data = await request.json()
        return [str(data.get(name) or "") for name in names]

    async def cloud_state(self, request):
        return web.json_response(self._cloud().status())

    async def cloud_owncloud(self, request):
        url, user, password, folder = await self._fields(request, "url", "user", "password", "folder")
        if not (url and user and password):
            raise web.HTTPBadRequest(text="Il faut le serveur, l'utilisateur et un mot de passe d'application.")
        account = await self._connect(self._cloud().connect_owncloud(url, user, password, folder))
        return web.json_response({"ok": True, "account": account})

    async def cloud_dropbox_start(self, request):
        app_key, folder = await self._fields(request, "app_key", "folder")
        if not app_key:
            raise web.HTTPBadRequest(text="Il faut la clé de l'app Dropbox.")
        return web.json_response({"url": self._cloud().dropbox_start(app_key, folder)})

    async def cloud_dropbox_finish(self, request):
        (code,) = await self._fields(request, "code")
        if not code:
            raise web.HTTPBadRequest(text="Il faut le code donné par Dropbox.")
        account = await self._connect(self._cloud().dropbox_finish(code))
        return web.json_response({"ok": True, "account": account})

    async def cloud_google_start(self, request):
        client_id, client_secret, folder = await self._fields(request, "client_id", "client_secret", "folder")
        if not (client_id and client_secret):
            raise web.HTTPBadRequest(text="Il faut l'ID client et son code secret.")
        info = await self._connect(self._cloud().google_start(client_id, client_secret, folder))
        return web.json_response(info)

    async def cloud_mode(self, request):
        (mode,) = await self._fields(request, "mode")
        if mode not in CLOUD_MODES:
            raise web.HTTPBadRequest(text="Mode inconnu.")
        self.camera.settings.cloud_mode = mode
        self.camera.save_soon()
        self.camera.wake_apps()
        self.camera.redraw()
        return web.json_response({"ok": True})

    async def cloud_action(self, request):
        (action,) = await self._fields(request, "action")
        app = self._cloud()
        if action in ("sync", "offload"):
            message = app.request(action)
        elif action == "stop":
            message = app.stop()
        else:
            raise web.HTTPBadRequest(text="Action inconnue.")
        return web.json_response({"ok": True, "message": message})

    async def cloud_disconnect(self, request):
        await self._cloud().disconnect()
        return web.json_response({"ok": True})


    # --- Partage (apps/share): the secrets come in, never go out

    def _share(self):
        app = next((a for a in self.camera.apps if getattr(a, "id", "") == "share"), None)
        if app is None:
            raise web.HTTPNotFound(text="Le partage n'est pas disponible.")
        return app

    async def _share_call(self, coro):
        from .apps.share.publishers import Refused, ShareError

        try:
            return await coro
        except Refused as e:
            raise web.HTTPBadRequest(text=f"Refusé : {e}")
        except (ShareError, aiohttp.ClientError, TimeoutError, OSError) as e:
            raise web.HTTPBadGateway(text=f"Service injoignable : {e or type(e).__name__}")

    def _shared_photo(self, data):
        folder, name = str(data.get("folder", "")), str(data.get("name", ""))
        if not name.upper().endswith(".JPG"):
            name += ".JPG"
        path = os.path.join(self.camera.storage.root, folder, name)
        if not FOLDER.match(folder) or not SHARED.match(name) or not os.path.isfile(path):
            raise web.HTTPNotFound(text="Photo introuvable.")
        return path

    async def share_state(self, request):
        return web.json_response(self._share().status())

    async def share_preview(self, request):
        data = await request.json()
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else None
        post, platforms = await asyncio.to_thread(self._share().preview, self._shared_photo(data), fields)
        return web.json_response({"post": post.to_dict(), "platforms": platforms})

    async def share_post(self, request):
        from .apps.share.config import PLATFORMS

        data = await request.json()
        app, path = self._share(), self._shared_photo(data)
        platforms = [p for p in PLATFORMS if p in (data.get("platforms") or []) and app.config.ready(p)]
        if not platforms:
            raise web.HTTPBadRequest(text="Aucune plateforme connectée choisie.")
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        post, _ = await asyncio.to_thread(app.preview, path, fields, ())
        return web.json_response({"ok": True, "message": app.add(path, post, platforms)})

    async def share_retry(self, request):
        return web.json_response({"ok": True, "message": self._share().retry(request.match_info["id"])})

    async def share_remove(self, request):
        if not self._share().remove(request.match_info["id"]):
            raise web.HTTPConflict(text="Publication en cours, ou déjà retirée.")
        return web.json_response({"ok": True})

    async def share_bluesky(self, request):
        data = await request.json()
        handle, password = str(data.get("handle") or ""), str(data.get("password") or "")
        if not (handle and password):
            raise web.HTTPBadRequest(text="Il faut l'identifiant et un mot de passe d'application.")
        account = await self._share_call(self._share().connect_bluesky(handle, password))
        return web.json_response({"ok": True, "account": account})

    async def share_flickr_start(self, request):
        data = await request.json()
        key, secret = str(data.get("key") or ""), str(data.get("secret") or "")
        if not (key and secret):
            raise web.HTTPBadRequest(text="Il faut la clé et le secret de l'API Flickr.")
        return web.json_response({"url": await self._share_call(self._share().flickr_start(key, secret))})

    async def share_flickr_finish(self, request):
        code = str((await request.json()).get("code") or "")
        if not code:
            raise web.HTTPBadRequest(text="Il faut le code donné par Flickr.")
        account = await self._share_call(self._share().flickr_finish(code))
        return web.json_response({"ok": True, "account": account})

    async def share_instagram(self, request):
        data = await request.json()
        user, password, sessionid = (str(data.get(k) or "") for k in ("user", "password", "sessionid"))
        if not (user and (password or sessionid)):
            raise web.HTTPBadRequest(text="Il faut l'identifiant, et le sessionid ou le mot de passe.")
        account = await self._share_call(self._share().connect_instagram(user, password, sessionid))
        return web.json_response({"ok": True, "account": account})

    async def share_defaults(self, request):
        data = await request.json()
        self._share().set_defaults(tags=data.get("tags"), lang=data.get("lang"))
        return web.json_response({"ok": True})

    async def share_disconnect(self, request):
        from .apps.share.config import PLATFORMS

        platform = request.match_info["platform"]
        if platform not in PLATFORMS:
            raise web.HTTPNotFound()
        self._share().disconnect(platform)
        return web.json_response({"ok": True})


def make_thumb(path, cache):
    from PIL import Image

    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with Image.open(path) as im:
        im.draft("RGB", (480, 360))  # the JPEG decoder shrinks for almost nothing
        im = im.convert("RGB")
        im.thumbnail((480, 360))
        tmp = cache + ".part"
        im.save(tmp, "JPEG", quality=80)
    os.replace(tmp, cache)


def install_preset(files):
    """Checks a preset (a .zip of its folder, or its files), then puts it in the user's
    presets folder; returns it. Raises PresetError."""
    if len(files) == 1 and next(iter(files)).lower().endswith(".zip"):
        name, data = next(iter(files.items()))
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = [m for m in z.infolist() if not m.is_dir()]
                # A folder zipped with its name: flatten it.
                files = {}
                for m in members:
                    base = os.path.basename(m.filename)
                    if base and not base.startswith("."):
                        files[base] = z.read(m)
                folder_name = os.path.splitext(name)[0]
        except zipfile.BadZipFile as e:
            raise looks.PresetError(f"archive illisible : {e}") from e
    else:
        folder_name = ""
    if "preset.toml" not in files:
        raise looks.PresetError("il faut un fichier preset.toml")
    os.makedirs(looks.user_dir(), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.path.dirname(looks.user_dir())) as tmp:
        staging = os.path.join(tmp, "preset")
        os.makedirs(staging)
        for base, data in files.items():
            with open(os.path.join(staging, base), "wb") as f:
                f.write(data)
        preset = looks.load_preset(staging)  # raises PresetError
        preset_id = slug(folder_name) if folder_name else slug(preset.name)
        if not PRESET_ID.match(preset_id):
            preset_id = "preset"
        target = os.path.join(looks.user_dir(), preset_id)
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.move(staging, target)
    return looks.load_preset(target)
