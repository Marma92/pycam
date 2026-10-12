# pycam

Un appareil photo autonome et utilisable au quotidien : Raspberry Pi + High Quality Camera (Sony IMX477).

Il vise à offrir :

- une visée en direct ;
- les modes P / S / A / M ;
- des aides à la mise au point manuelle (peaking, loupe, zébras, histogramme) ;
- du RAW DNG + JPEG, avec des simulations de pellicules ;
- des commandes physiques, une batterie et une interface web.

## Statut

Jalons M0 (« ça prend des photos ») et M1 (« appareil manuel ») terminés ; M2 et M3 en cours (`docs/features.md`). Le Pi 3B démarre directement sur l'appareil photo. Les boutons et molettes sont prévus et câblés sur le papier (`docs/hardware.md`) : en attendant, un clavier USB tient lieu de commandes physiques, une touche par commande de l'appareil.

Ce que l'appareil fait aujourd'hui :

- **Visée** sur l'écran HDMI (plan DRM, zéro copie), avec HUD : mode, vitesse, ISO, posemètre, histogramme RVB, grille, vues restantes.
- **Exposition** P / S / A / M au tiers d'IL, deux molettes, mesure pondérée centrale / spot / matricielle, balance des blancs (auto, préréglages, Kelvin, mémorisée, « pellicule »).
- **Mise au point manuelle** : peaking, zébras, loupe ×5 / ×10 déplaçable.
- **Déclenchement** en bascule (visée fluide) ou ZSL (sans latence) ; JPEG et DNG (compressé sans perte), écrits de façon durable.
- **Modes de déclenchement** : vue par vue, rafale, retardateur 2 s / 10 s, intervallomètre, **timelapse** (la vidéo MP4 est assemblée sur l'appareil, lissée contre le scintillement), bracketing d'exposition.
- **Poses longues** jusqu'à 10'40", avec compte à rebours, et **pose B** jusqu'à 30 min (une somme de poses de 1 s, développée par le moteur).
- **Empilement** de vues : moyenne (moins de bruit, pose longue simulée) ou maximum (traînées de lumière, d'étoiles).
- **Simulations de pellicules** : presets ouverts (une recette TOML, éventuellement une LUT), appliqués au JPEG avec grain, halation et vignettage, aperçus en visée ; le DNG reste neutre. Livrés : Standard, Base, Portrait 400, Velours 50, Cinéma 800T (+2), Triple-X 400. On peut écrire les siens (`docs/presets.md`).
- **Menus** Q (12 réglages de prise de vue) et MENU (tout le reste), **lecture** des photos (loupe, suppression) et des vidéos (décodeur matériel).
- **Économie d'énergie** : l'écran se met en veille pendant les longues prises.
- **Interface web** sur le réseau local (port 8080) : visée, déclencheur, choix du preset, galerie et téléchargement (photos et vidéos de timelapse), installation de presets.

## Comment c'est fait

- `engine/` : le moteur caméra en C++ (`pycam-engine`), lié à `librpicam_app`. Il fait la visée, les prises, les looks des JPEG, l'écriture DNG, les mesures, et se pilote par un socket Unix en JSON (`docs/protocol.md`).
- `app/` : l'application Python (`asyncio`) : commandes, exposition, menus, HUD, presets, rangement DCIM, réglages persistants, interface web.
- `system/` et `tools/install-services.sh` : les deux services systemd qui les lancent au démarrage.
- `tools/` : déploiement depuis un PC (`deploy.sh`), client du moteur, captures d'écran à distance, outil de presets.

Le développement se fait depuis un PC, par SSH sur le Pi : voir `docs/development.md`.

## Documentation

- [Architecture](docs/architecture.md) : découpage moteur C++ / application Python, pipeline caméra, affichage, commandes, menus, décisions.
- [Fonctionnalités](docs/features.md) : catalogue par jalons (M0 → M3) et état.
- [Protocole](docs/protocol.md) : commandes et évènements du moteur.
- [Presets](docs/presets.md) : la base d'image, le format des presets, les simulations de pellicules et comment en créer.
- [Matériel](docs/hardware.md) : configuration actuelle, liste d'achats chiffrée, câblage des boutons et molettes, budget énergétique.
- [Développement](docs/development.md) : boucle de travail, déploiement et tests sur le Pi par SSH.

## Licences

- pycam : licence MIT (`LICENSE`).
- `engine/tuning/` : fichiers de tuning de l'ISP dérivés de ceux de libcamera, © Raspberry Pi, licence BSD-2-Clause (`engine/tuning/README.md`).
- `engine/third_party/lj92` : encodeur JPEG sans perte d'Andrew Baldwin, licence MIT.

## Inspirations

- [rpicam-apps](https://github.com/raspberrypi/rpicam-apps) : la référence officielle de Raspberry Pi. On réutilise sa bibliothèque cœur `librpicam_app`.
- [CinePI](https://github.com/schoolpost/CinePI) / [cinepi-raw](https://github.com/cinepi/cinepi-raw) (schoolpost) : le découpage moteur C++ + interface Python, et l'écriture CinemaDNG.
- [PiDNG](https://github.com/schoolpost/PiDNG) : le format DNG pour les capteurs Raspberry Pi.
