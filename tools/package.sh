#!/bin/sh
# Fait l'archive d'une release : pycam-<version>-<arch>.tar.gz et SHA256SUMS, dans <sortie>.
# Le moteur doit être compilé, sur Raspberry Pi OS 64 bits ou son équivalent (la CI :
# .github/workflows/build.yml), dans le dépôt git (seuls les fichiers suivis partent).
# Usage : sh tools/package.sh engine/build dist
set -eu

build="$(realpath "${1:?dossier de compilation du moteur}")"
mkdir -p "${2:?dossier de sortie}"
out="$(realpath "$2")"
cd "$(dirname "$0")/.."

version="$(cat VERSION)"
arch="$(dpkg --print-architecture)"
name="pycam-$version"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/$name/engine/build"

# What the camera runs: the application (without its tests), the services, the tools, the
# documentation, the ISP tuning, and the engine compiled.
git ls-files -z VERSION LICENSE README.md CHANGELOG.md app/pycam system tools docs engine/tuning \
	| xargs -0 cp --parents -t "$stage/$name"
install -m 755 "$build/pycam-engine" "$stage/$name/engine/build/pycam-engine"
strip "$stage/$name/engine/build/pycam-engine"

# The Debian packages it needs: those of the libraries the engine names, and the
# application's. The camera installs those it lacks (app/pycam/updater.py), and brings
# libcamera and rpicam up to the versions the engine was compiled against.
libs="$(readelf -d "$build/pycam-engine" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')"
engine_pkgs="$(for lib in $libs; do dpkg -S "*/$lib" 2>/dev/null | cut -d: -f1; done | sort -u)"
[ -n "$engine_pkgs" ] || { echo "paquets du moteur introuvables" >&2; exit 1; }
app_pkgs="python3 python3-aiohttp python3-evdev python3-pil ffmpeg fonts-dejavu-core"

python3 - "$stage/$name/RELEASE.json" "$version" "$(git rev-parse HEAD)" "$arch" $engine_pkgs -- $app_pkgs <<'EOF'
import json, subprocess, sys, time

path, version, commit, arch, *rest = sys.argv[1:]
cut = rest.index("--")
engine, app = rest[:cut], rest[cut + 1:]


def installed(pkg):
    return subprocess.run(["dpkg-query", "-W", "-f=${Version}", pkg], capture_output=True, text=True, check=True).stdout


info = {
    "version": version,
    "commit": commit,
    "arch": arch,
    "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "depends": sorted(set(engine + app)),
    # The camera stack changes its ABI between versions: at least these, on the camera.
    "built_with": {p: installed(p) for p in engine if p.startswith(("libcamera", "librpicam"))},
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(info, f, indent=1)
    f.write("\n")
EOF

tarball="$name-$arch.tar.gz"
tar -C "$stage" --owner=0 --group=0 --numeric-owner --sort=name -czf "$out/$tarball" "$name"
(cd "$out" && sha256sum "$tarball" > SHA256SUMS)
echo "$out/$tarball"
cat "$stage/$name/RELEASE.json"
