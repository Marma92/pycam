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
- **Expositions longues.** L'IMX477 accepte des poses de plusieurs minutes (la limite exacte est à mesurer). La visée ne peut pas suivre : on affiche un compte à rebours.

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

## 3. Modes d'exposition (dans l'application)

L'ouverture est réglée sur l'objectif, donc les modes « classiques » se traduisent ainsi en contrôles libcamera :

| Mode | Vitesse (`ExposureTime`) | ISO (`AnalogueGain`) | Remarque |
|---|---|---|---|
| **P** (auto) | auto | auto | Correction d'exposition (`ExposureValue`) et mode de mesure (`AeMeteringMode`). |
| **S** (priorité vitesse) | fixe | auto | — |
| **A** (priorité ISO) | auto | fixe | Correspond à « l'aperture priority » des specs d'origine : l'ouverture est sur la bague et l'appareil choisit la vitesse. |
| **M** (manuel) | fixe | fixe | Le posemètre indique l'écart en IL. |

À ces modes s'ajoutent :

- ✅ **Balance des blancs** : automatique, préréglages (`AwbMode`), ou manuelle.
  - Le **Kelvin** passe par `ColourTemperature` : l'ISP en déduit les gains par la courbe de calibration de l'IMX477 (3000 K → gains R/B 2,08 / 2,20 ; 6500 K → 3,24 / 1,39, mesurés).
  - La **mémorisation** fige les gains (`ColourGains`) trouvés par la balance automatique, par exemple sur une charte grise.
  - Les deux demandent `AwbEnable` à faux, et un préréglage le remet à vrai. Les valeurs manuelles restées en mémoire dans le moteur sont alors ignorées (vérifié).
- ✅ **Mode de mesure** (`AeMeteringMode`, ligne en tête du menu rapide) : pondérée centrale (par défaut), spot, matricielle.
  - L'ISP découpe l'image en 15 zones de statistiques. Chaque mode est un jeu de poids sur ces zones, défini dans le fichier de tuning (`/usr/share/libcamera/ipa/rpi/vc4/imx477.json`) : centrale `[3,3,3,2,2,2,2,1,1,1,1,0,0,0,0]`, spot `[2,1,1,0…]`, matricielle tous à 1.
  - Mesuré sur une même scène : spot −1,6 IL, matricielle +0,4 IL par rapport à la centrale.
  - Le HUD montre une icône dans le bandeau (anneau, point, grille) et, en spot, un cercle au centre de la visée. Sa taille est approximative : la zone exacte dépend du découpage de l'ISP.
  - Le mode n'agit que sur l'exposition automatique (P, S, A). En M, le posemètre mesure toujours toute l'image (voir §2.3).
- **Rendu** : débruitage, netteté, contraste, saturation.
- **Profils d'image** : plus tard, via des fichiers de tuning alternatifs.

## 4. Affichage

Le moteur est **maître DRM/KMS**. Il n'y a pas de bureau graphique : on utilise Pi OS Lite, ou on démarre en console. Le compositeur matériel du VC4 (HVS) superpose plusieurs plans avec transparence et mise à l'échelle, sans travail CPU :

| Plan (du bas vers le haut) | Format | Contenu | Producteur | État |
|---|---|---|---|---|
| vidéo | YUV420, mis à l'échelle par le HVS | visée en direct | tampons caméra → moteur, zéro copie (dmabuf) | ✅ |
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
| `drive` | Vue par vue, rafale, retardateur, intervallomètre, bracketing, pose B. | ⬜ |
| `sensors` | IMU (niveau, orientation), batterie, RTC. | ⬜ |
| `web` | `aiohttp` : télécommande, galerie, téléchargement, visée distante en MJPEG basse résolution. | ⬜ |

**Commandes clavier, en attendant les boutons.** Le clavier ne garde qu'une touche par commande de l'appareil (`hardware.md` §2) ; tous les autres réglages sont dans les menus. Les touches sont choisies pour être au même endroit en QWERTY et en AZERTY.

| Touche | Commande de l'appareil | Action |
|---|---|---|
| Espace / Entrée | Déclencheur | Déclencher. Ferme aussi un menu, la loupe ou la revue, et déclenche quand même. |
| ← / → | Molette avant | En S et M : vitesse plus lente / plus rapide. En P et A : correction d'exposition −⅓ / +⅓ IL. Dans un menu : changer la valeur. |
| ↓ / ↑ | Molette arrière | ISO ; tournée en P ou S, elle passe l'ISO en manuel en partant de la mesure. Dans un menu : changer de ligne. |
| S / I | Appui sur la molette avant / arrière | Vitesse / ISO : basculer automatique ⇄ manuel (le passage en manuel part de la valeur mesurée). |
| Page↓ / Page↑ | — | Correction d'exposition, dans tous les modes. Seule façon de la régler en S : point ouvert, voir ci-dessous. |
| Tab | Q | Menu rapide ; Tab le ferme. Depuis le menu complet, passe au menu rapide. |
| Échap | MENU | Menu complet ; Échap le ferme. Dans la loupe, la quitte d'abord. |
| D | DISP | Affichage : détaillé → simple → nature. |
| L | LOUPE | Loupe ×5 → ×10 → coupée. Dans la loupe, les flèches la déplacent au lieu de régler vitesse et ISO (en attendant le joystick). La grille est masquée, et le HUD montre le grossissement et une mini-carte de la zone. |
| 1 / 2 | Fn1 / Fn2 | Ce que le menu leur attribue : peaking (Fn1 par défaut), zébras (Fn2 par défaut), mémoriser la balance, grille, bascule ⇄ ZSL, RAW + JPEG ⇄ JPEG. |

Pendant la revue après la prise, toute touche la ferme ; le déclencheur la ferme *et* déclenche.

**Les menus** (`menu.py`). La molette arrière choisit la ligne, la molette avant change la valeur, qui s'applique tout de suite.

- **Q, réglages rapides** : les 12 réglages de prise de vue. Mesure de la lumière, balance des blancs, température, peaking (coupé ou sa couleur), zébras, netteté, contraste, saturation (dont N&B), réduction du bruit, format, déclenchement (bascule ou ZSL), ouverture.
- **MENU, menu complet** : tous les réglages, par section, la configuration comprise.
  - Exposition : mesure, balance des blancs, température.
  - Mise au point : peaking, sensibilité du peaking, zébras.
  - Image : netteté, contraste, saturation, réduction du bruit.
  - Fichiers : format, taille et qualité JPEG, compression du RAW.
  - Prise de vue : déclenchement, revue après la prise.
  - Objectif : profil d'objectif, ouverture.
  - Affichage : grille des tiers, histogramme.
  - Boutons : fonctions de Fn1 et Fn2.

  Il fait 22 lignes et défile ; une barre à droite montre où l'on est.
- Quelques lignes ont un comportement propre :
  - **Balance des blancs** : la valeur « Mémorisée » garde les gains que la balance automatique mesure à cet instant (viser une charte grise d'abord). Fn peut faire la même chose sans ouvrir le menu.
  - **Température** : hors mode Kelvin, elle affiche la température mesurée (« ≈ 4500 K »). Le premier cran part de cette valeur et passe la balance en Kelvin.
  - **Déclenchement** : le changement n'est gardé que si le moteur l'accepte (il refuse pendant une prise).
  - **Objectif** : en changer oublie l'ouverture indiquée. Les profils sont dans `settings.json` ; par défaut, les deux objectifs officiels Raspberry Pi.
  - **Ouverture** : celle de la bague, écrite dans l'EXIF et affichée dans le bandeau. Sans objectif choisi, il n'y a rien à régler.

**La molette avant suit `hardware.md`** (décidé le 2026-10-11) : elle règle la vitesse quand elle est manuelle (S, M), sinon la correction d'exposition (P, A). Tourner la molette ne change donc plus le mode : c'est l'appui (S au clavier) qui passe la vitesse en manuel, en partant de la mesure. La correction est gardée en S, où elle guide l'ISO automatique.

**Point ouvert : la correction d'exposition en S.** Aucune molette ne la règle dans ce mode : la molette avant fait la vitesse, et la molette arrière passe l'ISO en manuel. Seul Page↑ / Page↓ la règle, sans équivalent physique. Piste : en S, la molette arrière ferait la correction, l'appui servant toujours à passer l'ISO en manuel.

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

- **Arborescence** : `~/DCIM/100PYCAM/PYC_0001.JPG` et `.DNG`. Le numéro est persistant et le dossier change tous les 9999 fichiers. Si on ajoute une clé USB plus tard, elle se monte au même endroit.
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
9. ⬜ **Durabilité des photos.** `capture_done` signifie « écrit », pas « sur la carte » : les données peuvent rester ~30 s dans le cache du noyau. Sur batterie, une coupure perdrait les dernières photos. Il faudra un `fsync` par fichier dans le thread d'écriture (coût à mesurer) avant l'usage nomade.
10. 🟨 **Commande de la correction d'exposition** : la molette avant la règle en P et A. En S, seul Page↑ / Page↓ la règle, sans équivalent physique. À trancher avant le câblage (§5).

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
