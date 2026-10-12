#!/usr/bin/env python3
"""Capture d'écran de l'appareil : la visée (commande snapshot du moteur) et le HUD
(mémoire partagée) composés comme le fait l'écran. Pour vérifier l'affichage à distance.

Usage (sur le Pi, moteur lancé) : python3 tools/screenshot.py /tmp/ecran.png [--scale 0.5]
"""

import argparse
import mmap
import os
import sys
import tempfile

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_cli import DEFAULT_SOCKET, Engine  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", help="fichier image (PNG ou JPEG)")
    parser.add_argument("--scale", type=float, default=1.0, help="réduction de la capture (ex. 0.5)")
    parser.add_argument("--socket", default=os.environ.get("PYCAM_ENGINE_SOCKET", DEFAULT_SOCKET))
    args = parser.parse_args()

    engine = Engine(args.socket)
    reply = engine.call({"cmd": "hud_info"})
    if not reply.get("ok"):
        sys.exit(f"pas d'affichage : {reply.get('detail')}")
    display = reply["display"]
    width, height = display["screen"]
    x, y, w, h = display["image_rect"]

    # The HUD first: the viewfinder snapshot takes a frame, the HUD may change meanwhile.
    hud_info = display["hud"]
    with open(hud_info["path"], "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as shm:
        hud = Image.frombuffer("RGBA", (width, height), shm[: hud_info["stride"] * height], "raw", "RGBA", 0, 1)
        hud = hud.copy()

    screen = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "viewfinder.jpg")
        shot = engine.call({"cmd": "snapshot", "path": path}).get("ok")
        while shot:
            msg = engine.read(3.0)
            if msg is None:
                # No viewfinder frame (a long exposure: the screen keeps the last one): the
                # HUD alone, over grey.
                print("pas d'image de visée : le HUD seul", file=sys.stderr)
                screen.paste((60, 60, 60, 255), (x, y, x + w, y + h))
                shot = False
            elif msg.get("path") == path:
                with Image.open(path) as viewfinder:
                    screen.paste(viewfinder.convert("RGBA").resize((w, h), Image.BILINEAR), (x, y))
                break

    screen.alpha_composite(hud)
    if args.scale != 1:
        screen = screen.resize((round(width * args.scale), round(height * args.scale)), Image.BILINEAR)
    screen.convert("RGB").save(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
