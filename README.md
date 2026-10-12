# pycam

Un appareil photo autonome et utilisable au quotidien : Raspberry Pi + High Quality Camera (Sony IMX477).

Il vise à offrir :

- une visée en direct et les modes P / S / A / M ;
- des aides à la mise au point manuelle (peaking, loupe, zébras, histogramme) ;
- du RAW DNG + JPEG, avec des simulations de pellicules ;
- une vidéo d'appoint ;
- des applications embarquées : sauvegarde dans le cloud, partage sur les réseaux ;
- des commandes physiques, une batterie et une interface web.

## Statut

- **M0** (« ça prend des photos ») et **M1** (« appareil manuel ») : terminés.
- **M3** (« avancé ») : presque terminé. Modes de déclenchement, timelapse, empilement, poses longues et pose B, ZSL, DNG compressé et vidéo sont faits. Les simulations de pellicules restent à affiner sur de vraies photos. Cloud sync et Partage sont testés contre de faux services, pas encore avec de vrais comptes.
- **M2** (« autonome ») : la partie logicielle est prête (menus, lecture, interface web, veille de l'écran). Reste le matériel : boutons et molettes (choisis et câblés sur le papier, `docs/hardware.md`), batterie, horloge, niveau électronique.

Le détail, fonction par fonction : `docs/features.md`. Le Pi 3B démarre directement sur l'appareil photo ; en attendant les boutons, un clavier USB tient lieu de commandes, une touche par commande de l'appareil.

## Ce que l'appareil fait aujourd'hui

### Photo

- **Visée** sur l'écran HDMI (plan DRM, zéro copie), avec HUD : mode, vitesse, ISO, posemètre, histogramme RVB, grille, repères de cadrage (9:16, 4:5, 1:1, 2.39:1), vues restantes.
- **Exposition** P / S / A / M au tiers d'IL, deux molettes, mesure pondérée centrale / spot / matricielle, balance des blancs (auto, préréglages, Kelvin, mémorisée, « pellicule »).
- **Mise au point manuelle** : peaking, zébras, loupe ×5 / ×10 déplaçable ; profils d'objectifs, ouverture inscrite dans l'EXIF.
- **Déclenchement** en bascule (visée fluide) ou ZSL (sans latence) ; JPEG et DNG compressé sans perte, écrits de façon durable (une coupure de courant ne laisse jamais de fichier tronqué).
- **Modes de déclenchement** : vue par vue, rafale, retardateur 2 s / 10 s, intervallomètre, bracketing d'exposition, **timelapse** (la vidéo MP4 est assemblée sur l'appareil, lissée contre le scintillement), **empilement** (moyenne : moins de bruit ou pose longue simulée ; maximum : traînées de lumière, d'étoiles).
- **Poses longues** jusqu'à 10'40", avec compte à rebours, et **pose B** jusqu'à 30 min (une somme de poses de 1 s, développée par le moteur).
- **Simulations de pellicules** : presets ouverts (une recette TOML, éventuellement une LUT), appliqués au JPEG avec grain, halation et vignettage, aperçus en visée ; le DNG reste neutre. Livrés : Standard, Base, Portrait 400, Velours 50, Cinéma 800T (+2), Triple-X 400. On peut écrire les siens (`docs/presets.md`).
- **Menus** Q (12 réglages de prise de vue) et MENU (tout le reste, applications comprises).
- **Économie d'énergie** : l'écran se met en veille pendant les longues prises (un tiers de CPU en moins).

### Vidéo d'appoint

- 1080p à 24, 25 ou 30 i/s, 720p jusqu'à 50 i/s, et **ralenti ×2** (50 i/s lus à 25) ; H.264 par l'encodeur matériel, MP4 daté de la prise.
- Exposition automatique avec correction, ou **vitesse à 180°** (le flou de bougé du cinéma) ; **exposition verrouillable** (AE-L) pour une prise.
- Contraste, saturation et **noir et blanc** (celui d'un preset N&B) appliqués par l'ISP, à l'écran comme dans le fichier.
- Repères de cadrage ; **arrêt propre avant que la carte soit pleine** (sinon la vidéo entière serait perdue).

### Lecture

- Les photos (loupe 100 % / 200 %, infos tirées de l'EXIF, histogramme, suppression) et les vidéos (décodeur matériel : lecture, pause).
- MENU sur la prise affichée : **partager** une photo, **extraire une image** d'une vidéo en pause.

### Applications (MENU › Applications)

- **Cloud sync** : la carte envoyée vers ownCloud / Nextcloud, Dropbox ou Google Drive dès que le Wi-Fi trouve Internet, puis après chaque prise. Au choix : tout garder, libérer la carte (seulement ce qui est vérifié dans le cloud, jamais par-dessus un autre fichier), ou à la demande. Jamais tout seul sur un réseau facturé (le partage de connexion d'un téléphone), ni pendant une prise qui dure.
- **Partage** : une photo publiée sur Instagram, Flickr et Bluesky, à la manière de [SPP](https://github.com/Marma92/SPP) : une légende tirée de la photo (appareil, objectif, date, et le preset en guise de pellicule), l'image que chaque réseau attend, et l'aperçu de ce qu'il publiera (où la légende est coupée). Une file d'attente publie dès que l'appareil est en ligne.

### Interface web

Sur le réseau local (port 8080), d'un téléphone ou d'un ordinateur : visée, déclencheur, choix du preset et du mode photo / vidéo ; galerie (photos et vidéos : lecture, téléchargement, suppression) ; installation de presets ; connexion des comptes cloud et des réseaux (l'appareil n'a pas de clavier) ; partage d'une photo avec titre, description, texte alternatif et hashtags. Les mots de passe et jetons restent sur l'appareil, dans des fichiers que seul son utilisateur peut lire.

## Comment c'est fait

- `engine/` : le moteur caméra en C++ (`pycam-engine`), lié à `librpicam_app`. Il fait la visée, les prises, les looks des JPEG, l'écriture DNG, l'empilement, l'enregistrement et la lecture des vidéos, les mesures, et se pilote par un socket Unix en JSON (`docs/protocol.md`).
- `app/` : l'application Python (`asyncio`) : commandes, exposition, menus, HUD, presets, rangement DCIM, réglages persistants, interface web, et les applications embarquées (`app/pycam/apps/` : `cloudsync`, `share`).
- `system/` et `tools/install-services.sh` : les deux services systemd qui les lancent au démarrage.
- `tools/` : déploiement depuis un PC (`deploy.sh`), client du moteur, captures d'écran et appuis de touches à distance, outil de presets, installation d'instagrapi pour le partage sur Instagram (`install-share.sh`).

Le développement se fait depuis un PC, par SSH sur le Pi : voir `docs/development.md`. Les tests (`cd app && python3 -m unittest discover -s tests -t .`) tournent en entier sur le Pi ; Cloud sync et Partage y sont testés contre de faux services, sans compte réel.

## Documentation

- [Architecture](docs/architecture.md) : découpage moteur C++ / application Python, pipeline caméra, affichage, commandes, menus, applications, décisions.
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
- [SPP](https://github.com/Marma92/SPP) (Simple Photo Poster) : la légende, la préparation des images et les règles de chaque réseau de l'application Partage.
