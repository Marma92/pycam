#!/bin/sh
# Copie les sources vers ~/pycam sur le Pi, compile le moteur, et redémarre les services
# pycam s'ils sont installés (tools/install-services.sh).
# Usage : tools/deploy.sh [--no-build]     (hôte : $PI_HOST, défaut ael@192.168.1.165)
set -eu

PI_HOST="${PI_HOST:-ael@192.168.1.165}"
cd "$(dirname "$0")/.."

dirs=""
for d in engine app tools system docs; do [ -d "$d" ] && dirs="$dirs $d"; done
# shellcheck disable=SC2086
tar -czf - --exclude='build' --exclude='__pycache__' $dirs \
	| ssh -o BatchMode=yes "$PI_HOST" 'mkdir -p ~/pycam && tar -xzf - -C ~/pycam'
echo "sources copiées vers $PI_HOST:~/pycam"

[ "${1:-}" = "--no-build" ] && exit 0

ssh -o BatchMode=yes "$PI_HOST" '
	set -e
	cd ~/pycam/engine
	[ -d build ] || CXX="ccache g++" meson setup build >/dev/null
	ninja -C build -j2
	if systemctl is-enabled --quiet pycam-engine.service 2>/dev/null; then
		sudo systemctl restart pycam-engine.service pycam.service
		echo "services pycam redémarrés"
	fi
'
