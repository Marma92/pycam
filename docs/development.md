# Développement

## Cible de test

- Pi 3B à `192.168.1.165`, utilisateur `ael`, accès SSH **par clé** (pas de mot de passe dans les scripts).
- Les scripts lisent l'hôte dans `PI_HOST`, par défaut `ael@192.168.1.165`.

## Organisation du dépôt

```
engine/    moteur C++ (meson) → binaire pycam-engine
app/       application Python → paquet pycam
system/    modèles des services systemd et de /run/pycam
tools/     déploiement, installation des services, audit, outils de test
docs/
```

## Services (l'appareil au quotidien)

Le Pi démarre directement sur l'appareil photo : deux services systemd, `pycam-engine` (le moteur) et `pycam` (l'application), installés depuis `~/pycam` :

```bash
sudo sh tools/install-services.sh             # installer, activer au démarrage, démarrer
sudo sh tools/install-services.sh --remove    # tout retirer
```

- **Journaux** : `journalctl -u pycam-engine -u pycam -f`.
- **Arrêter / relancer à la main** : `sudo systemctl stop pycam pycam-engine` et `start`. Pour lancer le moteur à la main (avec des options de test), il faut d'abord arrêter les services.
- **Robustesse** : le moteur redémarre seul s'il plante, l'application se reconnecte seule au moteur, et les deux réessaient indéfiniment au démarrage (la caméra peut apparaître après eux).

## Ajouter un objectif

En attendant les menus, les profils d'objectifs se modifient dans `~/.config/pycam/settings.json`, **l'application arrêtée**. Sinon, elle réécrit le fichier avec ce qu'elle a en mémoire.

```bash
sudo systemctl stop pycam
nano ~/.config/pycam/settings.json      # liste "lenses"
sudo systemctl start pycam
```

Un profil se présente ainsi : `{"name": "Fujinon 25 mm", "make": "Fujifilm", "focal_length": 25, "max_aperture": 1.4, "min_aperture": 22}`. `make` et `min_aperture` (16 par défaut) sont facultatifs, et un profil invalide est ignoré, avec un avertissement dans le journal.

## Tester l'écriture DNG

Toute modification de `engine/src/dng_writer.cpp` ou de `third_party/lj92` se vérifie par l'aller-retour, sur le Pi. Il faut LibRaw (`libraw-bin`) :

```bash
cd ~/pycam/engine && sh tests/dng_roundtrip.sh <un DNG pris par l'appareil>
```

Il réécrit l'image du DNG donné, en version non compressée puis compressée, avec notre écrivain. LibRaw doit relire les mêmes échantillons au bit près.

darktable lit les DNG avec son propre décodeur, rawspeed, plus strict que LibRaw. En cas de changement de format, le vérifier aussi : `sudo apt install darktable` (~40 Mo), puis `darktable-cli photo.DNG photo.jpg --core`.

## Boucle de travail

1. On édite sur le poste Windows, dans ce dépôt.
2. `sh tools/deploy.sh` copie `engine/`, `app/`, `tools/`, `system/` et `docs/` vers `~/pycam` sur le Pi (`tar | ssh`), puis compile le moteur **sur le Pi**. Avec `--no-build`, il copie seulement.
   - Compilation : meson + ninja, `ccache`, `-j2` pour ne pas saturer 1 Go de RAM.
   - Une recompilation complète prend ~1 min. Une recompilation incrémentale, quelques secondes.
   - Si les services sont installés, il les redémarre pour prendre le nouveau code.
3. Pour tester le moteur seul, on arrête les services et on le lance à la main :
   ```bash
   sudo systemctl stop pycam pycam-engine
   cd ~/pycam && nohup engine/build/pycam-engine > /tmp/pycam-engine.log 2>&1 < /dev/null &
   ```
   Options : `--zsl`, `--still-denoise cdn_fast`, `--zsl-buffers 3`… (`--help`). Le socket est `/run/pycam/engine.sock` dans tous les cas.
4. On le pilote avec le client de test :
   ```bash
   python3 tools/engine_cli.py state
   python3 tools/engine_cli.py set ExposureTime=8000 AnalogueGain=2
   python3 tools/engine_cli.py capture ~/DCIM/test/PYC_0001
   python3 tools/engine_cli.py events --seconds 3
   python3 tools/engine_cli.py raw '{"cmd": "quit"}'
   ```
   Le protocole est décrit dans `protocol.md`.
5. L'application tourne en service ; à la main (services arrêtés, moteur lancé) :
   ```bash
   cd ~/pycam/app && nohup python3 -m pycam -v > /tmp/pycam-app.log 2>&1 < /dev/null &
   ```
   - Elle prend la main sur les claviers branchés au Pi : voir les touches dans `architecture.md` §5.
   - Sans clavier, on simule des appuis : `sudo python3 tools/press.py right up space`.
   - Ses réglages sont dans `~/.config/pycam/settings.json`, et les photos dans `~/DCIM/100PYCAM/`.
   - Tests de la logique, sans Pi ni Pillow ni evdev : `cd app && python3 -m unittest discover -s tests -t .`
6. On rapatrie les images (`scp`) pour les inspecter.
7. **Capture d'écran à distance** : `python3 tools/screenshot.py /tmp/ecran.jpg --scale 0.5` compose la visée (commande `snapshot`) et le HUD (mémoire partagée) exactement comme l'écran. On la rapatrie avec `scp`. Elle prend ~2 s en 1080p.
8. Pour voir le rendu sur l'écran du Pi sans l'application :
   - avec le moteur lancé, `python3 tools/hud_demo.py` dessine un HUD de démonstration (grille, bandeau, histogramme) par-dessus la visée ;
   - sans le moteur, `sh tools/focus-preview.sh [loupe]` affiche l'aperçu rpicam et la netteté en direct dans le terminal, pratique pour régler la mise au point. À lancer avec `ssh -t` pour que Ctrl-C l'arrête.
9. On commite quand c'est validé sur le Pi.

## Dépendances installées sur le Pi (2026-10-11)

- **Build** : `librpicam-app-dev libcamera-dev libdrm-dev nlohmann-json3-dev libboost-program-options-dev libjpeg-dev libtiff-dev libexif-dev libpng-dev ccache`. gcc, meson et ninja étaient déjà présents.
- **Python** : `python3-evdev python3-aiohttp` ; Pillow et numpy étaient déjà présents. On utilise les paquets Debian plutôt qu'un venv.
- **Outils** : `socat i2c-tools`.

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
| rpicam-apps / librpicam-app1 / librpicam-app-dev | 1.13.0-1 |
| Picamera2 | 0.3.37-1 (sert seulement aux mesures rapides) |
| Toolchain | gcc 14.2, meson 1.7.0, ninja 1.12.1, Python 3.13.5 |
| Démarrage | Console (`multi-user.target`, `raspi-config` B1) depuis le 2026-10-11. Le bureau n'est plus lancé. |

**Les en-têtes de `librpicam-app-dev` diffèrent du dépôt GitHub au même numéro de version.** C'est toujours sur le Pi qu'on vérifie une signature : `/usr/include/rpicam-apps/`.

## Droits sur le Pi

`ael` a un `sudo` sans mot de passe pendant le développement : `/etc/sudoers.d/010_ael-nopasswd`, créé par l'utilisateur le 2026-10-11. Pour le retirer :

```bash
sudo rm /etc/sudoers.d/010_ael-nopasswd
```
