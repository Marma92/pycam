#!/usr/bin/env python3
"""Créer et tester des presets pycam sur un ordinateur (docs/presets.md §5).

Le même code que l'appareil (app/pycam/looks.py) : un preset donne le même rendu ici et
sur l'appareil, au grain près (non simulé ici). Il faut Python 3.11+ et Pillow.

Usage :
    python3 tools/pycam_preset.py identity hald.png              # l'image Hald identité, à retoucher
    python3 tools/pycam_preset.py check mon-preset/              # le preset est-il valide ? que fait-il ?
    python3 tools/pycam_preset.py apply mon-preset/ PYB_0012.JPG essai.jpg [--contrast 1] [--saturation -1]
    python3 tools/pycam_preset.py export mon-preset/ look.cube   # ou look.png (Hald), pour d'autres logiciels

Créer un preset à partir d'une retouche, dans n'importe quel logiciel photo :
  1. prendre une photo avec le preset « Base (neutre) », ou avec l'option « garder la base » ;
  2. régler son rendu sur cette photo, avec des réglages globaux seulement ;
  3. appliquer les mêmes réglages à l'image Hald identité, l'exporter en PNG : c'est la LUT ;
  4. un dossier avec cette image et un preset.toml (lut = "look.png"), copié dans
     ~/.config/pycam/presets/ sur l'appareil.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from pycam import looks  # noqa: E402


def need_pillow():
    try:
        from PIL import Image, ImageFilter  # noqa: F401
    except ImportError:
        sys.exit("Pillow est nécessaire : pip install pillow")


def cmd_identity(args):
    need_pillow()
    looks.hald_identity(args.level).save(args.output)
    side = args.level**3
    print(f"{args.output} : image Hald identité niveau {args.level} ({side}×{side}, LUT {args.level**2}³)")


def cmd_check(args):
    preset = looks.load_preset(args.preset)
    look = preset.look_table
    print(f"{preset.name} ({preset.short}), dossier « {preset.id} », base {preset.base}")
    if preset.description:
        print(f"  {preset.description}")
    if preset.base != looks.BASE:
        print(f"  attention : écrit pour la base {preset.base}, l'appareil a la base {looks.BASE}")
    for key in sorted(look):
        value = look[key]
        print(f"  look.{key} = {os.path.basename(value) if key == 'lut' else value}")
    print(f"  grain {preset.grain} (taille {preset.grain_size}), vignettage {preset.vignette}, décalage d'exposition {preset.exposure_bias:+} IL")
    table = looks.compile_look(preset, args.contrast, args.saturation)
    if looks.is_identity(table, looks.LUT_SIZE):
        print("  LUT : identité (le rendu de la base)")
        return
    curves = looks.neutral_curves(table, looks.LUT_SIZE)
    print("  gris  0,25 → %.3f   0,5 → %.3f   0,75 → %.3f" % tuple(curves["luma"][round(x * 255)] / 255 for x in (0.25, 0.5, 0.75)))
    preview = looks.fit_preview(table, looks.LUT_SIZE)
    if preview:
        print(f"  aperçu en visée : mélange {preview['luma_mix']}, chrominance {preview['chroma']}")


def compiled(args):
    return looks.compile_look(looks.load_preset(args.preset), args.contrast, args.saturation)


def cmd_apply(args):
    need_pillow()
    from PIL import Image, ImageFilter

    table = compiled(args)
    lut = ImageFilter.Color3DLUT(looks.LUT_SIZE, [v for rgb in table for v in rgb])
    with Image.open(args.input) as im:
        exif = im.info.get("exif")
        out = im.convert("RGB").filter(lut)
    out.save(args.output, quality=args.quality, **({"exif": exif} if exif else {}))
    print(f"{args.output} : {args.input} avec le preset (sans le grain ni le vignettage)")


def cmd_export(args):
    table = compiled(args)
    ext = os.path.splitext(args.output)[1].lower()
    if ext == ".cube":
        looks.write_cube(args.output, looks.LUT_SIZE, table, title=os.path.basename(os.path.normpath(args.preset)))
    elif ext == ".png":
        need_pillow()
        from PIL import Image

        level = args.level
        size, side = level * level, level**3
        step = 1 / (size - 1)
        pixels = []
        for b in range(size):
            for g in range(size):
                for r in range(size):
                    rgb = looks._sample(looks.LUT_SIZE, table, r * step, g * step, b * step)
                    pixels.append(tuple(round(255 * v) for v in rgb))
        im = Image.new("RGB", (side, side))
        im.putdata(pixels)
        im.save(args.output)
    else:
        sys.exit("export : .cube ou .png (Hald)")
    print(f"{args.output} : LUT du preset (entrée : la base pycam, Rec.709)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("identity", help="l'image Hald identité")
    p.add_argument("output")
    p.add_argument("--level", type=int, default=8, help="niveau Hald (8 : image 512×512, LUT 64³)")
    p.set_defaults(func=cmd_identity)

    def with_settings(p):
        p.add_argument("--contrast", type=int, default=0, choices=sorted(looks.CONTRAST), help="comme dans le menu")
        p.add_argument("--saturation", type=int, default=0, choices=sorted(looks.SATURATION), help="-3 : noir et blanc")

    p = sub.add_parser("check", help="vérifier un preset")
    p.add_argument("preset")
    with_settings(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("apply", help="appliquer un preset à un JPEG pris avec la base")
    p.add_argument("preset")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--quality", type=int, default=93)
    with_settings(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("export", help="exporter la LUT du preset (.cube ou .png Hald)")
    p.add_argument("preset")
    p.add_argument("output")
    p.add_argument("--level", type=int, default=8, help="niveau de l'image Hald")
    with_settings(p)
    p.set_defaults(func=cmd_export)

    args = parser.parse_args()
    try:
        args.func(args)
    except looks.PresetError as e:
        sys.exit(f"preset refusé : {e}")


if __name__ == "__main__":
    main()
