# Matériel

> Les références ci-dessous sont des **pistes à valider** avant achat : disponibilité, câble, compatibilité. Les consommations sont des **estimations** à mesurer.

## État actuel (audit du 2026-10-11)

| Élément | Détail |
|---|---|
| Carte | Raspberry Pi 3 Model B Rev 1.2 : 1 Go (905 Mo visibles), HDMI pleine taille, CSI 15 broches, GPIO 40 broches, micro-USB 5 V |
| OS | Raspberry Pi OS Trixie (Debian 13) 64 bits, noyau 6.18, `vc4-kms-v3d`, CMA 256 Mo |
| Capteur | High Quality Camera, Sony IMX477, monture C/CS, détectée par libcamera 0.7.2 |
| Objectif | *à préciser* |
| Écran de développement | HDMI 1920×1080 branché (bureau graphique actif) |

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
| 5 | **Q** (menu rapide) | Bouton tactile | Grille des ~12 réglages les plus utilisés ; on navigue au joystick et on change la valeur à la molette. |
| 6 | **MENU** | Bouton tactile | Ouvrir le menu complet / revenir en arrière. |
| 7 | **LECTURE** | Bouton tactile | Galerie. |
| 8 | **DISP** | Bouton tactile | Faire défiler les affichages : visée nature → détaillée → histogramme + niveau. |
| 9 | **LOUPE** | Bouton tactile | Loupe ×1 → ×5 → ×10 pour la mise au point manuelle. |
| 10 | **Fn1, Fn2** | Boutons tactiles | Assignables : peaking, zébras, balance des blancs, mode de déclenchement… |

La logique d'usage reprend celle d'un hybride expert (Fujifilm/Sony) : **deux molettes pour l'exposition, un bouton Q pour tout le reste**. Le menu complet ne sert presque qu'à la configuration.

Tout passe par des **overlays noyau** déclarés dans `config.txt` : `gpio-keys` pour les boutons et `rotary-encoder` pour les molettes. Le noyau gère l'anti-rebond, et les commandes arrivent comme des évènements clavier. Pendant le développement, un **clavier USB** remplace donc l'ensemble.

Si le câblage de 20 entrées devient pénible, on peut passer les boutons « lents » (Q, MENU, LECTURE, DISP, Fn) sur un expandeur I2C **MCP23017** (16 entrées, géré par le noyau). On garde le déclencheur et les molettes sur le GPIO natif pour la réactivité.

## 3. Alimentation + horloge (priorité haute pour l'usage nomade)

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

**Piste : LSM6DSOX** (accéléromètre + gyroscope, I2C en 0x6A/0x6B, carte Adafruit STEMMA QT / Qwiic). Elle sert à :

- le niveau électronique (horizon) ;
- l'orientation automatique (EXIF) ;
- la détection « appareil posé » pour la mise en veille de l'écran ;
- plus tard, les données gyroscopiques pour Gyroflow.

## 5. Autres (plus tard)

- **Carte SD** rapide (A2 / V30). Sur Pi 3B, le bus SD plafonne vers ~20 Mo/s, donc une carte haut de gamme n'apportera pas plus, mais une mauvaise carte coûte cher.
- **Dissipateur**. Le Pi 3B réduit sa fréquence vers 80 °C, et un boîtier fermé avec la visée continue peut l'y amener : à mesurer avec `vcgencmd measure_temp`. Au repos, on mesure 38,6 °C.
- **GPS** UART (u-blox) pour le géomarquage, en option.
- **Boîtier** imprimé en 3D. Les CAO de CinePI V2 peuvent servir d'inspiration, et on prévoit des filetages 1/4″.

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
| GPS TX / RX (option) | 14 / 15 | 8 / 10 |
| Molette avant A / B / appui | 17 / 27 / 22 | 11 / 13 / 15 |
| DISP | 18 | 12 |
| MENU / LECTURE | 20 / 21 | 38 / 40 |
| Molette arrière A / B / appui | 23 / 24 / 25 | 16 / 18 / 22 |
| *Libres* | 10, 11 | 19, 23 |

Au total : 20 entrées, le bus I2C et l'UART optionnel. Les GPIO 7–11 sont ceux du SPI0 ; on les utilise comme entrées simples, ce qui suppose de garder le SPI désactivé (c'est le cas par défaut). Les GPIO 0–1 (EEPROM ID) sont réservés.

## Et si le Pi 3B devient le facteur limitant ?

L'architecture ne dépend pas du modèle de Pi : `librpicam_app` gère aussi le Pi 4 (même ISP VC4, CPU ~3× plus rapide, jusqu'à 8 Go) et le Pi 5 (nouvel ISP PiSP, beaucoup plus rapide). Si la latence de déclenchement ou la fluidité ne suffisent pas, changer de carte sera le levier le plus simple. Le code ne devrait demander que des ajustements de réglages.
