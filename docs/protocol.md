# Protocole moteur ⇄ application

Le moteur `pycam-engine` écoute sur un socket Unix : `/run/pycam/engine.sock` par défaut, ou le chemin passé par `--socket`. Le dossier `/run/pycam` est créé au démarrage du Pi pour l'utilisateur de l'appareil (`system/pycam.tmpfiles.in`). Chaque message est **un objet JSON sur une ligne**, dans les deux sens. Plusieurs clients peuvent être connectés ; les évènements sont envoyés à tous.

Pour tester à la main :

```bash
python3 tools/engine_cli.py --help
```

On peut aussi dialoguer en direct avec `socat - UNIX-CONNECT:/run/pycam/engine.sock`.

## Commandes (application → moteur)

Toute commande peut porter un `id`, qui est renvoyé tel quel dans la réponse. Une réponse contient toujours `"ok": true|false`. En cas d'échec, elle contient aussi `error` (un code) et `detail` (un texte pour humains).

| Commande | Paramètres | Réponse en cas de succès |
|---|---|---|
| `ping` | — | — |
| `get_state` | — | `state` : caméra, stratégie, phase, fps, file d'écriture, flux, contrôles appliqués, `tuning`, `look` |
| `list_controls` | — | `controls` : pour chaque contrôle supporté, `min`/`max`/`default` et éventuellement `values` (énumérations) |
| `set_controls` | `controls` : `{nom: valeur}` | — : appliqués tout de suite, et gardés pour être rendus à la caméra à chaque redémarrage (photo en bascule, loupe, ZSL). Une valeur (`ExposureTime`, `AnalogueGain`) passe son mode en manuel ; un mode automatique (`ExposureTimeMode`, `AnalogueGainMode` à `Auto`, `AwbEnable` à `true`) oublie la valeur manuelle qu'il remplace ; `ColourGains` et `ColourTemperature` s'excluent. Une pose manuelle plus longue que l'image de visée (100 ms, 250 ms en ZSL) allonge les limites de durée d'image. |
| `capture` | `path` (sans extension), `formats` (`["jpeg","dng"]` par défaut), `dng_compression` (sans perte, `false` par défaut), `quality` (93), `exif` (liste `"EXIF.FocalLength=16/1"`… : toutes vont dans le JPEG ; celles de l'objectif et de l'ouverture aussi dans le DNG), `base_jpeg` (sans extension, facultatif : écrit aussi le JPEG sans le look, si un look est actif), `stack` (facultatif : `{"mode": "add", "frames": 0}`, voir plus bas) | `capture_id` |
| `capture_stop` | — | `stopping` : `true` si un empilement est en cours ; il se termine avec l'image en cours de pose. Une pose simple continue. |
| `capture_cancel` | — | `cancelled` : `true` si une prise était en cours ; elle est abandonnée, rien n'est écrit, et l'évènement `capture_error` porte `"error": "cancelled"`. |
| `configure` | Toutes facultatives :<br>• `strategy` : `"switch"` ou `"zsl"` ;<br>• `still_denoise` : `off`, `cdn_off`, `cdn_fast`, `cdn_hq` (en ZSL, `cdn_hq` devient `cdn_fast` pour garder 11,7 i/s) ;<br>• `still_size` : `[w, h]` du JPEG, ou `null` pour le capteur entier. Le DNG reste en pleine résolution ;<br>• `tuning` : le fichier de tuning de l'ISP, sans `.json`, dans le dossier `--tuning-dir` (`engine/tuning` par défaut), ou `null` pour celui de libcamera. Au démarrage : `pycam-imx477`. | — : en ZSL, un changement de réglage photo redémarre la caméra ; un changement de tuning la ferme et la rouvre (~0,2 s), puis envoie l'évènement `tuning` |
| `focus_aids` | `peaking` (bool), `peaking_color` (`red`, `yellow`, `green`, `blue`, `white` ou `[r, g, b]`), `peaking_sensitivity` (`low`, `medium`, `high`), `zebra` (bool), `zebra_level` (0–1, défaut 0,95) ; les clés absentes gardent leur valeur | — |
| `loupe` | `zoom` (1 = coupée, jusqu'à 16), `center` `[x, y]` (fractions de l'image, défaut le centre) ; les clés absentes gardent leur valeur | `loupe` : `{zoom, center}`. Entrer ou sortir de la loupe reconfigure la caméra (le capteur passe en pleine résolution) ; déplacer la zone ou changer de grossissement, non. |
| `look` | Tout le look d'un coup, les clés absentes étant coupées :<br>• `name` : pour l'état et les journaux ;<br>• `lut` : chemin d'un fichier LUT (voir plus bas), ou `null` ;<br>• `vignette` : assombrissement des coins, 0–1 ;<br>• `grain` : 0–1 ; `grain_size`, en pixels de l'image pleine résolution (0,5–8, 1 par défaut ; réduit d'autant pour un JPEG plus petit) ; `grain_colour` (0–1 : grain dans la chrominance, 0 pour le noir et blanc) ; `grain_shadows` (0 : surtout dans les tons moyens, 1 : dans les ombres) ; `grain_clumps` (0–1 : part des amas, 2,5 fois plus gros) ;<br>• `halation` : 0–1 ; `halation_radius` (étalement, en pixels de l'image pleine résolution, 24 par défaut) ; `halation_threshold` (niveau de la base, 0–1, à partir duquel la lumière s'étale ; 0,8 par défaut) ; `halation_colour` (`[r, g, b]`, 0–1) ;<br>• `preview` : l'aperçu en visée, ou `null` (voir plus bas). | `look` : l'état. Les photos déjà prises gardent le look du moment où elles ont été prises. |
| `snapshot` | `path` | — : la prochaine image de visée affichée, aides comprises mais sans le HUD, sera écrite en JPEG ; évènement `snapshot_done` ou `snapshot_error`. Sert au débogage à distance. |
| `hud_info` | — | `display` : taille de l'écran, `image_rect` `[x, y, w, h]` (où est l'image), `hud` (chemin de la mémoire partagée, largeur, hauteur, stride, format `"RGBA"`), compteurs d'images affichées et perdues |
| `video_play` | `path` (un MP4 H.264) | `video` : `duration` (s), `width`, `height`, `fps`. La vidéo remplace la visée à l'écran (la caméra continue), dans la zone d'image ; elle s'arrête d'elle-même à la fin en laissant sa dernière image. Erreur `bad_request` si elle ne peut pas être lue. |
| `video_pause` | `paused` (bool, `true` par défaut) | — |
| `video_stop` | — | — : retour à la visée |
| `video_mode` | `on` (bool) ; et `width`, `height` (1920×1080 par défaut, pairs, 2028×1080 au plus), `fps` (25), `bitrate` (bit/s, 12 000 000) | — : la caméra passe en mode vidéo (ou revient à la visée photo). Refusé (`busy`) pendant une prise ou un enregistrement s'il faut changer de réglages. En mode vidéo, `capture` répond `busy` et `loupe` `unsupported`. |
| `record_start` | `path` (le MP4) | `path` ; `busy` hors du mode vidéo ou pendant un enregistrement |
| `record_stop` | — | `path`, `duration` (s), `frames`, `bytes`, et `error` si la vidéo n'a pu être faite ; `recording: false` s'il n'y avait rien à arrêter. L'évènement `record_done` porte le même contenu. |
| `display_power` | `on` (bool) | — : l'écran éteint (DPMS) n'affiche plus rien, ni la visée ni le HUD, jusqu'à `on: true` |
| `hud_commit` | — | — (le contenu de la mémoire partagée a été copié : on peut dessiner le HUD suivant) |
| `quit` | — | — (le moteur termine les écritures en cours puis s'arrête) |

**Codes d'erreur**

| Code | Signification |
|---|---|
| `bad_request` | Paramètre manquant ou invalide. |
| `unsupported` | Contrôle non supporté par cette caméra. |
| `busy` | Capture déjà en cours, ou file d'écriture pleine. |
| `unknown_command` | Commande inconnue. |

`hud_info` et `hud_commit` répondent `unsupported` si le moteur tourne sans écran (`--no-display`, aucun écran branché, ou bureau graphique actif).

### HUD

Le HUD est une image RGBA de la taille de l'écran, sans alpha prémultiplié, dans `/dev/shm/pycam-hud`. L'application y dessine (avec Pillow, `img.tobytes()` en mode `"RGBA"`), puis envoie `hud_commit`. Le moteur la copie dans un tampon d'affichage et la pose par-dessus la visée au vblank suivant.

Par défaut, et tant que l'application n'a rien dessiné, le HUD est noir opaque autour de l'image (il masque la console) et transparent sur l'image. Un HUD dessiné par l'application doit garder ces marges opaques.

### Look

Le look s'applique au JPEG seul, jamais au DNG, dans le fil d'écriture, avant l'encodage. La visée en reçoit une approximation, l'aperçu. L'ordre est celui d'un appareil argentique : le vignettage (l'objectif), la halation (la lumière qui revient dans le film), puis la LUT (le rendu de la pellicule), puis le grain.

**Halation.** Les films sans couche anti-halo (CineStill) entourent les lumières vives d'un halo rouge : la lumière traverse le film, revient du presseur et impressionne la couche rouge. Le moteur repère les zones très claires du canal rouge de la base (la lumière qui atteint le support est surtout rouge, les autres couches et le filtre jaune ayant pris le reste), sur une grille de quelques pixels, les étale par trois flous en boîte (proches d'une gaussienne de `halation_radius`), puis ajoute la lueur, qui sature, avant la LUT. Coût mesuré sur 12 Mpx : ~0,16 s.

**Fichier LUT** (`docs/presets.md`), écrit par l'application, petit-boutiste :

| Octets | Contenu |
|---|---|
| 8 | `PYCAMLUT` |
| 4 | version : 1 |
| 4 | `n`, le nombre de points par axe (2 à 65 ; l'application écrit 33) |
| `n³ × 3 × 2` | les sorties R, G, B en `uint16` (0–65535 pour 0–1), le rouge variant le plus vite : indice `((b × n + g) × n + r) × 3` |

L'entrée est la base pycam : le YUV pleine plage BT.601 de l'ISP, vu comme du RVB 0–255. L'interpolation est tétraédrique.

**Coût mesuré** sur une photo de 12 Mpx, sur 3 fils (2026-10-11) : LUT ~0,75 s, grain ~0,25 à 0,4 s en plus selon sa taille. Il s'ajoute à l'écriture, en arrière-plan.

**Aperçu** (`preview`). La LUT complète occuperait les 4 cœurs à 30 i/s : la visée reçoit une approximation, que l'application ajuste sur la LUT par moindres carrés (`looks.fit_preview`) :

- `luma` : 256 valeurs, la courbe des gris ;
- `luma_mix` : `[a, b]`, la part de chrominance qui passe dans la luminance (le mélangeur noir et blanc) : Y' = `luma`[Y + a·Cb + b·Cr] ;
- `chroma` : la matrice 2×2 `[m00, m01, m10, m11]` appliquée à (Cb, Cr) par bloc de 2×2 pixels (saturation, décalages de teinte).

Elle est appliquée dans le fil d'affichage, en entiers, avant le peaking et les zébras, qui montrent donc la photo telle qu'elle sera : 5 à 8,5 ms par image de visée 1024×768. Mesuré sur une scène réelle avec « Standard » : RVB moyen 118/106/112 en visée, 117/104/111 dans le JPEG.

### Contrôles

Les noms sont ceux de libcamera, et `list_controls` donne la liste exacte.

- **Valeurs** : un nombre, un booléen, un tableau (`ColourGains: [r, b]`, `FrameDurationLimits: [min, max]`) ou un rectangle `[x, y, w, h]` (`ScalerCrop`).
- **Énumérations** : on peut donner la valeur ou le nom, complet (`"AwbDaylight"`) ou sans préfixe (`"Daylight"`).
- **Passage en manuel implicite** : donner `ExposureTime` passe `ExposureTimeMode` en `Manual`, sauf si le mode est précisé dans la même commande. Idem pour `AnalogueGain` et `AnalogueGainMode`. Pour revenir en auto : `{"ExposureTimeMode": "Auto"}`.
- **Persistance** : les contrôles s'appliquent tout de suite et sont réappliqués après chaque reconfiguration de la caméra (bascule, changement de stratégie).
- **Cadence de visée** : le moteur la gère lui-même. Elle va de 30 i/s à 10 i/s en basse lumière, et à 4 i/s en ZSL. Une pose manuelle plus longue n'est pas suivie par la visée : avec un gain manuel, elle est simulée (l'image la plus longue, le gain augmenté d'autant), et au-delà du gain du capteur la visée s'expose automatiquement ; la photo, elle, a bien la pose demandée (`architecture.md` §2.4).

### Empilements (pose B, stacking)

Avec `stack`, la prise additionne les images RAW successives en mode photo, chacune avec la pose réglée (la pose B : 1 s), jusqu'à `frames` images (`0` : jusqu'à `capture_stop`), 3 600 images au plus, et 30 minutes de pose pour une somme. `mode` : `add` (la somme : une seule pose aussi longue que toutes), `mean` (la moyenne : moins de bruit), `max` (le plus clair de chaque pixel : traînées de lumière). Le DNG contient le RAW combiné ; le JPEG est développé par le moteur à partir de lui (`docs/architecture.md` §2.4), puis reçoit le look. Pour ces prises, le DNG est écrit avant le JPEG. Chaque image donne un évènement `stack_frame`.

## Évènements (moteur → application)

| Évènement | Quand | Contenu |
|---|---|---|
| `ready` | Au démarrage | `state` |
| `display_ready` | Le moteur obtient l'écran après son démarrage (écran de démarrage Plymouth, écran branché après coup) | `display` (comme `hud_info`) : l'application installe alors son HUD |
| `stats` | ~5 fois/s en visée | `fps`, `exposure_us`, `analogue_gain`, `digital_gain`, `colour_gains`, `colour_temp`, `lux`, `focus_fom` (netteté mesurée par l'ISP), `ae_converged`, `sensor_temp`, `mean_y` (0–255), `histogram` (64 classes de luminance), `histogram_rgb` (rouge, vert, bleu : 3 × 64 classes), `viewfinder` (absent si la visée montre la pose réglée ; `simulated` ou `auto` pour une pose manuelle trop longue) |
| `exposing` | La caméra est en mode photo, la pose commence | `capture_id`, `exposure_us` (la pose manuelle, 0 en automatique), `stack` (booléen). Pour un compte à rebours. |
| `stack_frame` | Une image de plus dans un empilement | `capture_id`, `frames`, `exposure_us` (cumulée) |
| `captured` | L'image est prise (copiée hors des tampons caméra) | `capture_id`, `latency_ms` (commande → image), `copy_ms`, `exposure_us`, `analogue_gain`, `digital_gain`. En mode photo (bascule, ou pose longue en ZSL), en plus : `reconfigure_ms` et `first_frame_ms`. Pour un empilement : `frames`, et `exposure_us` la pose totale (`add`). |
| `file_saved` | Chaque fichier écrit, le JPEG en premier | `capture_id`, `path` : la revue après la prise part du JPEG sans attendre le DNG |
| `capture_done` | Les fichiers sont écrits | `capture_id`, `files` (dont le JPEG de base s'il a été demandé : il n'a pas de `file_saved`), `timings` (`jpeg_ms`, `dng_ms`, et selon les cas `look_ms`, `base_jpeg_ms`), `write_queue` |
| `capture_error` | Échec d'une capture ou de son écriture | `capture_id`, `error`, `files` déjà écrits |
| `snapshot_done` / `snapshot_error` | Après `snapshot` | `path` (et `error`) |
| `loupe` | Le moteur quitte la loupe de lui-même (un déclenchement : la photo est toujours en plein cadre) | `loupe` : `{zoom, center}` |
| `video_progress` | Pendant une lecture, ~2 fois/s | `position`, `duration` (s) |
| `record_progress` | Pendant un enregistrement, chaque seconde | `path`, `duration` (s), `bytes` |
| `record_done` | Fin d'un enregistrement (`record_stop`, redémarrage de la caméra, arrêt du moteur) | `path`, `duration`, `frames`, `bytes`, `error` |
| `record_error` | L'écriture a échoué (carte pleine) | `path`, `error` : envoyer `record_stop` |
| `video_ended` | Fin d'une lecture | `reason` (`end`, `stopped`, `error`), `position`, `error` |
| `tuning` | La caméra a été rouverte avec un autre fichier de tuning | `tuning` (nom ou `null`), `reopen_ms` |
| `error` | Incident caméra | `error` |

Le déclencheur peut être relâché, et une nouvelle prise acceptée, dès `captured`. `capture_done` arrive plus tard.

Les fichiers sont d'abord écrits en `.part`, forcés sur la carte (`fsync`), puis renommés : un fichier `.JPG` ou `.DNG` visible est toujours complet, même après une coupure de courant, et `capture_done` signifie « sur la carte », pas seulement « dans le cache du noyau ».

## Exemple

```jsonc
→ {"id": 7, "cmd": "set_controls", "controls": {"ExposureTime": 8000, "AnalogueGain": 2.0, "AwbMode": "Daylight"}}
← {"id": 7, "ok": true}
→ {"id": 8, "cmd": "capture", "path": "/home/ael/DCIM/100PYCAM/PYC_0042", "exif": ["EXIF.FocalLength=16/1"]}
← {"id": 8, "ok": true, "capture_id": 3}
← {"event": "captured", "capture_id": 3, "latency_ms": 277.6, "first_frame_ms": 207.1, "reconfigure_ms": 70.2, "copy_ms": 66.9, "exposure_us": 7998, "analogue_gain": 2.0, "digital_gain": 1.0}
← {"event": "capture_done", "capture_id": 3, "files": [".../PYC_0042.JPG", ".../PYC_0042.DNG"], "timings": {"jpeg_ms": 610.3, "dng_ms": 251.0}, "write_queue": 0}
```
