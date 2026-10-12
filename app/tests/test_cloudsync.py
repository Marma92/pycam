"""Cloud sync against small fake services (WebDAV, Dropbox, Google Drive) on 127.0.0.1."""

import asyncio
import hashlib
import json
import os
import re
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

    from pycam.apps.cloudsync import providers, sync
    from pycam.apps.cloudsync.config import CloudConfig
    from pycam.apps.cloudsync.state import SentFiles
    from pycam.web import WebServer

    HAVE_AIOHTTP = True
except ImportError:  # Cloud sync runs on the camera, where aiohttp is installed
    HAVE_AIOHTTP = False

from pycam.camera import Camera
from pycam.settings import Settings
from tests.test_camera import FakeEngine

OLD = time.time() - 3600  # files written long ago: settled


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def dropbox_hash(data):
    blocks = b"".join(hashlib.sha256(data[i : i + 4 * 1024 * 1024]).digest() for i in range(0, len(data), 4 * 1024 * 1024))
    return hashlib.sha256(blocks).hexdigest()


class FakeWebDav:
    """ownCloud's WebDAV, for the user ann / secret."""

    def __init__(self, checksums=True):
        self.files, self.folders, self.puts, self.checksums = {}, {""}, 0, checksums

    def app(self):
        app = web.Application(client_max_size=1 << 30)
        app.router.add_route("*", "/remote.php/webdav/{path:.*}", self.handle)
        return app

    async def handle(self, request):
        if request.headers.get("Authorization") != aiohttp.BasicAuth("ann", "secret").encode():
            return web.Response(status=401)
        path = request.match_info["path"].strip("/")
        if request.method == "MKCOL":
            if path in self.folders:
                return web.Response(status=405)
            if os.path.dirname(path) not in self.folders:
                return web.Response(status=409)
            self.folders.add(path)
            return web.Response(status=201)
        if request.method == "PUT":
            if "Transfer-Encoding" in request.headers:
                return web.Response(status=411)  # streamed, but with its length
            data = await request.read()
            if request.headers.get("OC-Checksum") != f"SHA1:{sha1(data)}":
                return web.Response(status=400)
            self.files[path] = data
            self.puts += 1
            return web.Response(status=201)
        if request.method == "PROPFIND":
            if path in self.folders:
                return web.Response(status=207, text='<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>')
            if path not in self.files:
                return web.Response(status=404)
            data = self.files[path]
            checksum = f"<oc:checksums><oc:checksum>SHA1:{sha1(data)} MD5:x</oc:checksum></oc:checksums>" if self.checksums else ""
            body = (
                '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:response><d:propstat><d:prop>'
                f"<d:getcontentlength>{len(data)}</d:getcontentlength>{checksum}</d:prop></d:propstat></d:response></d:multistatus>"
            )
            return web.Response(status=207, text=body)
        return web.Response(status=405)


class FakeDropbox:
    def __init__(self):
        self.files, self.sessions, self.uploads, self.challenge = {}, {}, 0, ""

    def app(self):
        app = web.Application(client_max_size=1 << 30)
        app.router.add_post("/oauth2/token", self.token)
        app.router.add_post("/2/users/get_current_account", self.account)
        app.router.add_post("/2/files/get_metadata", self.metadata)
        app.router.add_post("/2/files/upload", self.upload)
        app.router.add_post("/2/files/upload_session/start", self.start)
        app.router.add_post("/2/files/upload_session/append_v2", self.append)
        app.router.add_post("/2/files/upload_session/finish", self.finish)
        return app

    async def token(self, request):
        form = await request.post()
        if form.get("client_id") != "KEY":
            return web.Response(status=400)
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "R1":
            return web.json_response({"access_token": "A1", "expires_in": 14400})
        if form.get("grant_type") == "authorization_code" and form.get("code") == "CODE":
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            import base64

            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge:
                return web.json_response({"refresh_token": "R1", "access_token": "A1"})
        return web.Response(status=400)

    def _authorized(self, request):
        return request.headers.get("Authorization") == "Bearer A1"

    def _meta(self, path):
        data = self.files[path]
        return {".tag": "file", "path_display": path, "size": len(data), "content_hash": dropbox_hash(data)}

    async def account(self, request):
        if not self._authorized(request):
            return web.Response(status=401)
        return web.json_response({"email": "ann@example.org"})

    async def metadata(self, request):
        if not self._authorized(request):
            return web.Response(status=401)
        path = (await request.json())["path"]
        if path not in self.files:
            return web.json_response({"error_summary": "path/not_found/"}, status=409)
        return web.json_response(self._meta(path))

    def _commit(self, commit, data):
        path = commit["path"]
        if path in self.files and self.files[path] != data:
            assert commit["mode"] == "add" and commit["autorename"]
            stem, ext = os.path.splitext(path)
            path = f"{stem} (1){ext}"
        self.files[path] = data
        self.uploads += 1
        return web.json_response(self._meta(path))

    async def upload(self, request):
        if not self._authorized(request):
            return web.Response(status=401)
        return self._commit(json.loads(request.headers["Dropbox-API-Arg"]), await request.read())

    async def start(self, request):
        session = f"S{len(self.sessions)}"
        self.sessions[session] = await request.read()
        return web.json_response({"session_id": session})

    async def append(self, request):
        cursor = json.loads(request.headers["Dropbox-API-Arg"])["cursor"]
        if len(self.sessions[cursor["session_id"]]) != cursor["offset"]:
            return web.Response(status=409)
        self.sessions[cursor["session_id"]] += await request.read()
        return web.json_response(None)

    async def finish(self, request):
        arg = json.loads(request.headers["Dropbox-API-Arg"])
        data = self.sessions.pop(arg["cursor"]["session_id"]) + await request.read()
        return self._commit(arg["commit"], data)


class FakeGoogle:
    def __init__(self):
        self.files, self.sessions, self.approved, self.uploads = {}, {}, False, 0
        self.base = ""

    def app(self):
        app = web.Application(client_max_size=1 << 30)
        app.router.add_post("/token", self.token)
        app.router.add_post("/device/code", self.device)
        app.router.add_get("/drive/v3/about", self.about)
        app.router.add_get("/drive/v3/files", self.find)
        app.router.add_post("/drive/v3/files", self.folder)
        app.router.add_post("/upload/drive/v3/files", self.start)
        app.router.add_put("/upload/session/{id}", self.put)
        return app

    async def token(self, request):
        form = await request.post()
        if (form.get("client_id"), form.get("client_secret")) != ("CID", "CSECRET"):
            return web.json_response({"error": "invalid_client"}, status=401)
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "GR":
            return web.json_response({"access_token": "GA", "expires_in": 3600})
        if form.get("grant_type", "").endswith("device_code"):
            if not self.approved:
                return web.json_response({"error": "authorization_pending"}, status=428)
            return web.json_response({"refresh_token": "GR", "access_token": "GA"})
        return web.json_response({"error": "invalid_grant"}, status=400)

    async def device(self, request):
        form = await request.post()
        assert form["scope"] == "https://www.googleapis.com/auth/drive.file"
        return web.json_response(
            {"device_code": "DC", "user_code": "ABC-DEF", "verification_url": "https://www.google.com/device",
             "expires_in": 1800, "interval": 1}
        )  # fmt: skip

    def _authorized(self, request):
        return request.headers.get("Authorization") == "Bearer GA"

    async def about(self, request):
        if not self._authorized(request):
            return web.Response(status=401)
        return web.json_response({"user": {"emailAddress": "ann@gmail.example"}})

    def _new(self, **file):
        file_id = f"F{len(self.files)}"
        self.files[file_id] = file
        return file_id

    async def find(self, request):
        if not self._authorized(request):
            return web.Response(status=401)
        m = re.match(r"name = '((?:[^'\\]|\\.)*)' and '([^']+)' in parents and trashed = false", request.query["q"])
        name, parent = re.sub(r"\\(.)", r"\1", m.group(1)), m.group(2)
        folder = "mimeType = 'application/vnd.google-apps.folder'" in request.query["q"]
        found = [
            {"id": i, "size": str(len(f.get("data", b""))), "md5Checksum": hashlib.md5(f.get("data", b"")).hexdigest()}
            for i, f in self.files.items()
            if f["name"] == name and parent in f["parents"] and (not folder or f.get("folder"))
        ]
        return web.json_response({"files": found})

    async def folder(self, request):
        meta = await request.json()
        assert meta["mimeType"] == "application/vnd.google-apps.folder"
        return web.json_response({"id": self._new(name=meta["name"], parents=meta["parents"], folder=True)})

    async def start(self, request):
        if not self._authorized(request) or request.query.get("uploadType") != "resumable":
            return web.Response(status=401)
        session = f"U{len(self.sessions)}"
        self.sessions[session] = (await request.json(), int(request.headers["X-Upload-Content-Length"]))
        return web.Response(headers={"Location": f"{self.base}/upload/session/{session}"})

    async def put(self, request):
        meta, size = self.sessions.pop(request.match_info["id"])
        data = await request.read()
        assert len(data) == size
        file_id = self._new(name=meta["name"], parents=meta["parents"], data=data)
        self.uploads += 1
        return web.json_response({"id": file_id, "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest()})


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "DCIM")
        self.session = aiohttp.ClientSession()
        self.servers = []

    async def asyncTearDown(self):
        await self.session.close()
        for server in self.servers:
            await server.close()
        self.tmp.cleanup()

    async def serve(self, fake):
        server = TestServer(fake.app())
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("")).rstrip("/")

    def write(self, rel, data, mtime=OLD):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        os.utime(path, (mtime, mtime))
        return path


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
class ProvidersTest(Base):
    async def test_webdav(self):
        fake = FakeWebDav()
        url = await self.serve(fake)
        config = CloudConfig(provider="owncloud", url=url, user="ann", password="secret")
        dav = providers.WebDav(config, self.session)
        self.assertEqual(await dav.account(), "ann @ 127.0.0.1")
        a = self.write("100PYCAM/PYC_0001.JPG", b"jpeg" * 1000)
        self.assertEqual(await dav.upload(a, "DCIM/100PYCAM/PYC_0001.JPG"), f"sha1:{sha1(b'jpeg' * 1000)}")
        self.assertEqual(fake.files["pycam/DCIM/100PYCAM/PYC_0001.JPG"], b"jpeg" * 1000)
        await dav.upload(a, "DCIM/100PYCAM/PYC_0001.JPG")  # there already: not sent again
        self.assertEqual(fake.puts, 1)
        b = self.write("x/PYC_0001.JPG", b"another photo")  # the numbering started again
        await dav.upload(b, "DCIM/100PYCAM/PYC_0001.JPG")
        self.assertEqual(fake.files["pycam/DCIM/100PYCAM/PYC_0001.JPG"], b"jpeg" * 1000)
        self.assertEqual(fake.files["pycam/DCIM/100PYCAM/PYC_0001_1.JPG"], b"another photo")
        with self.assertRaises(providers.AuthError):
            await providers.WebDav(CloudConfig(provider="owncloud", url=url, user="ann", password="no"), self.session).account()

    async def test_webdav_without_checksums_never_overwrites(self):
        fake = FakeWebDav(checksums=False)
        url = await self.serve(fake)
        dav = providers.WebDav(CloudConfig(provider="owncloud", url=url, user="ann", password="secret"), self.session)
        a = self.write("100PYCAM/PYC_0001.DNG", b"1" * 100)
        b = self.write("x/PYC_0001.DNG", b"2" * 100)  # same size: as all uncompressed DNG
        await dav.upload(a, "DCIM/100PYCAM/PYC_0001.DNG")
        await dav.upload(b, "DCIM/100PYCAM/PYC_0001.DNG")
        self.assertEqual(fake.files["pycam/DCIM/100PYCAM/PYC_0001.DNG"], b"1" * 100)
        self.assertEqual(fake.files["pycam/DCIM/100PYCAM/PYC_0001_1.DNG"], b"2" * 100)

    async def test_dropbox(self):
        fake = FakeDropbox()
        url = await self.serve(fake)
        with mock.patch.multiple(providers.Dropbox, API=url, CONTENT=url, SINGLE_MAX=1000, SESSION_CHUNK=300):
            link, verifier = providers.Dropbox.authorize_url("KEY")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
            self.assertEqual((query["token_access_type"], query["code_challenge_method"]), (["offline"], ["S256"]))
            fake.challenge = query["code_challenge"][0]
            with self.assertRaises(providers.AuthError):
                await providers.Dropbox.finish(self.session, "KEY", "WRONG", verifier)
            token = await providers.Dropbox.finish(self.session, "KEY", " CODE ", verifier)
            config = CloudConfig(provider="dropbox", client_id="KEY", refresh_token=token)
            box = providers.Dropbox(config, self.session)
            self.assertEqual(await box.account(), "ann@example.org")
            small = self.write("100PYCAM/PYC_0001.JPG", b"s" * 500)
            big = self.write("100PYCAM/PYV_0002.MP4", bytes(range(256)) * 9)  # 2304 bytes: a session of 8 pieces
            await box.upload(small, "DCIM/100PYCAM/PYC_0001.JPG")
            await box.upload(big, "DCIM/100PYCAM/PYV_0002.MP4")
            self.assertEqual(fake.files["/pycam/DCIM/100PYCAM/PYV_0002.MP4"], bytes(range(256)) * 9)
            self.assertEqual(fake.uploads, 2)
            await box.upload(small, "DCIM/100PYCAM/PYC_0001.JPG")  # there already
            self.assertEqual(fake.uploads, 2)
            other = self.write("x/PYC_0001.JPG", b"other")
            await box.upload(other, "DCIM/100PYCAM/PYC_0001.JPG")
            self.assertEqual(fake.files["/pycam/DCIM/100PYCAM/PYC_0001.JPG"], b"s" * 500)
            self.assertEqual(fake.files["/pycam/DCIM/100PYCAM/PYC_0001 (1).JPG"], b"other")
            with self.assertRaises(providers.AuthError):
                await providers.Dropbox(CloudConfig(provider="dropbox", client_id="KEY", refresh_token="old"), self.session).account()

    async def test_google_drive(self):
        fake = FakeGoogle()
        url = await self.serve(fake)
        fake.base = url
        urls = dict(TOKEN=url + "/token", DEVICE=url + "/device/code", API=url + "/drive/v3", UPLOAD=url + "/upload/drive/v3")
        with mock.patch.multiple(providers.GoogleDrive, **urls):
            info = await providers.GoogleDrive.device_start(self.session, "CID")
            self.assertEqual(info["user_code"], "ABC-DEF")
            self.assertIsNone(await providers.GoogleDrive.device_poll(self.session, "CID", "CSECRET", "DC"))
            fake.approved = True
            token = await providers.GoogleDrive.device_poll(self.session, "CID", "CSECRET", "DC")
            config = CloudConfig(provider="gdrive", client_id="CID", client_secret="CSECRET", refresh_token=token)
            drive = providers.GoogleDrive(config, self.session)
            self.assertEqual(await drive.account(), "ann@gmail.example")
            a = self.write("100PYCAM/PYC_0001.JPG", b"a" * 100)
            b = self.write("100PYCAM/PYC_0002.JPG", b"b" * 100)
            await drive.upload(a, "DCIM/100PYCAM/PYC_0001.JPG")
            await drive.upload(b, "DCIM/100PYCAM/PYC_0002.JPG")
            folders = [f["name"] for f in fake.files.values() if f.get("folder")]
            self.assertEqual(folders, ["pycam", "DCIM", "100PYCAM"])  # made once
            await drive.upload(a, "DCIM/100PYCAM/PYC_0001.JPG")  # there already
            self.assertEqual(fake.uploads, 2)
            c = self.write("x/PYC_0001.JPG", b"c")
            await drive.upload(c, "DCIM/100PYCAM/PYC_0001.JPG")
            names = sorted(f["name"] for f in fake.files.values() if not f.get("folder"))
            self.assertEqual(names, ["PYC_0001.JPG", "PYC_0001_1.JPG", "PYC_0002.JPG"])
            # A new process, the folders found again rather than made twice.
            await providers.GoogleDrive(config, self.session).upload(c, "DCIM/100PYCAM/PYC_0003.JPG")
            self.assertEqual(len([f for f in fake.files.values() if f.get("folder")]), 3)


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
class CloudSyncTest(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake = FakeWebDav()
        url = await self.serve(self.fake)
        self.settings = Settings(dcim=self.root)
        self.camera = Camera(self.settings, os.path.join(self.tmp.name, "settings.json"), FakeEngine())
        self.app = sync.CloudSync(
            self.camera, config_path=os.path.join(self.tmp.name, "cloud.json"), db_path=os.path.join(self.tmp.name, "db")
        )
        self.app.config = CloudConfig(provider="owncloud", url=url, user="ann", password="secret", account="ann")
        self.app.sent = SentFiles(self.app.db_path)
        self.camera.apps = [self.app]
        self.write("100PYCAM/PYC_0001.JPG", b"j1")
        self.write("100PYCAM/PYC_0001.DNG", b"d1" * 50)
        self.write("100PYCAM/PYB_0001.JPG", b"b1")
        self.write("100PYCAM/PYC_0002.JPG", b"j2")
        self.write("101PYCAM/PYV_0003.MP4", b"v3" * 100)
        self.write("100PYCAM/PYC_0004.JPG", b"j4", mtime=time.time())  # still being written
        self.write("100PYCAM/notes.txt", b"not a shot")

    async def asyncTearDown(self):
        if self.app._session:
            await self.app._session.close()
        self.app.sent.close()
        await super().asyncTearDown()

    def remote(self):
        return sorted(p.removeprefix("pycam/DCIM/") for p in self.fake.files)

    def test_scan(self):
        shots = sync.scan(self.root)
        names = [[os.path.basename(f.path) for f in shot] for shot in shots]
        self.assertEqual(
            names,
            [["PYB_0001.JPG", "PYC_0001.DNG", "PYC_0001.JPG"], ["PYC_0002.JPG"], ["PYC_0004.JPG"], ["PYV_0003.MP4"]],
        )
        self.assertEqual(shots[0][1].remote, "DCIM/100PYCAM/PYC_0001.DNG")

    async def test_sync_keeps_the_card(self):
        await self.app._pass("sync")
        sent = ["100PYCAM/PYB_0001.JPG", "100PYCAM/PYC_0001.DNG", "100PYCAM/PYC_0001.JPG", "100PYCAM/PYC_0002.JPG", "101PYCAM/PYV_0003.MP4"]
        self.assertEqual(self.remote(), sent)
        self.assertTrue(os.path.exists(os.path.join(self.root, "100PYCAM/PYC_0001.DNG")))
        self.assertEqual(self.app.state, "idle")
        self.assertIn("5 fichiers envoyés", self.camera._message[0])
        await self.app._pass("sync")  # nothing new
        self.assertEqual(self.fake.puts, 5)
        await self.app._count()
        self.assertEqual(self.app.pending, (1, 2))  # PYC_0004, once written

    async def test_offload_frees_what_is_sent(self):
        self.settings.cloud_mode = "offload"
        self.app.wake()
        self.assertIn("effacé", self.camera._message[0])
        await self.app._pass("auto")  # the dial may only be going through it: nothing deleted yet
        self.assertEqual(len(self.fake.files), 5)
        self.assertTrue(os.path.exists(os.path.join(self.root, "100PYCAM/PYC_0001.JPG")))
        self.app._mode_since -= sync.MODE_DELAY
        await self.app._pass("auto")
        self.assertEqual(len(self.fake.files), 5)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "100PYCAM"))), ["PYC_0004.JPG", "notes.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "101PYCAM")))  # emptied, and not the current folder

    async def test_offload_keeps_what_changed_since(self):
        await self.app._pass("sync")
        shot = sync.scan(self.root)[0]
        self.write("100PYCAM/PYC_0001.JPG", b"edited", mtime=OLD + 5)
        self.assertEqual(self.app._free(shot), 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, "100PYCAM/PYB_0001.JPG")))
        await self.app._pass("offload")  # sent again, beside the first one, then freed
        self.assertIn("100PYCAM/PYC_0001_1.JPG", self.remote())
        self.assertFalse(os.path.exists(os.path.join(self.root, "100PYCAM/PYC_0001.JPG")))

    async def test_the_dial_leaving_offload_stops_the_deletions(self):
        self.settings.cloud_mode = "offload"
        self.app._check_mode()
        self.app._mode_since -= sync.MODE_DELAY
        upload = providers.WebDav.upload

        async def leave(provider, local, remote, progress=None):
            self.settings.cloud_mode = "sync"
            return await upload(provider, local, remote, progress)

        with mock.patch.object(providers.WebDav, "upload", leave):
            await self.app._pass("auto")
        self.assertEqual(len(self.fake.files), 5)
        self.assertTrue(os.path.exists(os.path.join(self.root, "100PYCAM/PYC_0001.JPG")))

    async def test_waits_for_the_camera(self):
        self.camera.recording = object()
        with mock.patch.object(sync, "BUSY_POLL", 0.02):
            job = asyncio.create_task(self.app._pass("sync"))
            await asyncio.sleep(0.1)
            self.assertEqual((self.app.state, self.fake.puts), ("paused", 0))
            self.camera.recording = None
            await asyncio.wait_for(job, 5)
        self.assertEqual(self.fake.puts, 5)

    async def test_errors(self):
        self.app.config.password = "wrong"
        await self.app._pass("sync")
        self.assertEqual(self.app.state, "auth")
        self.assertTrue(os.path.exists(os.path.join(self.root, "100PYCAM/PYC_0001.JPG")))
        self.app.config.url = "http://127.0.0.1:9"  # nothing there
        self.app._provider = None
        await self.app._pass("sync")
        self.assertEqual(self.app.state, "error")
        self.assertGreater(self.app._retry_at, time.monotonic() + 30)

    async def run_for(self, until, network=(True, False), seconds=5):
        async def state():
            return network

        self.app.sent.close()  # run() opens its own
        with mock.patch.object(sync, "network_state", state), mock.patch.object(sync, "SETTLE_SECONDS", 0.5):
            task = asyncio.create_task(self.app.run())
            try:
                deadline = time.monotonic() + seconds
                while not until() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.app.sent = SentFiles(self.app.db_path)  # run() closed it

    async def test_run_sends_once_online(self):
        await self.run_for(lambda: len(self.fake.files) == 6)
        self.assertEqual(len(self.fake.files), 6)  # PYC_0004 too, once settled

    async def test_run_does_nothing_on_a_metered_network_or_in_manual(self):
        await self.run_for(lambda: False, network=(True, True), seconds=0.5)
        self.assertEqual(self.fake.puts, 0)
        self.assertEqual(self.app.state_text(), "Réseau facturé · envoi à la main")
        self.settings.cloud_mode = "manual"
        await self.run_for(lambda: False, seconds=0.5)
        self.assertEqual(self.fake.puts, 0)
        self.app.request("sync")
        await self.run_for(lambda: self.fake.puts >= 5)
        self.assertGreaterEqual(self.fake.puts, 5)

    async def test_menu_and_hud(self):
        menu = self.app.menu(self.camera)
        self.assertEqual(menu.item.label, "Mode")
        rows = dict(menu.rows())
        self.assertEqual((rows["Service"], rows["Mode"]), ("ownCloud / Nextcloud", "Synchroniser · garder"))
        self.camera.menu = menu
        self.camera.on_menu_action("front_dial_plus")
        self.assertEqual(self.settings.cloud_mode, "offload")
        menu.move(2)
        self.assertEqual(menu.item.label, "Déverser maintenant")
        self.camera.on_menu_action("front_dial_plus")
        self.assertIsNone(self.app._request)  # a second press confirms
        self.camera.on_menu_action("front_dial_plus")
        self.assertEqual(self.app._request, "offload")
        self.app.state, self.app.progress = "sending", 40
        self.assertEqual(self.camera.view().task, "CLOUD 40 %")
        self.assertEqual(self.app.summary(), "Envoi 40 %")


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
class CloudWebTest(Base):
    async def test_connect_owncloud_from_the_web_page(self):
        fake = FakeWebDav()
        url = await self.serve(fake)
        settings = Settings(dcim=self.root)
        camera = Camera(settings, os.path.join(self.tmp.name, "settings.json"), FakeEngine())
        config_path = os.path.join(self.tmp.name, "cloud.json")
        camera.apps = [sync.CloudSync(camera, config_path=config_path, db_path=os.path.join(self.tmp.name, "db"))]
        client = TestClient(TestServer(WebServer(camera).app))
        await client.start_server()
        try:
            r = await client.post("/api/cloud/owncloud", json={"url": url, "user": "ann", "password": "no"})
            self.assertEqual(r.status, 400)
            r = await client.post("/api/cloud/owncloud", json={"url": url + "/remote.php/webdav/", "user": "ann", "password": "secret", "folder": "/Photos/cam/"})
            self.assertEqual((r.status, (await r.json())["account"]), (200, "ann @ 127.0.0.1"))
            state = await (await client.get("/api/cloud")).text()
            self.assertNotIn("secret", state)
            self.assertEqual(json.loads(state)["config"]["folder"], "Photos/cam")
            self.assertEqual(stat.S_IMODE(os.stat(config_path).st_mode) & 0o077, 0)  # its owner only
            self.assertEqual(CloudConfig.load(config_path).url, url)
            self.assertEqual((await client.post("/api/cloud/mode", json={"mode": "offload"})).status, 200)
            self.assertEqual(settings.cloud_mode, "offload")
            self.assertEqual((await client.post("/api/cloud/mode", json={"mode": "everything"})).status, 400)
            self.assertEqual((await client.post("/api/cloud/action", json={"action": "sync"})).status, 200)
            self.assertEqual((await client.delete("/api/cloud")).status, 200)
            self.assertFalse(CloudConfig.load(config_path).ready)
        finally:
            await client.close()


class UrlTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_AIOHTTP, "aiohttp")
    def test_owncloud_url(self):
        for typed, base in (
            ("cloud.example.org", "https://cloud.example.org"),
            ("https://cloud.example.org/", "https://cloud.example.org"),
            ("https://example.org/nextcloud/remote.php/webdav/", "https://example.org/nextcloud"),
            ("https://example.org/owncloud/index.php/apps/files", "https://example.org/owncloud"),
            ("http://192.168.1.20:8080", "http://192.168.1.20:8080"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(sync.owncloud_url(typed), base)
