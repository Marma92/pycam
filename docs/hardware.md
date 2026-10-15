# Matériel

> Les références ci-dessous sont des **pistes à valider** avant achat : disponibilité, câble, compatibilité. Les consommations sont des **estimations** à mesurer.

## État actuel (audit du 2026-10-11)

| Élément | Détail |
|---|---|
| Carte | Raspberry Pi 3 Model B Rev 1.2 : 1 Go (905 Mo visibles), HDMI pleine taille, CSI 15 broches, GPIO 40 broches, micro-USB 5 V |
| OS | Raspberry Pi OS Trixie (Debian 13) 64 bits, noyau 6.18, `vc4-kms-v3d`, CMA 256 Mo |
| Capteur | High Quality Camera, Sony IMX477, monture C/CS, détectée par libcamera 0.7.2 |
| Objectif | *à préciser* |
| Écran de développement | HDMI 1920×1080 branché ; démarrage en console depuis le 2026-10-11 (`multi-user.target`), l'appareil photo prend l'écran |
| Commandes | Un clavier USB, une touche par commande de l'appareil (README, « Au clavier ») |
| Déjà en main | Un gyroscope Adafruit (modèle à préciser, voir §4) |

## Choix retenus

| Sujet | Choix | Date |
|---|---|---|
| Écran | **HDMI, sans tactile** | 2026-10-11 |
| Navigation | **Entièrement aux boutons et molettes** : il faut assez de commandes physiques pour régler et naviguer dans les menus sans écran tactile | 2026-10-11 |

## 1. Écran HDMI (choisi)

**Pourquoi c'est un bon choix pour l'architecture.**

- La sortie HDMI du Pi 3B passe par le même compositeur matériel (HVS du VC4) que le DSI. On garde la superposition visée + peaking + HUD sans coût CPU (`architecture.md` §4).
- Aucune broche GPIO n'est utilisée : elles restent toutes disponibles pour les commandes.
- C'est déjà la configuration de développement actuelle.

**Critères de sélection d'un écran pour l'appareil**

| Critère | Cible | Pourquoi |
|---|---|---|
| Diagonale | 4″ à 5″ | Assez grand pour juger la netteté à la loupe, assez petit pour le boîtier. |
| Résolution | 800×480 (ou 1024×600) | Le HUD est rendu en Python : moins de pixels, c'est un rendu plus rapide. La visée est mise à l'échelle par le matériel. |
| Dalle | **IPS** | Angles de vue : on vise souvent à bout de bras ou en contre-plongée. |
| Luminosité | **≥ 800 cd/m²** si possible | Lisibilité en extérieur. C'est le point faible de la plupart des petits écrans « pour Pi ». |
| Rétroéclairage | Réglable (PWM) | Autonomie et confort de nuit. |
| Alimentation | 5 V par USB ou par fil | Ne pas prendre un écran en forme de « HAT » qui recouvre le connecteur GPIO. |
| Connectique | Câble HDMI plat (FPC), coudé | La prise HDMI pleine taille du Pi 3B est encombrante ; une nappe permet de replier l'écran dans le boîtier. |
| EDID | Fourni par l'écran | Sinon, il faut forcer le mode : `video=HDMI-A-1:800x480@60D` dans `cmdline.txt`. |

**Pistes**

| Modèle | Caractéristiques | Remarque |
|---|---|---|
| **Newhaven NHD-5.0-HDMI-N-RSXP** | 5″, 800×480, IPS, sans tactile, **~1100 cd/m²**, rétroéclairage PWM | Lisible en plein soleil ; le meilleur candidat trouvé. Ajustement possible de `config.txt`. |
| Adafruit HDMI 4 Pi 5″ (réf. 1678) | 5″, 800×480, sans tactile, menu OSD | Dalle **non IPS** (angles étroits) ; rupture de stock constatée. |
| Écrans 5″ HDMI génériques (souvent tactiles) | 800×480 / 1024×600 | On peut ignorer la dalle tactile (simplement ne pas brancher son USB). Vérifier IPS et luminosité. |

Plus tard, un **viseur électronique** (micro-écran HDMI + oculaire) pourra remplacer ou compléter l'écran, pour viser à l'œil en plein soleil. Le logiciel n'aura pas à changer.

## 2. Commandes physiques (sans tactile)

Comme il n'y a pas de tactile, les commandes doivent couvrir trois choses : **les réglages de prise de vue sans quitter la visée**, **la navigation rapide dans les menus**, et **la lecture**.

| # | Commande | Composant possible | Rôle principal |
|---|---|---|---|
| 1 | **Déclencheur 2 crans** | Interrupteur deux étages type ALPS SPPH4 | Mi-course : mémorisation AE / sortie de menu ou de loupe. Fond : déclenchement. |
| 2 | **Molette avant** (avec appui) | Encodeur EC11 ou Bourns PEC11R | Vitesse (S/M) ou correction d'exposition (P/A). Appui : vitesse auto ⇄ manuelle. En menu : changer la valeur. |
| 3 | **Molette arrière** (avec appui) | Encodeur EC11 ou PEC11R | ISO (A/M) ou correction d'exposition (P/S). Appui : ISO auto ⇄ manuel. En menu : faire défiler. En lecture : image suivante/précédente. |
| 4 | **Joystick 5 directions** | Commutateur de navigation 5 voies (type ALPS SKRH) | Menus : naviguer, valider (appui central). Visée : déplacer la loupe ou le point de mesure spot. |
| 5 | **Q** (menu rapide) | Bouton tactile | Les 12 réglages de prise de vue, en liste par-dessus la visée : la molette arrière choisit la ligne, la molette avant change la valeur (le joystick, plus tard). |
| 6 | **MENU** | Bouton tactile | Ouvrir le menu complet / revenir en arrière. |
| 7 | **LECTURE** | Bouton tactile | Galerie. |
| 8 | **DISP** | Bouton tactile | Faire défiler les affichages : détaillé → simple → nature (le niveau électronique s'y ajoutera avec l'IMU). |
| 9 | **LOUPE** | Bouton tactile | Loupe ×5 → ×10 → coupée pour la mise au point manuelle ; en lecture, 100 % → 200 %, et lecture / pause d'une vidéo. |
| 10 | **Fn1, Fn2** | Boutons tactiles | Assignables : peaking, zébras, balance des blancs, mode de déclenchement, photo / vidéo… |

La logique d'usage reprend celle d'un hybride expert (Fujifilm/Sony) : **deux molettes pour l'exposition, un bouton Q pour tout le reste**. Le menu complet ne sert presque qu'à la configuration.

Tout passe par des **overlays noyau** déclarés dans `config.txt` : `gpio-keys` pour les boutons et `rotary-encoder` pour les molettes. Le noyau gère l'anti-rebond, et les commandes arrivent comme des évènements clavier. Pendant le développement, un **clavier USB** remplace donc l'ensemble.

Si le câblage de 20 entrées devient pénible, on peut passer les boutons « lents » (Q, MENU, LECTURE, DISP, Fn) sur un expandeur I2C **MCP23017** (16 entrées, géré par le noyau). On garde le déclencheur et les molettes sur le GPIO natif pour la réactivité.

## 3. Alimentation + horloge (priorité haute pour l'usage nomade)

L'heure vient déjà du réseau (NTP), ou à défaut du téléphone qui ouvre le site de l'appareil (`clock.py`, 2026-10-13). Ce qui manque, c'est de la **garder entre deux démarrages sans réseau** : c'est le rôle de la RTC ci-dessous. Le GPS (§5) donnera aussi l'heure, sur le terrain, sans réseau ni téléphone.

**Recommandé : PiSugar 3 Plus**, conçu pour les Pi 3B/3B+/4B.

- Batterie de 5000 mAh.
- Contacts au dos : le GPIO reste libre.
- **RTC intégrée.**
- Niveau de batterie lisible en I2C.
- Bouton programmable et notification d'extinction propre.
- Point d'attention : son adresse I2C est en 0x57 et **0x68**. Il faut éviter un autre composant en 0x68 (DS3231, MPU-6050), ou changer l'adresse si c'est possible.

L'alternative est un HAT UPS à accus 18650 plus une RTC DS3231 séparée. C'est plus encombrant et plus de câblage.

**Budget énergétique (estimé, à mesurer)**

| Poste | Puissance |
|---|---|
| Pi 3B, visée active (ISP + 1 cœur pour l'analyse) | ~2,5–3,5 W |
| HQ Camera en fonctionnement | ~0,5–1 W |
| Écran HDMI 5″ (selon la luminosité : 1100 cd/m² au maximum consomme nettement plus) | ~1,5–3 W |
| **Total en visée** | **~4,5–7 W** |

Avec 5000 mAh × 3,7 V × ~85 % de rendement ≈ 15,7 Wh, on peut compter **~2,5 à 3 h de visée continue**. Un usage réel « allumé à la demande » dure bien plus. Le démarrage rapide (`architecture.md` §8) et la mise en veille de l'écran comptent donc beaucoup.

L'écran se branche sur un port USB du Pi. Les ports USB du Pi 3B partagent une limite de 1,2 A : un écran très lumineux peut l'approcher. Une alimentation 5 V directe de l'écran depuis la batterie sera peut-être préférable.

## 4. Centrale inertielle / IMU (priorité moyenne)

**Déjà en main : un gyroscope Adafruit** (2026-10-15), modèle à préciser. Avant de le brancher, vérifier son **adresse I2C** : 0x68 entre en conflit avec la RTC du PiSugar (MPU-6050, ICM-20948 ; leur broche AD0 passe en 0x69), alors que les LSM6DS… (0x6A/0x6B), BNO055 (0x28/0x29) ou BNO085 (0x4A/0x4B) n'en ont pas. Pour Gyroflow, il doit être **fixé rigidement**, aligné sur les axes du capteur.

À défaut, la **piste : LSM6DSOX** (accéléromètre + gyroscope, I2C en 0x6A/0x6B, carte Adafruit STEMMA QT / Qwiic). L'IMU sert à :

- le niveau électronique (horizon) ;
- l'orientation automatique (EXIF) ;
- la détection « appareil posé » pour la mise en veille de l'écran ;
- plus tard, les données gyroscopiques pour Gyroflow.

## 5. GPS (priorité relevée le 2026-10-15)

Pour le géomarquage des photos, et l'heure sur le terrain.

- **Liaison série** sur GPIO 14 / 15 (broches 8 / 10). Sur le Pi 3B, le bon UART (PL011) sert au Bluetooth, et celui qui reste (mini-UART) dérive avec la fréquence du processeur : `dtoverlay=disable-bt` dans `config.txt` rend le PL011 au GPIO (le Bluetooth ne sert pas à l'appareil).
- **PPS** (une impulsion par seconde) sur un GPIO libre (10 ou 11), avec l'overlay `pps-gpio` : l'heure à la microseconde, pour `chrony`.
- **Logiciel** : `gpsd` lit le module ; l'application inscrira la position dans l'EXIF de chaque photo (à écrire), et l'heure du GPS quand ni le réseau ni le téléphone ne la donnent.
- **Antenne** : vers le ciel, sur le capot, loin du Pi (son Wi-Fi, le HDMI et la carte SD rayonnent) ; un module avec prise u.FL permet une antenne active déportée.
- Module : un u-blox (M8, M9 ou M10) ou un Adafruit Ultimate GPS, **avec sortie PPS**.

## 6. Autres (plus tard)

- **Carte SD** rapide (A2 / V30). Sur Pi 3B, le bus SD plafonne vers ~20 Mo/s, donc une carte haut de gamme n'apportera pas plus, mais une mauvaise carte coûte cher.
- **Dissipateur**. Le Pi 3B réduit sa fréquence vers 80 °C, et un boîtier fermé avec la visée continue peut l'y amener : à mesurer avec `vcgencmd measure_temp`. Au repos, on mesure 38,6 °C.
- **Boîtier** imprimé en 3D. Les CAO de CinePI V2 peuvent servir d'inspiration, et on prévoit des filetages 1/4″.

## Liste d'achats (chiffrée le 2026-10-12)

Prix relevés chez les fabricants et distributeurs, à l'unité, hors port et TVA : ils changent souvent, **à vérifier au moment de commander**. Fournisseurs pratiques depuis la France : Mouser, Digi-Key, RS (pro, port offert au-delà d'un seuil), Kubii (Raspberry Pi), The Pi Hut (Royaume-Uni : droits de douane), LCSC ou AliExpress (moins cher, plus lent).

| Poste | Référence | Qté | Prix relevé | Remarque |
|---|---|---|---|---|
| **Écran** (au choix) | Newhaven **NHD-5.0-HDMI-N-RSXP** (5″, 800×480, IPS, ~1100 cd/m², sans tactile) | 1 | ~128 $ | Le seul lisible en plein soleil trouvé. Vérifier le stock de la version **sans** « -CTU » (tactile). |
| | ou Waveshare 5″ HDMI LCD (B) ou (H), 800×480 | 1 | 44 à 53 $ | Bien moins cher, mais luminosité non publiée (sans doute ~300–500 cd/m²) : juste en plein soleil. Le tactile s'ignore. |
| Nappe HDMI plate | Kit « DIY HDMI » Adafruit : nappe 20 cm + 2 adaptateurs (HDMI coudé côté Pi, selon l'écran de l'autre côté) | 1 | ~4 $ la nappe, adaptateurs en plus | Pour replier l'écran dans le boîtier. |
| **Batterie + horloge** | **PiSugar 3 Plus**, 5000 mAh, RTC, niveau en I2C | 1 | 49,99 $ | Annoncé « 3B/3B+/4B » sur Tindie, mais la page officielle cite 3B+ et 3A+ : **confirmer le Pi 3B** avant d'acheter. Adresses I2C 0x57 et 0x68. |
| **Molettes** | Bourns **PEC11R-4220F-S0024** (24 crans, avec appui) | 2 | ~2,2 $ | Ou des EC11 génériques (~1 €). Plus deux boutons de molette. |
| **Joystick 5 voies** | ALPS **SKRHABE010** | 1 | ~2,6 $ (LCSC ~0,8 $) | Composant CMS 7×7 mm : le prendre **sur une petite carte de connexion**, ou prévoir un adaptateur. |
| **Déclencheur 2 crans** | ALPS **SPPH410200** (momentané) | 1 | non trouvé en stock | Introuvable chez les distributeurs consultés : boutique d'ALPS, ou un **poussoir simple** pour le premier prototype (la mi-course n'est pas encore utilisée). |
| Boutons | Poussoirs tactiles 6×6 mm (type Omron B3F) | 8 | ~0,3 $ | Q, MENU, LECTURE, DISP, LOUPE, Fn1, Fn2, un de rechange. |
| **Centrale inertielle** | Le gyroscope Adafruit déjà en main ; sinon Adafruit **LSM6DSOX** (STEMMA QT / Qwiic), réf. 4438 | 1 | — (11,95 $) | Vérifier l'adresse I2C du sien (0x68 : conflit avec le PiSugar). Plus un câble STEMMA QT vers broches (~1 $). |
| **GPS** | Module u-blox (M8 à M10) ou Adafruit Ultimate GPS, avec sortie PPS | 1 | à chiffrer | UART + PPS, antenne vers le ciel (§5). |
| Divers | Plaque à pastilles, fils, connecteurs JST, dissipateur pour le Pi | — | ~10 $ | Prototype avant le boîtier imprimé. |

**Budget** : ~225 $ avec l'écran Newhaven, ~145 $ avec un Waveshare, hors port, TVA et boîtier.

Sources : [Newhaven chez Mouser](https://www.mouser.com/ProductDetail/Newhaven-Display/NHD-5.0-HDMI-N-RSXP-CTU?qs=IPgv5n7u5QbxjiYXvPRX3A%3D%3D), [chez Digi-Key](https://www.digikey.com/en/products/detail/newhaven-display-intl/NHD-5-0-HDMI-N-RSXP-CTU/16704913), [Waveshare 5″ (H)](https://www.waveshare.com/5inch-hdmi-lcd-h.htm), [Waveshare 5″ (B)](https://www.waveshare.com/5inch-hdmi-lcd-b.htm), [PiSugar 3 Plus](https://www.pisugar.com/products/pisugar-3-plus-raspberry-pi-ups), [sur Tindie](https://www.tindie.com/products/pisugar/pisugar-3-plus-battery-for-raspberry-pi-3b3b4b/), [ALPS SKRHABE010 chez Mouser](https://www.mouser.com/ProductDetail/Alps-Alpine/SKRHABE010?qs=seHrhfPpLDxEOwRqoDvYHw%3D%3D), [chez LCSC](https://lcsc.com/product-detail/5-way-Tactile-Switches_ALPS-Electric_SKRHABE010_ALPS-Electric-SKRHABE010_C139794.html), [ALPS SPPH4](https://tech.alpsalpine.com/prod/e/html/switch/push/spph4/spph4_list.html), [Bourns PEC11R chez RS](https://fr.rs-online.com/web/p/encodeurs-rotatifs-mecaniques/1674660), [Adafruit LSM6DSOX](https://www.adafruit.com/products/4438), [nappe HDMI Adafruit](https://www.jameco.com/z/3561-Adafruit-Industries-Diy-USB-or-HDMI-Cable-Parts-20-cm-Ribbon-Cable_2514111.html).

## Câblage : du bouton à la touche

Chaque bouton est déclaré dans `/boot/firmware/config.txt` avec l'overlay `gpio-key`, qui lui fait produire **le code de la touche que l'application connaît déjà** (`app/pycam/inputs.py`) : rien d'autre à changer pour les boutons. Les molettes passent par l'overlay `rotary-encoder`, en axes relatifs, que l'application lit aussi.

```ini
# Boutons : active_low=1 (le bouton relie à la masse), gpio_pull=2 (tirage interne vers le haut)
dtoverlay=gpio-key,gpio=6,keycode=57,label="declencheur",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=4,keycode=15,label="q",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=20,keycode=1,label="menu",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=21,keycode=25,label="lecture",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=18,keycode=32,label="disp",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=7,keycode=38,label="loupe",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=8,keycode=2,label="fn1",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=9,keycode=3,label="fn2",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=22,keycode=31,label="appui-avant",active_low=1,gpio_pull=2
dtoverlay=gpio-key,gpio=25,keycode=23,label="appui-arriere",active_low=1,gpio_pull=2
# Molettes : un axe relatif chacune (0 : REL_X, avant ; 1 : REL_Y, arrière)
dtoverlay=rotary-encoder,pin_a=17,pin_b=27,relative_axis=1,linux_axis=0,steps-per-period=4
dtoverlay=rotary-encoder,pin_a=23,pin_b=24,relative_axis=1,linux_axis=1,steps-per-period=4
```

| Commande | GPIO | Code de touche | Action dans l'application |
|---|---|---|---|
| Déclencheur (fond) | 6 | 57 `KEY_SPACE` | déclencher |
| Q / MENU / LECTURE | 4 / 20 / 21 | 15 `KEY_TAB` / 1 `KEY_ESC` / 25 `KEY_P` | menus, lecture |
| DISP / LOUPE | 18 / 7 | 32 `KEY_D` / 38 `KEY_L` | affichage, loupe |
| Fn1 / Fn2 | 8 / 9 | 2 `KEY_1` / 3 `KEY_2` | fonctions attribuées |
| Appui molette avant / arrière | 22 / 25 | 31 `KEY_S` / 23 `KEY_I` | vitesse / ISO auto ⇄ manuel |
| Molette avant / arrière | 17-27 / 23-24 | axe `REL_X` / `REL_Y` | comme ← → / ↑ ↓ |

- **Le joystick** et **la mi-course** du déclencheur (GPIO 12, 13, 16, 19, 26 et 5) seront branchés plus tard : l'application ne s'en sert pas encore. Le joystick produira les codes du pavé numérique (8, 2, 4, 6 et 5), pour ne pas se confondre avec les molettes.
- **À vérifier au montage** : le sens de chaque molette (inverser `pin_a` et `pin_b` au besoin) et `steps-per-period`, selon l'encodeur (un cran = un pas).
- Un clavier USB branché en même temps continue de fonctionner, ce qui aide pendant le montage.

## Plan de brochage GPIO (brouillon)

L'écran HDMI ne prend aucune broche. Toutes les entrées sont actives à l'état bas, avec pull-up interne.

| Fonction | GPIO (BCM) | Broche physique |
|---|---|---|
| I2C SDA / SCL (PiSugar : RTC + batterie, IMU) | 2 / 3 | 3 / 5 |
| Q | 4 | 7 |
| Déclencheur mi-course / fond | 5 / 6 | 29 / 31 |
| LOUPE | 7 | 26 |
| Fn1 / Fn2 | 8 / 9 | 24 / 21 |
| Joystick haut / bas | 12 / 13 | 32 / 33 |
| Joystick gauche / droite | 16 / 19 | 36 / 35 |
| Joystick appui (OK) | 26 | 37 |
| GPS TX / RX | 14 / 15 | 8 / 10 |
| GPS PPS | 10 | 19 |
| Molette avant A / B / appui | 17 / 27 / 22 | 11 / 13 / 15 |
| DISP | 18 | 12 |
| MENU / LECTURE | 20 / 21 | 38 / 40 |
| Molette arrière A / B / appui | 23 / 24 / 25 | 16 / 18 / 22 |
| *Libre* | 11 | 23 |

Au total : 20 entrées, le bus I2C, l'UART et le PPS du GPS. Les GPIO 7–11 sont ceux du SPI0 ; on les utilise comme entrées simples, ce qui suppose de garder le SPI désactivé (c'est le cas par défaut). Les GPIO 0–1 (EEPROM ID) sont réservés.

## Et si le Pi 3B devient le facteur limitant ?

L'architecture ne dépend pas du modèle de Pi : `librpicam_app` gère aussi le Pi 4 (même ISP VC4, CPU ~3× plus rapide, jusqu'à 8 Go) et le Pi 5 (nouvel ISP PiSP, beaucoup plus rapide). Si la latence de déclenchement ou la fluidité ne suffisent pas, changer de carte sera le levier le plus simple. Le code ne devrait demander que des ajustements de réglages.
