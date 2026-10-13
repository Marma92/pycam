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

## Installer une release (un appareil « normal »)

Sur un Raspberry Pi OS Lite 64 bits (Trixie) neuf, caméra branchée, connecté à Internet :

```bash
curl -fsSL https://raw.githubusercontent.com/Marma92/pycam/master/tools/install.sh | sudo sh
```

ou, avec une archive téléchargée depuis les releases GitHub : `sudo sh install.sh pycam-0.1.0-arm64.tar.gz`. L'appareil est ensuite dans `/opt/pycam`, démarre sur la visée, et se met à jour seul (`architecture.md` §8.1). Le journal des mises à jour : `journalctl -u pycam-update`, leur état : `/var/lib/pycam-update/status.json`.

**Développement et release sur le même Pi.** `tools/deploy.sh` copie dans `~/pycam`, et `sudo sh tools/install-services.sh` y fait tourner les services : l'appareil est alors en développement et ne cherche plus de mise à jour. `sudo sh tools/install.sh` le remet sur les releases.

## Publier une release

1. Mettre le numéro dans `VERSION` (SemVer : `0.1.1` pour une correction, `0.2.0` pour des fonctions), et décrire la version dans `CHANGELOG.md` (une section `## [0.1.1] — date`) : ce sont ses notes.
2. Fusionner dans `master`, puis poser le tag et le pousser :
   ```bash
   git tag v0.1.1 && git push origin v0.1.1
   ```
3. GitHub Actions (`.github/workflows/build.yml`) compile le moteur sur un runner ARM, dans Debian Trixie avec l'archive Raspberry Pi, lance les tests, fait l'archive (`tools/package.sh`) et publie la release, si le tag est bien la version de `VERSION`. Les appareils la trouvent à leur prochaine connexion.

Sur chaque push d'une branche, la CI fait tout sauf la publication : l'archive est un artefact du run (`gh run download`), pour l'essayer sur un Pi avant de la publier (`sudo sh tools/install.sh <archive>`).

**Essayer une mise à jour sans rien publier** (fait le 2026-10-13) : l'appareil installé depuis une archive de la CI, on fait passer `pycam-update.service` par un faux GitHub sur le Pi, avec une surcharge systemd :

```ini
# /etc/systemd/system/pycam-update.service.d/test.conf
[Service]
Environment=PYCAM_UPDATE_API=http://127.0.0.1:8765/repos/test/pycam/releases/latest
```

Le serveur rend un `releases/latest` au format de GitHub, et ses fichiers (seul `127.0.0.1` est admis hors HTTPS). Une fausse version se fait depuis l'archive de la CI : `VERSION`, `RELEASE.json` et la chaîne `pycam-engine 0.1.0` du moteur réécrits, sinon la préparation l'écarte, à raison. Ensuite, retirer la surcharge et **`/var/lib/pycam-update`** : il garde la liste des versions écartées, qui le resteraient pour de vrai.

## Services (l'appareil au quotidien)

Le Pi démarre directement sur l'appareil photo : deux services systemd, `pycam-engine` (le moteur) et `pycam` (l'application). En développement, installés depuis `~/pycam` :

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

## Créer un preset

Sur un ordinateur, avec Python 3.11+ et Pillow : `tools/pycam_preset.py` (voir `presets.md` §5). Puis copier le dossier du preset dans `~/.config/pycam/presets/` sur le Pi : il apparaît à la prochaine ouverture d'un menu.

```bash
scp -r mon-preset ael@192.168.1.165:.config/pycam/presets/
```

Pour régler un preset avec ce que seul le moteur dessine (grain, vignettage, halation), sur le Pi, avec le moteur compilé :

```bash
python3 tools/look_apply.py app/pycam/presets/cinema-800t-plus2 photo.jpg essai.png
```

## Tests unitaires du moteur

L'analyse d'image (histogrammes YUV → RVB) est testée sur des images synthétiques de couleur connue, sur le Pi après une compilation :

```bash
cd ~/pycam/engine && meson test -C build --print-errorlogs
```

## Boucle de travail

1. On édite sur le poste Windows, dans ce dépôt.
2. `sh tools/deploy.sh` copie `engine/`, `app/`, `tools/`, `system/` et `docs/` vers `~/pycam` sur le Pi (`tar | ssh`), puis compile le moteur **sur le Pi**. Avec `--no-build`, il copie seulement. La compilation arrête les services pycam et ne lance qu'un compilateur à la fois : deux des gros fichiers (`engine.cpp`, `develop.cpp`) en parallèle saturent le Gio du Pi 3B, qui ne répond alors plus, même en SSH (vu le 2026-10-12). `PYCAM_JOBS=2` en lance deux.
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
   - Sans clavier, on simule des appuis : `sudo python3 tools/press.py right up space` ; `space:3` tient la touche 3 s (rafale).
   - Ses réglages sont dans `~/.config/pycam/settings.json`, et les photos dans `~/DCIM/100PYCAM/`.
   - Tests de la logique, sans Pi ni Pillow ni evdev : `cd app && python3 -m unittest discover -s tests -t .` Ceux de l'interface web et de Cloud sync ont besoin d'aiohttp : ils sont sautés sans lui, et tournent sur le Pi. Les tests simulent une grande carte (`tests/__init__.py`) : sur le Pi, `/tmp` est un disque en mémoire de moins de 256 Mo libres, sous la réserve de la carte, et toutes les photos y seraient refusées. Cloud sync y est testé contre de faux services WebDAV, Dropbox et Google Drive lancés sur 127.0.0.1 : aucun compte réel n'est nécessaire.
6. On rapatrie les images (`scp`) pour les inspecter.
7. **Capture d'écran à distance** : `python3 tools/screenshot.py /tmp/ecran.jpg --scale 0.5` compose la visée (commande `snapshot`) et le HUD (mémoire partagée) exactement comme l'écran. On la rapatrie avec `scp`. Elle prend ~2 s en 1080p.
8. Pour voir le rendu sur l'écran du Pi sans l'application :
   - avec le moteur lancé, `python3 tools/hud_demo.py` dessine un HUD de démonstration (grille, bandeau, histogramme) par-dessus la visée ;
   - sans le moteur, `sh tools/focus-preview.sh [loupe]` affiche l'aperçu rpicam et la netteté en direct dans le terminal, pratique pour régler la mise au point. À lancer avec `ssh -t` pour que Ctrl-C l'arrête.
9. On commite quand c'est validé sur le Pi.

## Dépendances installées sur le Pi (2026-10-11)

- **Build** : `librpicam-app-dev libcamera-dev libdrm-dev nlohmann-json3-dev libboost-program-options-dev libjpeg-dev libtiff-dev libexif-dev libpng-dev libavcodec-dev libavformat-dev libavutil-dev ccache` (libav pour la lecture des vidéos, ajoutée le 2026-10-12). ffmpeg (déjà installé sur Pi OS) assemble les timelapses. gcc, meson et ninja étaient déjà présents.
- **Python** : `python3-evdev python3-aiohttp` ; Pillow était déjà présent. On utilise les paquets Debian plutôt qu'un venv. Seule exception : instagrapi (publication sur Instagram), sans paquet Debian, installé à part par `sh tools/install-share.sh` dans `~/.local/share/pycam/vendor` (fait le 2026-10-12 : instagrapi 3.0.22), et chargé seulement dans le processus qui publie sur Instagram.
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
