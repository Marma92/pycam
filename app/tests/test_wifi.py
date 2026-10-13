"""Wi-Fi without a keyboard (wifi.py), against a fake nmcli."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from pycam import wifi
from pycam.settings import Settings
from pycam.wifi import HOTSPOT, Wifi, split_terse

FREEBOX = "netplan-wlan0-Freebox"


class FakeCamera:
    def __init__(self):
        self.settings = Settings()
        self.messages = []
        self.tasks = []

    def show_message(self, text, warning=False, seconds=1.5):
        self.messages.append(text)

    def redraw(self):
        pass

    def save_soon(self):
        pass

    def spawn(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))


class FakeNetworkManager:
    """What nmcli answers, and does, for the commands wifi.py sends."""

    def __init__(self):
        self.connections = {FREEBOX: "Free:box"}  # name -> ssid
        self.active = FREEBOX
        self.passwords = {"Voisins": "le-bon-mot"}  # the networks around, and their keys
        self.calls = []

    async def __call__(self, *args, timeout=30):
        self.calls.append(args)
        a = list(args)
        if a[:5] == ["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"]:
            lines = ["lo:loopback:lo"] + ([f"{self.active.replace(':', chr(92) + ':')}:802-11-wireless:wlan0"] if self.active else [])
            return 0, "\n".join(lines) + "\n", ""
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            return 0, self.connections.get(a[-1], "") + "\n", ""
        if a[:4] == ["-t", "-f", "NAME,TYPE", "connection"]:
            lines = ["lo:loopback"] + [f"{n}:802-11-wireless" for n in self.connections]
            return 0, "\n".join(lines) + "\n", ""
        if a[:4] == ["-t", "-f", "SSID,SIGNAL,SECURITY", "device"]:
            if self.active == HOTSPOT:
                return 10, "", "Error: Scanning not allowed while in AP mode."
            return 0, "Free\\:box:80:WPA2\nVoisins:40:WPA2\nVoisins:55:WPA2\n:30:WPA2\nCafé:20:--\n", ""
        if a[:4] == ["connection", "add", "type", "wifi"]:
            self.connections[a[a.index("con-name") + 1]] = a[a.index("ssid") + 1]
            return 0, "", ""
        if a[:4] == ["--wait", "30", "connection", "up"]:
            self.active = a[-1]
            return 0, "", ""
        if a[:2] == ["connection", "down"]:
            self.active = FREEBOX if a[2] == HOTSPOT and FREEBOX in self.connections else ""
            return 0, "", ""
        if a[:3] == ["--wait", str(wifi.CONNECT_SECONDS), "device"]:
            ssid = a[5]
            password = a[a.index("password") + 1] if "password" in a else ""
            self.connections[ssid] = ssid  # nmcli makes the connection first
            if self.passwords.get(ssid) != password:
                self.active = ""
                return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
            self.active = ssid
            return 0, "", ""
        if a[:3] == ["connection", "delete", "id"]:
            self.connections.pop(a[3], None)
            return 0, "", ""
        return 0, "", ""


class WifiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.camera = FakeCamera()
        self.nm = FakeNetworkManager()
        self.wifi = Wifi(self.camera, run=self.nm)
        for name, value in (("REFRESH", 0), ("RADIO_SETTLE", 0), ("FIRST_SETUP_DELAY", 0)):
            patcher = mock.patch(f"pycam.wifi.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_terse_lines(self):
        self.assertEqual(split_terse("Free\\:box:80:WPA2"), ["Free:box", "80", "WPA2"])
        self.assertEqual(split_terse("a\\\\b::c"), ["a\\b", "", "c"])

    async def test_where_it_is(self):
        await self.wifi.refresh()
        self.assertEqual((self.wifi.ssid, self.wifi.hotspot, self.wifi.saved), ("Free:box", False, [FREEBOX]))
        self.assertTrue(self.wifi.summary().startswith("Free:box"))

    async def test_the_networks_around(self):
        networks = await self.wifi.scan()
        self.assertEqual([n["ssid"] for n in networks], ["Free:box", "Voisins", "Café"])  # the hidden one left out
        self.assertEqual(networks[1]["signal"], 55)  # its best access point
        self.assertFalse(networks[2]["secure"])

    async def test_its_access_point(self):
        await self.wifi.set_hotspot(True)
        self.assertTrue(self.wifi.hotspot)
        self.assertEqual(len(self.wifi.networks), 3)  # scanned while the radio was free
        password = self.camera.settings.hotspot_password
        self.assertEqual(len(password), 10)
        made = next(c for c in self.nm.calls if c[:2] == ("connection", "add"))
        settings = dict(zip(made[4::2], made[5::2]))
        self.assertEqual(settings["wifi-sec.psk"], password)
        # What the Pi 3B's chip accepts: no PMF; and not opened again at the next start.
        self.assertEqual((settings["wifi-sec.pmf"], settings["autoconnect"], settings["802-11-wireless.mode"]), ("disable", "no", "ap"))
        self.assertIn(password, self.camera.messages[-1])  # on the screen
        self.assertEqual(self.wifi.hud(), "WI-FI pycam-" + self.wifi.hotspot_ssid[-4:])  # and on the HUD while open
        self.assertEqual(await self.wifi.scan(), self.wifi.networks)  # the radio is busy: the last list
        await self.wifi.set_hotspot(False)
        self.assertEqual((self.wifi.hotspot, self.wifi.ssid), (False, "Free:box"))  # back on its network
        await self.wifi.set_hotspot(True)
        self.assertEqual(self.camera.settings.hotspot_password, password)  # the same, once made

    async def test_joining_a_network_from_the_access_point(self):
        await self.wifi.set_hotspot(True)
        # A wrong password: the connection made for it goes, the access point comes back.
        self.assertFalse(await self.wifi.connect("Voisins", "mauvais-mot"))
        self.assertIn("Secrets were required", self.wifi.error)
        self.assertNotIn("Voisins", self.nm.connections)
        self.assertTrue(self.wifi.hotspot)
        self.assertTrue(await self.wifi.connect("Voisins", "le-bon-mot"))
        self.assertEqual((self.wifi.ssid, self.wifi.hotspot, self.wifi.error), ("Voisins", False, ""))
        self.assertIn("Voisins", self.wifi.saved)

    async def test_a_known_network_is_never_deleted(self):
        self.nm.connections["Voisins"] = "Voisins"  # known, its password changed since
        self.assertFalse(await self.wifi.connect("Voisins", "ancien-mot"))
        self.assertIn("Voisins", self.nm.connections)

    async def test_forgotten(self):
        await self.wifi.refresh()
        self.assertTrue(await self.wifi.forget(FREEBOX))
        self.assertEqual(self.wifi.saved, [])
        self.assertFalse(await self.wifi.forget("nothing"))

    async def test_a_new_camera_opens_its_access_point(self):
        self.nm.connections, self.nm.active = {}, ""
        task = asyncio.create_task(self.wifi.run())
        for _ in range(50):
            await asyncio.sleep(0)
            if self.wifi.hotspot:
                break
        task.cancel()
        self.assertTrue(self.wifi.hotspot)

    async def test_a_camera_with_a_network_does_not(self):
        task = asyncio.create_task(self.wifi.run())
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        self.assertFalse(self.wifi.hotspot)
        self.assertFalse(any(c[:2] == ("connection", "add") for c in self.nm.calls))


if __name__ == "__main__":
    unittest.main()
