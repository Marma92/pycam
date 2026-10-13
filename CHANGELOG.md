# Journal des versions

Les versions suivent [SemVer](https://semver.org/lang/fr/) : `MAJEURE.MINEURE.CORRECTIF`. Une section par version ; la CI en fait les notes de la release GitHub (`.github/workflows/build.yml`).

## [0.1.0] — à venir

Première version publiée : l'appareil photo complet sur Raspberry Pi 3B + HQ Camera, piloté au clavier en attendant les boutons.

### Photo

- Visée HDMI avec HUD (histogramme RVB, grille, repères de cadrage, vues restantes), modes P / S / A / M au tiers d'IL, deux molettes, mesure centrale / spot / matricielle, balance des blancs.
- Aides à la mise au point manuelle : peaking, zébras, loupe ×5 / ×10 ; profils d'objectifs.
- JPEG et DNG (compressé sans perte), écrits de façon durable ; déclenchement en bascule ou ZSL.
- Rafale, retardateur, intervallomètre, bracketing, timelapse (vidéo assemblée sur l'appareil), empilement, poses longues et pose B.
- Simulations de pellicules (presets ouverts, LUT, grain, halation), aperçues en visée.

### Vidéo

- 1080p 24 / 25 / 30 i/s, 720p jusqu'à 50 i/s, ralenti ×2 ; H.264 matériel.
- Vitesse à 180°, exposition verrouillable, contraste / saturation / noir et blanc par l'ISP, arrêt propre avant la carte pleine.

### Lecture et interface web

- Lecture des photos et des vidéos, extraction d'une image.
- Interface web sur le réseau local : visée, déclencheur, galerie, presets, comptes, partage.

### Applications

- Cloud sync : ownCloud / Nextcloud, Dropbox, Google Drive (synchroniser, déverser, à la demande).
- Partage : Instagram, Flickr, Bluesky, à la manière de SPP.

### Système

- Installation en une commande sur Raspberry Pi OS 64 bits (`tools/install.sh`), dans `/opt/pycam`.
- Mises à jour : l'appareil cherche une nouvelle version dès qu'il trouve Internet, la télécharge, l'installe au démarrage suivant, et revient tout seul à la précédente si elle ne démarre pas.
- Releases construites et testées par GitHub Actions, moteur compilé pour le Pi.
