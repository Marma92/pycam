"""Who may use the web interface: the browsers paired with the camera by a code it shows
on its screen (docs/architecture.md, « La sécurité de l'interface web »).

The local network is not enough: on a café's or a hotel's Wi-Fi, anyone could otherwise
look at the photos, delete them, or connect a cloud account. Like a television pairing a
remote, without a keyboard to type a password:

- a browser not yet paired asks for a code: the camera shows six digits on its screen,
  for two minutes, five tries; three codes used up by wrong tries, and none for 15 min;
- typed in, the browser receives a random token in a cookie, valid for a year; the camera
  keeps only its SHA-256, in ~/.config/pycam/web-browsers.json (its user's only);
- MENU › Réseau, or the web page, forgets them.

Seeing the screen is what proves one holds the camera.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time

log = logging.getLogger(__name__)

PORT = 8080  # the web interface's (web.py)
COOKIE = "pycam_session"
TOKEN_SECONDS = 365 * 24 * 3600
CODE_SECONDS = 120
CODE_TRIES = 5
CODE_EVERY = 10  # seconds between two codes asked for: the screen is not a billboard
LOCK_AFTER = 3  # codes used up by wrong tries in a row...
LOCK_SECONDS = 15 * 60  # ...and no code for a while: guessing takes months
SEEN_EVERY = 24 * 3600  # "last seen" written at most once a day


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class PairingError(Exception):
    pass


class Access:
    def __init__(self, path):
        self.path = path
        self.browsers = self._load()
        self._code = None  # [code, expires (monotonic), tries]
        self._last_code = -CODE_EVERY
        self._used_up = 0  # codes used up by wrong tries, in a row
        self._locked_until = 0.0

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("unreadable %s (%s): no browser paired", self.path, e)
            return []
        now = time.time()
        return [b for b in data if isinstance(b, dict) and b.get("hash") and b.get("expires", 0) > now] if isinstance(data, list) else []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.browsers, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # --- A browser's token

    def browser(self, token):
        """The paired browser this token is, or None."""
        if not token:
            return None
        digest, now = _hash(token), time.time()
        for b in self.browsers:
            if hmac.compare_digest(b["hash"], digest) and b.get("expires", 0) > now:
                if now - b.get("seen", 0) > SEEN_EVERY:
                    b["seen"] = now
                    self._try_save()
                return b
        return None

    def _try_save(self):
        try:
            self._save()
        except OSError as e:
            log.warning("cannot save %s: %s", self.path, e)

    # --- Pairing

    @property
    def code_pending(self):
        return bool(self._code and time.monotonic() < self._code[1])

    def new_code(self):
        """Six digits for the camera's screen; the previous one no longer works. None: one
        was given a moment ago (the same stays valid)."""
        now = time.monotonic()
        if now < self._locked_until:
            raise PairingError(f"Trop d'essais : nouveau code dans {math.ceil((self._locked_until - now) / 60)} min.")
        if now - self._last_code < CODE_EVERY and self.code_pending:
            return None
        self._last_code = now
        code = f"{secrets.randbelow(10**6):06d}"
        self._code = [code, now + CODE_SECONDS, 0]
        return code

    def pair(self, code, name=""):
        """The code typed in: a new token for this browser, or PairingError."""
        if not self.code_pending:
            self._code = None
            raise PairingError("Aucun code en cours : en demander un.")
        self._code[2] += 1
        expected = self._code[0]
        if not hmac.compare_digest(expected.encode(), "".join(ch for ch in str(code) if ch.isdigit()).encode()):
            if self._code[2] >= CODE_TRIES:
                self._code = None
                self._used_up += 1
                log.warning("web: a pairing code used up by wrong tries (%d in a row)", self._used_up)
                if self._used_up >= LOCK_AFTER:
                    self._used_up = 0
                    self._locked_until = time.monotonic() + LOCK_SECONDS
                raise PairingError("Code incorrect, trop d'essais : en demander un autre.")
            raise PairingError("Code incorrect.")
        self._code, self._used_up = None, 0
        token = secrets.token_urlsafe(32)
        now = time.time()
        browser = {
            "id": secrets.token_hex(4),
            "hash": _hash(token),
            "name": str(name or "Navigateur")[:60],
            "created": now,
            "seen": now,
            "expires": now + TOKEN_SECONDS,
        }
        self.browsers.append(browser)
        self._save()
        log.info("web: browser %s paired (%s)", browser["id"], browser["name"])
        return token, browser

    def forget(self, browser_id=None):
        """One browser, or all of them (None). How many."""
        before = len(self.browsers)
        self.browsers = [b for b in self.browsers if browser_id is not None and b["id"] != browser_id]
        self._save()
        return before - len(self.browsers)

    def listing(self, current=None):
        return [
            {"id": b["id"], "name": b.get("name", ""), "created": b.get("created", 0), "seen": b.get("seen", 0), "current": b is current}
            for b in self.browsers
        ]
