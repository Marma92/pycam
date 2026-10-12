"""The network, as the applications need it (apps/)."""

import asyncio
import socket


async def network_state():
    """(online, metered), from NetworkManager. Online: connected to the Internet (not only
    to the network, nor behind a portal). Without NetworkManager: (True, False), to try."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "STATE,CONNECTIVITY,METERED", "general",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return True, False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except TimeoutError:
        proc.kill()
        return False, False
    state, connectivity, metered = (out.decode(errors="replace").strip().split(":") + ["", "", ""])[:3]
    return state == "connected" and connectivity in ("full", "unknown"), metered.startswith("yes")


def local_address():
    """The camera's address on its network (where the web interface is), or ""."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent: only the route is looked up
            return s.getsockname()[0]
    except OSError:
        return ""
