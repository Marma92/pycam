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
| Mode de mesure | Pondérée centrale, spot, matricielle. | ⬜ |
| Posemètre | Indicateur d'écart en IL en mode M. | ⬜ |
| Balance des blancs | Auto, préréglages, Kelvin manuel, mémorisation sur une charte grise. Auto et préréglages sont faits ; Kelvin et charte grise restent à faire. | 🟨 |
| Histogramme | Luminance et RVB dans le HUD. La luminance est faite, le RVB reste à faire. | 🟨 |
| Peaking | Couleur et sensibilité réglables. | ⬜ |
| Zébras | Seuil réglable. | ⬜ |
| Loupe de mise au point | ×2,5 / ×5 / ×10 ; vrais pixels grâce au passage en pleine résolution. | ⬜ |
| Revue après la prise | Affichage N secondes, désactivable. | ⬜ |
| Profils d'objectifs | Nom, focale, ouverture max, écrits dans l'EXIF. | ⬜ |
| Paramètres d'image | Qualité JPEG, RAW on/off, résolution, netteté, contraste, saturation, débruitage. | ⬜ |
| Réglages persistants | Fichier JSON, écriture atomique ; reprise à l'allumage. | ✅ |

## M2 — « Autonome » (dépend du matériel, voir `hardware.md`)

| Fonctionnalité | Détail | État |
|---|---|---|
| Boutons physiques | Déclencheur à deux crans, deux molettes, joystick 5 directions, Q, MENU, LECTURE, DISP, LOUPE, Fn1/Fn2 (voir `hardware.md` §2). | ⬜ |
| Menu rapide Q | Grille des réglages courants : navigation au joystick, valeur à la molette. | ⬜ |
| Menus | Navigation complète aux boutons et molettes, sans tactile ni clavier. | ⬜ |
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
| Profils d'image | « Simulations » : courbes et couleurs via des fichiers de tuning libcamera. | ⬜ |
| DNG compressé | LJ92 sans perte, comme dans CinePI, pour ~2× moins de place. | ⬜ |
| Vidéo | H.264 1080p30 matériel, avec les réglages manuels. | ⬜ |
| Point d'accès Wi-Fi | Pour le terrain, sans box. | ⬜ |
| GPS | Géomarquage, en option. | ⬜ |
| Données gyro | Export des données gyroscopiques pour la stabilisation vidéo (Gyroflow). | ⬜ |
