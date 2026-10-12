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
| `set_controls` | `controls` : `{nom: valeur}` | — |
| `capture` | `path` (sans extension), `formats` (`["jpeg","dng"]` par défaut), `dng_compression` (sans perte, `false` par défaut), `quality` (93), `exif` (liste `"EXIF.FocalLength=16/1"`… : toutes vont dans le JPEG ; celles de l'objectif et de l'ouverture aussi dans le DNG), `base_jpeg` (sans extension, facultatif : écrit aussi le JPEG sans le look, si un look est actif) | `capture_id` |
| `configure` | Toutes facultatives :<br>• `strategy` : `"switch"` ou `"zsl"` ;<br>• `still_denoise` : `off`, `cdn_off`, `cdn_fast`, `cdn_hq` (en ZSL, `cdn_hq` devient `cdn_fast` pour garder 11,7 i/s) ;<br>• `still_size` : `[w, h]` du JPEG, ou `null` pour le capteur entier. Le DNG reste en pleine résolution ;<br>• `tuning` : le fichier de tuning de l'ISP, sans `.json`, dans le dossier `--tuning-dir` (`engine/tuning` par défaut), ou `null` pour celui de libcamera. Au démarrage : `pycam-imx477`. | — : en ZSL, un changement de réglage photo redémarre la caméra ; un changement de tuning la ferme et la rouvre (~0,2 s), puis envoie l'évènement `tuning` |
| `focus_aids` | `peaking` (bool), `peaking_color` (`red`, `yellow`, `green`, `blue`, `white` ou `[r, g, b]`), `peaking_sensitivity` (`low`, `medium`, `high`), `zebra` (bool), `zebra_level` (0–1, défaut 0,95) ; les clés absentes gardent leur valeur | — |
| `loupe` | `zoom` (1 = coupée, jusqu'à 16), `center` `[x, y]` (fractions de l'image, défaut le centre) ; les clés absentes gardent leur valeur | `loupe` : `{zoom, center}`. Entrer ou sortir de la loupe reconfigure la caméra (le capteur passe en pleine résolution) ; déplacer la zone ou changer de grossissement, non. |
| `look` | Tout le look d'un coup, les clés absentes étant coupées :<br>• `name` : pour l'état et les journaux ;<br>• `lut` : chemin d'un fichier LUT (voir plus bas), ou `null` ;<br>• `vignette` : assombrissement des coins, 0–1 ;<br>• `grain` : 0–1 ; `grain_size`, en pixels de l'image pleine résolution (0,5–8, 1 par défaut ; réduit d'autant pour un JPEG plus petit) ; `grain_colour` (0–1 : grain dans la chrominance, 0 pour le noir et blanc) ; `grain_shadows` (0 : surtout dans les tons moyens, 1 : dans les ombres) ; `grain_clumps` (0–1 : part des amas, 2,5 fois plus gros) ;<br>• `preview` : l'aperçu en visée, ou `null` (voir plus bas). | `look` : l'état. Les photos déjà prises gardent le look du moment où elles ont été prises. |
| `snapshot` | `path` | — : la prochaine image de visée affichée, aides comprises mais sans le HUD, sera écrite en JPEG ; évènement `snapshot_done` ou `snapshot_error`. Sert au débogage à distance. |
| `hud_info` | — | `display` : taille de l'écran, `image_rect` `[x, y, w, h]` (où est l'image), `hud` (chemin de la mémoire partagée, largeur, hauteur, stride, format `"RGBA"`), compteurs d'images affichées et perdues |
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

Le look s'applique au JPEG seul, jamais au DNG, dans le fil d'écriture, avant l'encodage. La visée en reçoit une approximation, l'aperçu. L'ordre est celui d'un appareil argentique : le vignettage (l'objectif), puis la LUT (la pellicule), puis le grain.

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
- **Cadence de visée** : le moteur la gère lui-même. Elle va de 30 i/s à 10 i/s en basse lumière, et à 4 i/s en ZSL. Une pose manuelle plus longue est respectée.

## Évènements (moteur → application)

| Évènement | Quand | Contenu |
|---|---|---|
| `ready` | Au démarrage | `state` |
| `display_ready` | Le moteur obtient l'écran après son démarrage (écran de démarrage Plymouth, écran branché après coup) | `display` (comme `hud_info`) : l'application installe alors son HUD |
| `stats` | ~5 fois/s en visée | `fps`, `exposure_us`, `analogue_gain`, `digital_gain`, `colour_gains`, `colour_temp`, `lux`, `focus_fom` (netteté mesurée par l'ISP), `ae_converged`, `sensor_temp`, `mean_y` (0–255), `histogram` (64 classes de luminance), `histogram_rgb` (rouge, vert, bleu : 3 × 64 classes) |
| `captured` | L'image est prise (copiée hors des tampons caméra) | `capture_id`, `latency_ms` (commande → image), `copy_ms`, `exposure_us`, `analogue_gain`, `digital_gain`. En mode `switch`, en plus : `reconfigure_ms` et `first_frame_ms`. |
| `file_saved` | Chaque fichier écrit, le JPEG en premier | `capture_id`, `path` : la revue après la prise part du JPEG sans attendre le DNG |
| `capture_done` | Les fichiers sont écrits | `capture_id`, `files` (dont le JPEG de base s'il a été demandé : il n'a pas de `file_saved`), `timings` (`jpeg_ms`, `dng_ms`, et selon les cas `look_ms`, `base_jpeg_ms`), `write_queue` |
| `capture_error` | Échec d'une capture ou de son écriture | `capture_id`, `error`, `files` déjà écrits |
| `snapshot_done` / `snapshot_error` | Après `snapshot` | `path` (et `error`) |
| `loupe` | Le moteur quitte la loupe de lui-même (un déclenchement : la photo est toujours en plein cadre) | `loupe` : `{zoom, center}` |
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
