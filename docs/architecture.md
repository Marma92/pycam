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
- ✅ **Validé le 2026-10-11** : le moteur compile et se lie contre `librpicam-app-dev` 1.13.0, sans avertissement. Point d'attention : les en-têtes du paquet Debian ne sont pas identiques au dépôt git du même numéro (les écrivains d'images y prennent des `libcamera::Span`). **La référence, ce sont les en-têtes installés sur le Pi**, pas GitHub.

### 2.2 Pipeline caméra

| Situation | Mode capteur | Flux libcamera | Usage |
|---|---|---|---|
| Visée | 2028×1520, binning, ~30 i/s | `main` YUV420 à la taille de l'écran<br>`lores` YUV420 ~320×240 | `main` va à l'écran par le plan vidéo DRM (aucun travail CPU).<br>`lores` sert à l'analyse. |
| Loupe ×5 / ×10 ✅ | 4056×3040 en **10 bits** (le mode pleine résolution le plus rapide : **14 i/s** mesurés), recadré par `ScalerCrop` | `main` + `lores` | Mise au point fine sur de vrais pixels : à ×5, la zone de 811×608 pixels du capteur s'affiche presque 1:1 en 1024×768. C'est une visée ordinaire même en ZSL, car le recadrage s'appliquerait aussi au flux photo. Le déclencheur fait sortir de la loupe : la photo est toujours en plein cadre (0,63 s de délai en bascule, 0,41 s en ZSL). Déplacer la zone ne demande pas de reconfiguration. |
| Déclenchement | 4056×3040 | `raw` + `main` YUV pleine résolution | DNG à partir du `raw`, JPEG à partir du `main`. |
| ZSL (option) | 4056×3040 en continu | `raw` + `still` YUV + `viewfinder` | Déclenchement quasi instantané, mais visée à ~12 i/s et ~111 Mo de CMA (3 jeux de tampons). C'est un réglage « réactivité ou fluidité ». |

- **Latence au déclenchement.** Mesures avec `pycam-engine` sur le Pi 3B le 2026-10-11 : pose fixe de 4 ms, bureau graphique actif, scène noire.

  | Scénario | Résultat |
  |---|---|
  | Visée « bascule » (2028×1520 binné → 1024×768 + lores 320×240) | **30 i/s** |
  | Bascule, photo débruitée `cdn_hq` | latence **~505 ms** = 70 ms de reconfiguration + ~435 ms jusqu'à la 1re image |
  | Bascule, photo débruitée `cdn_fast` ou `cdn_off` | latence **~277 ms** = 70 + ~207 ms |
  | ZSL, `cdn_fast` / `cdn_off` | visée **11,7 i/s** (le maximum du capteur), latence 20–250 ms |
  | ZSL, `cdn_hq` | visée **3,9 i/s** seulement : à éviter |
  | Copie de l'image hors des tampons caméra (YUV + RAW, 37 Mo) | 65–165 ms |
  | Écriture JPEG 12 MP / DNG 24,8 Mo | ~270 ms / ~170–280 ms, sur une image noire. **À remesurer sur une vraie scène.** |

  **Lecture.**
  - Le débruitage couleur haute qualité (`cdn_hq`) coûte ~225 ms de traitement ISP par image en pleine résolution. C'est lui qui domine la latence, pas le changement de mode.
  - On propose donc trois réglages : **bascule + cdn_hq** (~0,5 s, meilleure qualité en ISO élevé) ; **bascule + cdn_fast** (~0,28 s) ; **ZSL** (~0–0,25 s, visée à 12 i/s).
  - La config pipeline `disable_startup_frame_drops` a été essayée sans effet : aucune image n'est jetée au redémarrage.
  - Pour comparaison, Picamera2 (Python) plafonne la ZSL à 4 i/s, contre 11,7 i/s pour le moteur C++.
- **File d'écriture.** Après la capture, le moteur revient *tout de suite* en visée. L'encodage JPEG, l'écriture DNG et l'EXIF tournent dans un thread dédié. La file est bornée par la mémoire, à environ 3 vues. Quand elle est pleine, le déclencheur est refusé et l'interface affiche « occupé ».
- ⚠️ **Interblocage dans rpicam-apps** (trouvé le 2026-10-11). Depuis libcamera 0.5, les premières images après un démarrage arrivent marquées « démarrage », et rpicam-apps les jette dans le thread de libcamera. Pour cela, il prend le verrou que `StopCamera()` garde pendant qu'il attend… ce même thread. Arrêter la caméra pendant ces images (un déclenchement juste après une reconfiguration) gelait le moteur.
  - Parade, puisqu'on lie la bibliothèque sans la modifier : le moteur **n'arrête jamais la caméra avant d'avoir reçu une vraie image** depuis son démarrage (ou après 3 s sans image).
  - Un déclenchement pendant le démarrage attend donc ce délai (~0,8 s mesuré) au lieu de geler.
  - Ces images de démarrage expliquent aussi les ~430 ms avant la première image en mode bascule, et pourquoi `disable_startup_frame_drops` n'y changeait rien : ce n'est plus le pipeline qui les jette.
- **Optimisations possibles, plus tard** :
  - réutiliser des tampons préalloués pour la copie, ce qui évite les fautes de page (65–165 ms aujourd'hui) ;
  - l'encodeur JPEG matériel du VC4 (V4L2 M2M) ;
  - la compression LJ92 du DNG.
- **Expositions longues** : voir §2.4.

### 2.3 Analyse d'image et aides

- ✅ **Histogrammes** (`analysis.cpp`), luminance et RVB, à ~5 Hz.
  - Calculés sur le flux `lores` (320×240), un pixel sur 2 dans chaque sens : 19 200 échantillons. En ZSL, qui n'a pas de `lores`, sur le flux de visée, avec un pas de largeur/160.
  - Le RVB vient du YUV : chaque échantillon prend la chrominance de son bloc 2×2, puis passe par la conversion BT.601 pleine plage (sYCC, l'espace de couleur des flux de visée), en virgule fixe.
  - Ils sont calculés avant de passer l'image à l'affichage : en ZSL, le fil d'affichage peint le peaking et les zébras dans ce même tampon.
  - Le HUD dessine le RVB en additif : blanc là où les trois couches se recouvrent, jaune, magenta ou cyan là où deux se recouvrent, la couleur seule au-dessus. L'échelle ignore les classes extrêmes : un noir ou un blanc écrêté monte jusqu'en haut au lieu d'écraser le reste. Le rendu coûte ~7 ms de plus que la luminance seule (126 contre 119 ms par image en 1080p).
  - Le menu rapide choisit RVB (par défaut) ou luminance. La revue après la prise montre le même, calculé sur le JPEG.
- ✅ **Peaking et zébras** (`engine/src/focus_aids.cpp`, 2026-10-11). Ils sont **dessinés dans l'image de visée** (1024×768) juste avant son affichage, sur le fil d'affichage, plutôt que dans un calque séparé comme prévu au départ :
  - le tampon de visée ne sert qu'à l'écran : les photos, qui viennent d'autres flux, restent intactes ;
  - il n'y a pas de plan supplémentaire, alors qu'un 3e plan mis à l'échelle ferait sauter la mémoire de lignes du VC4 (§4) ;
  - il ne peut y avoir aucun décalage entre l'image et ses contours.
- **Algorithme du peaking.**
  - Il travaille par blocs de 2×2 pixels, qui est la résolution de la chrominance en YUV420 : la moyenne de 4 pixels atténue le bruit, et un bloc se colore d'un coup en Y, U et V.
  - Gradient par différences centrales entre blocs voisins.
  - Seuil **adaptatif** : on garde la part la plus nette des blocs (1,5 / 3 / 6 % selon la sensibilité), sans descendre sous un plancher. Une image floue reste donc propre.
  - Coût mesuré : **~6 ms par image** sur le Pi 3B.
- **Zébras** : rayures diagonales qui défilent, sur les blocs au-dessus du seuil (95 % par défaut).
- La commande `snapshot` enregistre l'image de visée telle qu'affichée, aides comprises : c'est ce qui permet de **vérifier l'affichage à distance**.
- ✅ **Posemètre** (`app/pycam/meter.py`).
  - L'objectif est manuel : impossible de raisonner en ouvertures. Le posemètre raisonne sur la lumière qui arrive au capteur, via l'estimation `Lux` de l'ISP. Elle est calculée sur les statistiques brutes linéaires et ne dépend presque pas des réglages : ±10 % pour ±1 IL, mesuré.
  - L'exposition automatique, une fois stabilisée, donne la constante `lux × pose × gain ≈ K` (K ≈ 13,8·10⁶ mesuré). Elle est apprise en continu en mode P, correction d'exposition déduite, et conservée dans les réglages.
  - En manuel, l'écart affiché est `log2(pose × gain × lux / K)`.
  - Mesuré : pour des réglages à −1,1 / +0,9 IL, le posemètre lit −0,9 / +0,8 (les hautes lumières qui saturent le décalent un peu), soit ~0,15 IL d'écart. C'est suffisant pour un affichage au tiers.
  - Il ne tient pas encore compte du mode de mesure (spot, centrale), puisque `Lux` est une moyenne de toute l'image.

Plus tard, si le CPU ne suit pas, le peaking pourra passer en NEON, ou en shader GLES2 sur le GPU VC4.

### 2.4 Poses longues et pose B

- **Limites mesurées** (2026-10-12) : `ExposureTime` va de 56 µs à **674 s** (11'14"), le gain analogique de 1 à 22,3.
- **Une pose longue est une photo comme une autre**, prise en mode photo (bascule) : aucune image n'est jetée au démarrage, la photo arrive après la pose plus ~0,56 s (2 s → 2,57 s ; 8 s → 8,56 s). En ZSL, dont le flux continu ne peut pas faire de longues images, une pose plus longue que ses images (250 ms) passe aussi par le mode photo (2 s → 2,54 s, mesuré).
- **La visée garde au moins 10 i/s** (4 en ZSL). Une pose manuelle plus longue que l'image de visée (100 ms) est **simulée** : l'image la plus longue, avec un gain augmenté d'autant (pose × gain conservés), tant que le gain du capteur le permet. Au-delà (ISO 100 au-delà de ~2 s, ISO 800 au-delà de ~0,3 s), la visée passe en **exposition automatique** pour cadrer, l'évènement `stats` le signale et le HUD affiche « VISÉE AUTO ». Le posemètre du mode M se calcule alors sur les réglages, pas sur ce que fait le capteur.
- **Pendant la pose**, le moteur annonce son début (`exposing`), l'écran garde la dernière image de visée, et l'application affiche le temps restant. Le déclencheur l'annule (`capture_cancel`) : rien n'est écrit, la visée revient aussitôt, et le numéro de la photo reste pris (comme sur un appareil qui a déjà nommé sa prise). Comme aucune image n'arrive pendant des minutes, le moteur se réveille tout seul (toutes les 250 ms) quand il a quelque chose à faire, par exemple une annulation.
- **La pose B.** Le capteur fixe la durée d'une pose avant de la commencer : impossible de la terminer au relâchement. La pose B est donc **une somme de poses de 1 s**, prises sans interruption en mode photo (`capture` avec `stack`), jusqu'à `capture_stop` ou 30 minutes. Elle finit donc jusqu'à 1 s après le relâchement.
  - **Le DNG est la somme exacte des RAW** (`stack.cpp`, sommes sur 32 bits, ~49 Mo) : le niveau de noir n'est compté qu'une fois, le bruit autour du noir se moyenne au lieu d'être écrêté image par image, et le blanc sature comme sur le capteur. C'est l'équivalent d'une seule pose aussi longue ; seul le bruit de lecture croît, comme la racine du nombre d'images.
  - **Le JPEG est développé par le moteur** à partir de cette somme (`develop.cpp`), puisque l'ISP du Pi ne retraite pas un RAW : pixels chauds (un pixel isolé, 4 fois au-dessus de tous ses voisins, est remplacé), dématriçage Malvar-He-Cutler, gains de balance et gain numérique écrêtés à 1 comme dans l'ISP (sinon les hautes lumières où le vert sature virent au magenta : vu au premier essai), matrice couleur de l'ISP pour cette image (métadonnées), courbe de tons du tuning (`rpi.contrast`), YUV BT.601 pleine plage 4:2:0. Il reste ainsi sur la base pycam, et le preset s'applique ensuite comme à toute photo. Il n'y a ni débruitage, ni renforcement de la netteté, ni correction de l'ombrage de l'objectif.
  - **Mesuré** (2026-10-12, 3 poses de 1 s contre une pose de 3 s, même scène, ISO 100) : même pose totale (3,000 s) ; sur une grille de 36 zones, le JPEG développé et celui de l'ISP diffèrent de 0 à 3 % sur chaque couleur. Le développement prend ~2 s pour 12 Mpx (3 fils), plus le look (~1,7 s) et l'écriture.
  - **Deux tampons** en mode photo pour un empilement : avec un seul, la caméra n'avait où mettre l'image suivante pendant l'addition de la précédente, et perdait une image sur deux (des poses de 1 s espacées de 2 s, donc des trous dans la pose). Avec deux, elles se suivent : 5 poses de 1 s en 5,7 s depuis la commande, démarrage compris.
  - Le même mécanisme fait la **moyenne** (moins de bruit) et le **maximum** (traînées de lumière) des images : le mode Empilement (§5). Pour un empilement, l'ISP ne débruite pas les images YUV, qui ne servent pas. Une somme est limitée à 30 minutes de pose (la durée va dans les métadonnées, en µs sur 32 bits) ; une moyenne ou un maximum, à 3 600 images.

### 2.5 Vidéo

Un appareil fait pour la photo, avec la vidéo en appoint (2026-10-12).

- **La phase « vidéo »** (commande `video_mode`) reconfigure la caméra : mode capteur 2028×1080 (binning 2×2, toute la largeur du capteur, jusqu'à 50 i/s), mis à l'échelle par l'ISP en 1080p ou 720p, à une cadence fixe (25 i/s : la lumière secteur à 50 Hz ne scintille pas au 1/50), avec un petit flux 1024×576 pour l'écran et les mesures. L'écran montre ce flux en 16:9 dans la zone d'image, sans le look : la vidéo garde la base pycam (une LUT 3D à 25 i/s en 1080p dépasse le Pi 3). La loupe et les photos sont refusées dans ce mode.
- **L'enregistrement** (`recorder.cpp`, commandes `record_start` et `record_stop`) : l'encodeur H.264 du VideoCore (`H264Encoder` de rpicam-apps, profil High, niveau 4.1, une image clé par seconde) lit directement les tampons de la caméra (dmabuf, sans copie), et libavformat met le flux dans un MP4 (les SPS/PPS de la première image clé dans l'en-tête), sans son (le Pi n'a pas de micro). Le fichier est écrit en `.part`, puis renommé et forcé sur la carte. Chaque image reste à l'encodeur jusqu'à ce qu'il la rende ; l'enregistrement s'arrête toujours avant un redémarrage de la caméra (le fichier est fini, pas perdu).
- **Mesuré** (Pi 3B, 1080p25 à 12 Mbit/s) : 25 i/s exactes, ~7 % du CPU (4 cœurs) pour la prise et l'encodage. Deux pièges trouvés :
  - les options de l'encodeur se règlent directement : les faire analyser comme une ligne de commande relance le gestionnaire de caméras de rpicam, sous la caméra en marche (le moteur plantait), et l'encodeur prend la taille du flux H.264 dans ces options, pas dans le flux (sans elle, le micrologiciel refusait de démarrer) ;
  - la toute première utilisation de l'encodeur après le démarrage du moteur le bloque ~2 s : la première vidéo perdait ses images de la 5e à la 50e. Le moteur l'utilise donc une demi-seconde, dans un fichier jetable, en entrant pour la première fois en mode vidéo.

## 3. Modes d'exposition (dans l'application)

L'ouverture est réglée sur l'objectif, donc les modes « classiques » se traduisent ainsi en contrôles libcamera :

| Mode | Vitesse (`ExposureTime`) | ISO (`AnalogueGain`) | Remarque |
|---|---|---|---|
| **P** (auto) | auto | auto | Correction d'exposition (`ExposureValue`) et mode de mesure (`AeMeteringMode`). |
| **S** (priorité vitesse) | fixe, 1 s au plus | auto | L'exposition automatique a besoin de quelques images de la pose pour se régler : au-delà de 1 s, ce serait des dizaines de secondes. |
| **A** (priorité ISO) | auto | fixe | Correspond à « l'aperture priority » des specs d'origine : l'ouverture est sur la bague et l'appareil choisit la vitesse. |
| **M** (manuel) | fixe, jusqu'à 10'40", puis B | fixe | Le posemètre indique l'écart en IL (pas en B, qui n'a pas de durée). Au-delà de 1/10 s, la visée simule la pose (§2.4). |

À ces modes s'ajoutent :

- ✅ **Balance des blancs** : automatique, préréglages (`AwbMode`), ou manuelle.
  - Le **Kelvin** passe par `ColourTemperature` : l'ISP en déduit les gains par la courbe de calibration de l'IMX477 (3000 K → gains R/B 2,08 / 2,20 ; 6500 K → 3,24 / 1,39, mesurés).
  - La **mémorisation** fige les gains (`ColourGains`) trouvés par la balance automatique, par exemple sur une charte grise.
  - Les deux demandent `AwbEnable` à faux, et un préréglage le remet à vrai. Le moteur oublie alors les gains ou la température manuels : gardés, ils recoupaient la balance automatique au redémarrage suivant de la caméra (§10, point 12).
- ✅ **Mode de mesure** (`AeMeteringMode`, ligne en tête du menu rapide) : pondérée centrale (par défaut), spot, matricielle.
  - L'ISP découpe l'image en 15 zones de statistiques. Chaque mode est un jeu de poids sur ces zones, défini dans le fichier de tuning (`/usr/share/libcamera/ipa/rpi/vc4/imx477.json`) : centrale `[3,3,3,2,2,2,2,1,1,1,1,0,0,0,0]`, spot `[2,1,1,0…]`, matricielle tous à 1.
  - Mesuré sur une même scène : spot −1,6 IL, matricielle +0,4 IL par rapport à la centrale.
  - Le HUD montre une icône dans le bandeau (anneau, point, grille) et, en spot, un cercle au centre de la visée. Sa taille est approximative : la zone exacte dépend du découpage de l'ISP.
  - Le mode n'agit que sur l'exposition automatique (P, S, A). En M, le posemètre mesure toujours toute l'image (voir §2.3).
- **Rendu** : débruitage, netteté, contraste, saturation.
- **Presets et simulations de pellicules** : base fixe (tuning tiré du tuning scientific), puis une LUT 3D, du grain et du vignettage appliqués avant l'encodage JPEG. Orientations décidées, pas encore implémentées : voir `presets.md`.

## 4. Affichage

Le moteur est **maître DRM/KMS**. Il n'y a pas de bureau graphique : on utilise Pi OS Lite, ou on démarre en console. Le compositeur matériel du VC4 (HVS) superpose plusieurs plans avec transparence et mise à l'échelle, sans travail CPU :

| Plan (du bas vers le haut) | Format | Contenu | Producteur | État |
|---|---|---|---|---|
| vidéo | YUV420, mis à l'échelle par le HVS | visée en direct, ou une vidéo en lecture | tampons caméra, ou du décodeur H.264 → moteur, zéro copie (dmabuf) | ✅ |
| HUD | RGBA (`ABGR8888`), plein écran, sans mise à l'échelle | marges noires, réglages, histogramme, grille, niveau, menus | application Python → `/dev/shm/pycam-hud` → `hud_commit` | ✅ |
| ~~analyse~~ | — | peaking, zébras | dessinés dans l'image de visée (§2.3) | abandonné |

- ✅ **Implémenté le 2026-10-11** (`engine/src/display.cpp`). Ce qu'on a appris sur le VC4 :
  - le pilote expose 16 plans overlay pour le CRTC HDMI, au-dessus du plan primaire de la console ;
  - leur ordre est **fixe** (`zpos` immuable, croissant avec l'identifiant du plan), donc le moteur prend les deux plus bas, libres ;
  - tous acceptent YUV420 et `ABGR8888` : les octets R, G, B, A sont exactement le `"RGBA"` de Pillow, sans conversion ;
  - le mode de mélange `Coverage` donne l'alpha non prémultiplié de Pillow.
- ⚠️ **Mémoire de lignes du HVS.** Un troisième plan *mis à l'échelle* (un fond noir agrandi en 1080p) a fait échouer le HUD avec `ENOSPC`. D'où :
  - les marges noires sont dessinées **dans le HUD**, qui n'est pas mis à l'échelle ;
  - le plan d'analyse prévu pour le peaking aurait eu le même problème : le peaking est donc dessiné dans l'image de visée (§2.3).
- **Mesures, en 1080p :**
  - visée 22–30 i/s avec 0 à 1 image perdue sur 768 ; ZSL à 11,7 i/s, pas plus lente ;
  - moteur à ~10 % de CPU visée comprise, ~18 % avec un HUD rafraîchi à 5 Hz (copie de 8 Mo à chaque `hud_commit`) ;
  - pendant une bascule, la dernière image reste à l'écran.
- **Rendu du HUD.**
  - L'application dessine avec Pillow puis envoie `hud_commit` ; le moteur copie l'image dans un tampon libre (double tampon) et la pose au vblank suivant.
  - L'application doit dessiner **seulement quand quelque chose change** : une navigation dans un menu coûte un rendu, pas trente par seconde.
  - Coût mesuré (`tools/hud_demo.py`, grille + bandeau + histogramme) : 90–120 ms par rendu en 1080p. Sur un écran 800×480, 5 fois moins de pixels.
- ✅ **Lecture des vidéos** (2026-10-12, `video_player.cpp`). libavformat lit le MP4, le décodeur H.264 du VideoCore (`h264_v4l2m2m`, via libavcodec) rend des tampons **DRM PRIME** (YUV420 linéaire, un seul dmabuf), que l'affichage pose tels quels sur le plan vidéo à la place des images de la caméra, qui continue de tourner. Zéro copie : 25 i/s, le moteur reste à ~24 % d'un cœur (28 % en visée). En passant par une copie (ffmpeg vers un tube), on plafonnait à 20 i/s. Trois leçons :
  - **Tout ce que fait libav, sur un seul fil.** Rendre les images au décodeur depuis le fil d'affichage, pendant que le fil du lecteur décodait, l'a bloqué (« V4L2 capture poll unexpected timeout ») ; l'arrêt attendait alors ce fil, et tout le moteur se figeait. L'affichage dépose désormais les images finies dans une file que le fil du lecteur vide, et l'arrêt n'attend pas plus de 3 s.
  - **La mémoire de lignes, encore.** Un plan YUV en réserve autant que sa largeur (de source, ou de destination s'il est réduit), et chaque `SetPlane` réserve la nouvelle avant de rendre l'ancienne. En 1920 px de large, les deux ne tenaient pas : toutes les images après la première échouaient (`ENOSPC`). La vidéo est donc montrée dans la zone d'image (1440 px de large en 1080p), comme les photos.
  - 6 lectures d'affilée, avec pauses, arrêts et relectures : aucune erreur, aucune image perdue.
- ✅ **Écran en veille** (2026-10-12). La commande `display_power` éteint l'écran (DPMS) : rien n'est plus affiché, ni la visée (ni son look, ni les aides), ni le HUD. Attention au piège trouvé en le testant : écran éteint, un HUD reçu restait « à afficher », la boucle d'affichage tournait à vide en gardant son verrou, et le fil de la caméra s'y bloquait. Elle n'attend plus rien d'affichable tant que l'écran est éteint.
- **Pourquoi l'histogramme est dans le HUD Python.** Il change à 5 Hz, ce que Pillow tient. S'il le faut, il pourra passer côté moteur.
- **Écran retenu : HDMI, sans tactile** (`hardware.md` §1). Le HDMI passe par le même compositeur HVS, et le pilote expose aussi un connecteur *writeback* (`card0-Writeback-1`) : il permettra de faire des **captures d'écran du rendu composé** à distance pour les tests.
- ✅ **Le bureau s'est effacé** (2026-10-11). Le Pi démarre en console (`raspi-config` B1, `multi-user.target`, sans connexion automatique).
  - CMA libre : 43 Mo → **246 Mo** sur 256.
  - RAM disponible : 613 → **720 Mo**.
  - La console (fbcon) occupe le plan primaire HDMI ; le moteur pose ses plans par-dessus, comme le fait l'aperçu DRM de rpicam-apps. Quand le moteur s'arrête, ses plans disparaissent et la console revient.

## 5. L'application `pycam` (Python)

Un seul processus `asyncio` (`python3 -m pycam`, dans `app/`), découpé en modules :

| Module | Rôle | État |
|---|---|---|
| `engine_client` | Client du socket du moteur : commandes et flux d'évènements, reconnexion automatique. | ✅ |
| `camera` | Le contrôleur : actions, état, rendu du HUD (5 Hz max, seulement si la vue change). Il renvoie tout l'état au moteur à chaque (re)connexion. | ✅ |
| `exposure` | Modes P/S/A/M, pas de 1/3 IL, tables de vitesses et d'ISO. Un passage en manuel part de la valeur mesurée. | ✅ |
| `inputs` | Lecture **evdev** de tout ce qui a des touches utiles ; les périphériques sont « grabbés » et ceux branchés à chaud sont détectés. | ✅ |
| `menu` | Menus Q (12 réglages de prise de vue) et MENU (tous les réglages, par section, avec défilement). | ✅ |
| `hud` / `view` | `view` décrit ce qu'il faut montrer (une valeur comparable) ; `hud` le dessine avec Pillow. | ✅ |
| `settings` | Réglages persistants dans `~/.config/pycam/settings.json`, écriture atomique. JSON plutôt que TOML : la bibliothèque standard sait le lire *et* l'écrire. | ✅ |
| `storage` | DCIM, numérotation persistante, espace libre. | ✅ (suppression à venir) |
| `state` | Machine d'états : `VIEW` → `MENU` / `PLAYBACK` / `SLEEP`. | ⬜ |
| `drive` | Modes de déclenchement : vue par vue, rafale, retardateur, intervallomètre, bracketing ; et la pose B, et l'annulation d'une pose longue (voir plus bas). | ✅ |
| `sensors` | IMU (niveau, orientation), batterie, RTC. | ⬜ |
| `timelapse` | L'assemblage des vues d'un timelapse en vidéo MP4, par ffmpeg (voir plus bas). | ✅ |
| `video` | Le mode vidéo : définitions, débits, exposition automatique, temps restant (voir plus bas). | ✅ |
| `web` | `aiohttp`, port 8080, réseau local seulement : visée (une image par seconde, prise par la commande `snapshot`), déclencheur, preset et grain, galerie avec téléchargement et suppression, installation de presets. Une page unique sans dépendance (`web/index.html`). | ✅ |

**Commandes clavier, en attendant les boutons.** Le clavier ne garde qu'une touche par commande de l'appareil (`hardware.md` §2) ; tous les autres réglages sont dans les menus. Les touches sont choisies pour être au même endroit en QWERTY et en AZERTY.

| Touche | Commande de l'appareil | Action |
|---|---|---|
| Espace / Entrée | Déclencheur | Déclencher, selon le mode de déclenchement (tenu : la rafale continue). Pendant une séquence, l'arrête. Ferme aussi un menu, la loupe ou la revue, et déclenche quand même. |
| ← / → | Molette avant | En S et M : vitesse plus lente / plus rapide. En P et A : correction d'exposition −⅓ / +⅓ IL. Dans un menu : changer la valeur. |
| ↓ / ↑ | Molette arrière | En A et M : ISO. En P et S : correction d'exposition −⅓ / +⅓ IL. Dans un menu : changer de ligne. |
| S / I | Appui sur la molette avant / arrière | Vitesse / ISO : basculer automatique ⇄ manuel (le passage en manuel part de la valeur mesurée). |
| Tab | Q | Menu rapide ; Tab le ferme. Depuis le menu complet, passe au menu rapide. |
| Échap | MENU | Menu complet ; Échap le ferme. Dans la loupe, la quitte d'abord. |
| D | DISP | Affichage : détaillé → simple → nature. |
| L | LOUPE | Loupe ×5 → ×10 → coupée. Dans la loupe, les flèches la déplacent au lieu de régler vitesse et ISO (en attendant le joystick). La grille est masquée, et le HUD montre le grossissement et une mini-carte de la zone. |
| P | LECTURE | Les photos de la carte (voir ci-dessous). |
| 1 / 2 | Fn1 / Fn2 | Ce que le menu leur attribue : peaking (Fn1 par défaut), zébras (Fn2 par défaut), mémoriser la balance, grille, bascule ⇄ ZSL, RAW + JPEG ⇄ JPEG, mode de déclenchement suivant., photo ⇄ vidéo. |
| V | Sélecteur photo / vidéo | Passer du mode photo au mode vidéo, et retour (pas pendant un enregistrement). |

Pendant la revue après la prise, toute touche la ferme ; le déclencheur la ferme *et* déclenche.

**La lecture** (`playback.py`, bouton LECTURE). Elle s'ouvre sur la dernière photo et montre, par-dessus la visée, la photo, ce qu'elle a été prise (nom, vitesse, ISO, ouverture, preset, lus dans l'EXIF), sa place dans la carte (« 12/57 ») et son histogramme en affichage détaillé.

Les **vidéos** des timelapses y sont aussi, chacune après ses vues. Une vidéo s'affiche d'abord par sa première image (faite une fois par ffmpeg, puis gardée en cache), avec un signe de lecture, sa durée, sa définition, sa cadence et son nombre de vues. LOUPE la lit (par le moteur, sur le plan vidéo), la met en pause, la reprend, et la relit une fois finie ; pendant la lecture, le HUD ne garde qu'une barre de progression, le nom, le temps (« 0:02 / 0:04 ») et le signe de pause. La dernière image reste à la fin. Les molettes, LECTURE, MENU et le déclencheur l'arrêtent ; Q la supprime (ses vues restent).

| Commande | En lecture |
|---|---|
| Molette arrière | Photo précédente / suivante. |
| Molette avant | 10 photos en arrière / en avant. |
| LOUPE | Ajustée → 100 % (un pixel de la photo par pixel de l'écran) → 200 % → ajustée. Dans la loupe, les molettes déplacent la zone, avec la mini-carte. La photo est décodée en pleine résolution une fois (~1 s), puis seulement recadrée. Sur une vidéo : lecture, pause, reprise. |
| DISP | Détaillé (infos et histogramme) → simple (infos) → photo seule. |
| Q | Supprimer : un premier appui demande confirmation, un second dans les 3 s supprime la prise entière (JPEG, DNG, copie de base). Toute autre touche annule. |
| LECTURE, MENU | Revenir à la prise de vue. |
| Déclencheur | Revenir à la prise de vue *et* déclencher : on ne rate jamais une photo. |

**Les menus** (`menu.py`). La molette arrière choisit la ligne, la molette avant change la valeur, qui s'applique tout de suite.

- **Q, réglages rapides** : les 12 réglages de prise de vue. Simulation (le preset), simuler le grain, mesure de la lumière, balance des blancs, température, peaking (coupé ou sa couleur), netteté, contraste, saturation (dont N&B), format, mode de déclenchement, ouverture. La réactivité (bascule ou ZSL) et la réduction du bruit sont dans MENU ; la réactivité est attribuable à Fn.
- **MENU, menu complet** : tous les réglages, par section, la configuration comprise.
  - Exposition : mesure, balance des blancs, température.
  - Simulation : le preset (`presets.md`), simuler le grain.
  - Mise au point : peaking, sensibilité du peaking, zébras.
  - Image : netteté, contraste, saturation, réduction du bruit.
  - Fichiers : format, taille et qualité JPEG, compression du RAW.
  - Prise de vue : mode de déclenchement, intervalle et nombre de vues de l'intervallomètre, vues et écart du bracketing, réactivité (bascule ou ZSL), revue après la prise, garder la base (un JPEG neutre en plus, `PYB_0001.JPG`).
  - Objectif : profil d'objectif, ouverture, correction de l'ombrage (un autre tuning : la caméra est rouverte, ~0,2 s).
  - Affichage : grille des tiers, histogramme, écran pendant les longues prises.
  - Boutons : fonctions de Fn1 et Fn2.

  Il fait 42 lignes (titres compris) et défile ; une barre à droite montre où l'on est.
- Quelques lignes ont un comportement propre :
  - **Balance des blancs** : la valeur « Pellicule », juste après « Auto », prend la balance fixe du film du preset (5500 K pour Portrait 400 et Velours 50, 3200 K pour les Cinéma 800T), comme un vrai film : une pellicule tungstène sort bleue en lumière du jour. Un preset sans balance propre (Standard, Triple-X 400) reste alors en automatique. Le bandeau affiche « Film 3200 K ». La valeur « Mémorisée », en dernier, garde les gains que la balance automatique mesure à cet instant (viser une charte grise d'abord) ; Fn peut faire la même chose sans ouvrir le menu.
  - **Température** : hors mode Kelvin, elle affiche la température mesurée (« ≈ 4500 K »). Le premier cran part de cette valeur et passe la balance en Kelvin.
  - **Réactivité** : le changement n'est gardé que si le moteur l'accepte (il refuse pendant une prise).
  - **Objectif** : en changer oublie l'ouverture indiquée. Les profils sont dans `settings.json` ; par défaut, les deux objectifs officiels Raspberry Pi.
  - **Ouverture** : celle de la bague, écrite dans l'EXIF et affichée dans le bandeau. Sans objectif choisi, il n'y a rien à régler.

**Les modes de déclenchement** (`drive.py`, 2026-10-12). Ce que fait le déclencheur ; le mode est un réglage (Q, MENU › Prise de vue, ou Fn qui passe au suivant).

| Mode | Comportement |
|---|---|
| Vue par vue | Une photo par appui. |
| Rafale | Tant que le déclencheur est tenu (au moins une vue, 20 au plus) : chaque vue dès que le moteur a pris la précédente (`captured`), puis au rythme de l'écriture quand la file est pleine. Le clavier et les boutons envoient le relâchement (`shutter_release`) ; le déclencheur web fait appui + relâchement, donc une vue. |
| Retardateur 2 s / 10 s | Compte à rebours en gros chiffres au centre de la visée, puis une photo. |
| Timelapse | L'intervallomètre, plus une vidéo (voir ci-dessous). |
| Empilement | 4 à 128 vues, ou jusqu'à l'arrêt, combinées en une photo par le moteur (§2.4) : leur **moyenne** (moins de bruit, ou une pose longue simulée en plein jour, l'eau soyeuse sans filtre ND) ou leur **maximum** (traînées de phares ou d'étoiles). Hors M, l'exposition mesurée est figée pour toutes, et une balance automatique aussi, comme pour un bracketing ; en M (des poses de 20 s pour les étoiles), elle l'est déjà. Le déclencheur l'arrête en gardant ce qui est empilé. L'écran affiche le compte des vues (« 10/32 »). Pas d'alignement des vues : sur pied. |
| Intervallomètre | Une photo toutes les 2, 5, 10, 30 s, 1 ou 5 min ; 10, 25, 50, 100, 250 vues ou jusqu'à l'arrêt. En retard d'un intervalle entier (file d'écriture pleine), la suivante part un intervalle plus tard : pas de rattrapage en rafale. |
| Bracketing | 3 ou 5 vues autour de la mesure (0, −, +, −−, ++), à ±⅓, ±⅔ ou ±1 IL. L'exposition mesurée est figée (temps et gain manuels) et c'est le temps qui varie (le gain en S, où la vitesse est celle de l'utilisateur), ce qui ne dépend pas de la convergence de l'exposition automatique. Une balance automatique est figée aussi, pour que les vues se fusionnent. Pendant la série, les molettes d'exposition sont bloquées et l'apprentissage du posemètre suspendu ; l'exposition de l'utilisateur revient à la fin. |

Le déclencheur pressé pendant une séquence l'arrête (sauf la rafale, qu'il prolonge). Une séquence attend la file d'écriture (jusqu'à 15 s) au lieu d'afficher « Occupé ». Le bandeau montre le mode en haut à gauche (« RAFALE », « RET 10 s », « INT 10 s », « BKT 3 × 1 ») et, pendant la séquence, sa progression en orange (« INT 4/50 · 8 s », « BKT 2/3 · −1.0 »). Pas de revue après chaque vue d'une rafale ou d'un bracketing : seule la dernière est montrée, à la fin. L'interface web affiche la même chose, et son bouton devient « Arrêter la séquence ».

**Le timelapse** (`drive.py`, `timelapse.py`, 2026-10-12). Un mode de déclenchement : une vue toutes les 1 à 60 s, 100 à 1 500 vues ou jusqu'à l'arrêt (le menu donne la durée de vidéo qui en sortira : « 250 vues · 10 s de vidéo »), puis une vidéo.

- **Les vues** vont dans un dossier à elles (`101PYCAM`…), et les photos suivantes dans le dossier d'après. Par défaut, ce sont des JPEG de 3 Mpx (assez pour du 1080p, deux fois plus rapides à écrire, look compris), le temps du timelapse ; au choix, des photos complètes (les réglages de Fichiers, un JPEG au moins pour la vidéo). Le preset s'applique comme à toute photo.
- **La vidéo** (`PYT_0056.MP4` pour une première vue `PYC_0056.JPG`, à côté des vues) est faite par ffmpeg, en arrière-plan, quand la prise s'arrête (fin, ou déclencheur) : recadrage 16:9 (1920 × 1080) ou 4:3 (1440 × 1080), lissage du scintillement (le filtre `deflicker` ramène la luminosité de chaque vue à la moyenne de ses 9 voisines : les sauts de l'exposition automatique disparaissent), H.264 par l'**encodeur matériel** du VideoCore (`h264_v4l2m2m`, 20 Mbit/s), libx264 s'il échoue. Elle est écrite en `.part`, puis renommée. Sa progression s'affiche en haut à gauche (« VIDÉO 40 % ») et dans l'interface web, qui la liste, la lit et la télécharge.
- **Mesuré** (2026-10-12, 100 vues à 1 s, mode P, Cinéma 800T) : une vue toutes les 1,00 s en moyenne (0,86 à 1,19 s), vidéo prête 14 s après la dernière vue (~7 vues/s), 6,3 Mo pour 4 s de 1080p. Sur des vues au scintillement simulé de ±15 %, le lissage divise l'écart-type de la luminance par 3,4 ; sur le vrai timelapse, il reste de 0,28 niveau sur 255. L'assemblage ne bloque jamais l'application plus de 32 ms.
- La lecture sur l'appareil montre les vues et la vidéo, qu'elle lit (voir « La lecture »).

**L'écran en veille pendant les longues prises** (2026-10-12). Pendant un intervallomètre, un timelapse, une pose B ou une pose longue, l'écran s'éteint après 30 s sans appui (MENU › Affichage › Écran en longue prise : 10 s, 30 s, 1 min, ou jamais). Plus de HUD à dessiner (0,1 s de CPU à chaque rendu), plus de visée à afficher. Un appui le rallume et ne fait que ça : l'appui qui réveille ne déclenche pas, n'arrête rien. Le relâchement du déclencheur compte toujours (il termine une pose B tenue), et le déclencheur de l'interface web agit sans rallumer. L'écran se rallume seul à la fin de la prise. Mesuré pendant un intervallomètre (RAW + JPEG, Cinéma 800T, une vue toutes les 5 s) : le moteur passe de 174 à 129 % d'un cœur, l'application de 33 à 10 %, soit un tiers du CPU en moins, en plus de l'écran.

**Le mode vidéo** (`video.py`, §2.5). On y passe par MENU › Vidéo › Appareil, par une touche Fn (« Photo / Vidéo »), ou la touche V du clavier (un sélecteur sur l'appareil). Peu de réglages : la définition (1080p, 720p), la qualité (débit : 20, 12 ou 6 Mbit/s en 1080p, la moitié en 720p) et l'ISO (automatique, ou de 100 à 1600) ; le reste est automatique. La balance et la mesure sont celles de la photo (« Pellicule » devient automatique : pas de look en vidéo).

- Le déclencheur démarre et arrête l'enregistrement : `PYV_0169.MP4`, numéroté comme les photos. Pendant l'enregistrement, « ● REC 0:05 » en rouge ; pas de lecture, ni de retour en photo, et un changement de définition ou de qualité attend la fin.
- La molette avant règle la correction d'exposition, la molette arrière l'ISO (le premier cran part de l'ISO mesuré), l'appui sur la molette arrière remet l'ISO en automatique.
- Le menu Q devient « Réglages vidéo » : appareil, définition, qualité, ISO, mesure, balance, température, peaking, zébras.
- Le bandeau montre « V », la vitesse et l'ISO mesurés, la correction, la balance, « 1080p 25 i/s · Normale » et le temps d'enregistrement restant sur la carte. L'image est en 16:9, avec des bandes noires dans la zone d'image.
- Les vidéos vont dans la lecture et l'interface web (lecture, téléchargement, suppression), comme les timelapses. Le mode, enregistré dans les réglages, revient au démarrage.

**Les poses longues** (2026-10-12, §2.4). En M, la molette avant va jusqu'à 10'40" (la limite du capteur, 674 s), puis **B**.

- Une pose de 1 s et plus affiche son temps restant en gros au centre de l'écran, sous « POSE ». Le déclencheur l'annule (« Pose annulée »).
- **B** : la pose dure tant que le déclencheur est tenu, ou, au choix (MENU › Prise de vue › Pose B), d'un appui à l'autre, pour ne pas tenir le bouton pendant des minutes. Depuis l'interface web, toujours d'un appui à l'autre. Le temps écoulé s'affiche sous « POSE B ». Un appui pendant sa dernière seconde (après l'arrêt) ne fait rien : il ne doit jamais la perdre.
- Pendant une pose, les molettes d'exposition sont bloquées (« Pose en cours ») ; un changement de preset ou de balance attend la fin de la pose pour s'appliquer.
- B ignore le mode de déclenchement. Les poses longues, elles, se combinent avec lui : un intervallomètre de poses de 30 s fait un timelapse de nuit, la file d'attente patientant le temps de la pose.

**Les molettes** (décidé le 2026-10-11). Chacune règle sa valeur quand elle est manuelle, sinon la correction d'exposition :

| Mode | Molette avant | Molette arrière |
|---|---|---|
| P | correction | correction |
| S | vitesse | correction |
| A | correction | ISO |
| M | vitesse | ISO |

C'est le comportement de `hardware.md` pour la molette avant ; la molette arrière le complète en P et S. Tourner une molette ne change jamais le mode : c'est son appui (S, I au clavier) qui passe la valeur en manuel, en partant de la mesure, ou la rend automatique. La correction est gardée quand on passe en manuel, et n'agit pas en M.

**Pourquoi evdev.** Avec les overlays noyau, chaque bouton devient un évènement clavier (`KEY_*`) et chaque molette un évènement relatif (`REL_*`). Ça apporte plusieurs choses :

- l'anti-rebond est fait par le noyau ;
- aucun CPU n'est consommé à l'attente ;
- on peut **brancher un clavier USB** pour développer ;
- on peut simuler des appuis par `uinput` dans les tests automatiques (`tools/press.py`).

Constaté le 2026-10-11 : le pilote HDMI du Pi expose aussi un périphérique `vc4-hdmi` (HDMI-CEC) avec des touches de navigation. La télécommande d'une TV compatible CEC peut donc piloter l'appareil, gratuitement.

## 6. Protocole moteur ⇄ application

Le moteur écoute sur le socket Unix `/run/pycam/engine.sock`, et chaque message est un objet JSON sur une ligne. On a d'abord utilisé `/tmp`, abandonné parce que Debian y nettoie les vieux fichiers, ce qui est risqué pour un appareil allumé des jours.

Le protocole implémenté est décrit dans **[protocol.md](protocol.md)** :

- **commandes** : `ping`, `get_state`, `list_controls`, `set_controls`, `capture`, `configure`, `quit` ;
- **évènements** : `ready`, `stats`, `captured`, `capture_done`, `capture_error`, `error`.

Les commandes d'affichage (`overlay` pour peaking et zébras, `zoom` pour la loupe) viendront avec le module d'affichage.

**Pourquoi un socket Unix en JSON plutôt que Redis (le choix de CinePI) ?**

- On n'a pas de démon supplémentaire sur 1 Go de RAM.
- Il n'y a aucune dépendance côté Python.
- C'est inspectable à la main.
- Le moteur garde un état volatil ; c'est l'application qui est la source de vérité des réglages, et elle renvoie la configuration complète à chaque (re)connexion.

## 7. Stockage et formats

- **Arborescence** : `~/DCIM/100PYCAM/PYC_0001.JPG` et `.DNG`. Le numéro est persistant et le dossier change tous les 9999 fichiers, ainsi qu'au début et à la fin d'un timelapse (ses vues et sa vidéo `PYT_xxxx.MP4` dans un dossier à elles). Si on ajoute une clé USB plus tard, elle se monte au même endroit.
- **JPEG.** Qualité réglable. L'EXIF contient la vitesse, l'ISO et la date (venant de la RTC plus tard), puis l'orientation de l'IMU plus tard.
  - ✅ **Profil d'objectif** (2026-10-11, `app/pycam/lenses.py`). L'application passe des balises EXIF au moteur avec la commande `capture`, et rpicam-apps les écrit via libexif : `LensModel`, `LensMake`, `FocalLength`, `FocalLengthIn35mmFilm` (facteur de recadrage IMX477 ≈ 5,5 : un 16 mm équivaut à un 88 mm), `MaxApertureValue`, `LensSpecification`. Vérifié en relisant le fichier.
  - L'ouverture réglée sur la bague n'est pas connue de l'appareil. L'utilisateur l'indique (ligne Ouverture des menus) et elle est écrite en `FNumber` ; sinon, elle est omise plutôt qu'inventée.
- ✅ **DNG** (2026-10-11, `engine/src/dng_writer.cpp`). RAW 12 bits avec la matrice couleur de l'IMX477.
  - C'est **notre propre écrivain**, parti de celui de rpicam-apps (licence BSD-2), qui n'acceptait aucune balise en plus.
  - Il écrit le **profil d'objectif** : les mêmes balises `EXIF.*` que le JPEG, plus `LensInfo` du DNG.
  - Il offre la **compression sans perte** LJ92 (activée par défaut dans l'application, ligne « RAW compressé » du menu) : **12,1 Mo au lieu de 24,8 (÷ 2,05)**, pour ~1,5 s d'écriture en plus, en arrière-plan.
  - **Compatibilité vérifiée, et elle a dicté le format.** Quatre dispositions LJ92 ont été mesurées sur la même image :

    | Disposition | Taille | LibRaw (RawTherapee…) | rawspeed (darktable) |
    |---|---|---|---|
    | CinePI : 2 lignes du capteur par ligne JPEG, prédicteur 6 | ÷ 2,03 | ✅ | ❌ trame plus large que l'image |
    | 1 composante, prédicteur 6 | ÷ 1,40 | ✅ | ❌ prédicteur 6 non supporté |
    | 2 composantes, prédicteur 6 | ÷ 1,62 | ✅ | ❌ prédicteur 6 non supporté |
    | **Adobe : 2 composantes, prédicteur 1** | **÷ 2,05** | ✅ relu au bit près | ✅ rendu correct (`darktable-cli`) |

  - Pour cela, la bibliothèque `lj92` (MIT, reprise de CinePI) a été étendue : composantes et prédicteur (voir `engine/third_party/lj92/README.md`).
  - Un test d'aller-retour (`engine/tests/dng_roundtrip.sh`) écrit une vraie image en DNG, puis la fait relire par LibRaw au bit près.

## 8. Système

- **OS.** Raspberry Pi OS 64 bits démarré en console, sans bureau : le moteur doit pouvoir être maître DRM.
- ✅ **Services** (2026-10-11). Deux services systemd, installés par `tools/install-services.sh` depuis les modèles de `system/` :
  - `pycam-engine.service` : moteur, `Nice=-5`, redémarrage automatique ;
  - `pycam.service` : application. Elle ne dépend que *faiblement* du moteur (`Wants`) : elle se reconnecte seule, et un redémarrage du moteur ne la relance pas.
  - Tous deux réessaient indéfiniment au démarrage, au cas où la caméra apparaîtrait tard.
  - Le socket est dans `/run/pycam`, créé au démarrage par `tmpfiles.d`.
- ⚠️ **Plymouth garde l'écran.** L'écran de démarrage (`splash` dans `cmdline.txt`) reste maître DRM de 12 s à 43 s après le démarrage du noyau, et le moteur, lancé à 21 s, n'avait pas l'écran. Deux parades :
  - le service moteur commence par `plymouth quit --retain-splash`, comme le font les gestionnaires de session graphique : le logo reste affiché jusqu'à ce que la visée le remplace ;
  - le moteur retente l'écran toutes les 2 s tant qu'il ne l'a pas, et l'annonce par l'évènement `display_ready`. Ça couvre aussi un écran branché après coup.
- **Démarrage mesuré** (2026-10-11) : services lancés à 22 s après le démarrage du noyau, écran pris à 24,7 s, **visée à 25,4 s**. Il faut ajouter ~3–4 s de firmware avant le noyau. Gros postes repérés par `systemd-analyze critical-chain` : cloud-init, ~6 s (inutile après la première installation), le montage et la vérification de `/boot`, et le réseau.
- **Démarrage rapide** (objectif M2 : moins de 12 s jusqu'à la visée) :
  - désactiver les services inutiles, à commencer par cloud-init ;
  - `boot_delay=0`, et peut-être remplacer le logo Plymouth par un écran pycam ;
  - ne pas attendre le réseau ;
  - lancer le moteur plus tôt (dépendances systemd minimales).
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

1. ✅ **Latence de changement de mode**, mesurée dans le moteur (§2.2) : 0,28 à 0,5 s en bascule selon le débruitage, 0 à 0,25 s en ZSL. On propose les deux modes.
2. ✅ **Mémoire CMA** : sans bureau, 246 Mo sont libres sur 256. C'est assez pour la ZSL (~111 Mo) avec de la marge pour l'affichage.
3. ✅ **Plans DRM overlay** sur HDMI : visée + HUD fonctionnent (§4). Reste un point ouvert : la mémoire de lignes pour un troisième plan mis à l'échelle (peaking).
4. ✅ **`librpicam_app`** : `librpicam-app-dev` 1.13.0 est installé, et le moteur compile et tourne.
5. ✅ **Écran** : HDMI sans tactile.
6. ✅ **Droits** : `sudo` sans mot de passe pour `ael` pendant le développement.
7. 🟨 **Temps d'encodage réels.** Sur une image très bruitée (gain 16, donc difficile à compresser, JPEG de 2,1 Mo) : JPEG ~350 ms, DNG ~180–230 ms. Reste à confirmer sur une vraie scène.
8. 🟨 **Temps de démarrage** : la visée apparaît 25,4 s après le démarrage du noyau (§8). Le démarrage complet du système prend ~50 s, mais l'appareil est utilisable avant. L'objectif est de moins de 12 s (M2).
9. ✅ **Durabilité des photos** (2026-10-12). Chaque fichier est écrit en `.part`, forcé sur la carte (`fsync`), renommé, puis le dossier est forcé à son tour : `capture_done` signifie « sur la carte », et une coupure de courant ne laisse jamais de fichier tronqué sous son vrai nom. Coût mesuré sur la carte SD du Pi : JPEG 0,67 s au lieu de 0,4, DNG compressé 2,8 s au lieu de 1,8. La revue n'attend pas : elle part du JPEG, écrit en premier. Une photo RAW + JPEG occupe le fil d'écriture ~4 s avec le look, ce qui limite surtout les rafales.
10. ✅ **Commande de la correction d'exposition** : la molette dont la valeur est automatique la règle (§5). Plus de touche dédiée.
11. ✅ **Course à l'arrêt de la caméra** (2026-10-12, vue pendant les essais du bracketing : deux plantages du moteur, « double free or corruption »). Dans rpicam-apps 1.13, `StopCamera()` vide la liste des requêtes rendues à l'application sans verrou, alors que `queueRequest()` y cherche et y efface, verrou pris, quand un autre fil rend une image. Le fil d'affichage rendait parfois la sienne à ce moment-là. Le moteur lui fait désormais rendre ses images *avant* d'arrêter la caméra (`ReleaseFrames`). 200 captures de suite en bascule sans plantage ensuite ; la course étant rare, aucun essai ne l'a reproduite à la demande avant le correctif.
12. ✅ **Contrôles mémorisés** (2026-10-12). Le moteur rend tous les contrôles reçus à la caméra à chaque redémarrage (photo en bascule, loupe, ZSL). Des gains de balance reçus une fois (balance « Mémorisée », bracketing) y restaient après le retour en Auto, et coupaient la balance automatique à la photo suivante. Désormais, un mode automatique oublie la valeur manuelle qu'il remplace, et les gains et la température s'excluent (`remember_controls`, testé). De même, une pose manuelle plus longue que l'image de visée allonge tout de suite les limites de durée d'image ; avant, il fallait attendre un redémarrage, et en ZSL une pose de ½ s était coupée à ¼ s.
13. ✅ **Compiler le moteur sur le Pi 3B** (2026-10-12). Un compilateur prend jusqu'à 690 Mo sur `engine.cpp` (nlohmann/json, `-O3`). Deux en parallèle ont saturé le Pi jusqu'à ne plus répondre, même en SSH : il a fallu le redémarrer à la main. `tools/deploy.sh` n'en lance plus qu'un (~10 min pour tout recompiler).

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
