import io
import os
import tempfile
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
        self.client = TestClient(TestServer(WebServer(self.camera).app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        looks.refresh()
        self.tmp.cleanup()

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
