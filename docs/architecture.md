# Architecture

> Statut : **v0**. D1 (découpage moteur C++ / application Python) a été validé le 2026-10-11, et D7 (écran HDMI sans tactile) choisi le même jour. Les autres décisions (§11) restent à confirmer à l'usage.

## 0. Contraintes de départ

| Élément | Valeur | Conséquence |
|---|---|---|
| SoC | Raspberry Pi 3B : 4 × Cortex-A53 @ 1,2 GHz, VideoCore IV | Le CPU est faible : tout traitement par pixel à la cadence de la visée doit être natif (C++), petit (basse résolution) ou délégué au GPU/ISP. |
| RAM | 1 Go (partagée avec le GPU / CMA) | Une image pleine résolution pèse ~18,5 Mo en RAW 12 bits et ~18,5 Mo en YUV420. La file d'enregistrement doit être bornée à 3 ou 4 vues. |
| ISP | ISP VC4, piloté par libcamera (pipeline handler `rpi/vc4`) | Débayérisation, réduction de bruit, mise à l'échelle et statistiques AE/AWB sont faites gratuitement par l'ISP. |
| Capteur | Sony IMX477 (HQ Camera), 12,3 MP, RAW 12 bits, monture C/CS | Objectifs **manuels** : l'ouverture et la mise au point sont des bagues. Pas d'AF, et l'appareil ne connaît pas l'ouverture. |
| Modes capteur (12 bits, mesurés) | 4056×3040 à 11,7 i/s · 2028×1520 (binning 2×2) à 45 i/s · 2028×1080 à 63 i/s · 1332×990 à 102 i/s. En 10 bits, un peu plus rapide (14 / 54 / 75 / 120 i/s). | La visée se fait en binning, la photo en pleine résolution. Il faut donc changer de mode, ce qui ajoute une latence (mesurée en §2.2). |
| Stockage | Carte SD, ~20 Mo/s au mieux sur Pi 3B | Un RAW+JPEG fait ~25–30 Mo : on peut compter 1 à 1,5 s d'écriture par vue. L'écriture doit être asynchrone. |
| Horloge | Pas de RTC sur le Pi 3B | Sans réseau, la date des photos sera fausse. Une RTC est nécessaire (voir `hardware.md`). |
| Vidéo | Encodeur H.264 matériel (jusqu'à 1080p30) | La vidéo est possible mais secondaire. Le RAW vidéo à la CinePI n'est pas réaliste sur un Pi 3B (débit SD trop faible). |

## 1. Principe : deux processus, deux langages

```
┌──────────────────────────────── Raspberry Pi 3B ─────────────────────────────────┐
│                                                                                  │
│   pycam-engine  (C++17)                         pycam  (Python 3, asyncio)       │
│   « le chemin chaud »                           « le cerveau »                   │
│   ───────────────────────                       ───────────────────────          │
│   • libcamera via librpicam_app                 • machine d'états (visée,        │
│   • visée → écran (DRM/KMS, plans HW)             menu, lecture, capture…)       │
│   • analyse : histogramme, peaking, zébras      • modes d'exposition P/S/ISO/M   │
│   • capture : RAW (DNG) + JPEG (+EXIF)          • entrées : boutons, molettes    │
│   • file d'écriture asynchrone                  • rendu du HUD/menus → shm       │
│   • (plus tard) vidéo H.264                     • réglages persistants           │
│                                                 • capteurs : IMU, RTC, batterie  │
│            ▲      socket Unix, JSON lignes        • serveur web (télécommande,   │
│            └───────────── commandes / évènements ─┘   galerie, visée distante)   │
│            ▲                                                                     │
│            └──── mémoire partagée /dev/shm/pycam-hud (ARGB, calque HUD) ─────    │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Pourquoi ce découpage ?** Ce n'est pas l'accès aux fonctions qui manquait à Python : Picamera2 expose à peu près tout ce que libcamera sait faire. Ce qui manque sur un Pi 3B, c'est **la performance sur le chemin des images** : aides à la mise au point à chaque trame, latence de visée, encodage RAW/JPEG, copies de tampons de 18 Mo en numpy. À l'inverse, tout le reste (menus, logique des modes, matériel, web) change souvent, est peu coûteux et s'écrit bien plus vite en Python.

Le moteur C++ est donc volontairement **petit et « bête »**. Il exécute des ordres (« configure ceci », « déclenche », « affiche le peaking ») et publie des mesures. Toute la logique de l'appareil vit dans l'application Python.

C'est le même découpage que CinePI, qui marche bien : `cinepi-raw` en C++, un fork de rpicam-apps, est piloté par une interface Python à travers Redis.

## 2. Le moteur `pycam-engine` (C++)

### 2.1 Bâti sur `librpicam_app`, sans fork

rpicam-apps installe sa bibliothèque cœur (`librpicam_app.so`, pkg-config `rpicam_app`) avec ses en-têtes (`/usr/include/rpicam-apps/…`). On la **lie** au lieu de la forker comme l'a fait CinePI :

- **Ce qu'on réutilise :**
  - `RPiCamApp` : ouverture de la caméra, choix des modes capteur, allocation des tampons, boucle de requêtes, `ConfigureViewfinder/Still/Zsl/Video`, `SetControls`.
  - Les écrivains d'images `image/` : JPEG avec EXIF via libexif, DNG via libtiff.
  - Les encodeurs vidéo.
  - Les options (fichier de tuning, débruitage, etc.).
- **Ce qu'on écrit :**
  - Le module d'affichage (§4).
  - L'analyse d'image (§2.3).
  - L'ordonnanceur de capture et des modes de déclenchement.
  - La file d'écriture.
  - Le serveur IPC (§6).
- **Pourquoi pas un fork ?** On suit les mises à jour de Raspberry Pi par `apt` : correctifs ISP, tuning IMX477, évolutions de libcamera. Un fork se désynchronise. CinePI est aujourd'hui figé sur une vieille base.
- **Risque :** l'API de `librpicam_app` n'est pas garantie stable. On fige la version testée dans `development.md`. Si une mise à jour casse la compilation, on embarque les fichiers nécessaires dans le dépôt (licence BSD-2, compatible).

### 2.2 Pipeline caméra

| Situation | Mode capteur | Flux libcamera | Usage |
|---|---|---|---|
| Visée | 2028×1520, binning, ~30 i/s | `main` YUV420 à la taille de l'écran<br>`lores` YUV420 ~320×240 | `main` va à l'écran par le plan vidéo DRM (aucun travail CPU).<br>`lores` sert à l'analyse. |
| Loupe ×5 / ×10 | 4056×3040, 10 i/s, avec `ScalerCrop` | `main` + `lores` | Mise au point fine sur de vrais pixels, pas sur une image binnée. |
| Déclenchement | 4056×3040 | `raw` + `main` YUV pleine résolution | DNG à partir du `raw`, JPEG à partir du `main`. |
| ZSL (option) | 4056×3040 en continu | `raw` + `main` + `lores` | Déclenchement instantané, mais visée à 10 i/s et plus de mémoire CMA. C'est un réglage « réactivité ou fluidité ». |

- **Latence au déclenchement.** Changer de mode (viewfinder → still) coûte un arrêt/reconfiguration de la caméra. Mesures sur le Pi 3B le 2026-10-11, avec Picamera2 (même libcamera dessous) :

  | Scénario | Résultat |
  |---|---|
  | Visée binnée 1024×768 + lores, pose 4 ms | **30,7 i/s** (plafond de la configuration à 30 i/s) |
  | Bascule visée → pleine résolution → image, pose 4 ms | **360–390 ms** |
  | Même bascule en scène sombre (pose 66 ms, gain 16) | 650–740 ms : les trames de démarrage durent plus longtemps |
  | ZSL en Python (pleine résolution + lores en continu) | visée **4 i/s**, image disponible en ~330 ms |
  | Pleine résolution YUV en continu via `rpicam-vid` (C++) | **~10–11 i/s** : en C++, la ZSL donne une visée à ~10 i/s au lieu de 4 |
  | `rpicam-still --immediate`, JPEG, de bout en bout | ~4,3 s, dont l'ouverture de la caméra et l'encodage JPEG logiciel 12 MP ; à décomposer dans le moteur |
  | Idem JPEG + DNG | ~4,6 s (DNG non compressé : 24,8 Mo ; JPEG : 2 Mo) |

  Lecture : **~0,4 s de latence en mode « bascule »**, c'est l'ordre d'un compact des années 2000 ; **~0 s en ZSL**, mais avec une visée à ~10 i/s. On implémente les deux et on laisse le choix dans les réglages (« fluidité » ou « réactivité »). L'encodage JPEG logiciel (~1–2 s estimés) doit absolument se faire hors du chemin de visée. Piste d'optimisation : l'encodeur JPEG matériel du VC4 (V4L2 M2M), à évaluer.
- **File d'écriture.** Après la capture, le moteur revient *tout de suite* en visée. L'encodage JPEG, l'écriture DNG et l'EXIF tournent dans un thread dédié. La file est bornée par la mémoire, à environ 3 vues. Quand elle est pleine, le déclencheur est refusé et l'interface affiche « occupé ».
- **Expositions longues.** L'IMX477 accepte des poses de plusieurs minutes (la limite exacte est à mesurer). La visée ne peut pas suivre : on affiche un compte à rebours.

### 2.3 Analyse d'image (par trame, sur `lores`)

Tout se calcule sur le flux `lores` (~77 000 pixels), donc pour quelques ms de CPU par trame :

- **Histogramme** : luminance et RVB, à ~5 Hz.
- **Peaking** : gradient de Sobel sur Y, avec seuil et couleur réglables. On le dessine dans un calque ARGB à la résolution `lores` ; le scaler matériel du VC4 l'agrandit à la taille de l'écran.
- **Zébras** : pixels au-dessus d'un seuil réglable.
- **Posemètre** pour le mode M : luminance moyenne pondérée selon le mode de mesure, convertie en écart d'IL par rapport à la cible.

Plus tard, si le CPU ne suit pas, le peaking pourra passer en shader GLES2 sur le GPU VC4.

## 3. Modes d'exposition (dans l'application)

L'ouverture est réglée sur l'objectif, donc les modes « classiques » se traduisent ainsi en contrôles libcamera :

| Mode | Vitesse (`ExposureTime`) | ISO (`AnalogueGain`) | Remarque |
|---|---|---|---|
| **P** (auto) | auto | auto | Correction d'exposition (`ExposureValue`) et mode de mesure (`AeMeteringMode`). |
| **S** (priorité vitesse) | fixe | auto | — |
| **A** (priorité ISO) | auto | fixe | Correspond à « l'aperture priority » des specs d'origine : l'ouverture est sur la bague et l'appareil choisit la vitesse. |
| **M** (manuel) | fixe | fixe | Le posemètre indique l'écart en IL. |

À ces modes s'ajoutent :

- **Balance des blancs** : auto, préréglages (`AwbMode`), ou manuelle en Kelvin / gains (`ColourGains`).
- **Rendu** : débruitage, netteté, contraste, saturation.
- **Profils d'image** : plus tard, via des fichiers de tuning alternatifs.

## 4. Affichage

Le moteur est **maître DRM/KMS**. Il n'y a pas de bureau graphique : on utilise Pi OS Lite, ou on démarre en console. Le compositeur matériel du VC4 (HVS) superpose plusieurs plans avec transparence et mise à l'échelle, sans travail CPU :

| z | Plan | Format | Contenu | Producteur |
|---|---|---|---|---|
| 0 | vidéo | YUV420 | visée en direct | ISP → moteur (zéro copie, dmabuf) |
| 1 | analyse | ARGB8888 à la taille `lores` | peaking, zébras | moteur, à chaque trame |
| 2 | HUD | ARGB8888 à la taille de l'écran | réglages, histogramme, grille, niveau, menus | application Python → `/dev/shm/pycam-hud` |

- **Rendu du HUD.** L'application dessine avec Pillow, **seulement quand quelque chose change**. Elle publie un numéro de version, et le moteur copie le tampon dans le plan HUD au prochain vblank. Une navigation dans un menu coûte un rendu, pas trente par seconde.
- **Pourquoi l'histogramme est dans le HUD Python.** Il change à 5 Hz, ce que Pillow tient sur un petit écran. S'il le faut, il pourra passer dans le plan 1, côté moteur.
- **Écran retenu : HDMI, sans tactile** (`hardware.md` §1). Le HDMI passe par le même compositeur HVS, et le pilote expose aussi un connecteur *writeback* (`card0-Writeback-1`) : il permettra de faire des **captures d'écran du rendu composé** à distance pour les tests.
- **Le bureau doit s'effacer.** Aujourd'hui, `lightdm` + `labwc` occupent la sortie HDMI et l'essentiel de la CMA : 43 Mo libres sur 256 avec le bureau. Pour que le moteur soit maître DRM, le Pi doit démarrer en console (`multi-user.target`). Le bureau reste lançable à la demande pour le développement.
- **Vérification nécessaire.** Il faut confirmer que `vc4-kms-v3d` expose au moins 2 plans overlay sur le CRTC HDMI. On le vérifiera au premier prototype d'affichage, une fois le bureau arrêté.

## 5. L'application `pycam` (Python)

Un seul processus `asyncio`, découpé en modules :

| Module | Rôle |
|---|---|
| `engine_client` | Client du socket du moteur : commandes et flux d'évènements, reconnexion. |
| `state` | Machine d'états : `VIEW` → `MENU` / `PLAYBACK` / `CAPTURING` / `SLEEP`. |
| `exposure` | Modes P/S/A/M, pas d'IL (1/3), tables de vitesses et d'ISO. |
| `drive` | Vue par vue, rafale, retardateur, intervallomètre, bracketing, pose B. |
| `input` | Lecture **evdev** : boutons et molettes passent par les pilotes noyau `gpio-keys` et `rotary-encoder`. Pas de polling GPIO en Python. |
| `hud` | Rendu Pillow des calques : réglages, grille des tiers, niveau, histogramme, menus. |
| `settings` | Réglages persistants dans `~/.config/pycam/settings.toml`, avec des profils d'objectifs. |
| `storage` | DCIM, numérotation, espace libre, suppression. |
| `sensors` | IMU (niveau, orientation), batterie, RTC. |
| `web` | `aiohttp` : télécommande, galerie, téléchargement, visée distante en MJPEG basse résolution. |

**Pourquoi evdev.** Avec les overlays noyau, chaque bouton devient un évènement clavier (`KEY_*`) et chaque molette un évènement relatif (`REL_*`). Ça apporte plusieurs choses :

- l'anti-rebond est fait par le noyau ;
- aucun CPU n'est consommé à l'attente ;
- on peut **brancher un clavier USB** pour développer ;
- on peut simuler des appuis par `uinput` dans les tests automatiques.

## 6. Protocole moteur ⇄ application

Le moteur écoute sur le socket Unix `/run/pycam/engine.sock`. Chaque message est un objet JSON sur une ligne. On peut le tester à la main avec `socat - UNIX-CONNECT:/run/pycam/engine.sock`.

```jsonc
// app → moteur (commandes, avec id pour corréler la réponse)
{"id": 1, "cmd": "configure", "viewfinder": {"mode": "2028x1520", "fps": 30}, "zsl": false}
{"id": 2, "cmd": "set_controls", "controls": {"ExposureTime": 8000, "AnalogueGain": 2.0, "AeEnable": false}}
{"id": 3, "cmd": "capture", "formats": ["dng", "jpeg"], "path": "/home/ael/DCIM/100PYCAM/PYC_0042", "exif": {"LensModel": "Arducam 16mm"}}
{"id": 4, "cmd": "overlay", "peaking": {"on": true, "threshold": 0.3, "color": "#ff0000"}, "zebra": {"on": false}}
{"id": 5, "cmd": "zoom", "factor": 5.0, "center": [0.5, 0.5]}

// moteur → app (réponses et évènements)
{"id": 3, "ok": true}
{"event": "stats", "fps": 29.8, "exposure_us": 8000, "gain": 2.0, "lux": 312, "colour_temp": 5200, "ev_offset": -0.7, "histogram": {"y": [/* 64 classes */]}}
{"event": "capture_done", "files": ["…/PYC_0042.DNG", "…/PYC_0042.JPG"], "ms": 1830}
{"event": "busy", "queue": 3}
{"event": "error", "message": "…"}
```

**Pourquoi un socket Unix en JSON plutôt que Redis (le choix de CinePI) ?**

- On n'a pas de démon supplémentaire sur 1 Go de RAM.
- Il n'y a aucune dépendance côté Python.
- C'est inspectable à la main.
- Le moteur garde un état volatil ; c'est l'application qui est la source de vérité des réglages, et elle renvoie la configuration complète à chaque (re)connexion.

## 7. Stockage et formats

- **Arborescence** : `~/DCIM/100PYCAM/PYC_0001.JPG` et `.DNG`. Le numéro est persistant et le dossier change tous les 9999 fichiers. Si on ajoute une clé USB plus tard, elle se monte au même endroit.
- **JPEG.** Qualité réglable. EXIF complet : vitesse, ISO, date venant de la RTC, orientation venant de l'IMU, et profil d'objectif saisi par l'utilisateur (focale, ouverture max, nom). L'ouverture réelle n'est pas connue de l'appareil ; elle peut être saisie rapidement si on veut.
- **DNG.** RAW 12 bits avec la matrice couleur IMX477, lisible par Lightroom, darktable et RawTherapee. On utilise d'abord l'écrivain de rpicam-apps. Plus tard, une compression LJ92 sans perte comme dans CinePI (`lj92.h`) pour diviser la taille par ~2.

## 8. Système

- **OS.** Raspberry Pi OS **Lite** 64 bits, sans bureau : le moteur doit pouvoir être maître DRM.
- **Services.** Deux services systemd, `pycam-engine.service` et `pycam.service`, ce dernier dépendant du premier, avec redémarrage automatique.
- **Démarrage rapide** (objectif : moins de 12 s jusqu'à la visée) :
  - désactiver les services inutiles ;
  - `boot_delay=0` et `disable_splash=1` ;
  - ne pas attendre le réseau ;
  - lancer le moteur tôt.
- **Robustesse sur batterie.** Racine en lecture seule (`overlayroot`) plus une partition de données, pour qu'une coupure brutale ne corrompe pas le système. À activer une fois le projet stabilisé.
- **Heure.** RTC I2C (overlay `i2c-rtc`), synchronisée par NTP quand le Wi-Fi est disponible.

## 9. Testabilité

- **Moteur.**
  - Option `--no-display` pour tourner sans écran, en SSH.
  - Option `--stats-log` pour mesurer fps, latences et mémoire.
  - Scripts de bench (latence de déclenchement, temps d'écriture) lancés à distance.
- **Application.**
  - Un `FakeEngine` (faux socket) permet les tests `pytest` sur n'importe quel PC.
  - Les entrées sont simulées par `uinput` sur le Pi.
- **Vérification visuelle.**
  - Je capture une image ou une copie d'écran du plan DRM sur le Pi et la rapatrie pour la regarder.
  - La visée distante web sert aussi d'outil de debug.

## 10. Points ouverts / risques

1. ✅ **Latence de changement de mode** : ~0,4 s en bascule, ~0 en ZSL (§2.2). On propose les deux modes.
2. 🟨 **Mémoire CMA** : 256 Mo au total. Avec le bureau actif, il n'en reste que 28 Mo en ZSL, ce qui est juste. Il faut remesurer sans bureau, puis ajuster éventuellement `cma=` dans `config.txt`.
3. ⬜ **Plans DRM overlay** sur HDMI : à vérifier au premier prototype d'affichage.
4. ✅ **`librpicam_app`** : `librpicam-app-dev` 1.13.0 est disponible dans les dépôts (pas encore installé). Il reste à valider une compilation.
5. ✅ **Écran** : HDMI sans tactile.
6. ⬜ **Droits** : `sudo` demande un mot de passe pour `ael`. Il en faut pour installer les paquets de build, arrêter le bureau et éditer `config.txt`.

## 11. Décisions (résumé)

| # | Décision | Alternatives écartées | Pourquoi |
|---|---|---|---|
| D1 | Moteur C++ + application Python, deux processus | Tout en Python (Picamera2) ; tout en C++ | Perfs sur le chemin des images et vitesse de dev sur le reste ; un crash de l'UI ne tue pas la caméra. |
| D2 | Lier `librpicam_app` | Forker rpicam-apps (CinePI) ; libcamera seul | On suit les mises à jour sans réécrire la gestion caméra et les écrivains DNG/JPEG. |
| D3 | Socket Unix + JSON lignes, avec shm pour le HUD | Redis ; ZeroMQ ; D-Bus | Zéro démon, zéro dépendance, débogable avec `socat`. |
| D4 | Composition par plans DRM du VC4 | Qt/EGL ; composition CPU ; X11/Wayland | Zéro copie et zéro CPU pour la visée ; pas de bureau à démarrer. |
| D5 | Entrées par overlays noyau + evdev | gpiozero / polling | Anti-rebond noyau, clavier USB utilisable en dev, tests par uinput. |
| D6 | Compilation native sur le Pi | Cross-compilation depuis Windows | Simplicité. Le moteur est petit, donc la compilation sur le Pi reste supportable. |
| D7 | Écran HDMI sans tactile ; navigation 100 % boutons et molettes | DSI tactile ; SPI | Choix de l'utilisateur (2026-10-11). Le GPIO reste libre et on garde la composition matérielle. |
| D8 | Deux stratégies de capture, « bascule » (visée 30 i/s, ~0,4 s de latence) et « ZSL » (visée ~10 i/s, latence ~0), au choix | Une seule stratégie imposée | D'après les mesures du 2026-10-11 : aucune n'est meilleure dans tous les cas. |
