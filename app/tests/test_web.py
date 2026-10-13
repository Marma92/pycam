import asyncio
import io
import os
import tempfile
import time
import unittest
import zipfile

try:
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    from pycam.web import WebServer, local_client

    HAVE_AIOHTTP = True
except ImportError:  # the web interface runs on the camera, where aiohttp is installed
    HAVE_AIOHTTP = False

try:
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False

from pycam import looks
from pycam.camera import Camera
from pycam.settings import Settings
from tests.test_camera import FakeEngine


class LocalOnlyTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
    def test_addresses(self):
        for address, ok in (
            ("192.168.1.10", True),
            ("10.0.0.3", True),
            ("127.0.0.1", True),
            ("::1", True),
            ("fe80::1%wlan0", True),
            ("fd12::5", True),
            ("::ffff:192.168.1.5", True),
            ("8.8.8.8", False),
            ("2a01:e0a:9cf:ab90::1", False),  # a public IPv6 address: refused
            ("", False),
        ):
            with self.subTest(address=address):
                self.assertEqual(local_client(address), ok)


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
class WebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME")}
        os.environ["XDG_CACHE_HOME"] = os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        looks.refresh()
        self.settings = Settings(dcim=os.path.join(self.tmp.name, "DCIM"))
        self.engine = FakeEngine()
        self.camera = Camera(self.settings, os.path.join(self.tmp.name, "settings.json"), self.engine)
        self.web = WebServer(self.camera)
        self.client = TestClient(TestServer(self.web.app))
        await self.client.start_server()
        # Paired, as the page does (web_access.py): the other tests go with its cookie.
        await self.pair(self.client)

    async def pair(self, client, name="tests"):
        r = await client.post("/api/access/code")
        self.assertEqual(r.status, 200, await r.text())
        r = await client.post("/api/access/pair", json={"code": self.web.access._code[0], "name": name})
        self.assertEqual(r.status, 200, await r.text())
        self.web.access._last_code -= 60  # the next one at once

    async def asyncTearDown(self):
        await self.client.close()
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        looks.refresh()
        self.tmp.cleanup()

    async def test_only_paired_browsers(self):
        stranger = TestClient(TestServer(self.web.app))
        await stranger.start_server()
        self.addAsyncCleanup(stranger.close)
        r = await stranger.get("/")
        self.assertEqual(r.status, 200)  # the page, to type the code in
        self.assertEqual((await (await stranger.get("/api/access")).json())["paired"], False)
        for path in ("/api/state", "/api/photos", "/api/preview.jpg", "/api/system", "/api/cloud"):
            self.assertEqual((await stranger.get(path)).status, 401, path)
        self.assertEqual((await stranger.post("/api/shutter")).status, 401)
        # The code, on the camera's screen.
        r = await stranger.post("/api/access/code")
        code = self.web.access._code[0]
        self.assertEqual(self.camera.view().message, f"Code du site : {code[:3]} {code[3:]}")
        self.assertEqual((await stranger.post("/api/access/pair", json={"code": "000000" if code != "000000" else "111111"})).status, 403)
        self.assertEqual((await stranger.post("/api/access/pair", json={"code": code, "name": "Firefox · Linux"})).status, 200)
        self.assertEqual((await stranger.get("/api/state")).status, 200)
        names = [b["name"] for b in (await (await stranger.get("/api/access/browsers")).json())["browsers"]]
        self.assertEqual(names, ["tests", "Firefox · Linux"])
        # Forgotten: no more access.
        mine = next(b for b in self.web.access.browsers if b["name"] == "Firefox · Linux")
        await self.client.post("/api/access/forget", json={"id": mine["id"]})
        self.assertEqual((await stranger.get("/api/state")).status, 401)
        # The code off (MENU › Réseau): the whole local network again.
        self.settings.web_code = False
        self.assertEqual((await stranger.get("/api/state")).status, 200)

    async def test_the_clock_from_the_browser(self):
        set_to = []
        self.camera.clock.synced_path = os.path.join(self.tmp.name, "no-ntp")
        self.camera.clock.setter = set_to.append
        r = await (await self.client.post("/api/clock", json={"now": (time.time() + 7200) * 1000})).json()
        self.assertEqual((r["result"], r["synchronized"]), ("set", False))
        self.assertEqual(len(set_to), 1)
        self.assertEqual(self.camera.view().message, "Heure réglée sur le téléphone")

    async def test_wifi(self):
        from tests.test_wifi import FakeNetworkManager

        from pycam.wifi import Wifi

        self.assertEqual((await self.client.get("/api/wifi")).status, 404)  # no NetworkManager
        self.camera.wifi = Wifi(self.camera, run=FakeNetworkManager())
        state = await (await self.client.get("/api/wifi")).json()
        self.assertEqual((state["ssid"], state["hotspot"]), ("Free:box", False))
        networks = (await (await self.client.post("/api/wifi/scan")).json())["networks"]
        self.assertEqual(networks[0]["ssid"], "Free:box")
        self.assertEqual((await self.client.post("/api/wifi/connect", json={"ssid": "Voisins", "password": "court"})).status, 400)
        r = await (await self.client.post("/api/wifi/connect", json={"ssid": "Voisins", "password": "le-bon-mot"})).json()
        self.assertEqual(r["message"], "Connexion à Voisins…")
        await asyncio.sleep(0.05)
        self.assertEqual(self.camera.wifi.ssid, "Voisins")

    async def test_system_and_updates(self):
        from pycam.updates import Updates

        r = await self.client.get("/api/system")
        self.assertEqual(r.status, 404)  # no updates (the tests)
        self.camera.updates = updates = Updates(self.camera, status_path=os.path.join(self.tmp.name, "status.json"), managed=True)
        updates.request_path = os.path.join(self.tmp.name, "update-request")
        state = await (await self.client.get("/api/system")).json()
        self.assertEqual((state["managed"], state["mode"], state["state_text"]), (True, "auto", "Jamais vérifié"))
        r = await self.client.post("/api/system", json={"mode": "notify"})
        self.assertEqual((r.status, self.settings.update_mode), (200, "notify"))
        self.assertEqual((await self.client.post("/api/system", json={"mode": "always"})).status, 400)
        r = await (await self.client.post("/api/system", json={"action": "check"})).json()
        self.assertEqual(r["message"], "Recherche…")
        with open(updates.request_path) as f:
            self.assertEqual(f.read().strip(), "check")
        r = await (await self.client.post("/api/system", json={"action": "install"})).json()
        self.assertEqual(r["message"], "Aucune mise à jour à installer")

    async def test_only_from_its_own_page(self):
        # A page of another site, in a browser on the same network: refused.
        r = await self.client.post("/api/shutter", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/shutter", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        host = f"{self.client.host}:{self.client.port}"
        r = await self.client.post("/api/shutter", headers={"Origin": f"http://{host}"})
        self.assertEqual(r.status, 200)
        # A site's name resolved to the camera ("DNS rebinding"): refused; the camera's own names: fine.
        self.assertEqual((await self.client.get("/api/state", headers={"Host": "evil.example:8080"})).status, 403)
        self.assertEqual((await self.client.get("/api/state", headers={"Host": "pycam.local:8080"})).status, 200)

    async def test_state_shutter_and_preset(self):
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual((state["mode"], state["preset"]), ("P", "standard"))
        self.assertIn("portrait-400", [p["id"] for p in state["presets"]])
        self.assertEqual((await self.client.post("/api/shutter")).status, 200)
        self.assertIn("capture", [name for name, _ in self.engine.calls])
        self.assertEqual((await self.client.post("/api/preset", json={"id": "portrait-400"})).status, 200)
        self.assertEqual(self.settings.preset, "portrait-400")
        self.assertEqual((await self.client.post("/api/preset", json={"id": "nope"})).status, 404)

    @unittest.skipUnless(HAVE_PILLOW, "Pillow")
    async def test_photos(self):
        folder = os.path.join(self.settings.dcim, "100PYCAM")
        os.makedirs(folder)
        Image.new("RGB", (64, 48), (200, 100, 50)).save(os.path.join(folder, "PYC_0001.JPG"))
        open(os.path.join(folder, "PYC_0001.DNG"), "wb").close()
        photos = await (await self.client.get("/api/photos")).json()
        self.assertEqual([(p["name"], p["dng"]) for p in photos], [("PYC_0001", True)])
        thumb = await self.client.get("/api/thumb/100PYCAM/PYC_0001.JPG")
        self.assertEqual((thumb.status, thumb.content_type), (200, "image/jpeg"))
        download = await self.client.get("/api/photo/100PYCAM/PYC_0001.DNG?download=1")
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertEqual((await self.client.get("/api/photo/100PYCAM/..%2Fsecret.JPG")).status, 404)
        deleted = await (await self.client.delete("/api/photo/100PYCAM/PYC_0001.JPG")).json()
        self.assertEqual(sorted(deleted["removed"]), ["PYC_0001.DNG", "PYC_0001.JPG"])
        self.assertEqual(await (await self.client.get("/api/photos")).json(), [])

    async def test_videos(self):
        folder = os.path.join(self.settings.dcim, "101PYCAM")
        os.makedirs(folder)
        with open(os.path.join(folder, "PYT_0008.MP4"), "wb") as f:
            f.write(b"\0" * 100)
        videos = await (await self.client.get("/api/videos")).json()
        self.assertEqual([(v["folder"], v["name"], v["size"]) for v in videos], [("101PYCAM", "PYT_0008.MP4", 100)])
        download = await self.client.get("/api/photo/101PYCAM/PYT_0008.MP4?download=1")
        self.assertEqual(download.status, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        deleted = await (await self.client.delete("/api/photo/101PYCAM/PYT_0008.MP4")).json()
        self.assertEqual(deleted["removed"], ["PYT_0008.MP4"])
        self.assertEqual(await (await self.client.get("/api/videos")).json(), [])

    async def test_install_and_remove_a_preset(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Mon film/preset.toml", 'format = 1\nname = "Mon film"\n[look]\nsaturation = 0.8\n')
        form = FormData()
        form.add_field("file", archive.getvalue(), filename="mon-film.zip")
        reply = await self.client.post("/api/presets", data=form)
        self.assertEqual(reply.status, 200, await reply.text())
        self.assertEqual((await reply.json())["id"], "mon-film")
        self.assertIn("mon-film", looks.registry())  # in the menus at once
        listed = {p["id"]: p for p in await (await self.client.get("/api/presets")).json()}
        self.assertFalse(listed["mon-film"]["builtin"])
        self.assertTrue(listed["standard"]["builtin"])
        bad = FormData()
        bad.add_field("file", b'format = 1\n[look]\nsaturation = 9\n', filename="preset.toml")
        self.assertEqual((await self.client.post("/api/presets", data=bad)).status, 400)
        self.assertEqual((await self.client.delete("/api/presets/standard")).status, 404)  # built in
        self.assertEqual((await self.client.delete("/api/presets/mon-film")).status, 200)
        self.assertNotIn("mon-film", looks.registry())


if __name__ == "__main__":
    unittest.main()
