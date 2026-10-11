#!/bin/sh
# Inventaire du Pi : OS, mémoire, caméra, libcamera/rpicam, affichage DRM.
# Usage depuis le poste de dev : ssh ael@192.168.1.165 'sh -s' < tools/pi-audit.sh

section() { printf '\n===== %s =====\n' "$1"; }

section "Système"
cat /proc/device-tree/model 2>/dev/null; echo
. /etc/os-release && echo "$PRETTY_NAME"
uname -srm
getconf LONG_BIT | sed 's/$/ bits userland/'
uptime -p

section "Mémoire"
free -m
grep -E 'Cma(Total|Free)' /proc/meminfo
vcgencmd get_mem arm 2>/dev/null; vcgencmd get_mem gpu 2>/dev/null

section "config.txt (lignes actives)"
grep -vE '^\s*(#|$)' /boot/firmware/config.txt 2>/dev/null || grep -vE '^\s*(#|$)' /boot/config.txt
section "cmdline.txt"
cat /boot/firmware/cmdline.txt 2>/dev/null

section "Paquets caméra"
dpkg-query -W -f='${Package} ${Version}\n' 'libcamera*' 'rpicam*' 'librpicam*' 'python3-picamera2' 2>/dev/null
section "Paquet de dev librpicam disponible ?"
apt-cache policy librpicam-app-dev 2>/dev/null | head -4
ls /usr/include/rpicam-apps 2>/dev/null && echo "(en-têtes rpicam-apps installés)"
pkg-config --modversion rpicam_app 2>/dev/null && echo "(pkg-config rpicam_app OK)"

section "Caméras détectées"
rpicam-hello --list-cameras 2>&1 | head -30

section "Affichage (DRM)"
for c in /sys/class/drm/card*-*; do
  [ -e "$c/status" ] && printf '%s: %s %s\n' "$(basename "$c")" "$(cat "$c/status")" "$(head -1 "$c/modes" 2>/dev/null)"
done
ls -l /dev/dri/ 2>/dev/null
systemctl get-default
systemctl is-active display-manager 2>/dev/null | sed 's/^/display-manager: /'

section "Outils de build"
for t in gcc g++ meson ninja cmake pkg-config python3 git; do
  printf '%-10s %s\n' "$t" "$(command -v $t >/dev/null && $t --version 2>&1 | head -1 || echo ABSENT)"
done

section "Python"
python3 -c 'import sys; print(sys.version)'
for m in PIL evdev aiohttp numpy picamera2; do
  python3 -c "import $m; print('$m', getattr($m, '__version__', 'ok'))" 2>/dev/null || echo "$m ABSENT"
done

section "Stockage / température / alimentation"
df -h / | tail -1
vcgencmd measure_temp 2>/dev/null
vcgencmd get_throttled 2>/dev/null
i2cdetect -y 1 2>/dev/null || echo "I2C non activé ou i2c-tools absent"
