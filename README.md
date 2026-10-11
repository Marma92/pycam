# pycam

Un appareil photo autonome et utilisable au quotidien : Raspberry Pi + High Quality Camera (Sony IMX477).

Il vise à offrir :

- une visée en direct ;
- les modes P / S / A / M ;
- des aides à la mise au point manuelle (peaking, loupe, zébras, histogramme) ;
- du RAW DNG + JPEG ;
- des commandes physiques, une batterie et une interface web.

**Statut** : jalon M0 en cours.

- Le moteur caméra C++ (`engine/`) tourne sur le Pi 3B : visée, déclenchement JPEG + DNG, mesures, pilotage par socket.
- Restent l'affichage HDMI et l'application Python.

## Documentation

- [Architecture](docs/architecture.md) : découpage moteur C++ / application Python, pipeline caméra, affichage, protocole, décisions.
- [Fonctionnalités](docs/features.md) : catalogue par jalons (M0 → M3) et état.
- [Protocole](docs/protocol.md) : commandes et évènements du moteur.
- [Matériel](docs/hardware.md) : configuration actuelle, ajouts envisagés (écran, boutons, batterie, IMU…), brochage, budget énergétique.
- [Développement](docs/development.md) : boucle de travail, déploiement et tests sur le Pi par SSH.

## Inspirations

- [rpicam-apps](https://github.com/raspberrypi/rpicam-apps) : la référence officielle de Raspberry Pi. On réutilise sa bibliothèque cœur `librpicam_app`.
- [CinePI](https://github.com/schoolpost/CinePI) / [cinepi-raw](https://github.com/cinepi/cinepi-raw) (schoolpost) : le découpage moteur C++ + interface Python, et l'écriture CinemaDNG.
- [PiDNG](https://github.com/schoolpost/PiDNG) : le format DNG pour les capteurs Raspberry Pi.
