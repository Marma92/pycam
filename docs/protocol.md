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
| `get_state` | — | `state` : caméra, stratégie, phase, fps, file d'écriture, flux, contrôles appliqués |
| `list_controls` | — | `controls` : pour chaque contrôle supporté, `min`/`max`/`default` et éventuellement `values` (énumérations) |
| `set_controls` | `controls` : `{nom: valeur}` | — |
| `capture` | `path` (sans extension), `formats` (`["jpeg","dng"]` par défaut), `quality` (93), `exif` (liste `"EXIF.FocalLength=16/1"`…) | `capture_id` |
| `configure` | `strategy` : `"switch"` ou `"zsl"` | — |
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
| `stats` | ~5 fois/s en visée | `fps`, `exposure_us`, `analogue_gain`, `digital_gain`, `colour_gains`, `colour_temp`, `lux`, `focus_fom` (netteté mesurée par l'ISP), `ae_converged`, `sensor_temp`, `mean_y` (0–255), `histogram` (64 classes de luminance) |
| `captured` | L'image est prise (copiée hors des tampons caméra) | `capture_id`, `latency_ms` (commande → image), `copy_ms`, `exposure_us`, `analogue_gain`, `digital_gain`. En mode `switch`, en plus : `reconfigure_ms` et `first_frame_ms`. |
| `capture_done` | Les fichiers sont écrits | `capture_id`, `files`, `timings` (`jpeg_ms`, `dng_ms`), `write_queue` |
| `capture_error` | Échec d'une capture ou de son écriture | `capture_id`, `error`, `files` déjà écrits |
| `error` | Incident caméra | `error` |

Le déclencheur peut être relâché, et une nouvelle prise acceptée, dès `captured`. `capture_done` arrive plus tard.

Les fichiers sont d'abord écrits en `.part`, puis renommés : un fichier `.JPG` ou `.DNG` visible est toujours complet.

## Exemple

```jsonc
→ {"id": 7, "cmd": "set_controls", "controls": {"ExposureTime": 8000, "AnalogueGain": 2.0, "AwbMode": "Daylight"}}
← {"id": 7, "ok": true}
→ {"id": 8, "cmd": "capture", "path": "/home/ael/DCIM/100PYCAM/PYC_0042", "exif": ["EXIF.FocalLength=16/1"]}
← {"id": 8, "ok": true, "capture_id": 3}
← {"event": "captured", "capture_id": 3, "latency_ms": 277.6, "first_frame_ms": 207.1, "reconfigure_ms": 70.2, "copy_ms": 66.9, "exposure_us": 7998, "analogue_gain": 2.0, "digital_gain": 1.0}
← {"event": "capture_done", "capture_id": 3, "files": [".../PYC_0042.JPG", ".../PYC_0042.DNG"], "timings": {"jpeg_ms": 610.3, "dng_ms": 251.0}, "write_queue": 0}
```
