# Développement

## Cible de test

- Pi 3B à `192.168.1.165`, utilisateur `ael`, accès SSH **par clé** (pas de mot de passe dans les scripts).
- Les scripts lisent l'hôte dans `PI_HOST`, par défaut `ael@192.168.1.165`.

## Organisation du dépôt (cible)

```
engine/    moteur C++ (meson) → binaire pycam-engine
app/       application Python → paquet pycam
system/    unités systemd, extraits de config.txt, overlays
tools/     scripts de déploiement, d'audit, de bench
docs/
```

## Boucle de travail

1. On édite sur le poste Windows, dans ce dépôt.
2. `tools/deploy.sh` copie les sources vers `~/pycam` sur le Pi (`tar | ssh`, sans dépendance à rsync).
3. On compile **sur le Pi** : `meson setup` puis `ninja -C engine/build`. Le moteur reste petit ; on active `ccache` et on limite à `-j2` pour ne pas saturer 1 Go de RAM.
4. On teste en SSH :
   - le moteur sans écran (`--no-display`) ;
   - le pilotage par le socket (`socat`) ;
   - les images capturées, rapatriées et inspectées ;
   - les mesures, avec les scripts de bench.
5. On commite quand c'est validé sur le Pi.

## Dépendances côté Pi (prévisionnel)

- **Build** : `build-essential meson ninja-build pkg-config ccache`, `librpicam-app-dev` (ou les en-têtes de rpicam-apps), `libcamera-dev`, `libdrm-dev`, `nlohmann-json3-dev`, `libjpeg-dev libtiff-dev libexif-dev`.
- **Python** : on utilise les paquets Debian quand ils existent (`python3-pil python3-evdev python3-aiohttp`). Sinon, un venv `--system-site-packages`.
- **Outils** : `socat`, `i2c-tools`.

## Audit de la cible

```bash
ssh ael@192.168.1.165 'sh -s' < tools/pi-audit.sh
```

Le script fait l'inventaire de l'OS, de la mémoire et de la CMA, de la caméra, des versions de libcamera et rpicam, des sorties DRM et des outils de build.

## Version de référence (audit du 2026-10-11)

| Composant | Version |
|---|---|
| OS | Raspberry Pi OS Trixie (Debian 13), 64 bits |
| Noyau | 6.18.50+rpt-rpi-v8 |
| libcamera | 0.7.2+rpt20260817-1 |
| rpicam-apps / librpicam-app1 | 1.13.0-1 (`librpicam-app-dev` 1.13.0-1 disponible, non installé) |
| Picamera2 | 0.3.37-1 (sert seulement aux mesures rapides) |
| Toolchain | gcc 14.2, meson 1.7.0, ninja 1.12.1, Python 3.13.5 |
| Python déjà présent | Pillow 11.1, numpy 2.2.4 (manquent : evdev, aiohttp) |

## Droits sur le Pi

`sudo` demande un mot de passe pour `ael`, et je ne le saisis pas. Pour les opérations système (apt, systemd, `config.txt`), deux options :

- l'utilisateur lance lui-même la commande que je prépare ;
- l'utilisateur crée une règle sudo sans mot de passe pour `ael`, qu'il pourra retirer à la fin du développement.
