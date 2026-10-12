#!/usr/bin/env python3
"""Applique un preset complet à une image, avec le code du moteur : LUT, mais aussi grain,
vignettage et halation, que tools/pycam_preset.py n'imite pas. Pour régler un preset.

Sur le Pi, le moteur compilé (engine/build/look-apply) :
    python3 tools/look_apply.py app/pycam/presets/cinema-800t-plus2 photo.jpg essai.png [--contrast 1]

L'image d'entrée est vue comme la base pycam (Rec.709) : une photo prise avec le preset
« Base », ou une image de synthèse.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
from pycam import looks  # noqa: E402


def to_yuv420(im):
    """Full range BT.601 planar YUV420, as the ISP gives (Pillow's YCbCr is the same)."""
    w, h = im.size
    y, cb, cr = im.convert("YCbCr").split()
    half = (w // 2, h // 2)
    return y.tobytes() + cb.resize(half, Image.BOX).tobytes() + cr.resize(half, Image.BOX).tobytes()


def from_yuv420(data, w, h):
    n = w * h
    y = Image.frombytes("L", (w, h), data[:n])
    cb = Image.frombytes("L", (w // 2, h // 2), data[n : n + n // 4]).resize((w, h), Image.BILINEAR)
    cr = Image.frombytes("L", (w // 2, h // 2), data[n + n // 4 :]).resize((w, h), Image.BILINEAR)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("preset")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--contrast", type=int, default=0, choices=sorted(looks.CONTRAST))
    parser.add_argument("--saturation", type=int, default=0, choices=sorted(looks.SATURATION))
    parser.add_argument("--no-grain", action="store_true")
    args = parser.parse_args()

    preset = looks.load_preset(args.preset)
    compiled = looks.compiled_look(preset, args.contrast, args.saturation)
    params = dict(looks.engine_look(preset, grain=not args.no_grain), lut=compiled.lut)
    with Image.open(args.input) as im:
        im = im.convert("RGB")
        w, h = im.width & ~1, im.height & ~1
        im = im.crop((0, 0, w, h))
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.yuv"), os.path.join(tmp, "out.yuv")
        with open(src, "wb") as f:
            f.write(to_yuv420(im))
        tool = os.path.join(ROOT, "engine", "build", "look-apply")
        subprocess.run([tool, src, dst, str(w), str(h), json.dumps(params)], check=True)
        with open(dst, "rb") as f:
            from_yuv420(f.read(), w, h).save(args.output)
    print(f"{args.output} : {preset.name}")


if __name__ == "__main__":
    main()
