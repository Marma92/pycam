"""The updates, the application's side (updates.py): when it asks, what it shows."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from pycam import __version__, menu
from pycam.settings import Settings
from pycam.updates import CHECK_EVERY, Updates


class FakeCamera:
    def __init__(self, run_dir):
        self.settings = Settings()
        self.engine = SimpleNamespace(path=os.path.join(run_dir, "engine.sock"))
        self.hud = object()
        self.recording = self.exposing = None
        self.drive = SimpleNamespace(active=False)
        self.task_status, self.writing = "", 0
        self.messages = []
        self.updates = None

    def show_message(self, text, warning=False, seconds=1.5):
        self.messages.append(text)

    def redraw(self):
        pass

    def save_soon(self):
        pass


class UpdatesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.camera = FakeCamera(self.tmp.name)
        self.status_path = os.path.join(self.tmp.name, "status.json")
        self.updates = Updates(self.camera, status_path=self.status_path, managed=True)
        self.camera.updates = self.updates
        self.network = mock.patch("pycam.updates.network_state", new=mock.AsyncMock(return_value=(True, False)))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write_status(self, **status):
        status.setdefault("time", time.time())
        with open(self.status_path, "w") as f:
            json.dump(status, f)

    def requested(self):
        """The request waiting for root, removed (as updater.py does)."""
        path = self.updates.request_path
        if not os.path.exists(path):
            return None
        with open(path) as f:
            action = f.read().strip()
        os.remove(path)
        return action

    async def test_looks_when_online_at_most_every_six_hours(self):
        await self.updates.step()
        self.assertEqual(self.requested(), "check")
        await self.updates.step()
        self.assertIsNone(self.requested())  # just done
        self.camera.settings.update_checked -= CHECK_EVERY + 1
        with mock.patch("pycam.updates.network_state", new=mock.AsyncMock(return_value=(True, True))):
            await self.updates.step()
        self.assertIsNone(self.requested())  # a phone's hotspot: later
        self.camera.settings.update_mode = "off"
        await self.updates.step()
        self.assertIsNone(self.requested())
        self.camera.settings.update_mode = "notify"
        await self.updates.step()
        self.assertEqual(self.requested(), "check")

    async def test_nothing_asked_while_root_works(self):
        self.write_status(state="downloading", progress=0.4)
        await self.updates.step()
        self.assertIsNone(self.requested())
        self.assertEqual(self.updates.state_text(), "Téléchargement 40 %")

    async def test_installed_at_the_next_start(self):
        # The check due at the same time does not replace the request (one at a time).
        self.write_status(state="ready", available={"version": "9.9.9"})
        pause = asyncio.sleep  # the module's sleep is the one patched
        with mock.patch("pycam.updates.asyncio.sleep", new=mock.AsyncMock()):
            task = asyncio.create_task(self.updates.run())
            await pause(0.05)
            task.cancel()
        self.assertEqual(self.requested(), "install")
        self.assertIn("Mise à jour 9.9.9…", self.camera.messages)

    async def test_only_proposed(self):
        self.camera.settings.update_mode = "notify"
        self.camera.settings.update_checked = time.time()
        self.write_status(state="ready", available={"version": "9.9.9"})
        await self.updates.step()
        self.assertIsNone(self.requested())
        self.assertEqual(self.camera.messages, ["Mise à jour 9.9.9 prête"])
        await self.updates.step()
        self.assertEqual(len(self.camera.messages), 1)  # once
        self.assertEqual((self.updates.action_text(), self.updates.state_text()), ("Installer 9.9.9", "9.9.9 prête"))
        self.assertEqual(self.updates.act(), "Installation de 9.9.9…")
        self.assertEqual(self.requested(), "install")

    async def test_not_during_a_shot(self):
        self.camera.settings.update_checked = time.time()
        self.write_status(state="ready", available={"version": "9.9.9"})
        await self.updates.step()
        self.camera.drive.active = True
        self.assertEqual(self.updates.act(), "Pas pendant une prise")
        self.assertIsNone(self.requested())

    async def test_the_result_shown_once_after_the_restart(self):
        self.write_status(state="installed", current=__version__)
        self.updates.status = self.updates.read()
        self.updates._announce({}, self.updates.status)
        self.assertEqual(self.camera.messages, [f"pycam {__version__} installé"])
        self.updates._announce({}, self.updates.status)  # the next start
        self.assertEqual(len(self.camera.messages), 1)
        self.write_status(state="failed", message="9.9.9 n'a pas démarré : retour à 0.1.0")
        self.updates.status = self.updates.read()
        self.updates._announce({}, self.updates.status)
        self.assertEqual(self.camera.messages[-1], "9.9.9 n'a pas démarré : retour à 0.1.0")
        self.assertEqual(self.updates.state_text(), "9.9.9 n'a pas démarré")

    async def test_asked_by_hand(self):
        self.camera.settings.update_mode = "off"
        self.assertEqual(self.updates.act(), "Recherche…")
        self.assertEqual(self.requested(), "check")
        self.write_status(state="up_to_date", message=f"À jour ({__version__})")
        await self.updates.step()
        self.assertEqual(self.camera.messages, [f"À jour ({__version__})"])
        self.assertEqual(self.updates.act("install"), "Aucune mise à jour à installer")

    async def test_a_development_tree(self):
        updates = Updates(self.camera, status_path=self.status_path, managed=False)
        await updates.run()  # returns at once
        self.assertIn("développement", updates.act())
        self.assertIsNone(self.requested())
        self.assertEqual(updates.version_text(), f"{__version__} · développement")

    async def test_the_menu(self):
        lines = {item.label: item for item in menu.FULL}
        self.assertEqual(lines["Version"].text(self.camera), __version__)
        self.assertEqual(lines["Mise à jour"].text(self.camera), "Rechercher")
        lines["Mise à jour"].target(self.camera)
        self.assertEqual(self.requested(), "check")
        self.camera.updates = None  # the tests of the camera
        self.assertEqual(lines["État"].text(self.camera), "—")

    def test_a_stale_status(self):
        self.updates.status = {"state": "installing", "time": time.time() - 7200}
        self.assertEqual(self.updates.state, "error")


if __name__ == "__main__":
    unittest.main()
