#!/bin/sh
# Copie les sources vers ~/pycam sur le Pi, compile le moteur, et redémarre les services
# pycam s'ils sont installés (tools/install-services.sh).
# Usage : tools/deploy.sh [--no-build]     (hôte : $PI_HOST, défaut ael@192.168.1.165)
set -eu

PI_HOST="${PI_HOST:-ael@192.168.1.165}"
cd "$(dirname "$0")/.."

dirs=""
for d in VERSION engine app tools system docs; do [ -e "$d" ] && dirs="$dirs $d"; done
# shellcheck disable=SC2086
tar -czf - --exclude='build' --exclude='__pycache__' $dirs \
	| ssh -o BatchMode=yes "$PI_HOST" 'mkdir -p ~/pycam && tar -xzf - -C ~/pycam'
echo "sources copiées vers $PI_HOST:~/pycam"

[ "${1:-}" = "--no-build" ] && exit 0

# The camera and the application are stopped during the build: with them running, two
# compilers in parallel do not fit in the Pi 3B's 1 GB, which then swaps for tens of
# minutes (seen 2026-10-12, a header change rebuilding the whole engine). Even stopped,
# two of the big files at once (engine.cpp, develop.cpp: nlohmann/json) made it stop
# answering, SSH included: one compiler at a time, unless PYCAM_JOBS says otherwise.
ssh -o BatchMode=yes "$PI_HOST" "JOBS=${PYCAM_JOBS:-1}; "'
	set -e
	cd ~/pycam/engine
	services=""
	if systemctl is-enabled --quiet pycam-engine.service 2>/dev/null; then
		services="pycam-engine.service pycam.service"
		sudo systemctl stop $services
	fi
	[ -d build ] || CXX="ccache g++" meson setup build >/dev/null
	status=0
	ninja -C build -j$JOBS || status=$?
	if [ -n "$services" ]; then
		sudo systemctl start $services
		echo "services pycam redémarrés"
	fi
	exit $status
'
