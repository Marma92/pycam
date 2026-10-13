"""Pairing a browser with a code shown on the screen (web_access.py), and the clock from
a browser (clock.py)."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from pycam import web_access
from pycam.clock import Clock
from pycam.web_access import Access, PairingError


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "pycam", "web-browsers.json")
        self.access = Access(self.path)

    def test_paired_by_the_code_on_the_screen(self):
        self.assertIsNone(self.access.browser("anything"))
        with self.assertRaises(PairingError):
            self.access.pair("123456")  # no code asked for
        code = self.access.new_code()
        self.assertRegex(code, r"^\d{6}$")
        self.assertIsNone(self.access.new_code())  # the same one, still on the screen
        token, browser = self.access.pair(f"{code[:3]} {code[3:]}", "Safari · iPhone")  # as typed, with its space
        self.assertEqual(self.access.browser(token), browser)
        self.assertFalse(self.access.code_pending)  # used
        with self.assertRaises(PairingError):
            self.access.pair(code)
        # Only the token's SHA-256 is kept, in a file of the camera user's only.
        with open(self.path) as f:
            self.assertNotIn(token, f.read())
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(Access(self.path).browser(token)["name"], "Safari · iPhone")  # after a restart

    def test_wrong_codes(self):
        for _ in range(web_access.LOCK_AFTER):
            code = self.access.new_code()
            wrong = f"{(int(code) + 1) % 10**6:06d}"
            for _ in range(web_access.CODE_TRIES - 1):
                with self.assertRaisesRegex(PairingError, "^Code incorrect.$"):
                    self.access.pair(wrong)
            with self.assertRaisesRegex(PairingError, "trop d'essais"):
                self.access.pair(wrong)
            with self.assertRaisesRegex(PairingError, "Aucun code"):
                self.access.pair(code)  # used up: the right one no longer works
            self.access._last_code -= web_access.CODE_EVERY
        with self.assertRaisesRegex(PairingError, "Trop d'essais : nouveau code dans 15 min"):
            self.access.new_code()  # three codes used up: none for a while

    def test_a_code_does_not_last(self):
        code = self.access.new_code()
        with mock.patch("pycam.web_access.time.monotonic", return_value=time.monotonic() + web_access.CODE_SECONDS + 1):
            with self.assertRaisesRegex(PairingError, "Aucun code"):
                self.access.pair(code)

    def test_forgotten(self):
        tokens = []
        for _ in range(3):
            code = self.access.new_code()
            self.access._last_code -= web_access.CODE_EVERY
            tokens.append(self.access.pair(code)[0])
        first = self.access.browser(tokens[0])
        self.assertEqual(self.access.forget(first["id"]), 1)
        self.assertIsNone(self.access.browser(tokens[0]))
        self.assertIsNotNone(self.access.browser(tokens[1]))
        self.assertEqual(self.access.forget(), 2)
        self.assertEqual(Access(self.path).browsers, [])

    def test_a_token_expires(self):
        code = self.access.new_code()
        token, browser = self.access.pair(code)
        browser["expires"] = time.time() - 1
        self.assertIsNone(self.access.browser(token))
        with open(self.path, "w") as f:
            json.dump([browser], f)
        self.assertEqual(Access(self.path).browsers, [])  # dropped at the next start


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.synced = os.path.join(self.tmp.name, "synchronized")
        self.set = []
        self.clock = Clock(self.synced, setter=self.set.append)

    def test_from_the_browser_without_network(self):
        now = time.time()
        self.assertEqual(self.clock.from_browser(now * 1000 + 20_000), "ok")  # close enough
        self.assertEqual(self.clock.from_browser((now + 3600) * 1000), "set")
        self.assertAlmostEqual(self.set[0], now + 3600, delta=1)
        self.assertIn("téléphone", self.clock.text())

    def test_never_over_the_network(self):
        open(self.synced, "w").close()
        self.assertEqual(self.clock.from_browser((time.time() + 3600) * 1000), "network")
        self.assertEqual(self.set, [])
        self.assertIn("réseau", self.clock.text())

    def test_without_the_right_to(self):
        def refuse(seconds):
            raise PermissionError(1, "Operation not permitted")

        clock = Clock(self.synced, setter=refuse)
        self.assertEqual(clock.from_browser((time.time() - 86400) * 1000), "denied")
        self.assertIn("à régler", clock.text())


if __name__ == "__main__":
    unittest.main()
