"""The cloud services Cloud sync sends to (docs/architecture.md §5).

Each one sends a file as a stream (never whole in memory: a video may weigh gigabytes),
then checks it is there whole before it counts as sent, the way the service allows:
- ownCloud / Nextcloud (WebDAV, an app password): the server checks the SHA1 given with
  the file (ownCloud), and gives back its size;
- Dropbox (the user's own app, PKCE: no secret): its content hash, computed here too;
  above 150 MB, in a session of 8 MB pieces;
- Google Drive (the user's own client, "TV and limited input devices", drive.file: only
  the files it made): their MD5; a resumable upload.
Dropbox and Google Drive give short-lived access tokens, renewed from the refresh token.

Never over another file: once the camera is emptied, the cloud holds the only copy, and
the numbering may start again (settings reset). A file of the same name and the same
content counts as sent; of another content, this one goes next to it (PYC_0001_1.JPG).
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp

CHUNK = 1 << 20  # read at a time
TIMEOUT = aiohttp.ClientTimeout(total=None, connect=20, sock_read=120)


class CloudError(Exception):
    """The service refused, or could not be reached."""


class AuthError(CloudError):
    """The account must be connected again."""


def file_hash(path, kind):
    """md5, sha1, or "dropbox" (SHA-256 of the SHA-256 of each 4 MB block). Slow: in a thread."""
    if kind == "dropbox":
        blocks = hashlib.sha256()
        with open(path, "rb") as f:
            while block := f.read(4 * 1024 * 1024):
                blocks.update(hashlib.sha256(block).digest())
        return blocks.hexdigest()
    h = hashlib.new(kind)
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


async def _chunks(path, progress=None, start=0, length=None):
    """The file (or a part of it) as it is read, for aiohttp to send; progress(bytes sent)."""
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        f.seek(start)
        left = os.path.getsize(path) - start if length is None else length
        sent = start
        while left > 0:
            data = await loop.run_in_executor(None, f.read, min(CHUNK, left))
            if not data:
                break
            left -= len(data)
            sent += len(data)
            if progress:
                progress(sent)
            yield data


def _names(path, tries=100):
    """The path, then the same with _1, _2... before the extension."""
    stem, ext = os.path.splitext(path)
    yield path
    for n in range(1, tries):
        yield f"{stem}_{n}{ext}"


async def _check(response, what):
    if response.status in (401, 403):
        raise AuthError(f"{what} : accès refusé ({response.status})")
    if response.status >= 400:
        text = (await response.text())[:200]
        raise CloudError(f"{what} : HTTP {response.status} {text}")


class Provider:
    def __init__(self, config, session):
        self.config = config
        self.session = session

    async def account(self):
        """Who the account is, checking it can be reached."""
        raise NotImplementedError

    async def upload(self, local, remote, progress=None):
        """Sends `local` as `remote` (a relative path: DCIM/100PYCAM/PYC_0001.JPG), checks
        it; returns its checksum ("sha1:…"). Raises CloudError."""
        raise NotImplementedError


# --- ownCloud / Nextcloud


class WebDav(Provider):
    def __init__(self, config, session):
        super().__init__(config, session)
        self.base = config.url.rstrip("/") + "/remote.php/webdav"
        self.auth = aiohttp.BasicAuth(config.user, config.password)
        self._folders = set()

    def _url(self, path):
        return self.base + "/" + urllib.parse.quote(path.strip("/"))

    async def account(self):
        async with self.session.request("PROPFIND", self.base + "/", auth=self.auth, headers={"Depth": "0"}) as r:
            await _check(r, "connexion")
        return f"{self.config.user} @ {urllib.parse.urlsplit(self.config.url).hostname}"

    async def _folder(self, path):
        done = ""
        for part in path.strip("/").split("/"):
            done = f"{done}/{part}" if done else part
            if done in self._folders:
                continue
            async with self.session.request("MKCOL", self._url(done) + "/", auth=self.auth) as r:
                if r.status not in (201, 405):  # 405: there already
                    await _check(r, "dossier")
            self._folders.add(done)

    async def _remote(self, path):
        """(size, SHA1 or "" if the server does not keep it) of a file there, or None."""
        body = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop><d:getcontentlength/><oc:checksums/></d:prop></d:propfind>"
        )
        headers = {"Depth": "0", "Content-Type": "application/xml"}
        async with self.session.request("PROPFIND", self._url(path), data=body, headers=headers, auth=self.auth) as r:
            if r.status == 404:
                return None
            await _check(r, "vérification")
            text = await r.text()
        size, checksums = _propfind(text)
        sums = {s.split(":", 1)[0].upper(): s.split(":", 1)[1].lower() for s in checksums.split() if ":" in s}
        return size, sums.get("SHA1", "")

    async def upload(self, local, remote, progress=None):
        size = os.path.getsize(local)
        sha1 = await asyncio.to_thread(file_hash, local, "sha1")
        await self._folder(os.path.dirname(f"{self.config.folder}/{remote}"))
        for path in _names(f"{self.config.folder}/{remote}"):
            there = await self._remote(path)
            if there is None:
                break
            if there == (size, sha1):
                return f"sha1:{sha1}"  # there already (the record of it was lost)
        else:
            raise CloudError(f"{remote} : trop de fichiers de ce nom sur le serveur")
        headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream", "OC-Checksum": f"SHA1:{sha1}"}
        async with self.session.put(self._url(path), data=_chunks(local, progress), headers=headers, auth=self.auth) as r:
            await _check(r, "envoi")
        there = await self._remote(path)
        if there is None or there[0] != size:
            raise CloudError(f"{remote} : {there and there[0]} octets sur le serveur, {size} ici")
        if there[1] and there[1] != sha1:
            raise CloudError(f"{remote} : empreinte différente sur le serveur")
        return f"sha1:{sha1}"


def _propfind(text):
    """(size, checksums) of a one-file PROPFIND answer."""
    root = ET.fromstring(text)
    size = root.find(".//{DAV:}getcontentlength")
    checksum = root.find(".//{http://owncloud.org/ns}checksum")
    return (int(size.text) if size is not None and size.text else None), (checksum.text or "") if checksum is not None else ""


# --- Dropbox


class Dropbox(Provider):
    API = "https://api.dropboxapi.com"
    CONTENT = "https://content.dropboxapi.com"
    AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
    SINGLE_MAX = 150 * 1024 * 1024  # above: an upload session
    SESSION_CHUNK = 8 * 1024 * 1024

    def __init__(self, config, session):
        super().__init__(config, session)
        self._access, self._expires = "", 0.0

    async def _headers(self):
        if not self._access or time.monotonic() > self._expires:
            data = {"grant_type": "refresh_token", "refresh_token": self.config.refresh_token, "client_id": self.config.client_id}
            async with self.session.post(self.API + "/oauth2/token", data=data) as r:
                if r.status in (400, 401):
                    raise AuthError("Dropbox : la connexion est à refaire")
                await _check(r, "jeton")
                js = await r.json()
            self._access = js["access_token"]
            self._expires = time.monotonic() + js.get("expires_in", 14400) - 60
        return {"Authorization": f"Bearer {self._access}"}

    async def account(self):
        async with self.session.post(self.API + "/2/users/get_current_account", headers=await self._headers()) as r:
            await _check(r, "compte")
            js = await r.json()
        return js.get("email") or js.get("name", {}).get("display_name") or "Dropbox"

    async def upload(self, local, remote, progress=None):
        size = os.path.getsize(local)
        expected = await asyncio.to_thread(file_hash, local, "dropbox")
        path = f"/{self.config.folder}/{remote}"
        async with self.session.post(self.API + "/2/files/get_metadata", json={"path": path}, headers=await self._headers()) as r:
            if r.status == 409:  # not there
                there = {}
            else:
                await _check(r, "vérification")
                there = await r.json()
        if there.get("content_hash") == expected:
            return f"dropbox:{expected}"  # there already (the record of it was lost)
        # "add": next to another file of the same name ("PYC_0001 (1).JPG"), unless the same.
        commit = {"path": path, "mode": "add", "autorename": True, "mute": True}

        async def post(url, arg, start, length):
            headers = {
                **await self._headers(),
                "Dropbox-API-Arg": json.dumps(arg),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(length),
            }
            async with self.session.post(self.CONTENT + url, data=_chunks(local, progress, start, length), headers=headers) as r:
                await _check(r, "envoi")
                return await r.json(content_type=None) or {}

        if size <= self.SINGLE_MAX:
            meta = await post("/2/files/upload", commit, 0, size)
        else:
            session_id, offset = None, 0
            while offset < size:
                length = min(self.SESSION_CHUNK, size - offset)
                if session_id is None:
                    session_id = (await post("/2/files/upload_session/start", {"close": False}, offset, length))["session_id"]
                elif offset + length >= size:
                    cursor = {"session_id": session_id, "offset": offset}
                    meta = await post("/2/files/upload_session/finish", {"cursor": cursor, "commit": commit}, offset, length)
                else:
                    cursor = {"session_id": session_id, "offset": offset}
                    await post("/2/files/upload_session/append_v2", {"cursor": cursor, "close": False}, offset, length)
                offset += length
        if meta.get("size") != size or meta.get("content_hash") != expected:
            raise CloudError(f"{remote} : Dropbox n'a pas le même fichier")
        return f"dropbox:{expected}"

    @classmethod
    def authorize_url(cls, app_key):
        """Where the user lets the app in (Dropbox then shows a code to paste here), and the
        PKCE verifier to keep for finish()."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = {
            "client_id": app_key,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "token_access_type": "offline",
        }
        return f"{cls.AUTHORIZE}?{urllib.parse.urlencode(query)}", verifier

    @classmethod
    async def finish(cls, session, app_key, code, verifier):
        """The refresh token for the code the user pasted."""
        data = {"code": code.strip(), "grant_type": "authorization_code", "code_verifier": verifier, "client_id": app_key}
        async with session.post(cls.API + "/oauth2/token", data=data) as r:
            if r.status == 400:
                raise AuthError("Dropbox refuse ce code (expiré, ou mal recopié ?)")
            await _check(r, "jeton")
            return (await r.json())["refresh_token"]


# --- Google Drive


class GoogleDrive(Provider):
    TOKEN = "https://oauth2.googleapis.com/token"
    DEVICE = "https://oauth2.googleapis.com/device/code"
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"
    SCOPE = "https://www.googleapis.com/auth/drive.file"
    FOLDER = "application/vnd.google-apps.folder"

    def __init__(self, config, session):
        super().__init__(config, session)
        self._access, self._expires = "", 0.0
        self._folders = {}

    async def _headers(self):
        if not self._access or time.monotonic() > self._expires:
            data = {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self.config.refresh_token,
                "grant_type": "refresh_token",
            }
            async with self.session.post(self.TOKEN, data=data) as r:
                if r.status in (400, 401):
                    raise AuthError("Google Drive : la connexion est à refaire")
                await _check(r, "jeton")
                js = await r.json()
            self._access = js["access_token"]
            self._expires = time.monotonic() + js.get("expires_in", 3600) - 60
        return {"Authorization": f"Bearer {self._access}"}

    async def account(self):
        params = {"fields": "user(emailAddress,displayName)"}
        async with self.session.get(self.API + "/about", params=params, headers=await self._headers()) as r:
            await _check(r, "compte")
            user = (await r.json()).get("user", {})
        return user.get("emailAddress") or user.get("displayName") or "Google Drive"

    async def _find(self, name, parent, folder=False):
        quoted = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name = '{quoted}' and '{parent}' in parents and trashed = false"
        if folder:
            q += f" and mimeType = '{self.FOLDER}'"
        params = {"q": q, "fields": "files(id,size,md5Checksum)", "spaces": "drive"}
        async with self.session.get(self.API + "/files", params=params, headers=await self._headers()) as r:
            await _check(r, "recherche")
            return (await r.json()).get("files", [])

    async def _folder(self, path):
        """The id of a folder (made if needed), from the Drive's root."""
        parent, done = "root", ""
        for part in path.strip("/").split("/"):
            done = f"{done}/{part}"
            if done not in self._folders:
                found = await self._find(part, parent, folder=True)
                if found:
                    self._folders[done] = found[0]["id"]
                else:
                    meta = {"name": part, "mimeType": self.FOLDER, "parents": [parent]}
                    async with self.session.post(
                        self.API + "/files", params={"fields": "id"}, json=meta, headers=await self._headers()
                    ) as r:
                        await _check(r, "dossier")
                        self._folders[done] = (await r.json())["id"]
            parent = self._folders[done]
        return parent

    async def upload(self, local, remote, progress=None):
        size = os.path.getsize(local)
        md5 = await asyncio.to_thread(file_hash, local, "md5")
        folder, name = os.path.split(f"{self.config.folder}/{remote}")
        parent = await self._folder(folder)
        for candidate in _names(name):
            existing = await self._find(candidate, parent)
            if any(f.get("md5Checksum") == md5 for f in existing):
                return f"md5:{md5}"  # there already (the record of it was lost)
            if not existing:
                break
        else:
            raise CloudError(f"{remote} : trop de fichiers de ce nom sur le Drive")
        headers = {
            **await self._headers(),
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(size),
        }
        params = {"uploadType": "resumable", "fields": "id,size,md5Checksum"}
        meta = {"name": candidate, "parents": [parent]}
        async with self.session.post(f"{self.UPLOAD}/files", params=params, json=meta, headers=headers) as r:
            await _check(r, "envoi")
            location = r.headers["Location"]
        async with self.session.put(location, data=_chunks(local, progress), headers={"Content-Length": str(size)}) as r:
            await _check(r, "envoi")
            js = await r.json()
        if int(js.get("size", -1)) != size or js.get("md5Checksum") != md5:
            raise CloudError(f"{remote} : Google Drive n'a pas le même fichier")
        return f"md5:{md5}"

    @classmethod
    async def device_start(cls, session, client_id):
        """{user_code, verification_url, device_code, interval, expires_in}: the user types
        the code at the address, from a phone or a computer."""
        async with session.post(cls.DEVICE, data={"client_id": client_id, "scope": cls.SCOPE}) as r:
            if r.status in (400, 401):
                raise AuthError("Google refuse ce client (identifiant, type « TV et appareils à entrée limitée » ?)")
            await _check(r, "connexion")
            return await r.json()

    @classmethod
    async def device_poll(cls, session, client_id, client_secret, device_code):
        """The refresh token once the user said yes; None while waiting. Raises AuthError
        if refused or expired."""
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        async with session.post(cls.TOKEN, data=data) as r:
            js = await r.json(content_type=None)
        if "refresh_token" in js:
            return js["refresh_token"]
        if js.get("error") in ("authorization_pending", "slow_down"):
            return None
        raise AuthError(f"Google : {js.get('error_description') or js.get('error') or 'refusé'}")


PROVIDER_CLASSES = {"owncloud": WebDav, "dropbox": Dropbox, "gdrive": GoogleDrive}


def make_provider(config, session):
    return PROVIDER_CLASSES[config.provider](config, session)
