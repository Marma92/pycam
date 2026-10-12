# Fichiers de tuning de la « base pycam »

Ces fichiers fixent le rendu de l'ISP sur lequel s'appliquent les presets (`docs/presets.md` §3). Les modifier change la base : il faut alors changer son numéro, et les presets écrits pour l'ancienne base sont à revoir.

| Fichier | Contenu |
|---|---|
| `pycam-imx477.json` | Copie exacte de `imx477_scientific.json` (libcamera `0.7.2+rpt20260817-1`, `/usr/share/libcamera/ipa/rpi/vc4/`) : courbe Rec.709, sans contraste adaptatif, 19 matrices couleur, sans correction d'ombrage de l'objectif. |
| `pycam-imx477-alsc.json` | Le même, avec le bloc `rpi.alsc` (correction d'ombrage de l'objectif) du tuning standard `imx477.json`, placé au même endroit que dans celui-ci. |

Les deux forment la **base pycam 1**. La correction d'ombrage agit sur la répartition spatiale (coins de l'image), pas sur la courbe ni sur les couleurs : les presets valent pour les deux.

Le moteur les cherche dans le dossier donné par `--tuning-dir`, et l'application choisit lequel charger (commande `configure`, clé `tuning`).

## Licence

Ces fichiers proviennent de libcamera (`src/ipa/rpi/vc4/data/`) :

```
Copyright (C) 2019-2025 Raspberry Pi Ltd

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```
