#!/bin/sh
# Installe instagrapi pour l'application Partage (publication sur Instagram), à part du
# Python du système : Debian n'en a pas de paquet. Il va dans ~/.local/share/pycam/vendor,
# que l'application n'ajoute à son chemin que pour Instagram ; rien d'autre n'en dépend.
# Usage, sur le Pi : sh tools/install-share.sh      (puis redémarrer le service pycam)
set -eu

VENDOR="${XDG_DATA_HOME:-$HOME/.local/share}/pycam/vendor"
mkdir -p "$VENDOR"
python3 -m pip install --upgrade --target "$VENDOR" instagrapi
echo "instagrapi installé dans $VENDOR"
