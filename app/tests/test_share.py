"""Partage: the caption, the pictures, and the platforms against fakes on 127.0.0.1 (Bluesky,
Flickr) or in place of instagrapi."""

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

try:
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from pycam.apps.share import app as share_app
    from pycam.apps.share import publishers
    from pycam.apps.share.config import ShareConfig
    from pycam.web import WebServer

    HAVE_AIOHTTP = True
except ImportError:  # the applications run on the camera, where aiohttp is installed
    HAVE_AIOHTTP = False

try:
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False

from pycam.apps.share import caption
from pycam.camera import Camera
from pycam.settings import Settings
from tests.test_camera import FakeEngine


def make_photo(path, size=(2400, 1800), software="pycam, Cinéma 800T"):
    """A JPEG with the EXIF the engine writes (its UTF-8 in an ASCII field included)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.effect_noise(size, 12).convert("RGB")  # noise: a JPEG as heavy as a real photo
    exif = Image.Exif()
    exif[305] = software.encode()  # as the engine: UTF-8 bytes, which Pillow keeps as they are
    exif[0x8769] = {271: "Raspberry Pi", 272: "imx477", 36867: "2026:10:12 13:44:56", 33434: 0.0167}
    im.save(path, "JPEG", quality=95, exif=exif)
    return path


class CaptionTest(unittest.TestCase):
    def test_caption_blocks(self):
        post = caption.Post(title="Le phare", camera="Raspberry Pi HQ Camera", film="Cinéma 800T", tags="pycam #film")
        self.assertEqual(post.caption, "Le phare.\n.\n📷 Raspberry Pi HQ Camera.\n🎞️ Cinéma 800T.\n.\n#pycam #film")
        self.assertEqual(post.tag_words, ["pycam", "film"])
        self.assertEqual(caption.Post().caption, "")
        self.assertEqual(caption.Post(description="Une description ", tags=" ").caption, "Une description.")

    def test_cut_where_the_limit_falls(self):
        text = "🎞️ " + "a" * 400
        self.assertEqual(caption.graphemes("🎞️"), 1)  # the variation selector does not count
        kept, cut = caption.split_text(text, 300, caption.graphemes)
        self.assertEqual(caption.graphemes(kept), 300)
        self.assertTrue(kept.endswith("…"))
        self.assertEqual(kept[:-1] + cut, text)
        self.assertEqual(caption.split_text("court", 300), ("court", ""))
        self.assertEqual(caption.split_text("x" * 5000, None), ("x" * 5000, ""))

    @unittest.skipUnless(HAVE_PILLOW, "Pillow")
    def test_from_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_photo(os.path.join(tmp, "PYC_0001.JPG"), (64, 48))
            post = caption.from_photo(path, "#pycam")
            self.assertEqual(
                (post.camera, post.film, post.date, post.tags),
                ("Raspberry Pi HQ Camera", "Cinéma 800T", "2026-10-12", "#pycam"),
            )
            make_photo(path, (64, 48), software="pycam, Standard")
            self.assertEqual(caption.from_photo(path).film, "")  # not a film


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
class SignatureTest(unittest.TestCase):
    def test_oauth_signature(self):
        # The example of Twitter's documentation (OAuth 1.0a, HMAC-SHA1).
        params = {
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
            "include_entities": "true",
            "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
            "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": "1318622958",
            "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
            "oauth_version": "1.0",
        }
        signature = publishers.oauth_sign(
            "POST",
            "https://api.twitter.com/1.1/statuses/update.json",
            params,
            "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
            "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        )
        self.assertEqual(signature, "hCtSmYh+iHYCEqBWrE7C7hYmtUk=")

    def test_facets_count_bytes(self):
        text = "Été 🎞️ #film et #pycam"
        tags = publishers.facets(text)
        raw = text.encode()
        self.assertEqual([raw[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode() for f in tags], ["#film", "#pycam"])
        self.assertEqual(tags[0]["features"][0]["tag"], "film")


@unittest.skipUnless(HAVE_PILLOW, "Pillow")
class ImagesTest(unittest.TestCase):
    def test_each_platform_its_picture(self):
        from pycam.apps.share import images

        with tempfile.TemporaryDirectory() as tmp:
            photo = make_photo(os.path.join(tmp, "PYC_0001.JPG"))
            square = images.prepare("instagram", photo, os.path.join(tmp, "ig.jpg"))
            with Image.open(square) as im:
                self.assertEqual(im.size, (1440, 1440))
                self.assertEqual(im.getpixel((5, 5)), (255, 255, 255))  # the white band above
            sky = images.prepare("bluesky", photo, os.path.join(tmp, "bs.jpg"))
            with Image.open(sky) as im:
                self.assertLessEqual(max(im.size), 2000)
            self.assertLessEqual(os.path.getsize(sky), 976 * 1024)
            self.assertEqual(images.prepare("flickr", photo, os.path.join(tmp, "fl.jpg")), photo)


# --- Fakes


class FakeBluesky:
    def __init__(self):
        self.records, self.blobs, self.logins, self.expire_once = [], [], 0, False
        self.base = ""

    def app(self):
        app = web.Application(client_max_size=1 << 24)
        app.router.add_post("/xrpc/com.atproto.server.createSession", self.session)
        app.router.add_post("/xrpc/com.atproto.repo.uploadBlob", self.upload)
        app.router.add_post("/xrpc/com.atproto.repo.createRecord", self.record)
        return app

    async def session(self, request):
        body = await request.json()
        if (body["identifier"], body["password"]) != ("ann.bsky.social", "abcd-efgh-ijkl-mnop"):
            return web.json_response({"error": "AuthenticationRequired", "message": "Invalid identifier or password"}, status=401)
        self.logins += 1
        return web.json_response(
            {
                "did": "did:plc:ann",
                "handle": "ann.bsky.social",
                "accessJwt": f"J{self.logins}",
                "refreshJwt": "R",
                "didDoc": {"service": [{"id": "#atproto_pds", "serviceEndpoint": self.base}]},
            }
        )

    def _authorized(self, request):
        return request.headers.get("Authorization") == f"Bearer J{self.logins}"

    async def upload(self, request):
        if self.expire_once:
            self.expire_once = False
            return web.json_response({"error": "ExpiredToken", "message": "Token has expired"}, status=400)
        if not self._authorized(request):
            return web.json_response({"error": "InvalidToken"}, status=401)
        data = await request.read()
        if len(data) > 1_000_000 or request.headers["Content-Type"] != "image/jpeg":
            return web.json_response({"error": "BlobTooLarge"}, status=400)
        self.blobs.append(data)
        return web.json_response({"blob": {"$type": "blob", "ref": {"$link": "bafy1"}, "mimeType": "image/jpeg", "size": len(data)}})

    async def record(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "InvalidToken"}, status=401)
        body = await request.json()
        assert body["repo"] == "did:plc:ann" and body["collection"] == "app.bsky.feed.post"
        self.records.append(body["record"])
        return web.json_response({"uri": f"at://did:plc:ann/app.bsky.feed.post/3k{len(self.records)}", "cid": "c"})


class FakeFlickr:
    KEY, SECRET = "fkey", "fsecret"

    def __init__(self):
        self.uploads = []

    def app(self):
        app = web.Application(client_max_size=1 << 26)
        app.router.add_get("/services/oauth/request_token", self.request_token)
        app.router.add_get("/services/oauth/access_token", self.access_token)
        app.router.add_get("/services/rest/", self.rest)
        app.router.add_post("/services/upload/", self.upload)
        return app

    def _valid(self, request, params, token_secret=""):
        params = dict(params)
        signature = params.pop("oauth_signature", "")
        url = str(request.url.with_query(None))
        expected = publishers.oauth_sign(request.method, url, params, self.SECRET, token_secret)
        return params.get("oauth_consumer_key") == self.KEY and signature == expected

    async def request_token(self, request):
        if not self._valid(request, request.query) or request.query.get("oauth_callback") != "oob":
            return web.Response(status=401, text="oauth_problem=signature_invalid")
        return web.Response(text="oauth_callback_confirmed=true&oauth_token=RT&oauth_token_secret=RTS")

    async def access_token(self, request):
        q = request.query
        if not self._valid(request, q, "RTS") or q.get("oauth_token") != "RT" or q.get("oauth_verifier") != "123-456-789":
            return web.Response(status=401, text="oauth_problem=token_rejected")
        return web.Response(text="fullname=Ann&oauth_token=AT&oauth_token_secret=ATS&user_nsid=12345%40N00&username=ann")

    async def rest(self, request):
        if not self._valid(request, request.query, "ATS"):
            return web.json_response({"stat": "fail", "code": 98, "message": "Invalid auth token"})
        return web.json_response({"stat": "ok", "user": {"id": "12345@N00", "username": {"_content": "ann"}}})

    async def upload(self, request):
        form = await request.post()
        fields = {k: v for k, v in form.items() if k != "photo"}
        if not self._valid(request, fields, "ATS") or fields.get("oauth_token") != "AT":
            return web.Response(text='<rsp stat="fail"><err code="98" msg="Invalid auth token" /></rsp>')
        self.uploads.append((fields, form["photo"].file.read()))
        return web.Response(text=f'<?xml version="1.0"?><rsp stat="ok"><photoid>{770 + len(self.uploads)}</photoid></rsp>')


# A stand-in for instagrapi, installed where tools/install-share.sh puts the real one: it
# runs in instagram_worker.py's process. A device in its settings, a sessionid that works;
# the uploads go to the file $FAKE_INSTAGRAM_LOG.
FAKE_INSTAGRAPI = """
import json, os, time, types


class LoginRequired(Exception):
    pass


class ChallengeRequired(Exception):
    pass


class Client:
    def __init__(self):
        self.settings = {"uuids": {"phone_id": "device-%d" % time.monotonic_ns()}}
        self.username = None

    def load_settings(self, path):
        with open(path) as f:
            self.settings = json.load(f)

    def dump_settings(self, path):
        with open(path, "w") as f:
            json.dump(self.settings, f)

    def login_by_sessionid(self, sessionid):
        if sessionid != "SID":
            raise LoginRequired("bad session")
        self.username = "ann_ig"
        self.settings["cookies"] = {"sessionid": sessionid}

    def login(self, user, password):
        raise ChallengeRequired("checkpoint")

    def photo_upload(self, path, caption):
        assert os.path.exists(path)
        with open(os.environ["FAKE_INSTAGRAM_LOG"], "a") as f:
            f.write(json.dumps({"path": path, "caption": caption, "device": self.settings["uuids"]["phone_id"]}) + "\\n")
        with open(os.environ["FAKE_INSTAGRAM_LOG"]) as f:
            return types.SimpleNamespace(code="C%d" % len(f.readlines()))
"""


def install_fake_instagrapi(data_home):
    package = os.path.join(data_home, "pycam", "vendor", "instagrapi")
    os.makedirs(package)
    with open(os.path.join(package, "__init__.py"), "w") as f:
        f.write(FAKE_INSTAGRAPI)


def instagram_uploads(log):
    with open(log) as f:
        return [json.loads(line) for line in f]


@unittest.skipUnless(HAVE_AIOHTTP and HAVE_PILLOW, "aiohttp, Pillow")
class ShareTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers = []
        self.bluesky, self.flickr = FakeBluesky(), FakeFlickr()
        self.instagram_log = os.path.join(self.tmp.name, "instagram.log")
        install_fake_instagrapi(os.path.join(self.tmp.name, "xdg"))
        self.bluesky.base = await self.serve(self.bluesky)
        flickr = await self.serve(self.flickr)
        self.patches = [
            mock.patch.object(publishers.Bluesky, "ENTRY", self.bluesky.base),
            mock.patch.multiple(
                publishers.Flickr, OAUTH=flickr + "/services/oauth", REST=flickr + "/services/rest/", UPLOAD=flickr + "/services/upload/"
            ),
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.tmp.name, "xdg"), "FAKE_INSTAGRAM_LOG": self.instagram_log}),
        ]
        for p in self.patches:
            p.start()
        self.root = os.path.join(self.tmp.name, "DCIM")
        self.photo = make_photo(os.path.join(self.root, "100PYCAM", "PYC_0001.JPG"))
        self.settings = Settings(dcim=self.root)
        self.camera = Camera(self.settings, os.path.join(self.tmp.name, "settings.json"), FakeEngine())
        self.config_path = os.path.join(self.tmp.name, "share.json")
        self.share = share_app.Share(self.camera, config_path=self.config_path, data_path=os.path.join(self.tmp.name, "share"))
        self.camera.apps = [self.share]

    async def asyncTearDown(self):
        if self.share._session:
            await self.share._session.close()
        for p in self.patches:
            p.stop()
        for server in self.servers:
            await server.close()
        self.tmp.cleanup()

    async def serve(self, fake):
        server = TestServer(fake.app())
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("")).rstrip("/")

    async def connect_all(self):
        self.assertEqual(await self.share.connect_bluesky("@ann.bsky.social", "abcd-efgh-ijkl-mnop"), "ann.bsky.social")
        page = await self.share.flickr_start("fkey", "fsecret")
        self.assertIn("oauth_token=RT", page)
        self.assertIn("perms=write", page)
        self.assertEqual(await self.share.flickr_finish(" 123-456-789 "), "ann")
        self.assertEqual(await self.share.connect_instagram("ann_ig", sessionid="SID"), "ann_ig")

    async def tick(self, online=True):
        async def state():
            return online, False

        with mock.patch.object(share_app, "network_state", state):
            await self.share._tick()

    async def test_accounts(self):
        with self.assertRaises(publishers.AuthError):
            await self.share.connect_bluesky("ann.bsky.social", "wrong")
        with self.assertRaises(publishers.AuthError):
            await self.share.flickr_finish("123-456-789")  # not started
        await self.connect_all()
        self.assertEqual(self.share.config.configured, ["instagram", "flickr", "bluesky"])
        async with aiohttp.ClientSession() as session:
            self.assertEqual(await publishers.Flickr(self.share.config, session).check(), "ann")
        saved = ShareConfig.load(self.config_path)
        self.assertEqual((saved.flickr_token, saved.flickr_nsid, saved.bluesky_handle), ("AT", "12345@N00", "ann.bsky.social"))
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode) & 0o077, 0)
        device = os.path.join(self.share.data_dir, "instagram.json")
        self.assertEqual(stat.S_IMODE(os.stat(device).st_mode), 0o600)
        status = json.dumps(self.share.status())
        for secret in ("abcd-efgh", "fsecret", "ATS", "SID"):
            self.assertNotIn(secret, status)
        with self.assertRaises(publishers.AuthError) as raised:
            await self.share.connect_instagram("ann_ig", password="pw")  # a new device: a challenge
        self.assertIn("sessionid", str(raised.exception))
        self.share.disconnect("instagram")
        self.assertFalse(os.path.exists(device))
        self.assertFalse(ShareConfig.load(self.config_path).ready("instagram"))

    async def test_publish_everywhere(self):
        await self.connect_all()
        post = caption.from_photo(self.photo, "#pycam #film")
        post.title, post.description = "Le phare", "Une " + "très " * 80 + "longue légende."
        self.share.add(self.photo, post, ["instagram", "flickr", "bluesky"])
        await self.tick(online=False)
        self.assertEqual(len(self.share.queue.entries("pending")), 3)  # waits for the network
        await self.tick()
        item = self.share.queue.items[0]
        urls = {p: e["url"] for p, e in item["platforms"].items()}
        self.assertEqual(
            urls,
            {
                "instagram": "https://www.instagram.com/p/C1/",
                "flickr": "https://www.flickr.com/photos/12345@N00/771/",
                "bluesky": "https://bsky.app/profile/ann.bsky.social/post/3k1",
            },
        )
        record = self.bluesky.records[0]
        self.assertTrue(record["text"].endswith("…"))  # cut at 300 graphemes
        self.assertEqual(caption.graphemes(record["text"]), 300)
        self.assertEqual(record["langs"], ["fr"])
        image = record["embed"]["images"][0]
        ratio = image["aspectRatio"]
        self.assertAlmostEqual(ratio["width"] / ratio["height"], 4 / 3, places=2)
        self.assertLessEqual(max(ratio.values()), 2000)
        self.assertEqual(image["alt"], post.description)
        fields, data = self.flickr.uploads[0]
        self.assertEqual((fields["title"], fields["tags"]), ("Le phare", "pycam film"))
        self.assertIn("🎞️ Cinéma 800T.", fields["description"])
        with open(self.photo, "rb") as f:
            self.assertEqual(data, f.read())  # the original
        (upload,) = instagram_uploads(self.instagram_log)
        self.assertIn("#pycam #film", upload["caption"])
        with open(os.path.join(self.share.data_dir, "instagram.json")) as f:
            self.assertEqual(json.load(f)["uuids"]["phone_id"], upload["device"])  # the device kept since connecting
        self.assertFalse(os.listdir(os.path.join(self.share.data_dir, "prepared")))  # cleaned
        self.assertEqual(self.share.photo_status(self.photo), "Publiée · Instagram, Flickr, Bluesky")

    async def test_failures(self):
        await self.connect_all()
        self.bluesky.expire_once = True  # the session ran out: signed in again, posted
        self.share.add(self.photo, caption.Post(title="a"), ["bluesky"])
        await self.tick()
        self.assertEqual(self.share.queue.items[-1]["platforms"]["bluesky"]["status"], "done")
        self.assertEqual(self.bluesky.logins, 3)  # to connect, to post, and once the session ran out
        # The platform out of reach: again later.
        self.share.config.flickr_token = "revoked"
        self.share._publishers.pop("flickr", None)
        with mock.patch.object(publishers.Flickr, "UPLOAD", "http://127.0.0.1:9/"):
            self.share.add(self.photo, caption.Post(title="b"), ["flickr"])
            await self.tick()
        entry = self.share.queue.items[-1]["platforms"]["flickr"]
        self.assertEqual((entry["status"], entry["attempts"]), ("pending", 1))
        self.assertGreater(entry["next"], time.time() + 30)
        # Refused (a revoked token): waits for a retry by hand.
        entry["next"] = 0
        await self.tick()
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Invalid auth token", entry["error"])
        self.assertEqual(self.share.retry(), "1 publication(s) relancée(s)")
        self.assertEqual(entry["status"], "pending")
        # The photo deleted meanwhile.
        os.remove(self.photo)
        await self.tick()
        self.assertEqual((entry["status"], entry["error"]), ("failed", "la photo n'est plus sur la carte"))

    async def test_waits_for_the_camera_and_survives_a_restart(self):
        await self.connect_all()
        self.share.add(self.photo, caption.Post(), ["bluesky"])
        self.camera.recording = object()
        await self.tick()
        self.assertEqual(self.bluesky.records, [])
        self.share.queue.items[0]["platforms"]["bluesky"]["status"] = "sending"  # cut short
        self.share.queue.save()
        queue = share_app.Queue(self.share.queue.path)
        self.assertEqual(queue.items[0]["platforms"]["bluesky"]["status"], "pending")

    async def test_from_playback(self):
        await self.connect_all()
        self.share.config.selected = ["bluesky"]
        self.camera.on_action("play")
        self.camera.on_action("menu")
        self.assertEqual(self.camera.menu.title, "PYC_0001")
        self.assertEqual(self.camera.menu.item.label, "Partager")
        self.camera.on_action("front_dial_plus")
        menu = self.camera.menu
        self.assertEqual(menu.title, "Partager PYC_0001")
        rows = dict(menu.rows())
        self.assertEqual((rows["Instagram"], rows["Flickr"], rows["Bluesky"]), ("Non", "Non", "Oui"))
        self.assertEqual(rows["Tirée de la photo"], "Cinéma 800T · 2026-10-12 · #pycam")
        self.assertEqual(menu.item.label, "Instagram")
        self.camera.on_action("front_dial_minus")  # a toggle: either way
        self.assertEqual(dict(menu.rows())["Instagram"], "Oui")
        while menu.item.label != "Publier":
            self.camera.on_action("rear_dial_minus")
        self.camera.on_action("front_dial_plus")
        self.assertIsNone(self.camera.menu)
        self.assertTrue(self.camera.playback)  # back to the photo
        item = self.share.queue.items[0]
        self.assertEqual(list(item["platforms"]), ["instagram", "bluesky"])
        self.assertEqual(self.share.config.selected, ["instagram", "bluesky"])
        self.assertEqual(self.share.photo_status(self.photo), "En attente")
        self.share.working = "bluesky"
        self.assertEqual(self.share.hud(), "PARTAGE BLUESKY")

    async def test_web(self):
        client = TestClient(TestServer(WebServer(self.camera).app))
        await client.start_server()
        try:
            r = await client.post("/api/share/bluesky", json={"handle": "ann.bsky.social", "password": "abcd-efgh-ijkl-mnop"})
            self.assertEqual(r.status, 200)
            body = {"folder": "100PYCAM", "name": "PYC_0001", "fields": {"description": "x" * 400}}
            preview = await (await client.post("/api/share/preview", json=body)).json()
            sky = next(p for p in preview["platforms"] if p["platform"] == "bluesky")
            self.assertTrue(sky["ready"] and sky["cut"] and sky["length"] > 300)
            insta = next(p for p in preview["platforms"] if p["platform"] == "instagram")
            self.assertEqual((insta["ready"], insta["cut"]), (False, ""))
            r = await client.post("/api/share/post", json={**body, "platforms": ["bluesky", "instagram"]})
            self.assertEqual(r.status, 200)
            self.assertEqual(list(self.share.queue.items[0]["platforms"]), ["bluesky"])  # the one connected
            self.assertEqual(self.share.queue.items[0]["post"]["description"], "x" * 400)
            r = await client.post("/api/share/post", json={"folder": "100PYCAM", "name": "../../etc/passwd", "platforms": ["bluesky"]})
            self.assertEqual(r.status, 404)
            state = await (await client.get("/api/share")).text()
            self.assertNotIn("abcd-efgh", state)
            item = self.share.queue.items[0]["id"]
            self.assertEqual((await client.delete(f"/api/share/queue/{item}")).status, 200)
            self.assertEqual(self.share.queue.items, [])
        finally:
            await client.close()
