"""Wi-Fi without a keyboard (docs/architecture.md §8): the camera opens its own access
point, a phone joins it and opens the web page, and chooses the network the camera must
join. Through NetworkManager (nmcli), which a polkit rule lets the camera's user drive
(system/pycam.polkit.rules.in): the application has no session to ask for it.

- MENU › Réseau › Point d'accès: on or off; its name, password and the page's address are
  shown there (seeing the screen proves one holds the camera).
- A new camera, which knows no network and has none after a minute, opens it by itself:
  the way in for its first connection.
- The radio is one: while it is an access point, it is on no other network. Joining one
  closes the access point; if that fails (a wrong password), the access point comes back,
  with the error on the page.
"""

import asyncio
import logging
import secrets
import socket
import time

from .apps.network import local_address

log = logging.getLogger(__name__)

HOTSPOT = "pycam-hotspot"  # its NetworkManager connection
IFACE = "wlan0"
HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's shared mode
CONNECT_SECONDS = 45
FIRST_SETUP_DELAY = 60  # after the start, before a camera without any network opens its access point
REFRESH = 30
RADIO_SETTLE = 4  # seconds for the radio, back from the access point, to see the networks again
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # nothing to mistake on a small screen
WIFI = "802-11-wireless"


def split_terse(line):
    """A line of `nmcli -t`: fields split on ":", "\\:" and "\\\\" unescaped."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


async def nmcli(*args, timeout=30):
    """(return code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "délai dépassé"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace").strip()


def _error(err):
    """nmcli's error, without its "Error: " prefix."""
    return (err or "échec").splitlines()[-1].removeprefix("Error: ").strip()


class Wifi:
    title = "Wi-Fi"

    def __init__(self, camera, run=nmcli):
        self.camera = camera
        self.nmcli = run
        self.connection = ""  # the active Wi-Fi connection's name
        self.ssid = ""  # its network (the access point's own, when it is one)
        self.hotspot = False
        self.wired = False  # another active connection: the camera has a network anyway
        self.address = ""
        self.networks = []  # the last scan: [{ssid, signal, secure}]
        self.saved = []  # the networks it knows: their connections' names
        self.busy = ""  # "hotspot", "connect", "scan"
        self.error = ""
        self._lock = asyncio.Lock()

    # --- What it is on

    @property
    def hotspot_ssid(self):
        try:
            with open(f"/sys/class/net/{IFACE}/address") as f:
                suffix = f.read().strip().replace(":", "")[-4:]
        except OSError:
            suffix = socket.gethostname()[-4:]
        return f"pycam-{suffix}"

    def hotspot_password(self):
        s = self.camera.settings
        if len(s.hotspot_password) < 8:
            s.hotspot_password = "".join(secrets.choice(ALPHABET) for _ in range(10))
            self.camera.save_soon()
        return s.hotspot_password

    async def refresh(self):
        rc, out, _ = await self.nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
        active = [(split_terse(line) + ["", "", ""])[:3] for line in out.splitlines()] if rc == 0 else []
        self.connection = next((name for name, kind, _ in active if kind == WIFI), "")
        self.hotspot = self.connection == HOTSPOT
        self.wired = any(kind not in (WIFI, "loopback") for _, kind, _ in active)
        self.ssid = ""
        if self.connection:
            rc, out, _ = await self.nmcli("-g", f"{WIFI}.ssid", "connection", "show", self.connection)
            self.ssid = out.strip() if rc == 0 else ""
        rc, out, _ = await self.nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        if rc == 0:
            pairs = [(split_terse(line) + [""])[:2] for line in out.splitlines()]
            self.saved = [name for name, kind in pairs if kind == WIFI and name != HOTSPOT]
        self.address = HOTSPOT_ADDRESS if self.hotspot else local_address()

    def summary(self):
        """For the menu: where the camera is."""
        if self.busy == "connect":
            return "Connexion…"
        if self.hotspot:
            return f"Point d'accès {self.ssid or self.hotspot_ssid}"
        if self.ssid:
            return f"{self.ssid} · {self.address}" if self.address else self.ssid
        return "Câble" if self.wired else "Non connecté"

    def hud(self):
        """On the HUD while the access point is open: the network to join."""
        return f"WI-FI {self.ssid or self.hotspot_ssid}" if self.hotspot else ""

    def hotspot_text(self):
        if self.busy == "hotspot":
            return "…"
        return "Activé" if self.hotspot else "Coupé"

    # --- Scanning, joining, forgetting

    async def scan(self):
        """The networks around, the best signal first. While the radio is the access point,
        the last scan (it was made just before)."""
        if self.hotspot:
            return self.networks
        self.busy = "scan"
        try:
            rc, out, err = await self.nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes")
        finally:
            self.busy = ""
        if rc:
            self.error = f"Recherche impossible : {_error(err)}"
            return self.networks
        best = {}
        for line in out.splitlines():
            ssid, signal, security = (split_terse(line) + ["", "", ""])[:3]
            strength = int(signal) if signal.isdigit() else 0
            if ssid and (ssid not in best or strength > best[ssid]["signal"]):
                best[ssid] = {"ssid": ssid, "signal": strength, "secure": security not in ("", "--")}
        self.networks = sorted(best.values(), key=lambda n: -n["signal"])
        return self.networks

    async def connect(self, ssid, password=""):
        """Joins a network (a new one, or a known one again). True if it is on it."""
        async with self._lock:
            self.busy, self.error = "connect", ""
            await self.refresh()
            was_hotspot, known = self.hotspot, set(self.saved)
            try:
                if was_hotspot:
                    await self.nmcli("connection", "down", HOTSPOT)
                    await self.nmcli("device", "wifi", "rescan")
                    await asyncio.sleep(RADIO_SETTLE)
                args = ["--wait", str(CONNECT_SECONDS), "device", "wifi", "connect", ssid, "ifname", IFACE]
                if password:
                    args += ["password", password]
                rc, _, err = await self.nmcli(*args, timeout=CONNECT_SECONDS + 15)
                if rc == 0:
                    log.info("wifi: joined %s", ssid)
                    return True
                self.error = f"{ssid} : {_error(err)}"
                log.warning("wifi: cannot join %s: %s", ssid, _error(err))
                # A connection made for nothing (a wrong password) goes: NetworkManager would
                # try it again and again.
                await self.refresh()
                for name in set(self.saved) - known:
                    await self.nmcli("connection", "delete", "id", name)
                if was_hotspot:
                    await self._hotspot_up()
                return False
            finally:
                self.busy = ""
                await self.refresh()
                self.camera.redraw()

    async def forget(self, name):
        if name not in self.saved:
            return False
        rc, _, err = await self.nmcli("connection", "delete", "id", name)
        if rc:
            self.error = f"{name} : {_error(err)}"
        await self.refresh()
        return rc == 0

    # --- The access point

    async def set_hotspot(self, on):
        async with self._lock:
            self.busy, self.error = "hotspot", ""
            self.camera.redraw()
            try:
                await self.refresh()
                if on and not self.hotspot:
                    await self.scan()  # while the radio is free: the networks to choose from
                    await self._hotspot_up()
                elif not on and self.hotspot:
                    rc, _, err = await self.nmcli("connection", "down", HOTSPOT)
                    if rc:
                        self.error = f"Point d'accès : {_error(err)}"
            finally:
                self.busy = ""
                await self.refresh()
                self.camera.redraw()
        if on and self.hotspot:
            self.camera.show_message(f"Wi-Fi {self.hotspot_ssid} · {self.hotspot_password()}", seconds=30)

    async def _hotspot_up(self):
        """Made each time, as the Pi's chip needs it. Not `nmcli device wifi hotspot`: it
        leaves the management frames' protection (PMF) to its default, and the Pi 3B's chip
        (brcmfmac) then refuses the access point's keys ("key setting validation failed",
        seen 2026-10-13). WPA2 alone, CCMP, PMF off, 2.4 GHz (the Pi 3B's only band)."""
        await self.nmcli("connection", "delete", "id", HOTSPOT)
        rc, _, err = await self.nmcli(
            "connection", "add", "type", "wifi", "ifname", IFACE, "con-name", HOTSPOT,
            # Only when asked for, or for a new camera: not again at the next start.
            "autoconnect", "no", "ssid", self.hotspot_ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable",
            "wifi-sec.psk", self.hotspot_password(),
        )  # fmt: skip
        if rc == 0:
            rc, _, err = await self.nmcli("--wait", "30", "connection", "up", "id", HOTSPOT, timeout=45)
        if rc:
            self.error = f"Point d'accès impossible : {_error(err)}"
            log.warning("wifi: no access point: %s", _error(err))
            return False
        log.info("wifi: access point %s open", self.hotspot_ssid)
        return True

    def toggle_hotspot(self):
        """MENU › Réseau › Point d'accès."""
        if not self.busy:
            self.camera.spawn(self.set_hotspot(not self.hotspot))

    # --- The background work

    async def run(self):
        started = time.monotonic()
        await self.refresh()
        first_setup = True
        while True:
            await asyncio.sleep(REFRESH)
            if self.busy:
                continue
            before = (self.ssid, self.hotspot, self.address)
            await self.refresh()
            if (self.ssid, self.hotspot, self.address) != before:
                self.camera.redraw()
            if first_setup and time.monotonic() - started >= FIRST_SETUP_DELAY:
                first_setup = False
                if not self.saved and not self.connection and not self.wired:
                    log.info("wifi: no network known nor found: access point for the first connection")
                    await self.set_hotspot(True)

    def to_dict(self):
        return {
            "ssid": self.ssid,
            "hotspot": self.hotspot,
            "hotspot_ssid": self.hotspot_ssid,
            "address": self.address,
            "wired": self.wired,
            "saved": self.saved,
            "connection": self.connection,
            "networks": self.networks,
            "busy": self.busy,
            "error": self.error,
        }
