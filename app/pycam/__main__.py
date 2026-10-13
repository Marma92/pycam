"""python3 -m pycam : the camera application (the engine must be running)."""

import argparse
import asyncio
import logging
import os
import shutil
import signal

from . import __version__
from .camera import Camera
from .engine_client import DEFAULT_SOCKET, EngineClient
from .settings import default_path, load_settings

log = logging.getLogger("pycam")


def main():
    parser = argparse.ArgumentParser(prog="pycam", description="Application de l'appareil photo pycam.")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=f"socket du moteur (défaut {DEFAULT_SOCKET})")
    parser.add_argument("--settings", default=default_path(), help="fichier de réglages")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    parser.add_argument("--version", action="version", version=f"pycam {__version__}")
    parser.add_argument("--check", action="store_true", help="tout charger, donner la version et s'arrêter (mises à jour)")
    args = parser.parse_args()
    if args.check:
        check()
        return
    # Under systemd the journal already timestamps and names each line.
    fmt = "%(levelname)s %(name)s: %(message)s"
    if not os.environ.get("JOURNAL_STREAM"):
        fmt = "%(asctime)s pycam " + fmt
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt)
    asyncio.run(run(args))


def check():
    """Before an update is installed (updater.py): everything the camera loads, loaded."""
    from . import apps, hud, inputs, updates, web, wifi  # noqa: F401

    print(f"pycam {__version__}")


async def run(args):
    # Imported here: they need Pillow and evdev, which the logic and its tests do not.
    from .hud import Hud
    from .inputs import Inputs

    settings = load_settings(args.settings)
    camera = Camera(settings, args.settings, EngineClient(args.socket), hud_factory=Hud)
    inputs = Inputs(camera.on_action)
    try:
        from .apps import make_apps

        camera.apps = make_apps(camera)
    except ImportError as e:  # no aiohttp
        log.warning("no applications: %s", e)
    from .updates import Updates

    camera.updates = Updates(camera)
    if shutil.which("nmcli"):  # NetworkManager: Raspberry Pi OS's
        from .wifi import Wifi

        camera.wifi = Wifi(camera)
    camera.ready_path = os.path.join(os.path.dirname(args.socket), "ready")

    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)

    web_server = None
    if settings.web:
        try:
            from .web import WebServer

            web_server = WebServer(camera)
            await web_server.start()
        except (ImportError, OSError) as e:  # no aiohttp, or the port is taken
            log.warning("no web interface: %s", e)
            web_server = None

    log.info("pycam %s started, settings in %s", __version__, args.settings)
    try:
        await camera.run(inputs)
    except asyncio.CancelledError:
        pass
    finally:
        if web_server:
            await web_server.stop()
        camera.shutdown()
        log.info("stopped")


if __name__ == "__main__":
    main()
