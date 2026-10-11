#!/usr/bin/env python3
"""Démo du HUD : dessine avec Pillow dans la mémoire partagée du moteur, par-dessus la visée.

Prototype de ce que fera l'application (docs/architecture.md §4) : grille des tiers,
bandeau d'informations et histogramme, rafraîchis à chaque évènement "stats" (~5 Hz).

Usage (sur le Pi, moteur lancé) : python3 tools/hud_demo.py [--seconds N]
"""

import argparse
import mmap
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_cli import DEFAULT_SOCKET, Engine  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def shutter_text(exposure_us):
    seconds = exposure_us / 1e6
    if seconds >= 0.5:
        return f'{seconds:.1f}"'
    return f"1/{round(1 / seconds)}" if seconds > 0 else "-"


class Hud:
    def __init__(self, info):
        hud = info["hud"]
        self.size = (hud["width"], hud["height"])
        self.x, self.y, self.w, self.h = info["image_rect"]
        fd = os.open(hud["path"], os.O_RDWR)
        self.shm = mmap.mmap(fd, hud["stride"] * hud["height"])
        os.close(fd)
        scale = self.h / 1080
        self.font = ImageFont.truetype(FONT, round(34 * scale))
        self.font_big = ImageFont.truetype(FONT_BOLD, round(44 * scale))
        self.scale = scale

    def draw(self, stats):
        # Noir opaque autour de l'image (il masque la console), transparent dessus.
        img = Image.new("RGBA", self.size, (0, 0, 0, 255))
        d = ImageDraw.Draw(img)
        x, y, w, h, s = self.x, self.y, self.w, self.h, self.scale
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=(0, 0, 0, 0))

        # Grille des tiers, sur l'image seulement.
        line = (255, 255, 255, 90)
        for i in (1, 2):
            d.line([(x + w * i // 3, y), (x + w * i // 3, y + h)], fill=line, width=max(1, round(2 * s)))
            d.line([(x, y + h * i // 3), (x + w, y + h * i // 3)], fill=line, width=max(1, round(2 * s)))

        # Bandeau du bas : vitesse, ISO, température de couleur, netteté, fps.
        bar_h = round(70 * s)
        d.rectangle([x, y + h - bar_h, x + w, y + h], fill=(0, 0, 0, 150))
        iso = round(stats.get("analogue_gain", 1) * stats.get("digital_gain", 1) * 100)
        items = [
            (shutter_text(stats.get("exposure_us", 0)), self.font_big),
            (f"ISO {iso}", self.font_big),
            (f"{stats.get('colour_temp', 0)} K", self.font),
            (f"net. {stats.get('focus_fom', 0):.0f}", self.font),
            (f"{stats.get('fps', 0):.0f} i/s", self.font),
        ]
        cx = x + round(30 * s)
        for text, font in items:
            top = y + h - bar_h + (bar_h - font.size) // 2 - round(4 * s)
            d.text((cx, top), text, font=font, fill=(255, 255, 255, 255))
            cx += d.textlength(text, font=font) + round(45 * s)

        # Histogramme de luminance, en haut à droite de l'image.
        hist = stats.get("histogram") or []
        if hist:
            hw, hh = round(320 * s), round(150 * s)
            hx, hy = x + w - hw - round(30 * s), y + round(30 * s)
            d.rectangle([hx, hy, hx + hw, hy + hh], fill=(0, 0, 0, 120))
            peak = max(hist) or 1
            bw = hw / len(hist)
            for i, v in enumerate(hist):
                bh = round(hh * v / peak)
                if bh:
                    d.rectangle([hx + i * bw, hy + hh - bh, hx + (i + 1) * bw - 1, hy + hh], fill=(255, 255, 255, 200))

        self.shm[:] = img.tobytes()  # octets R, G, B, A : le format attendu (ABGR8888)

    def clear(self):
        """Revient au HUD par défaut du moteur : marges noires, rien sur l'image."""
        img = Image.new("RGBA", self.size, (0, 0, 0, 255))
        ImageDraw.Draw(img).rectangle([self.x, self.y, self.x + self.w - 1, self.y + self.h - 1], fill=(0, 0, 0, 0))
        self.shm[:] = img.tobytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=os.environ.get("PYCAM_ENGINE_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--seconds", type=float, default=0, help="durée (0 : jusqu'à Ctrl-C)")
    args = parser.parse_args()

    engine = Engine(args.socket)
    reply = engine.call({"cmd": "hud_info"})
    if not reply.get("ok"):
        sys.exit(f"pas d'affichage : {reply.get('detail')}")
    hud = Hud(reply["display"])

    render_ms, frames = 0.0, 0
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            msg = engine.read(1.0)
            if not msg or msg.get("event") != "stats":
                continue
            t = time.monotonic()
            hud.draw(msg)
            engine.call({"cmd": "hud_commit"})
            render_ms += (time.monotonic() - t) * 1000
            frames += 1
    except KeyboardInterrupt:
        pass
    finally:
        hud.clear()
        engine.call({"cmd": "hud_commit"})
        if frames:
            print(f"{frames} HUD dessinés, {render_ms / frames:.0f} ms en moyenne (dessin + copie + commit)")


if __name__ == "__main__":
    main()
