#!/bin/sh
# Round trip test of the DNG writer, on the Pi: a real raw frame is written as DNG
# (plain and LJ92 compressed) by build/dng-roundtrip, then decoded by LibRaw, an
# independent reader; every sample must come back unchanged.
# Needs: libraw-bin (unprocessed_raw), python3-numpy, python3-pil, a DNG to take the frame from.
# Usage: sh tests/dng_roundtrip.sh <any DNG written by the camera>
set -eu

source_dng="$1"
cd "$(dirname "$0")/.."
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cp "$source_dng" "$work/source.dng"
unprocessed_raw -T "$work/source.dng" >/dev/null
python3 - "$work" <<'EOF'
import sys
import numpy as np
from PIL import Image
work = sys.argv[1]
a = np.asarray(Image.open(f"{work}/source.dng.tiff")).astype(np.uint16)
a.tofile(f"{work}/frame.u16")
open(f"{work}/size", "w").write(f"{a.shape[1]} {a.shape[0]}\n")
EOF
read -r width height < "$work/size"
build/dng-roundtrip "$work/frame.u16" "$width" "$height" "$work/out" >/dev/null

for dng in "$work/out_plain.dng" "$work/out_lj92.dng"; do
	unprocessed_raw -T "$dng" >/dev/null
done
python3 - "$work" "$source_dng" <<'EOF'
import os, sys
import numpy as np
from PIL import Image
work, source = sys.argv[1], sys.argv[2]
ref = np.fromfile(f"{work}/frame.u16", dtype=np.uint16)
ok = True
for name in ("out_plain", "out_lj92"):
    got = np.asarray(Image.open(f"{work}/{name}.dng.tiff")).astype(np.uint16).ravel()
    same = got.shape == ref.shape and np.array_equal(got, ref)
    ok &= same
    size = os.path.getsize(f"{work}/{name}.dng") / 1e6
    print(f"{name}: {size:.1f} Mo, LibRaw relit {'les mêmes échantillons' if same else 'DES ÉCHANTILLONS DIFFÉRENTS'}")
sys.exit(0 if ok else 1)
EOF
