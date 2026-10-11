"""python3 -m pycam : the camera application (the engine must be running)."""

import argparse
import asyncio
import logging
import signal

from .camera import Camera
from .engine_client import DEFAULT_SOCKET, EngineClient
from .settings import default_path, load_settings

log = logging.getLogger("pycam")


def main():
    parser = argparse.ArgumentParser(prog="pycam", description="Application de l'appareil photo pycam.")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=f"socket du moteur (défaut {DEFAULT_SOCKET})")
    parser.add_argument("--settings", default=default_path(), help="fichier de réglages")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s pycam %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(args))


async def run(args):
    # Imported here: they need Pillow and evdev, which the logic and its tests do not.
    from .hud import Hud
    from .inputs import Inputs

    settings = load_settings(args.settings)
    camera = Camera(settings, args.settings, EngineClient(args.socket), hud_factory=Hud)
    inputs = Inputs(camera.on_action)

    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)

    log.info("started, settings in %s", args.settings)
    try:
        await camera.run(inputs)
    except asyncio.CancelledError:
        pass
    finally:
        camera.shutdown()
        log.info("stopped")


if __name__ == "__main__":
    main()
