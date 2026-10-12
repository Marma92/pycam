"""The web interface: the camera from a phone or a computer on the same network
(docs/architecture.md §5).

- the viewfinder, roughly once a second, and a remote shutter;
- the preset and its grain;
- the photos: thumbnails, download (JPEG, DNG), deletion;
- the presets: install one made on a computer, remove it.

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

from aiohttp import web

from . import looks
from .playback import companions, describe, list_photos

log = logging.getLogger(__name__)

PORT = 8080
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
PREVIEW = "/run/pycam/web-preview.jpg"  # tmpfs, written by the engine
PREVIEW_MAX_AGE = 0.5  # seconds: two browsers watching share the same frame
MAX_UPLOAD = 32 * 1024**2
FOLDER = re.compile(r"^\d{3}PYCAM$")
SHOT = re.compile(r"^PY[CB]_\d{4}\.(JPG|DNG)$")
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
                web.get("/api/preview.jpg", self.preview),
                web.get("/api/photos", self.photos),
                web.get("/api/thumb/{folder}/{name}", self.thumb),
                web.get("/api/photo/{folder}/{name}", self.photo),
                web.delete("/api/photo/{folder}/{name}", self.delete_photo),
                web.get("/api/presets", self.presets),
                web.post("/api/presets", self.install_preset),
                web.delete("/api/presets/{id}", self.remove_preset),
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
                "drive_running": view.drive_running,
                "countdown": view.countdown,
                "exposure_timer": view.exposure_timer,
                "exposure_label": view.exposure_label,
                "preset": preset.id,
                "grain": s.grain,
                "grain_label": looks.grain_label(preset),
                "presets": [{"id": p.id, "name": p.name} for p in looks.registry().values()],
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
        if not path.endswith(".JPG") or os.path.basename(path).startswith("PYB_"):
            raise web.HTTPBadRequest(text="On supprime une prise par son JPEG.")
        removed = []
        for f in companions(path):
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
