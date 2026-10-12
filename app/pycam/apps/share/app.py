"""Partage: a photo from the card to Instagram, Flickr and Bluesky (docs/architecture.md §5),
after SPP (Simple Photo Poster).

From playback, MENU › Partager on a photo: the platforms ticked (the last choice), the
caption made from the photo (camera, lens, the preset's film, date) and the default
hashtags, then Publier. From the web interface, the same with a title, a description, an
alt text and other hashtags, and the preview of what each platform will publish (where a
caption is cut).

A post waits in a queue (~/.local/share/pycam/share/queue.json) until the camera is online,
then goes to each platform in turn, one failing never stopping the others (as in SPP). Worth
another try (the network, a platform busy): again in 1, 5, then 15 min, five times at most.
Refused (an account to connect again, a caption refused): it waits for a retry by hand.
Never while the camera is busy (a recording, a sequence, a long exposure).
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time

import aiohttp

from ...menu import Item, Menu
from ..network import local_address, network_state
from . import caption, images
from .config import LABELS, PLATFORMS, ShareConfig, data_dir
from .publishers import PUBLISHERS, TIMEOUT, AuthError, Flickr, Refused, ShareError, instagrapi_missing

log = logging.getLogger(__name__)

NETWORK_SECONDS = 20
RETRY_SECONDS = (60, 300, 900)
ATTEMPTS = 5
BUSY_POLL = 2.0
HISTORY = 50  # posts done kept, for their links
PHOTO = re.compile(r"^PY[CB]_\d{4}\.JPG$")


def new_entry():
    return {"status": "pending", "url": "", "error": "", "attempts": 0, "next": 0.0}


class Queue:
    """The posts, waiting or done: [{id, photo, post, platforms: {name: entry}, created}]."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.items = [item for item in json.load(f) if isinstance(item, dict) and "platforms" in item]
        except FileNotFoundError:
            self.items = []
        except (OSError, ValueError) as e:
            log.warning("unreadable share queue %s (%s)", path, e)
            self.items = []
        for item in self.items:  # a post cut short by a restart goes again
            for entry in item["platforms"].values():
                if entry["status"] == "sending":
                    entry["status"] = "pending"

    def save(self):
        done = [i for i in self.items if all(e["status"] == "done" for e in i["platforms"].values())]
        for item in done[:-HISTORY]:
            self.items.remove(item)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(self, photo, post, platforms):
        item = {
            "id": secrets.token_hex(4),
            "photo": photo,
            "post": post.to_dict(),
            "platforms": {p: new_entry() for p in platforms},
            "created": time.time(),
        }
        self.items.append(item)
        self.save()
        return item

    def get(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def entries(self, status):
        return [(i, p, e) for i in self.items for p, e in i["platforms"].items() if e["status"] == status]

    def latest(self, photo):
        return next((i for i in reversed(self.items) if i["photo"] == photo), None)


class Share:
    id = "share"
    title = "Partage"

    def __init__(self, camera, config_path=None, data_path=None):
        self.camera = camera
        self.config_path = config_path
        self.config = ShareConfig.load(config_path)
        self.data_dir = data_path or data_dir()
        self.queue = Queue(os.path.join(self.data_dir, "queue.json"))
        self.online = False
        self.working = ""  # the platform being published to
        self._session = None
        self._publishers = {}
        self._wake = asyncio.Event()
        self._next_network = 0.0
        self._flickr = None  # (key, secret, request token, its secret) while connecting

    # --- What the camera shows

    def summary(self, camera=None):
        if self.working:
            return f"Publication · {LABELS[self.working]}"
        pending = len(self.queue.entries("pending"))
        if pending:
            return f"{pending} en attente"
        if failed := len(self.queue.entries("failed")):
            return f"{failed} en échec"
        return ", ".join(LABELS[p] for p in self.config.configured) or "Aucun compte"

    def hud(self):
        return f"PARTAGE {LABELS[self.working].upper()}" if self.working else ""

    def wake(self):
        """Nothing to do with new files: a photo is shared by hand."""

    def files_in_use(self):
        """The photos not published everywhere yet (waiting, or failed: to retry)."""
        return {i["photo"] for i in self.queue.items if any(e["status"] != "done" for e in i["platforms"].values())}

    def menu(self, camera):
        address = local_address()
        items = [Item("Comptes")]
        for p in PLATFORMS:
            items.append(Item(LABELS[p], kind="info", text=lambda c, p=p: self.config.account(p) if self.config.ready(p) else "Non connecté"))
        items += [
            Item("Configurer", kind="info", text=lambda c: f"page web {address}:8080" if address else "page web, onglet Partage"),
            Item("Publications"),
            Item("En attente", kind="info", text=lambda c: str(len(self.queue.entries("pending"))) + ("" if self.online else " · hors ligne")),
            Item("En échec", kind="info", text=lambda c: str(len(self.queue.entries("failed")))),
            Item("Réessayer les échecs", kind="run", target=lambda c: c.show_message(self.retry())),
        ]
        return Menu(camera.settings, items=items, title="Partage", context=camera)

    def photo_items(self, camera, path):
        if not PHOTO.match(os.path.basename(path or "")):
            return []  # a video
        return [Item("Partager", kind="open", target=lambda c: self.share_menu(c, path), text=lambda c: self.photo_status(path))]

    def photo_status(self, path):
        item = self.queue.latest(path)
        if not item:
            return "›"
        states = [e["status"] for e in item["platforms"].values()]
        if "pending" in states or "sending" in states:
            return "En attente"
        if "failed" in states:
            return "Échec · page web"
        return "Publiée · " + ", ".join(LABELS[p] for p in item["platforms"])

    def share_menu(self, camera, path):
        """The platforms to tick, what the caption will say, Publier."""
        post = caption.from_photo(path, self.config.tags)
        ready = self.config.configured
        chosen = set(p for p in (self.config.selected or ready) if p in ready)

        def toggle(p):
            chosen.symmetric_difference_update({p})

        items = [Item("Publier sur")]
        for p in PLATFORMS:
            if p in ready:
                items.append(Item(LABELS[p], kind="toggle", target=lambda c, p=p: toggle(p), text=lambda c, p=p: "Oui" if p in chosen else "Non"))
            else:
                items.append(Item(LABELS[p], kind="info", text=lambda c: "Non connecté (page web)"))
        gear = " · ".join(x for x in (post.film, post.date, post.hashtags) if x) or "—"
        items += [
            Item("Légende"),
            Item("Tirée de la photo", kind="info", text=lambda c: gear),
            Item("Titre, description", kind="info", text=lambda c: "page web › Photos"),
            Item("Publier", kind="run", target=lambda c: self._publish_pressed(c, path, post, chosen)),
        ]
        return Menu(camera.settings, items=items, title=f"Partager {os.path.basename(path)[:-4]}", context=camera)

    def _publish_pressed(self, camera, path, post, chosen):
        if not chosen:
            camera.show_message("Aucune plateforme choisie", warning=True)
            return
        camera.show_message(self.add(path, post, [p for p in PLATFORMS if p in chosen]))
        camera.menu, camera.menu_stack = None, []  # back to the photo
        camera.redraw()

    def _changed(self):
        self.camera.redraw()

    # --- The queue

    def add(self, photo, post, platforms):
        """Queues a post; the message to show."""
        self.queue.add(photo, post, platforms)
        self.config.selected = list(platforms)
        self.config.save(self.config_path)
        log.info("share %s on %s", os.path.basename(photo), ", ".join(platforms))
        self._wake.set()
        return "Publication en cours" if self.online else "Partage en attente de connexion"

    def retry(self, item_id=None):
        """The failed ones, again (of one post, or all)."""
        count = 0
        for item, _, entry in self.queue.entries("failed"):
            if item_id in (None, item["id"]):
                entry.update(status="pending", attempts=0, next=0.0)
                count += 1
        self.queue.save()
        self._wake.set()
        self._changed()
        return f"{count} publication(s) relancée(s)" if count else "Aucun échec"

    def remove(self, item_id):
        item = self.queue.get(item_id)
        if item and not any(e["status"] == "sending" for e in item["platforms"].values()):
            self.queue.items.remove(item)
            self.queue.save()
            return True
        return False

    def _due(self):
        now = time.time()
        return [(i, p, e) for i, p, e in self.queue.entries("pending") if e["next"] <= now]

    def _busy(self):
        c = self.camera
        return bool(c.recording or c.drive.active or c.exposing or c.task_status)

    async def run(self):
        try:
            while True:
                await self._tick()
                pending = self.queue.entries("pending")
                if pending:
                    soonest = min(e["next"] for _, _, e in pending) - time.time()
                    delay = max(1.0, min(NETWORK_SECONDS, soonest))
                else:
                    delay = 3600
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            if self._session:
                await self._session.close()

    async def _tick(self):
        if not self._due():
            return
        now = time.monotonic()
        if now >= self._next_network or not self.online:
            self.online, _ = await network_state()  # a share is asked for: metered or not
            self._next_network = now + NETWORK_SECONDS
        if not self.online or self._busy():
            return
        for item, platform, entry in self._due():
            if item not in self.queue.items or entry["status"] != "pending":
                continue  # removed meanwhile
            while self._busy():
                await asyncio.sleep(BUSY_POLL)
            await self._publish(item, platform, entry)

    def _publisher(self, platform):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
            self._publishers = {}
        if platform not in self._publishers:
            self._publishers[platform] = PUBLISHERS[platform](self.config, self._session, self.data_dir)
        return self._publishers[platform]

    async def _publish(self, item, platform, entry):
        name, label = os.path.basename(item["photo"])[:-4], LABELS[platform]
        post = caption.Post.from_dict(item["post"])
        target = os.path.join(self.data_dir, "prepared", f"{item['id']}-{platform}.jpg")
        entry["status"], self.working = "sending", platform
        self._changed()
        try:
            if not self.config.ready(platform):
                raise Refused(f"{label} n'est plus connecté")
            publisher = self._publisher(platform)
            image = await asyncio.to_thread(images.prepare, platform, item["photo"], target)
            url = await publisher.publish(post, image, publisher.render(post)[0])
        except FileNotFoundError:
            entry.update(status="failed", error="la photo n'est plus sur la carte")
        except (Refused, ValueError) as e:
            entry.update(status="failed", error=str(e))
            log.warning("share %s on %s: %s", name, platform, e)
            self.camera.show_message(f"{label} : échec (page web)", warning=True)
        except (ShareError, aiohttp.ClientError, TimeoutError, OSError) as e:
            entry["attempts"] += 1
            entry["error"] = str(e) or type(e).__name__
            if entry["attempts"] >= ATTEMPTS:
                entry["status"] = "failed"
            else:
                entry["status"] = "pending"
                entry["next"] = time.time() + RETRY_SECONDS[min(entry["attempts"], len(RETRY_SECONDS)) - 1]
            log.warning("share %s on %s: %s (attempt %d)", name, platform, entry["error"], entry["attempts"])
        else:
            entry.update(status="done", url=url, error="")
            log.info("shared %s on %s: %s", name, platform, url)
            self.camera.show_message(f"{name} publiée sur {label}")
        finally:
            if entry["status"] == "sending":  # cancelled: again at the next start
                entry["status"] = "pending"
            self.working = ""
            if os.path.exists(target):
                os.remove(target)
            self.queue.save()
            self._changed()

    # --- From the web interface

    def preview(self, photo, fields=None, platforms=PLATFORMS):
        """What each platform will get, without the network: its picture, its caption and
        what it cuts of it."""
        post = caption.from_photo(photo, self.config.tags)
        for key, value in (fields or {}).items():
            if key in caption.Post.__dataclass_fields__:
                setattr(post, key, str(value))
        out = []
        for p in platforms:
            cls = PUBLISHERS[p]
            text, cut = cls.render(post)
            out.append(
                {
                    "platform": p,
                    "label": LABELS[p],
                    "ready": self.config.ready(p),
                    "image": cls.image_label,
                    "text": text,
                    "cut": cut,
                    "length": cls.measure(cls.normalise(post.caption)),
                    "limit": cls.limit,
                }
            )
        return post, out

    def status(self):
        def item_view(item):
            return {
                "id": item["id"],
                "photo": os.path.basename(item["photo"]),
                "folder": os.path.basename(os.path.dirname(item["photo"])),
                "title": item["post"].get("title", ""),
                "created": int(item["created"]),
                "platforms": {p: {k: e[k] for k in ("status", "url", "error", "attempts")} for p, e in item["platforms"].items()},
            }

        return {
            **self.config.public(),
            "labels": LABELS,
            "online": self.online,
            "working": self.working,
            "instagrapi": instagrapi_missing(),
            "flickr_pending": bool(self._flickr),
            "queue": [item_view(i) for i in reversed(self.queue.items)],
        }

    def _saved(self, *platforms):
        self.config.save(self.config_path)
        for p in platforms:
            self._publishers.pop(p, None)
        self._wake.set()
        self._changed()

    async def _check(self, platform, config):
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            publisher = PUBLISHERS[platform](config, session, self.data_dir)
            return await publisher.check()

    async def connect_bluesky(self, handle, password):
        config = ShareConfig(bluesky_handle=handle.strip().lstrip("@"), bluesky_password=password.strip())
        handle = await self._check("bluesky", config)
        self.config.bluesky_handle, self.config.bluesky_password = handle, config.bluesky_password
        self._saved("bluesky")
        return handle

    async def flickr_start(self, key, secret):
        key, secret = key.strip(), secret.strip()
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            page, token, token_secret = await Flickr.request_token(session, key, secret)
        self._flickr = (key, secret, token, token_secret)
        return page

    async def flickr_finish(self, code):
        if not self._flickr:
            raise AuthError("Recommencer la connexion à Flickr")
        key, secret, token, token_secret = self._flickr
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            access = await Flickr.access_token(session, key, secret, token, token_secret, code)
        c = self.config
        c.flickr_key, c.flickr_secret = key, secret
        c.flickr_token, c.flickr_token_secret = access["oauth_token"], access["oauth_token_secret"]
        c.flickr_user, c.flickr_nsid = access.get("username", ""), access.get("user_nsid", "")
        self._flickr = None
        self._saved("flickr")
        return c.flickr_user

    async def connect_instagram(self, user, password="", sessionid=""):
        config = ShareConfig(
            instagram_user=user.strip().lstrip("@"), instagram_password=password, instagram_sessionid=sessionid.strip()
        )
        if config.instagram_user != self.config.instagram_user:
            self._remove_instagram_device()  # another account: another device
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            publisher = PUBLISHERS["instagram"](config, session, self.data_dir)
            account = await publisher.check()
        c = self.config
        c.instagram_user, c.instagram_password, c.instagram_sessionid = account, password, config.instagram_sessionid
        self._saved("instagram")
        return account

    def set_defaults(self, tags=None, lang=None):
        if tags is not None:
            self.config.tags = str(tags).strip()
        if lang is not None and re.match(r"^[a-z]{2,3}$", str(lang).strip()):
            self.config.lang = str(lang).strip()
        self._saved()

    def disconnect(self, platform):
        self.config.forget(platform)
        if platform == "instagram":
            self._remove_instagram_device()  # it holds the session too
        self._saved(platform)

    def _remove_instagram_device(self):
        try:
            os.remove(os.path.join(self.data_dir, "instagram.json"))
        except FileNotFoundError:
            pass
