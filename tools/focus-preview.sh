#!/bin/sh
# Aperçu plein écran sur la sortie HDMI du Pi, pour régler la mise au point,
# avec la netteté mesurée par l'ISP (FocusFoM) affichée en direct dans le terminal.
# Plus la valeur est haute, plus l'image est nette : tourner la bague jusqu'au maximum.
#
# Usage (depuis le poste de dev) : ssh -t ael@192.168.1.165 sh pycam/tools/focus-preview.sh [loupe]
#   loupe = 1 (défaut, champ complet) ou 2, 4… (centre agrandi, en pleine résolution)
# Ctrl-C pour arrêter. Outil provisoire, en attendant l'affichage du moteur.

zoom="${1:-1}"

# La caméra ne sert qu'à un programme à la fois : arrêter proprement le moteur s'il tourne.
pkill -INT -x pycam-engine 2>/dev/null && sleep 2

if [ "$zoom" = 1 ]; then
	# Champ complet, capteur en binning 2x2 : visée fluide.
	set -- --mode 2028:1520:12:P --width 1014 --height 760
else
	# Centre agrandi, capteur en pleine résolution : on voit les vrais pixels (~11 i/s).
	roi=$(awk -v z="$zoom" 'BEGIN { s = 1 / z; o = (1 - s) / 2; printf "%.4f,%.4f,%.4f,%.4f", o, o, s, s }')
	set -- --mode 4056:3040:12:P --width 1014 --height 760 --roi "$roi"
fi

# mawk (l'awk de Debian) garde l'entrée d'un pipe en mémoire : le passer en mode ligne à ligne.
awk_cmd="awk"
command -v mawk >/dev/null && awk_cmd="mawk -W interactive"

echo "Aperçu sur l'écran HDMI du Pi (loupe x$zoom). Ctrl-C pour arrêter."
echo "Tourner la bague de mise au point pour faire monter la netteté au maximum."
echo

rpicam-vid -t 0 "$@" --codec yuv420 -o /dev/null \
	--metadata - --metadata-format txt 2>/dev/null |
	$awk_cmd -F= '
		/^FocusFoM=/ {
			v = $2 + 0
			if (v > best) best = v
			if (++n % 8) next   # ~4 rafraichissements par seconde
			len = best ? int(40 * v / best) : 0
			bar = ""
			for (i = 0; i < len; i++) bar = bar "#"
			printf "\rnetteté %6d  [%-40s]  max vu %6d ", v, bar, best
			fflush()
		}'
