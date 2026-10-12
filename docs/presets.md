# Presets et simulations de pellicules (réflexion)

Statut : **orientations décidées le 2026-10-11** (§10). Les mesures datent du même jour, sur le Pi 3B.

**Fait** (étapes 1 à 5 du §11) :
- la base fixe : `engine/tuning`, chargée dès le démarrage, et la correction d'ombrage en réglage séparé ;
- le moteur : LUT 3D, grain et vignettage sur le JPEG, option « garder la base » (`docs/protocol.md`, « look ») ;
- l'application (`app/pycam/looks.py`) : lecture des presets, compilation en LUT avec le contraste et la saturation du menu, mise en cache (`~/.cache/pycam/looks`), ligne « Simulation » des menus, nom court dans le bandeau, nom du preset dans l'EXIF (`Software`) ;
- deux presets intégrés : « Standard », le rendu par défaut (la courbe du tuning standard du Pi, sans son contraste adaptatif), et « Base (neutre) », la base telle quelle ;
- l'aperçu en visée (`docs/protocol.md`, « look ») : une courbe, un mélange et une matrice, ajustés sur la LUT ; l'histogramme en direct est corrigé par les courbes des gris du look ;
- l'outil `tools/pycam_preset.py`, pour créer et tester les presets sur un ordinateur (§5).

- les premières versions des pellicules (méthode A, §6) : Portrait 400, Velours 50, Cinéma 800T, Cinéma 800T +2 et Triple-X 400.

- le grain des pellicules : intensité, finesse, grain coloré, répartition selon les tons, amas ; réglage « Simuler le grain », avec la finesse affichée dans le menu.

- la balance du film (`[shooting] white_balance`) : 5500 K pour Portrait 400 et Velours 50, 3200 K pour les Cinéma 800T, appliquée avec la balance des blancs « Pellicule ».

**Reste** : affiner les pellicules sur de vraies photos de l'appareil ; la halation ; `pycam_preset.py fit` pour la méthode B.

**Ajouter un preset** : copier son dossier dans `~/.config/pycam/presets/`. Il apparaît à la prochaine ouverture d'un menu, et un preset du même nom de dossier qu'un preset intégré le remplace. Un preset illisible est ignoré, avec un avertissement dans le journal (`journalctl -u pycam`).

## 1. Objectifs

- Appliquer un preset aux JPEG, en JPEG seul comme en RAW + JPEG.
- Faire la part belle aux **simulations de pellicules**, et pouvoir créer nous-mêmes des presets proches de la colorimétrie de pellicules emblématiques.
- Un système **ouvert** : l'utilisateur crée ses propres presets sur un ordinateur, avec ses outils habituels, puis les copie sur l'appareil.

## 2. Ce que le matériel permet (mesuré)

**Contrôles de l'ISP réglables à chaud** : saturation, contraste, luminosité, netteté, et une **matrice couleur** (`ColourCorrectionMatrix`).

- La matrice n'est prise en compte **que si la balance des blancs automatique est coupée**. Vérifié avec une matrice qui échange rouge et bleu : ignorée avec AWB active, appliquée avec AWB coupée.
- La courbe de tons (gamma) n'est **pas** réglable à chaud. Elle est fixée par le fichier de tuning, et en changer impose de redémarrer la caméra.
- L'ISP ne sort que du **YUV 8 bits**.

**Les deux fichiers de tuning de l'IMX477** livrés avec libcamera :

| | `imx477.json` (actuel) | `imx477_scientific.json` |
|---|---|---|
| Contraste adaptatif (`ce_enable`) | **actif** : la courbe de tons change selon la scène | coupé : rendu identique d'une scène à l'autre |
| Courbe de tons | courbe « agréable », propre au Pi | exactement la courbe **Rec.709** (vérifié point par point), donc inversible |
| Matrices couleur | 6, de 2850 à 7600 K | 19, de 2000 à 8600 K |
| Correction d'ombrage de l'objectif (ALSC) | oui (calibrée pour un objectif) | non |
| Comparaison A/B, une même scène | dominante cyan (RVB 91/108/111) | plus neutre (RVB 96/92/98) |

**Coût d'une LUT 3D** (interpolation tétraédrique, code non optimisé, caméra en marche) :

| Image | 1 fil | 4 fils | Grain en plus |
|---|---|---|---|
| Photo 12 Mpx | 2,1 s | **0,63 s** | +0,06 s |
| Visée 1024×768 | 125 ms | 33 ms | +2 ms |

- La taille de la LUT (17³ ou 33³) ne change presque rien au coût : on prend 33³.
- Pour la photo, 0,6 s en arrière-plan dans le fil d'écriture est acceptable : le DNG compressé en prend déjà 1,5.
- Pour la visée, la LUT complète à 30 i/s occuperait les 4 cœurs : **impossible tel quel**.

## 3. Principe proposé : une base fixe, et un look = une LUT 3D

```
capteur ─► ISP (tuning fixe : balance, matrice, courbe Rec.709) ─► YUV « base pycam 1 »
                                                                    │
            ┌───────────────────────────────────────────────────────┤
            ▼                                                       ▼
   photo : LUT 3D + grain + vignettage ─► JPEG         visée : aperçu approché (§7)
   DNG : inchangé, c'est le négatif
```

1. **Une base fixe et documentée**, « base pycam 1 » : primaires Rec.709, courbe Rec.709, pleine plage, sans contraste adaptatif. C'est le contrat entre l'appareil et les presets. Un preset écrit pour cette base donne le même résultat sur l'appareil et sur un ordinateur.
2. **Tout preset se compile en une seule LUT 3D** (33³), plus des paramètres de grain et de vignettage, et quelques réglages de prise de vue. Ça marche que le preset vienne d'un fichier LUT, d'une recette de paramètres, ou des deux.
3. **Le moteur reste « bête »** (architecture §1) : il applique une LUT et du grain dans le fil d'écriture, avant l'encodage JPEG. C'est l'application Python qui lit et compile les presets.
4. **Le rendu « Standard » devient lui aussi un preset**, une LUT qui donne un rendu agréable à partir de la base. Il n'y a ainsi qu'un seul chemin.

**Conséquence : la base doit être la même pour tout l'appareil.** Changer de tuning impose de redémarrer la caméra, ce n'est donc pas possible preset par preset. Je propose un fichier de tuning à nous, versionné dans le dépôt (`engine/tuning/pycam-imx477.json`) :

- partir de `imx477_scientific.json` ;
- la **correction d'ombrage de l'objectif** (ALSC) est un réglage à part, indépendant du preset (décidé). Il y aura donc deux variantes du tuning, avec et sans l'ALSC du tuning standard. Elles forment la même base : l'ALSC corrige les coins de l'image (vignettage et dérive de couleur), pas la courbe ni les couleurs, donc les presets restent valables. Changer ce réglage redémarre la caméra (quelques secondes, à mesurer). Plus tard : une calibration par objectif, rangée dans son profil ;
- le moteur le charge avec `--tuning-file` ;
- toute modification de ce fichier change le numéro de la base. Les presets déclarent la base pour laquelle ils ont été écrits.

**Netteté, contraste, saturation** : avec une base fixe, le contraste et la saturation de l'ISP restent neutres. Les réglages du menu deviennent des ajustements compilés dans la LUT, après le look, comme les réglages de tons et de couleur d'un boîtier Fujifilm par-dessus sa simulation. La netteté et la réduction du bruit restent dans l'ISP.

## 4. Le format de preset

Un preset est un dossier, que l'on copie dans `~/.config/pycam/presets/` (carte SD, `scp`, et plus tard l'interface web prévue) :

```
portra-400/
├── preset.toml     # obligatoire : la recette
├── look.cube       # facultatif : une LUT 3D (.cube), ou bien
└── look.png        # une image Hald CLUT (voir §5)
```

TOML plutôt que JSON : l'appareil n'a qu'à le lire (`tomllib`, bibliothèque standard), et un humain l'écrit plus facilement, commentaires compris. Exemple :

```toml
format = 1
base = "pycam-1"                 # la base pour laquelle le preset est écrit
name = "Portrait 400"            # affiché dans le menu
short = "P400"                   # affiché dans le bandeau
author = "…"

[shooting]                       # réglages de prise de vue (facultatifs)
white_balance = 5500             # la balance du film, en Kelvin (3200 : tungstène), ou "auto" ;
                                 # appliquée quand la balance des blancs est sur « Pellicule »
exposure_bias = -0.3             # protège les hautes lumières ; la courbe relève les tons moyens
monochrome = false

[look]                           # tout ce qui suit est compilé en une seule LUT 3D
lut = "look.cube"                # facultatif ; appliqué en premier
strength = 1.0                   # mélange avec la base
tone_curve = [[0.0, 0.03], [0.25, 0.23], [0.5, 0.52], [0.75, 0.79], [1.0, 0.95]]  # noirs relevés, épaule douce
saturation = 0.92

[look.colours]                   # par famille de couleurs : [teinte ±60°, saturation ×, luminosité ±1]
green = [-8, 0.85, 0.03]         # les verts vers le jaune, moins saturés, un peu plus clairs
blue = [-12, 0.85, 0.05]         # les ciels vers le cyan
split_toning = { shadows = [190, 0.04], highlights = [45, 0.03] }  # teinte, intensité

[grain]                          # le grain de la pellicule (réglage « Simuler le grain »)
amount = 0.25                    # intensité, 0-1
size = 1.2                       # finesse : taille en pixels de l'image pleine résolution
colour = 0.35                    # grain coloré (les trois couches d'un film couleur) ; 0 : noir et blanc
shadows = 0.5                    # 0 : surtout dans les tons moyens (diapositive) ; 1 : dans les ombres (négatif)
clumps = 0.15                    # part des amas, plus gros que le grain fin

[vignette]
amount = 0.1
```

- Pour une pellicule noir et blanc : `monochrome = true` et un **mélangeur de canaux**, `mixer = [rouge, vert, bleu]` (la sensibilité spectrale de l'émulsion). Plus tard, des **filtres** jaune, orange, rouge ou vert, réglables dans le menu comme sur un boîtier argentique.
- Les courbes par canal s'écrivent `red_curve`, `green_curve` et `blue_curve`, sur le modèle de `tone_curve`.
- `[look.colours]` règle huit familles de couleurs, comme le panneau TSL d'un logiciel photo : `red` (0°), `orange` (30°), `yellow` (60°), `green` (120°), `aqua` (180°), `blue` (225°), `purple` (270°), `magenta` (315°). Une couleur entre deux familles reçoit un mélange des deux réglages ; les gris ne bougent pas.
- Ordre d'application, comme dans un boîtier argentique : la LUT du preset, le mélange noir et blanc, les courbes, les familles de couleurs et la saturation, le virage (`split_toning`), le mélange avec la base (`strength`), puis le contraste et la saturation du menu.
- **Le grain** (`[grain]`) ne passe pas par la LUT : le moteur le dessine sur le JPEG, différent à chaque photo et sans motif répété. Il a cinq caractéristiques : l'intensité, la finesse (taille), la part de grain coloré, sa répartition selon les tons, et la part d'amas. Un JPEG réduit (tailles M et S) reçoit un grain plus fin d'autant. Le menu indique la finesse du grain du preset choisi (« Oui · fin »), calculée d'après l'intensité et la taille : aucun, très fin, fin, moyen, marqué. Le grain ne se voit pas dans la visée, trop petite : il apparaît dans la loupe de la lecture, à 100 %.
- Coût sur le Pi : ~2,7 s pour compiler un preset avec des familles de couleurs, une fois (gardé en mémoire et sur disque) ; un cran de contraste ou de saturation dans le menu, ~0,5 s.
- Tout est optionnel : un preset peut n'être qu'une LUT, ou qu'une recette.

## 5. Créer ses presets sur un ordinateur

**1. Retoucher une image « Hald CLUT ».** C'est le chemin le plus ouvert, car il marche avec n'importe quel logiciel : Lightroom, Capture One, darktable, RawTherapee, Photoshop, GIMP, Affinity…

- L'outil fournit une image identité, un PNG où chaque pixel est une couleur.
- On lui applique ses réglages, puis on l'exporte en PNG 16 bits : l'image retouchée *est* la LUT.
- Seules les retouches globales passent : pas de masques, pas de netteté, pas de réduction du bruit.
- Le plus simple : régler d'abord son look sur un JPEG pris en preset « Base », puis copier ces réglages sur l'image Hald.

**2. Exporter une LUT `.cube`** depuis DaVinci Resolve, Photoshop ou Affinity. L'entrée attendue est la base, c'est-à-dire du Rec.709. Les LUT « Rec.709 » du commerce s'appliquent donc directement, avec un rendu proche.

**3. Écrire une recette** à la main, en TOML seul, ou la générer par un script.

**L'outil pour l'ordinateur, `tools/pycam_preset.py`** (Python 3.11+ et Pillow ; aucune autre dépendance). Il partage le code de compilation de l'appareil (`app/pycam/looks.py`), pour un résultat identique :

```bash
python3 tools/pycam_preset.py identity hald.png                   # l'image Hald identité (niveau 8 : 512×512)
python3 tools/pycam_preset.py check mon-preset/                   # valide ? que fait-il aux gris ?
python3 tools/pycam_preset.py apply mon-preset/ PYB_0012.JPG essai.jpg [--contrast 1] [--saturation -1]
python3 tools/pycam_preset.py export mon-preset/ look.cube        # ou look.png (Hald), pour Resolve, RawTherapee…
```

- `apply` part d'un JPEG de base (preset « Base », ou option « garder la base ») ; il n'imite ni le grain ni le vignettage.
- Vérifié sur le Pi le 2026-10-12 :
  - l'aller-retour de « Standard » par `export` puis un preset fait de cette LUT redonne la même LUT : exactement en `.cube`, à 0,55 niveau sur 255 près en Hald (l'arrondi 8 bits de l'image) ;
  - sur une même scène, `apply` sur le JPEG de base donne le JPEG de l'appareil : moyennes RVB 103/84/92 contre 104/84/91. L'écart moyen par pixel, ~5 niveaux, est surtout le bruit entre les deux photos ; la base, elle, s'écarte de 13 à 17 niveaux.
- Plus tard : `fit`, pour construire une LUT à partir d'une mire (§6, méthode B).

## 6. S'approcher de la colorimétrie d'une pellicule

**Ce qui fait le rendu d'une pellicule :**

- **La courbe caractéristique**, avec son pied et son épaule. Un négatif a une grande latitude dans les hautes lumières ; une inversible (diapositive) est contrastée, avec peu de latitude.
- **La couleur** : les sensibilités spectrales des couches et les recouvrements des colorants. D'où des teintes qui ne se ramènent pas à une matrice : peaux chaudes, verts éteints, bleus profonds, rouges denses selon les films.
- **Les dérives de teinte** des ombres et des hautes lumières.
- **Le grain**, dont la taille et la visibilité dépendent de la densité.
- **La balance fixe** : lumière du jour (5500 K) ou tungstène (3200 K). Sous une lumière d'intérieur, une pellicule lumière du jour vire à l'orangé, et ça fait partie du rendu.
- **La halation** (halo rouge autour des hautes lumières, typique de la CineStill). C'est un effet spatial, impossible dans une LUT : il faudra un traitement à part dans le moteur, plus tard.
- **Pour un négatif, le tirage ou le scan compte autant que la pellicule** : un Portra scanné sur Frontier et sur Noritsu ne donne pas le même rendu. Il faudra choisir une référence.

**Trois méthodes, par fidélité croissante :**

| Méthode | Principe | Il faut | Fidélité |
|---|---|---|---|
| **A. À l'œil** | Une recette (courbe, teintes, grain) réglée sur des photos de référence. | Des références visuelles. | Bonne impression d'ensemble, approximative sur les teintes. |
| **B. Par mire** | Photographier une mire ColorChecker avec l'appareil, et sur la vraie pellicule dans la même lumière, puis scanner. `pycam-preset fit` ajuste une LUT lissée entre les deux, plus la courbe de tons sur les gris. | Une mire, des pellicules, un scan. Bibliothèque `colour-science` (licence BSD). | Bonne sur les couleurs mesurées ; dépend du scan. |
| **C. Modèle physique** | Simuler la pellicule à partir de ses fiches techniques : sensibilités spectrales, courbes caractéristiques, densités des colorants, puis le tirage. Calculé sur l'ordinateur et exporté en LUT. Des projets libres existent (par exemple agx-emulsion ; licence et état à vérifier). | La base linéarisée : possible, car la courbe Rec.709 s'inverse exactement. | La plus fidèle en principe ; c'est de la R&D. |

Avec plus de dix ans de pratique argentique, des pellicules et des scans à disposition, **B est possible dès le départ**. A sert à valider la chaîne de bout en bout, B vise la fidélité, C reste de l'exploration.

**Premier jet (décidé)** : quatre pellicules, une par famille. Le projet sera public : dans l'appareil, chaque preset porte un nom à nous, qui évoque la pellicule sans reprendre la marque. Les noms réels n'apparaissent ici qu'à titre descriptif ; à revoir avant publication.

| Famille | Pellicule de référence | Nom dans l'appareil | Court | Lumière |
|---|---|---|---|---|
| Négatif couleur | Kodak Portra 400 | Portrait 400 | P400 | lumière du jour |
| Inversible | Fujifilm Velvia 50 | Velours 50 | V50 | lumière du jour |
| Cinéma | CineStill 800T | Cinéma 800T | C800T | tungstène, 3200 K |
| Noir et blanc | Kodak Tri-X 400 | Triple-X 400 | TX400 | — |

Plus tard : Ektar 100, Gold 200, Superia 400, Provia 100F, Kodachrome 64, HP5 Plus.

### Premières versions (méthode A, 2026-10-12)

Les références : les publications Instagram de l'auteur et d'un ami photographe (avec son accord), dont les légendes donnent la pellicule. Mesurées dans le navigateur, sans les enregistrer, en écartant les cadres et fonds blancs des montages. Ce sont des scans de labo ou maison, peut-être retouchés, recompressés par Instagram (~1440 px) : de bonnes références d'ensemble, pas des mesures de laboratoire.

| Pellicule | Images | Noirs (1 %) | Médiane | Blancs (99 %) | Saturation | Couleurs |
|---|---|---|---|---|---|---|
| Portra 400 | 48 + 4 | 0,08 | 0,55 | 0,94 | 0,25 | peaux ~28-32°, verts ~106° (vers le jaune), ciels ~207° (cyan), gris neutres |
| Velvia 50 | 3 | 0,075 | 0,56 | 0,96 | 0,32 | ciels ~219° (plus profonds), jaunes et verts saturés |
| CineStill 800T | 4 | 0,11 | 0,35 | 0,85 | 0,31 | mat ; verts ~140° (canard) |
| CineStill 800T poussée +2 | 10 | 0,03 | 0,25 | 0,89 | 0,59 | bleus ~207° et rouges très saturés |
| Tri-X 400 | 5 | 0,03 | 0,44 | 0,92 | — | contraste franc, épaule douce |

Les presets correspondants (`app/pycam/presets/`) en reprennent les courbes et les couleurs, complétées par le caractère connu de chaque film. Ils ont été vérifiés sur une mire de synthèse et une photo de l'appareil : les premières versions de Velours 50 et Cinéma 800T +2, trop saturées, écrasaient les couleurs vives en aplats ; elles ont été atténuées. La CineStill 800T a deux presets, développement normal et poussé, parce que les deux rendus diffèrent nettement. Ce qui manque encore : la halation des CineStill, la balance tungstène fixe, et le réglage fin sur de vraies scènes (peaux, végétation, nuit).

### Protocole de prise de vue pour la méthode B

Le but : des paires « même scène, même lumière » entre l'appareil et la pellicule, pour mesurer l'écart entre les deux.

- **Matériel** : une mire couleur (voir « Quelle mire ? » ci-dessous), une carte grise, un trépied, l'appareil pycam et un boîtier argentique côte à côte.
- **Lumière** :
  - lumière du jour, en plein soleil et à l'ombre, pour Portrait 400, Velours 50 et Triple-X 400 ;
  - tungstène (ampoule halogène ou à incandescence) pour Cinéma 800T, plus une scène de nuit avec des sources lumineuses, pour la halation.

  Noter la lumière de chaque prise.
- **Fourchettes d'exposition**, pour mesurer la courbe caractéristique :
  - négatif : −2, −1, 0, +1, +2 IL ;
  - Velvia : de −1 à +1 IL par tiers.

  L'appareil pycam fait la même fourchette, en RAW + JPEG, avec l'option « garder la base » activée.
- **Le scan** : une seule chaîne par pellicule, puisque le scan fait partie du rendu.
  - Noter le scanner ou le labo, et le logiciel d'inversion.
  - Exporter en TIFF 16 bits sans corrections automatiques si possible.
  - Pour des scans de labo (Frontier, Noritsu), garder le rendu du labo : c'est lui qu'on imite.
- **Scènes complémentaires**, hors mire : des peaux (essentiel pour Portrait 400), de la végétation, du ciel, une scène très contrastée. Elles servent à vérifier les couleurs que la mire ne couvre pas.
- **Tes scans existants**, sans prise jumelle, servent de références pour la méthode A et pour valider : répartition des tons, teinte des ombres et des hautes lumières, grain.
- **Rangement** : un dossier par pellicule, avec les scans, les prises de l'appareil et des notes (lumière, exposition, scanner).

**Quelle mire ?** La méthode B n'a pas besoin des valeurs certifiées d'une mire : on compare ce que l'appareil et la pellicule voient sur les mêmes cases. Il faut surtout :
- des cases stables et mates ;
- des pigments proches des objets réels (peaux, feuillages, ciel) ;
- une échelle de gris.

| Option | Prix indicatif (octobre 2026) | Pour nous |
|---|---|---|
| Calibrite ColorChecker Classic Mini (24 cases dont 6 gris) | 55 à 74 $ | Le meilleur compromis : des pigments conçus pour imiter des objets naturels. Petite, donc à cadrer de près. |
| ColorChecker Classic, grand format | ~99 $ | La même en grand, plus simple à cadrer avec un objectif standard. |
| Datacolor SpyderCheckr 24 | ~49 € | Une alternative sérieuse, moins chère. |
| Nuanciers de peinture (magasin de bricolage) | gratuit | Beaucoup de teintes, de vrais pigments, mats. À coller sur un carton : peaux, verts, bleus, gris. Bon point de départ, puis bon complément. |
| Carte grise 18 % et fourchettes d'exposition | ~10 à 20 € | Suffit pour la courbe de tons, la partie la plus importante. Rien pour les couleurs. |
| Mire imprimée en jet d'encre, ou affichée sur un écran | — | À éviter. Leurs spectres sont trop différents de ceux des objets réels (métamérisme) : la pellicule et le capteur ne les verraient pas comme ils voient le monde, et la LUT apprendrait de travers. |

Recommandation : commencer gratuitement avec des nuanciers et une carte grise, le temps de valider la chaîne. Acheter une ColorChecker Classic Mini ou une SpyderCheckr 24 quand on attaque la méthode B pour de bon.

**Une astuce de latitude.** La base est en 8 bits et coupe net les hautes lumières. Pour une épaule « film », un preset peut demander une sous-exposition de ⅓ à 1 IL, que sa courbe compense, comme les modes DR200/DR400 de Fujifilm. Les ombres sont alors un peu plus bruitées, mais le grain le masque.

## 7. La visée : aperçu approché, résultat exact à la revue

La LUT complète ne tient pas à 30 i/s sur le CPU (§2). Je propose :

- **En visée, un aperçu approché** et peu coûteux, à mesurer (quelques millisecondes estimées), décomposé ainsi :
  - la courbe de tons, sur la luminance, par une table de 256 entrées ;
  - une matrice couleur, appliquée à la chrominance par blocs de 2×2 ;
  - en noir et blanc, le mélangeur de canaux.

  Quand la balance des blancs est fixe, ce qui est le cas d'un preset « pellicule », la matrice peut même passer dans l'ISP, gratuitement (§2).
- **Le résultat exact apparaît à la revue après la prise**, puisque la revue montre le JPEG.
- Plus tard, si l'écart gêne, on pourra appliquer la LUT sur le GPU (shader GLES2 sur le VC4). C'est un chantier à part.

## 8. RAW + JPEG, et le DNG

- **Le DNG reste neutre** : c'est le négatif.
- Le nom et la version du preset sont écrits dans l'EXIF du JPEG et dans le DNG. Ça sert à la traçabilité, et à réappliquer le même preset sur l'ordinateur.
- **Par défaut, le preset est un parti pris définitif** : le JPEG porte le rendu choisi, et c'est tout.
- **Option « garder la base »**, coupée par défaut, à activer dans le menu (section Prise de vue). Elle ajoute un JPEG de base, ou une prévisualisation pleine taille dans le DNG. On peut alors réappliquer n'importe quel preset plus tard, exactement, sur l'appareil en lecture comme sur l'ordinateur. Coût : 3 à 5 Mo de plus par photo.
- **Bonus possible, un « bracketing de pellicules »** : une seule prise, plusieurs JPEG avec plusieurs presets, pour 0,6 s de plus par preset.
- Plus tard : un profil DCP (avec sa table « Look » et sa courbe de tons) embarqué dans le DNG, pour que RawTherapee ou Lightroom l'ouvrent avec le rendu. Ce serait une approximation : un DCP travaille sur les données brutes, pas sur notre base.

## 9. Dans l'appareil

- **Menu rapide** : une ligne « Simulation », la liste des presets installés.
- **Menu complet** : une section « Simulation » avec l'intensité, le grain (selon le preset, coupé, faible ou fort) et le filtre noir et blanc. L'option « garder la base » va dans la section « Prise de vue ».
- **Bandeau** : le nom court du preset (`P400`).
- **Correction de l'ombrage** (section Objectif) : marche / arrêt, indépendante du preset. La changer redémarre la caméra.
- **Moteur** : une commande `look`. Le moteur reçoit la LUT compilée (un fichier dans `/run/pycam`), le grain et le vignettage, et les applique dans le fil d'écriture sur 3 fils avant l'encodage JPEG.
- **Rafale** : chaque photo coûte 0,6 à 0,7 s d'écriture en plus, et la file d'attente s'allonge d'autant.

## 10. Décisions (2026-10-11)

1. ✅ **Tout l'appareil passe sur une base fixe**, y compris sans preset : un tuning à nous, tiré du tuning scientific. La correction d'ombrage de l'objectif est un réglage séparé du preset, dans un premier temps (§3).
2. ✅ **Aperçu approché en visée, résultat exact à la revue.** Le GPU, plus tard si besoin.
3. ✅ **Garder la base : oui, mais en option**, coupée par défaut. Par défaut, le preset est un parti pris définitif. L'option se trouve dans le menu, section « Prise de vue ».
4. ✅ **Méthodes A et B dès le départ**, grâce aux pellicules et aux scans disponibles (protocole au §6). Pas de mire pour l'instant : les options sont au §6.
5. ✅ **Premier jet** : Portra 400, Velvia 50, CineStill 800T, Tri-X 400, sous les noms Portrait 400, Velours 50, Cinéma 800T et Triple-X 400 (§6).
6. ✅ **Projet public à terme** : des noms à nous, qui évoquent sans reprendre les marques (Portrait 400 pour la Portra 400). Pas de LUT tierce sans licence compatible.

## 11. Étapes proposées

1. **Base** : tuning `pycam-imx477` versionné, et preset « Standard » en LUT. Comparaisons A/B sur plusieurs scènes, intérieur et extérieur.
2. **Moteur** : commande `look`, LUT et grain dans le fil d'écriture. Mesures, puis optimisation (entiers, NEON).
3. **Application** : format TOML, compilateur numpy, menus, EXIF.
4. **Outil `pycam-preset`** : identité Hald, `apply`, `check`, `convert`.
5. **Aperçu approché** en visée.
6. **Premiers presets** (méthode A), puis la mire (méthode B).
