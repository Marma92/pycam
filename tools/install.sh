#!/bin/sh
# Installe pycam sur Raspberry Pi OS 64 bits (Trixie), depuis la dernière release publiée
# sur GitHub, ou depuis une archive déjà téléchargée : les paquets Debian nécessaires,
# /opt/pycam, les services et les mises à jour automatiques. L'appareil démarre ensuite
# directement sur la visée.
#   curl -fsSL https://raw.githubusercontent.com/Marma92/pycam/master/tools/install.sh | sudo sh
#   sudo sh tools/install.sh pycam-0.1.0-arm64.tar.gz
# L'utilisateur de l'appareil est celui qui lance sudo, ou PYCAM_USER.
# Relancé sur la version installée, il réinstalle seulement les services.
set -eu

REPOSITORY=Marma92/pycam

[ "$(id -u)" = 0 ] || { echo "à lancer avec sudo" >&2; exit 1; }
arch="$(dpkg --print-architecture)"
[ "$arch" = arm64 ] || { echo "pycam demande Raspberry Pi OS 64 bits (arm64), pas $arch" >&2; exit 1; }
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || echo "attention : pycam est fait pour Trixie, ce système est ${VERSION_CODENAME:-inconnu}" >&2
user="${PYCAM_USER:-${SUDO_USER:-}}"
if [ -z "$user" ] || [ "$user" = root ] || ! id "$user" >/dev/null 2>&1; then
	echo "l'utilisateur de l'appareil ? sudo PYCAM_USER=<utilisateur> sh install.sh" >&2
	exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ $# -ge 1 ]; then
	tarball="$(realpath "$1")"
else
	command -v curl >/dev/null || { apt-get update && apt-get install -y curl; }
	curl -fsSL --proto =https "https://api.github.com/repos/$REPOSITORY/releases/latest" -o "$tmp/release.json"
	# shellcheck disable=SC2046
	set -- $(python3 - "$tmp/release.json" "$arch" <<'EOF'
import json, sys

release = json.load(open(sys.argv[1], encoding="utf-8"))
version = release["tag_name"].lstrip("v")
assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
name = f"pycam-{version}-{sys.argv[2]}.tar.gz"
print(name, assets[name], assets["SHA256SUMS"])
EOF
)
	tarball="$tmp/$1"
	echo "téléchargement de $1"
	curl -fL --proto =https -o "$tarball" "$2"
	curl -fsSL --proto =https -o "$tmp/SHA256SUMS" "$3"
	(cd "$tmp" && sha256sum -c --ignore-missing SHA256SUMS)
fi

# The release's own installer: the same code as its updates (app/pycam/updater.py).
mkdir "$tmp/release"
tar -xzf "$tarball" -C "$tmp/release"
python3 -I "$tmp"/release/pycam-*/app/pycam/updater.py install-file "$tarball" --user "$user"

# On the console, not a desktop: the camera takes the screen.
systemctl set-default multi-user.target >/dev/null
echo "pycam installé dans /opt/pycam pour $user. Redémarrer (sudo reboot) : l'appareil démarre sur la visée."
