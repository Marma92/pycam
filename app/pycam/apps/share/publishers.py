"""The platforms a photo is shared on, after SPP's publishers (docs/architecture.md §5).

- Bluesky: its XRPC API, with an app password; the hashtags as real facets, the alt text,
  the aspect ratio and the language.
- Flickr: its API with the user's own key, OAuth 1.0a with a code to paste (nothing to
  redirect to on a camera); the original file.
- Instagram: its private API through instagrapi, as SPP does (personal accounts have no
  official way to post); a 1440² square. Signed in with a browser's sessionid rather than
  the password, past the verification Instagram asks of a new device.

Each says what it publishes of a caption (render(): no network, for the preview) and
raises ShareError (worth trying again: the network, the platform busy) or Refused (the
platform said no: by hand, once fixed); AuthError when the account must be connected again.
"""

import asyncio
import base64
import datetime
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp
import yarl

from .caption import TAG, graphemes, split_text

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=300, connect=20)


class ShareError(Exception):
    retry = True


class Refused(ShareError):
    retry = False


class AuthError(Refused):
    """The account must be connected again."""


async def _check(response, what):
    if response.status in (401, 403):
        raise AuthError(f"{what} : accès refusé ({response.status})")
    if response.status == 429 or response.status >= 500:
        raise ShareError(f"{what} : HTTP {response.status}, réessayer plus tard")
    if response.status >= 400:
        raise Refused(f"{what} : HTTP {response.status} {(await response.text())[:200]}")


class Publisher:
    name = ""
    label = ""
    image_label = ""
    limit = None  # in the platform's own unit; None: none

    def __init__(self, config, session, data_dir=""):
        self.config = config
        self.session = session
        self.data_dir = data_dir

    @staticmethod
    def measure(text):
        return len(text)

    @staticmethod
    def normalise(text):
        return text

    @classmethod
    def render(cls, post):
        """(published, cut): the caption as the platform will have it."""
        return split_text(cls.normalise(post.caption), cls.limit, cls.measure)

    async def check(self):
        """Who the account is, checking it can be used."""
        raise NotImplementedError

    async def publish(self, post, image, text):
        """Posts; returns the post's address."""
        raise NotImplementedError


# --- Bluesky


def facets(text):
    """The hashtags as Bluesky wants them: tags at their byte offsets in UTF-8."""
    out = []
    for m in TAG.finditer(text):
        start = len(text[: m.start()].encode())
        out.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(m.group(0).encode())},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}],
            }
        )
    return out


class Bluesky(Publisher):
    name, label, image_label = "bluesky", "Bluesky", "2000 px, moins de 1 Mo"
    limit = 300  # graphemes
    ENTRY = "https://bsky.social"

    def __init__(self, config, session, data_dir=""):
        super().__init__(config, session, data_dir)
        self._pds = self._jwt = self._did = self._handle = ""

    @staticmethod
    def measure(text):
        return graphemes(text)

    @staticmethod
    def normalise(text):
        return unicodedata.normalize("NFC", text)

    async def _login(self):
        body = {"identifier": self.config.bluesky_handle.lstrip("@"), "password": self.config.bluesky_password}
        async with self.session.post(self.ENTRY + "/xrpc/com.atproto.server.createSession", json=body) as r:
            if r.status in (400, 401):
                raise AuthError("Bluesky refuse cet identifiant ou ce mot de passe d'application")
            await _check(r, "connexion")
            js = await r.json()
        self._did, self._jwt, self._handle = js["did"], js["accessJwt"], js.get("handle", body["identifier"])
        self._pds = self.ENTRY
        for service in (js.get("didDoc") or {}).get("service", []):  # the account's own server
            if service.get("id") == "#atproto_pds" and service.get("serviceEndpoint"):
                self._pds = service["serviceEndpoint"].rstrip("/")

    async def check(self):
        await self._login()
        return self._handle

    async def _xrpc(self, method, **kwargs):
        headers = {"Authorization": f"Bearer {self._jwt}", **kwargs.pop("headers", {})}
        async with self.session.post(f"{self._pds}/xrpc/{method}", headers=headers, **kwargs) as r:
            if r.status in (400, 401):
                js = await r.json(content_type=None) or {}
                if js.get("error") in ("ExpiredToken", "InvalidToken", "AuthMissing") or r.status == 401:
                    raise AuthError(js.get("message") or "session expirée")
                raise Refused(f"Bluesky : {js.get('message') or js.get('error') or r.status}")
            await _check(r, "Bluesky")
            return await r.json()

    async def publish(self, post, image, text):
        if not self._jwt:
            await self._login()
        try:
            return await self._publish(post, image, text)
        except AuthError:
            await self._login()  # the session ran out: once more, with a new one
            return await self._publish(post, image, text)

    async def _publish(self, post, image, text):
        with open(image, "rb") as f:
            data = f.read()
        size = await asyncio.to_thread(_image_size, image)
        blob = (await self._xrpc("com.atproto.repo.uploadBlob", data=data, headers={"Content-Type": "image/jpeg"}))["blob"]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": now,
            "langs": [self.config.lang or "fr"],
            "embed": {
                "$type": "app.bsky.embed.images",
                # Alt text is expected on Bluesky: the title or the description if none.
                "images": [
                    {
                        "alt": post.alt or post.description or post.title or "",
                        "image": blob,
                        "aspectRatio": {"width": size[0], "height": size[1]},
                    }
                ],
            },
        }
        if tags := facets(text):
            record["facets"] = tags
        body = {"repo": self._did, "collection": "app.bsky.feed.post", "record": record}
        uri = (await self._xrpc("com.atproto.repo.createRecord", json=body))["uri"]
        return f"https://bsky.app/profile/{self._handle}/post/{uri.rsplit('/', 1)[-1]}"


def _image_size(path):
    from PIL import Image

    with Image.open(path) as im:
        return im.size


# --- Flickr


def _enc(value):
    return urllib.parse.quote(str(value), safe="~-._")


def oauth_sign(method, url, params, consumer_secret, token_secret=""):
    """OAuth 1.0a, HMAC-SHA1 (RFC 5849 §3.4)."""
    pairs = sorted((_enc(k), _enc(v)) for k, v in params.items())
    base = "&".join((method.upper(), _enc(url), _enc("&".join(f"{k}={v}" for k, v in pairs))))
    key = f"{_enc(consumer_secret)}&{_enc(token_secret)}"
    return base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


def _oauth(key, token="", **extra):
    params = {
        "oauth_consumer_key": key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
        **extra,
    }
    if token:
        params["oauth_token"] = token
    return params


def _signed_url(url, params, secret, token_secret=""):
    params = {**params, "oauth_signature": oauth_sign("GET", url, params, secret, token_secret)}
    query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in params.items())
    return yarl.URL(f"{url}?{query}", encoded=True)  # as signed: no other quoting


class Flickr(Publisher):
    name, label, image_label = "flickr", "Flickr", "l'original"
    OAUTH = "https://www.flickr.com/services/oauth"
    REST = "https://www.flickr.com/services/rest/"
    UPLOAD = "https://up.flickr.com/services/upload/"

    async def check(self):
        c = self.config
        params = _oauth(c.flickr_key, c.flickr_token, method="flickr.test.login", format="json", nojsoncallback="1")
        async with self.session.get(_signed_url(self.REST, params, c.flickr_secret, c.flickr_token_secret)) as r:
            await _check(r, "Flickr")
            js = await r.json(content_type=None)
        if js.get("stat") != "ok":
            raise AuthError(f"Flickr : {js.get('message', 'refusé')}")
        return js["user"]["username"]["_content"]

    async def publish(self, post, image, text):
        c = self.config
        params = _oauth(c.flickr_key, c.flickr_token, title=post.title, description=text, tags=" ".join(post.tag_words))
        params["oauth_signature"] = oauth_sign("POST", self.UPLOAD, params, c.flickr_secret, c.flickr_token_secret)
        form = aiohttp.FormData()
        for k, v in params.items():
            form.add_field(k, v)
        with open(image, "rb") as f:
            form.add_field("photo", f, filename=os.path.basename(image), content_type="image/jpeg")
            async with self.session.post(self.UPLOAD, data=form) as r:
                await _check(r, "Flickr")
                root = ET.fromstring(await r.text())
        if root.get("stat") != "ok":
            err = root.find("err")
            code, msg = (err.get("code"), err.get("msg")) if err is not None else ("", "refusé")
            raise (AuthError if code in ("96", "97", "98", "99", "100") else Refused)(f"Flickr : {msg}")
        photo = root.findtext("photoid")
        if c.flickr_nsid:
            return f"https://www.flickr.com/photos/{c.flickr_nsid}/{photo}/"
        return f"https://www.flickr.com/photo.gne?id={photo}"

    @classmethod
    async def request_token(cls, session, key, secret):
        """(the page where the user lets the key in, which then shows a code; the request
        token and its secret, for access_token())."""
        url = cls.OAUTH + "/request_token"
        async with session.get(_signed_url(url, _oauth(key, oauth_callback="oob"), secret)) as r:
            if r.status in (400, 401):
                raise AuthError("Flickr refuse cette clé (ou ce secret)")
            await _check(r, "Flickr")
            data = dict(urllib.parse.parse_qsl(await r.text()))
        token, token_secret = data["oauth_token"], data["oauth_token_secret"]
        page = f"{cls.OAUTH}/authorize?{urllib.parse.urlencode({'oauth_token': token, 'perms': 'write'})}"
        return page, token, token_secret

    @classmethod
    async def access_token(cls, session, key, secret, token, token_secret, verifier):
        """{oauth_token, oauth_token_secret, username, user_nsid} for the code shown by Flickr."""
        url = cls.OAUTH + "/access_token"
        params = _oauth(key, token, oauth_verifier=verifier.strip())
        async with session.get(_signed_url(url, params, secret, token_secret)) as r:
            if r.status in (400, 401):
                raise AuthError("Flickr refuse ce code (expiré, ou mal recopié ?)")
            await _check(r, "Flickr")
            return dict(urllib.parse.parse_qsl(await r.text()))


# --- Instagram


def vendor_dir():
    """Where instagrapi is installed for the camera (tools/install-share.sh): out of the
    system's Python, which has no Debian package of it."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "pycam", "vendor")


def instagrapi_missing():
    """Why Instagram cannot be used here, or ""."""
    vendor = vendor_dir()
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.append(vendor)
    try:
        import instagrapi  # noqa: F401
    except ImportError:
        return "instagrapi n'est pas installé (tools/install-share.sh)"
    return ""


# instagrapi's exceptions that mean: sign in again (by their names: it need not be there).
AUTH_FAILURES = ("LoginRequired", "ChallengeRequired", "BadPassword", "TwoFactorRequired", "ChallengeUnknownStep",
                 "SelectContactPointRecoveryForm", "RecaptchaChallengeForm", "ReloginAttemptExceeded")  # fmt: skip
RETRY_LATER = ("PleaseWaitFewMinutes", "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout")


class Instagram(Publisher):
    name, label, image_label = "instagram", "Instagram", "carré de 1440 px, bandes blanches"
    limit = 2200

    def __init__(self, config, session, data_dir=""):
        super().__init__(config, session, data_dir)
        self._client = None

    @property
    def settings_path(self):
        """instagrapi's device and session: Instagram recognises the camera by it."""
        return os.path.join(self.data_dir, "instagram.json")

    def _dump(self, client):
        os.makedirs(self.data_dir, exist_ok=True)
        client.dump_settings(self.settings_path)
        os.chmod(self.settings_path, 0o600)

    def _connect(self):
        if missing := instagrapi_missing():
            raise Refused(missing)
        from instagrapi import Client

        client = Client()
        if os.path.exists(self.settings_path):
            client.load_settings(self.settings_path)
        else:
            # The device made now is kept before signing in: a verification aborts the
            # login, and coming back as another device only brings another (SPP).
            self._dump(client)
        c = self.config
        try:
            if c.instagram_sessionid:
                client.login_by_sessionid(c.instagram_sessionid)
            else:
                client.login(c.instagram_user, c.instagram_password)
        finally:
            self._dump(client)
        return client

    def _call(self, work):
        """work(client) in this thread, signed in; instagrapi's errors as ours."""
        try:
            if self._client is None:
                self._client = self._connect()
            try:
                return work(self._client)
            finally:
                self._dump(self._client)
        except ShareError:
            raise
        except Exception as e:  # instagrapi's own, and requests'
            kind = type(e).__name__
            message = f"Instagram : {kind} {e}".strip()
            if kind in AUTH_FAILURES:
                self._client = None
                if kind.startswith("Challenge") or "Form" in kind:
                    message = "Instagram demande une vérification : se connecter avec le sessionid d'un navigateur"
                raise AuthError(message) from e
            raise (ShareError if kind in RETRY_LATER else Refused)(message) from e

    async def check(self):
        def who(client):
            return client.username or self.config.instagram_user

        return await asyncio.to_thread(self._call, who)

    async def publish(self, post, image, text):
        def upload(client):
            return f"https://www.instagram.com/p/{client.photo_upload(image, text).code}/"

        return await asyncio.to_thread(self._call, upload)


PUBLISHERS = {cls.name: cls for cls in (Instagram, Flickr, Bluesky)}
