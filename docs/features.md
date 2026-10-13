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
| Balance des blancs | Auto, Pellicule (la balance fixe du film du preset : lumière du jour 5500 K ou tungstène 3200 K), 5 préréglages, Kelvin manuel (2000–10 000 K, en partant de la mesure), mémorisation des gains mesurés (charte grise). | ✅ |
| Histogramme | RVB additif (par défaut) ou luminance, au choix dans le menu rapide, en visée détaillée et à la revue. Calculé par le moteur sur le flux basse résolution. | ✅ |
| Peaking | Couleur (5) et sensibilité (3) réglables ; seuil adaptatif ; ~6 ms par image. | ✅ |
| Zébras | Marche / arrêt dans les menus ou par Fn2. Seuil réglable dans le protocole (95 % par défaut), pas encore dans l'application. | ✅ |
| Loupe de mise au point | ×5 / ×10 (le protocole accepte 1 à 16), déplaçable ; vrais pixels grâce au passage en pleine résolution (14 i/s) ; compatible avec le peaking ; le déclencheur la quitte. | ✅ |
| Revue après la prise | Affichage 1, 2 ou 4 s, désactivable, avec nom, vitesse, ISO et histogramme de la photo. Visible ~1,5 s après l'appui : l'application décode le JPEG en 1/4 (~0,2 s). Plus rapide plus tard si le moteur fournit l'aperçu à partir du YUV. | ✅ |
| Profils d'objectifs | Nom, marque, focale, ouverture max (et l'ouverture réglée, si indiquée) écrits dans l'EXIF du JPEG et du DNG ; choix par N et F, affichage dans le bandeau. Pas encore de menu pour créer un profil : on édite `settings.json`. | ✅ |
| Paramètres d'image | Dans le menu rapide :<br>• netteté (dans l'ISP), contraste et saturation (−2…+2, plus noir et blanc ; compilés dans le look), tous visibles en visée ;<br>• réduction du bruit (aucune, légère, rapide, haute qualité ; dans MENU › Image) ;<br>• taille JPEG L / M / S (12, 6, 3 Mpx ; le DNG reste en pleine résolution) ;<br>• qualité JPEG, RAW on/off, compression du RAW. | ✅ |
| Réglages persistants | Fichier JSON, écriture atomique ; reprise à l'allumage. | ✅ |

## M2 — « Autonome » (dépend du matériel, voir `hardware.md`)

| Fonctionnalité | Détail | État |
|---|---|---|
| Boutons physiques | Déclencheur à deux crans, deux molettes, joystick 5 directions, Q, MENU, LECTURE, DISP, LOUPE, Fn1/Fn2 (voir `hardware.md` §2). Liste d'achats chiffrée et câblage prêts (`hardware.md`) : chaque bouton produit la touche que l'application connaît, et elle lit déjà les molettes (`rotary-encoder`). Reste à acheter et câbler. | 🟨 |
| Menu rapide Q | Les 12 réglages de prise de vue, par-dessus la visée : simulation, grain, mesure, balance des blancs et température, peaking, netteté, contraste, saturation, format, mode de déclenchement, ouverture. La molette arrière choisit la ligne, la molette avant change la valeur, le déclencheur ferme et déclenche. Au clavier : Tab. Fait en avance avec M1. | ✅ |
| Menus | MENU : tous les réglages par section (exposition, mise au point, image, fichiers, prise de vue, objectif, affichage, boutons), avec défilement. Le clavier ne garde plus qu'une touche par commande de l'appareil (`architecture.md` §5) ; reste à brancher les vrais boutons (M2). Au clavier : Échap. | 🟨 |
| Lecture / galerie | Bouton LECTURE (touche P) : défilement à la molette arrière (de 10 en 10 à l'avant), loupe 100 % / 200 % déplaçable, infos tirées de l'EXIF (vitesse, ISO, ouverture, preset), histogramme, suppression en deux appuis de la prise entière, MENU pour le menu de la prise (Partager). Les vidéos des timelapses aussi : première image, puis lecture par le décodeur matériel (LOUPE : lecture, pause, reprise). Le déclencheur ramène à la visée et déclenche. | ✅ |
| Veille de l'écran | Pendant les longues prises (intervallomètre, timelapse, pose B, pose longue), écran éteint après 10 s, 30 s ou 1 min sans appui ; un appui le rallume sans rien faire d'autre. Un tiers de CPU en moins mesuré. Reste : une veille générale de l'appareil inactif, et l'extinction du rétroéclairage de l'écran définitif. | ✅ |
| Batterie | Niveau affiché, extinction propre sur batterie faible. | ⬜ |
| Horloge | RTC ; réglage de la date dans les menus. | ⬜ |
| Niveau électronique | Horizon et inclinaison par l'IMU ; orientation EXIF automatique. | ⬜ |
| Démarrage rapide | Moins de 12 s de l'allumage à la visée. | 🧪 |
| Robustesse | Racine en lecture seule et partition photos séparée. | ⬜ |
| Installation | `tools/install.sh` : sur un Raspberry Pi OS 64 bits (Trixie) neuf, une commande installe la dernière release (paquets Debian, `/opt/pycam`, services, mises à jour) ; l'appareil démarre ensuite sur la visée. | ✅ |
| Mises à jour | Releases construites et testées par GitHub Actions (moteur compilé pour le Pi), publiées par un tag. L'appareil cherche dès qu'il trouve Internet (pas sur un réseau facturé, au plus toutes les 6 h), télécharge, vérifie (SHA-256) et essaie la nouvelle version en arrière-plan, puis l'installe au démarrage suivant (automatiques), sur demande (proposées) ou jamais (désactivées) : MENU › Système, onglet Système du site. La nouvelle version est essayée avant que rien ne s'arrête (bibliothèques, modules), et l'appareil revient seul à la précédente si elle ne démarre pas (`architecture.md` §8.1). | ✅ |
| Interface web | Sur le réseau local, port 8080 (`web.py`) : visée rafraîchie chaque seconde, déclencheur, choix du preset et du grain ; photos (vignettes, téléchargement JPEG et DNG, suppression) ; presets (installer un .zip ou ses fichiers depuis l'ordinateur, supprimer) ; Cloud sync (compte, mode, envoi à la main). Refuse toute adresse hors réseau local, IPv6 publique comprise, et toute commande venue d'une autre page que la sienne (une page malveillante ouverte sur le même réseau) ou d'un nom d'hôte étranger. Réglage « Interface web » dans MENU. Pas encore de mot de passe. | ✅ |

## M3 — « Avancé »

| Fonctionnalité | Détail | État |
|---|---|---|
| Modes de déclenchement | Vue par vue, rafale (déclencheur tenu, 20 vues au plus), retardateur 2 s / 10 s (gros compte à rebours), intervallomètre (2 s à 5 min, 10 à 250 vues ou jusqu'à l'arrêt), bracketing d'exposition (3 ou 5 vues, ±⅓, ±⅔ ou ±1 IL, exposition et balance figées). Dans Q et MENU › Prise de vue, ou sur Fn ; le déclencheur arrête une séquence. Badge en haut à gauche, progression en orange ; visible aussi dans l'interface web. Mesuré : en bascule, en RAW + JPEG avec un look, la rafale prend ~1 vue/s jusqu'à la file d'écriture pleine (3 vues), puis suit l'écriture (~5 s/vue). | ✅ |
| Timelapse | Mode de déclenchement : une vue toutes les 1 à 60 s, 100 à 1 500 vues ou jusqu'à l'arrêt, dans un dossier à elles (JPEG 3 Mpx par défaut, ou photos complètes), puis une vidéo MP4 1080p 16:9 ou 4:3 à 24, 25 ou 30 i/s, assemblée par ffmpeg avec l'encodeur H.264 matériel et lissée contre le scintillement. Progression à l'écran et dans l'interface web, qui lit et télécharge les vidéos. Mesuré : 100 vues à 1,00 s d'écart, vidéo prête 14 s après. La lecture de l'appareil lit la vidéo, et l'écran se met en veille pendant la prise. | ✅ |
| Empilement | Mode de déclenchement : 4 à 128 vues ou jusqu'à l'arrêt, moyennées (moins de bruit, pose longue simulée) ou au maximum (traînées de lumière ou d'étoiles), combinées en RAW par le moteur ; exposition et balance figées, déclencheur pour arrêter. Mesuré : la moyenne de 8 vues divise le bruit par 2,96 (√8 = 2,83), 8 vues à 1/15 s en 1,9 s. Les vues de moins d'une seconde laissent de petits trous entre elles (~5,6 vues/s à 1/15 s) : pour des traînées continues, des poses d'une seconde ou plus. Pas d'alignement des vues (sur pied). | ✅ |
| Pose longue / pose B | En M jusqu'à 10'40" (le capteur va à 674 s), S jusqu'à 1 s. Visée fluide : la pose est simulée par le gain, ou exposée en automatique (« VISÉE AUTO ») au-delà ; le posemètre de M se calcule sur les réglages. Temps restant en gros pendant la pose, que le déclencheur annule. **B** après 10'40" : déclencheur tenu, ou un appui puis un autre (MENU › Prise de vue, et toujours depuis le web), jusqu'à 30 min. Elle additionne des poses de 1 s sans trou : DNG somme exacte des RAW, JPEG développé par le moteur (couleurs à 0–3 % de l'ISP), puis le preset. Pas de débruitage ni de netteté sur ce JPEG. | ✅ |
| ZSL | Déclenchement instantané, en option (compromis avec la fluidité de visée) : ligne « Réactivité » de MENU, ou Fn. Fait en avance avec M0. | ✅ |
| Simulations de pellicules | Presets ouverts (recette TOML, LUT `.cube` ou Hald), compilés en une LUT 3D avec grain et vignettage, appliqués au JPEG ; DNG neutre. Fait : base fixe, correction d'ombrage à part, moteur (LUT, grain, vignettage, « garder la base »), presets Standard et Base, menus, EXIF, aperçu en visée et histogramme corrigé, outil `tools/pycam_preset.py`, premières versions des pellicules (Portrait 400, Velours 50, Cinéma 800T, Cinéma 800T +2, Triple-X 400). Halation des CineStill et balance « Pellicule » (tungstène, lumière du jour). Reste : les affiner sur de vraies photos, halation en visée, méthode B. Conception : `presets.md`. | 🟨 |
| DNG compressé | LJ92 sans perte, au format Adobe (2 composantes, prédicteur 1) : 12,1 Mo au lieu de 24,8 ; relu au bit près par LibRaw et rendu par darktable. Activé par défaut, ligne « RAW compressé » du menu. Fait en avance avec M1. | ✅ |
| Vidéo | Mode vidéo d'appoint : 1080p à 24, 25 ou 30 i/s, 720p jusqu'à 50 i/s, H.264 par l'encodeur matériel (zéro copie, ~7 % de CPU), en MP4 sans son ; qualité (débit) et ISO réglables, exposition automatique avec correction, ou vitesse à 180° (flou de bougé du cinéma), exposition verrouillable (S : AE-L) ; arrêt propre avant la carte pleine (sinon le fichier serait perdu) ; date de prise dans le MP4 ; repères de cadrage 9:16, 4:5, 1:1, 2.39:1 ; image d'une vidéo extraite en photo depuis la lecture ; ralenti ×2 en 720p (50 i/s lus à 25) ; contraste, saturation et noir et blanc (preset N&B) par l'ISP ; écran 16:9, « ● REC » et temps restant ; vidéos dans la lecture et l'interface web. Pas de look complet sur la vidéo (le contraste, la saturation et le noir et blanc seulement), pas de son (pas de micro). | ✅ |
| Applications et Cloud sync | MENU › Applications ; première application, **Cloud sync** : envoi de la carte vers ownCloud / Nextcloud, Dropbox ou Google Drive à chaque connexion à Internet puis après chaque prise. Trois modes : synchroniser en gardant tout, déverser en libérant la carte (une prise n'est effacée qu'une fois tous ses fichiers vérifiés dans le cloud), ou manuel. Jamais tout seul sur un réseau facturé ni pendant une prise qui dure ; jamais par-dessus un autre fichier. Comptes connectés depuis la page web, avec l'app ou le client OAuth de l'utilisateur. Mesuré : 1,1 Go en 163 s vers un serveur local, à l'octet près. Reste : l'essayer sur de vrais comptes. | 🟨 |
| Partage | Application Partage, inspirée de SPP : publier une photo sur Instagram, Flickr et Bluesky depuis la lecture (MENU › Partager) ou la galerie web, avec titre, description, texte alternatif et hashtags, et l'aperçu de ce que publiera chaque plateforme (image, légende coupée à sa limite). Légende tirée de l'EXIF (appareil, objectif, date, le preset comme pellicule). File d'attente jusqu'à la connexion, nouvel essai en cas d'échec, une plateforme ne bloquant jamais les autres. Testé contre de faux services ; reste : de vrais comptes, et installer instagrapi sur le Pi. | 🟨 |
| Point d'accès Wi-Fi | Pour le terrain, sans box. | ⬜ |
| GPS | Géomarquage, en option. | ⬜ |
| Données gyro | Export des données gyroscopiques pour la stabilisation vidéo (Gyroflow). | ⬜ |
