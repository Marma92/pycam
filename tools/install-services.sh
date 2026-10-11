#!/bin/sh
# Installe et démarre les services pycam (moteur + application) sur le Pi.
# Usage, sur le Pi, depuis le dossier du projet :
#   sudo sh tools/install-services.sh             installe, active au démarrage, démarre
#   sudo sh tools/install-services.sh --remove    arrête, désactive, désinstalle
set -eu

[ "$(id -u)" = 0 ] || { echo "à lancer avec sudo" >&2; exit 1; }
user="${SUDO_USER:-ael}"
pycam="$(cd "$(dirname "$0")/.." && pwd)"
units="pycam-engine.service pycam.service"

if [ "${1:-}" = "--remove" ]; then
	systemctl disable --now $units 2>/dev/null || true
	for unit in $units; do rm -f "/etc/systemd/system/$unit"; done
	rm -f /etc/tmpfiles.d/pycam.conf
	systemctl daemon-reload
	echo "services pycam désinstallés"
	exit 0
fi

[ -x "$pycam/engine/build/pycam-engine" ] || { echo "moteur non compilé : $pycam/engine/build/pycam-engine" >&2; exit 1; }

fill() { sed -e "s|@USER@|$user|g" -e "s|@PYCAM@|$pycam|g" "$1"; }

fill "$pycam/system/pycam.tmpfiles.in" > /etc/tmpfiles.d/pycam.conf
systemd-tmpfiles --create /etc/tmpfiles.d/pycam.conf
for unit in $units; do
	fill "$pycam/system/$unit.in" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable $units
systemctl restart $units
echo "services pycam installés pour $user ($pycam), actifs au démarrage"
systemctl --no-pager --lines=0 status $units || true
