#!/usr/bin/env python3
"""Simule des appuis de touches avec un clavier virtuel (uinput), pour tester l'application
sans clavier branché. L'application détecte le clavier virtuel en ~2 s.

Usage (sur le Pi ; /dev/uinput demande root) :
    sudo python3 tools/press.py space                  # déclencher
    sudo python3 tools/press.py right right up tab d   # vitesse, ISO, menu rapide, affichage
"""

import argparse
import time

from evdev import UInput, ecodes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("keys", nargs="+", help="noms de touches evdev sans KEY_ : space, right, up, tab…")
    parser.add_argument("--wait", type=float, default=3.0, help="délai pour que l'application voie le clavier")
    parser.add_argument("--interval", type=float, default=0.5, help="délai entre deux touches")
    args = parser.parse_args()

    codes = []
    for name in args.keys:
        code = ecodes.ecodes.get("KEY_" + name.upper())
        if code is None:
            parser.error(f"touche inconnue : {name}")
        codes.append(code)

    # KEY_SPACE always declared, so that the application recognises a keyboard.
    capabilities = {ecodes.EV_KEY: sorted(set(codes) | {ecodes.KEY_SPACE})}
    with UInput(capabilities, name="pycam-test-keyboard") as keyboard:
        time.sleep(args.wait)
        for code in codes:
            keyboard.write(ecodes.EV_KEY, code, 1)
            keyboard.syn()
            time.sleep(0.05)
            keyboard.write(ecodes.EV_KEY, code, 0)
            keyboard.syn()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
