# Fonctionnalités

Le catalogue est organisé par jalons. Chaque jalon donne un appareil **utilisable** ; le suivant le rend meilleur.

Légende d'état : ⬜ à faire · 🟨 en cours · ✅ fait · 🧪 à mesurer/valider

## Specs d'origine (pycam v1, 2022–2024)

Elles viennent de l'ancien `pycam.py` et sont toutes reprises ci-dessous :

- mode manuel avec vitesse, ISO et balance des blancs → **M1** ;
- priorité « ouverture », c'est-à-dire vitesse auto → **M1**, mode **A** (voir `architecture.md` §3) ;
- résolution et netteté réglables → **M1** ;
- visée « nature » (sans HUD) et visée « détaillée » (avec HUD) → **M0/M1** ;
- grille des tiers → **M0** ;
- galerie simple → **M2** ;
- déclenchement sur une entrée physique → **M0** (clavier), puis **M2** (boutons).

## M0 — « Ça prend des photos »

Objectif : valider l'architecture de bout en bout sur le Pi 3B.

| Fonctionnalité | Détail | État |
|---|---|---|
| Moteur + protocole | `pycam-engine` lié à `librpicam_app`, piloté par socket JSON (`protocol.md`). | ✅ |
| Visée en direct | Sur l'écran HDMI par plan DRM, zéro copie (30 i/s en bascule, 11,7 i/s en ZSL). | ✅ |
| Déclenchement | DNG + JPEG pleine résolution ; deux stratégies (bascule / ZSL) ; retour immédiat à la visée. | ✅ |
| File d'écriture | Asynchrone, bornée, écriture atomique (`.part` puis renommage) ; évènement `capture_done`. | ✅ |
| Réglages de base | Vitesse, ISO, balance des blancs (préréglages), correction d'exposition, au clavier. | ✅ |
| Mesures en visée | Histogramme, luminance, lux, température de couleur, netteté ISP (`focus_fom`), à 5 Hz. | ✅ |
| HUD minimal | Mode, vitesse, ISO (valeurs auto en bleu), correction, balance des blancs, formats, vues restantes, grille des tiers, messages. Trois affichages : détaillé (avec histogramme et état ZSL / écriture), simple, nature. | ✅ |
| Entrées | Claviers USB via evdev, branchés à chaud ; ils simulent les futurs boutons (`architecture.md` §5). | ✅ |
| Rangement | `~/DCIM/100PYCAM/PYC_0001.JPG/.DNG`, numérotation persistante. | ✅ |
| Service | Démarrage automatique (systemd) : la visée apparaît seule ~25 s après le démarrage du noyau, sans SSH. | ✅ |
| Mesures | Latence de déclenchement, fps de visée, temps d'écriture : voir `architecture.md` §2.2. À refaire sur une vraie scène et sans bureau. | 🟨 |

## M1 — « Appareil manuel »

| Fonctionnalité | Détail | État |
|---|---|---|
| Modes P / S / A / M | Voir `architecture.md` §3 ; pas de 1/3 IL. Fait en avance avec M0 : le mode découle de ce qui est manuel. | ✅ |
| Correction d'exposition | ±3 IL. | ✅ |
| Mode de mesure | Pondérée centrale, spot, matricielle, dans le menu rapide. Icône dans le bandeau, cercle au centre de la visée en spot. Agit sur l'exposition automatique ; le posemètre du mode M mesure toute l'image. | ✅ |
| Posemètre | Échelle −3…+3 IL au tiers dans le bandeau : toujours en M, et en avertissement (orange) quand l'exposition automatique est hors de portée. Précision ~0,15 IL. | ✅ |
| Balance des blancs | Auto, 5 préréglages, Kelvin manuel (2000–10 000 K, en partant de la mesure), mémorisation des gains mesurés (charte grise). | ✅ |
| Histogramme | RVB additif (par défaut) ou luminance, au choix dans le menu rapide, en visée détaillée et à la revue. Calculé par le moteur sur le flux basse résolution. | ✅ |
| Peaking | Couleur (5) et sensibilité (3) réglables ; seuil adaptatif ; ~6 ms par image. | ✅ |
| Zébras | Marche / arrêt dans les menus ou par Fn2. Seuil réglable dans le protocole (95 % par défaut), pas encore dans l'application. | ✅ |
| Loupe de mise au point | ×5 / ×10 (le protocole accepte 1 à 16), déplaçable ; vrais pixels grâce au passage en pleine résolution (14 i/s) ; compatible avec le peaking ; le déclencheur la quitte. | ✅ |
| Revue après la prise | Affichage 1, 2 ou 4 s, désactivable, avec nom, vitesse, ISO et histogramme de la photo. Visible ~1,5 s après l'appui : l'application décode le JPEG en 1/4 (~0,2 s). Plus rapide plus tard si le moteur fournit l'aperçu à partir du YUV. | ✅ |
| Profils d'objectifs | Nom, marque, focale, ouverture max (et l'ouverture réglée, si indiquée) écrits dans l'EXIF du JPEG et du DNG ; choix par N et F, affichage dans le bandeau. Pas encore de menu pour créer un profil : on édite `settings.json`. | ✅ |
| Paramètres d'image | Dans le menu rapide :<br>• netteté (dans l'ISP), contraste et saturation (−2…+2, plus noir et blanc ; compilés dans le look), tous visibles en visée ;<br>• réduction du bruit (aucune, légère, rapide, haute qualité) ;<br>• taille JPEG L / M / S (12, 6, 3 Mpx ; le DNG reste en pleine résolution) ;<br>• qualité JPEG, RAW on/off, compression du RAW. | ✅ |
| Réglages persistants | Fichier JSON, écriture atomique ; reprise à l'allumage. | ✅ |

## M2 — « Autonome » (dépend du matériel, voir `hardware.md`)

| Fonctionnalité | Détail | État |
|---|---|---|
| Boutons physiques | Déclencheur à deux crans, deux molettes, joystick 5 directions, Q, MENU, LECTURE, DISP, LOUPE, Fn1/Fn2 (voir `hardware.md` §2). Fn1 et Fn2 sont attribuables dans le menu ; ils sont déjà simulés au clavier (1 et 2). | ⬜ |
| Menu rapide Q | Les 12 réglages de prise de vue, par-dessus la visée : simulation, mesure, balance des blancs et température, peaking, netteté, contraste, saturation, réduction du bruit, format, déclenchement, ouverture. La molette arrière choisit la ligne, la molette avant change la valeur, le déclencheur ferme et déclenche. Au clavier : Tab. Fait en avance avec M1. | ✅ |
| Menus | MENU : tous les réglages par section (exposition, mise au point, image, fichiers, prise de vue, objectif, affichage, boutons), avec défilement. Le clavier ne garde plus qu'une touche par commande de l'appareil (`architecture.md` §5) ; reste à brancher les vrais boutons (M2). Au clavier : Échap. | 🟨 |
| Lecture / galerie | Défilement, loupe, histogramme, suppression. | ⬜ |
| Batterie | Niveau affiché, extinction propre sur batterie faible. | ⬜ |
| Horloge | RTC ; réglage de la date dans les menus. | ⬜ |
| Niveau électronique | Horizon et inclinaison par l'IMU ; orientation EXIF automatique. | ⬜ |
| Démarrage rapide | Moins de 12 s de l'allumage à la visée. | 🧪 |
| Robustesse | Racine en lecture seule et partition photos séparée. | ⬜ |
| Interface web | Télécommande, visée distante, galerie et téléchargement par Wi-Fi. | ⬜ |

## M3 — « Avancé »

| Fonctionnalité | Détail | État |
|---|---|---|
| Modes de déclenchement | Rafale, retardateur 2 s / 10 s, intervallomètre (timelapse), bracketing AEB 3/5 vues. | ⬜ |
| Pose longue / pose B | Jusqu'à la limite du capteur (à mesurer), avec compte à rebours. | ⬜ |
| ZSL | Déclenchement instantané, en option (compromis avec la fluidité de visée). | ⬜ |
| Simulations de pellicules | Presets ouverts (recette TOML, LUT `.cube` ou Hald), compilés en une LUT 3D avec grain et vignettage, appliqués au JPEG ; DNG neutre. Fait : base fixe, correction d'ombrage à part, moteur (LUT, grain, vignettage, « garder la base »), presets Standard et Base, menus, EXIF, aperçu en visée et histogramme corrigé. Reste : outil `pycam-preset`, les pellicules (Portrait 400, Velours 50, Cinéma 800T, Triple-X 400). Conception : `presets.md`. | 🟨 |
| DNG compressé | LJ92 sans perte, au format Adobe (2 composantes, prédicteur 1) : 12,1 Mo au lieu de 24,8 ; relu au bit près par LibRaw et rendu par darktable. Activé par défaut, ligne « RAW compressé » du menu. Fait en avance avec M1. | ✅ |
| Vidéo | H.264 1080p30 matériel, avec les réglages manuels. | ⬜ |
| Point d'accès Wi-Fi | Pour le terrain, sans box. | ⬜ |
| GPS | Géomarquage, en option. | ⬜ |
| Données gyro | Export des données gyroscopiques pour la stabilisation vidéo (Gyroflow). | ⬜ |
