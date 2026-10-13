"""The updates, root's side (docs/architecture.md, « Les mises à jour »).

A release is a tarball built by GitHub Actions (.github/workflows/build.yml), the engine
compiled for the Pi: pycam-0.1.0-arm64.tar.gz, and the SHA256SUMS of its files. It is
unpacked in /opt/pycam/releases/<version>; /opt/pycam/current points at the one running,
/opt/pycam/previous at the one before it, to go back to.

The application decides when (it knows whether the camera is online, and idle): it writes
"check" or "install" in /run/pycam/update-request, which starts this script as root
(pycam-update.path, then pycam-update.service). The answer goes to
/var/lib/pycam-update/status.json, which the application reads. It cannot give anything
else: a release only comes from GitHub, over HTTPS, checked against its SHA256SUMS.

- check: the last release on GitHub; if it is newer, downloaded, checked (SHA-256),
  unpacked, the Debian packages it needs installed, and tried while the camera goes on
  (its engine loads with all its libraries, its application loads all its modules). One
  that cannot run here is set aside, never proposed; the others wait, ready.
- install: the release prepared, its engine tried again, then the camera restarted on it
  (a few seconds). If the application does not say it is ready within two minutes
  (/run/pycam/ready), back to the previous release, and this one is never proposed again.

Standard library only, run as root with `python3 -I`: nothing from the camera user's
environment. tools/install.sh runs `updater.py install-file <tarball> --user <user>` for
the first installation.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "Marma92/pycam"
ARCH = "arm64"
# The camera's services, and the updates' own.
SERVICES = ("pycam-engine.service", "pycam.service")
UNITS = SERVICES + ("pycam-update.service", "pycam-update.path")
HEALTH_SECONDS = 120  # for the application to say it is ready on the new release
MAX_BYTES = 512 * 1024**2  # a release is ~5 MB: anything that big is not one
NOTES_CHARS = 4000
APT_SECONDS = 1800
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """transient: worth trying again later (the network, apt); otherwise the release itself
    is at fault, and is not proposed again."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


class NotPublished(UpdateError):
    """No release on GitHub yet."""

    def __init__(self, message):
        super().__init__(message, transient=True)


def parse_version(text):
    m = VERSION.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(n) for n in m.groups()) if m else None


def newer(version, than):
    v, t = parse_version(version), parse_version(than)
    return v is not None and (t is None or v > t)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_url(url):
    """HTTPS only (a local server too, for the tests)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        raise UpdateError(f"adresse refusée : {url}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub sends its files from another host: followed, if still over HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _write_json(path, data, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _tail(text, lines=3):
    return " ".join((text or "").strip().splitlines()[-lines:])[-300:]


class Updater:
    def __init__(self, root="/opt/pycam", state="/var/lib/pycam-update", run_dir="/run/pycam",
                 systemd="/etc/systemd/system", tmpfiles="/etc/tmpfiles.d/pycam.conf",
                 polkit="/etc/polkit-1/rules.d/50-pycam.rules", api=None, runner=None):  # fmt: skip
        self.root = root
        self.state = state
        self.run_dir = run_dir
        self.systemd = systemd
        self.tmpfiles = tmpfiles
        self.polkit = polkit
        self.api = api or f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
        self.runner = runner or subprocess.run
        self.health_seconds = HEALTH_SECONDS
        self.as_root = getattr(os, "geteuid", lambda: -1)() == 0
        self._opener = urllib.request.build_opener(_CheckedRedirects)
        self._saved = 0.0

    # --- Where things are

    @property
    def releases(self):
        return os.path.join(self.root, "releases")

    @property
    def downloads(self):
        return os.path.join(self.state, "downloads")

    def link(self, name):
        return os.path.join(self.root, name)  # "current", "previous"

    def target(self, name):
        """The release a link points at, or None."""
        path = self.link(name)
        return os.path.realpath(path) if os.path.islink(path) and os.path.isdir(path) else None

    def current_version(self):
        current = self.target("current")
        return self.release_version(current) if current else None

    @staticmethod
    def release_version(path):
        try:
            with open(os.path.join(path, "VERSION"), encoding="ascii") as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None

    # --- Status, for the application (status.json) and the configuration (config.json)

    def load(self):
        try:
            with open(os.path.join(self.state, "status.json"), encoding="utf-8") as f:
                status = json.load(f)
            if isinstance(status, dict):
                return status
        except (OSError, ValueError):
            pass
        return {}

    def save(self, **changes):
        status = self.load()
        status.update(changes)
        status["current"] = self.current_version()
        status["time"] = time.time()
        os.makedirs(self.state, exist_ok=True)
        _write_json(os.path.join(self.state, "status.json"), status)
        self._saved = time.monotonic()
        if "message" in changes and changes["message"]:
            print(changes["message"], flush=True)
        return status

    def config(self):
        try:
            with open(os.path.join(self.state, "config.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # --- check: the last release, downloaded and prepared

    def _get(self, url, limit, accept="application/octet-stream"):
        check_url(url)
        request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"pycam-updater/{self.current_version()}"})
        try:
            with self._opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as e:
            if e.code == 404 and url == self.api:
                raise NotPublished("aucune version publiée") from e
            raise UpdateError(f"GitHub répond {e.code}", transient=True) from e
        except (OSError, http.client.HTTPException) as e:  # no network, DNS, TLS
            raise UpdateError(f"GitHub injoignable ({e})", transient=True) from e
        if len(data) > limit:
            raise UpdateError(f"réponse trop grande : {url}")
        return data

    def latest(self):
        """The last release published: its version, notes, date, file, url, size, sha256."""
        try:
            release = json.loads(self._get(self.api, 1 << 20, "application/vnd.github+json"))
            tag, assets = release["tag_name"], {a["name"]: a for a in release.get("assets", [])}
        except (ValueError, KeyError, TypeError) as e:
            raise UpdateError(f"réponse de GitHub illisible ({e})", transient=True) from e
        version = tag.lstrip("v")
        if parse_version(version) is None:
            raise UpdateError(f"version inconnue : {tag}", transient=True)
        name = f"pycam-{version}-{ARCH}.tar.gz"
        if name not in assets or "SHA256SUMS" not in assets:
            raise UpdateError(f"release {version} incomplète", transient=True)
        sums = {}
        for line in self._get(assets["SHA256SUMS"]["browser_download_url"], 1 << 16).decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                sums[parts[1].lstrip("*")] = parts[0]
        if name not in sums:
            raise UpdateError(f"pas d'empreinte pour {name}", transient=True)
        return {
            "version": version,
            "notes": (release.get("body") or "")[:NOTES_CHARS],
            "published": release.get("published_at") or "",
            "file": name,
            "url": assets[name]["browser_download_url"],
            "size": assets[name].get("size") or 0,
            "sha256": sums[name],
        }

    def check(self):
        """The last release; a newer one downloaded, unpacked and tried at once, in the
        background: installing it then only restarts the camera, and one that cannot run
        here is set aside before it is even proposed."""
        status, current = self.load(), self.current_version()
        failed = status.get("failed", [])
        known = status.get("available") or {}
        ready = status.get("state") == "ready" and self.release_version(os.path.join(self.releases, known.get("version", "?"))) == known.get("version")
        self.save(state="checking", message="Recherche d'une mise à jour", progress=0)
        try:
            release = self.latest()
        except NotPublished:
            self._prune()
            return self.save(state="up_to_date", available=None, checked=time.time(), message=f"À jour ({current}) : aucune version publiée")
        except UpdateError as e:
            if ready:  # the one prepared is still there
                return self.save(state="ready", message=f"Mise à jour {known['version']} prête")
            return self.save(state="error", message=f"Recherche impossible : {e}")
        version, url = release["version"], release.pop("url")
        if not newer(version, current) or version in failed:
            self._prune()
            message = f"{version} ignorée : son installation a échoué" if version in failed else f"À jour ({current})"
            return self.save(state="up_to_date", available=None, checked=time.time(), message=message)
        if ready and known.get("sha256") == release["sha256"]:
            return self.save(state="ready", available=release, checked=time.time(), message=f"Mise à jour {version} prête")
        self._prune()  # one prepared before, never installed
        path = os.path.join(self.downloads, release["file"])
        try:
            self._download(release, url, path)
            self.save(state="preparing", message=f"Préparation de {version}", progress=1)
            self._prepare(self._unpack(path, version), version)
        except UpdateError as e:
            self._prune()
            if e.transient:
                return self.save(state="error", available=None, message=f"{version} : {e}")
            return self.save(state="failed", available=None, failed=failed + [version], message=f"{version} écartée : {e}")
        finally:
            self._clean_downloads()
        return self.save(state="ready", available=release, checked=time.time(), progress=1, message=f"Mise à jour {version} prête")

    def _download(self, release, url, path):
        if release["size"] > MAX_BYTES:
            raise UpdateError("fichier trop grand")
        os.makedirs(self.downloads, exist_ok=True)
        free = shutil.disk_usage(self.downloads).free
        if free < 4 * release["size"] + 64 * 1024**2:  # the tarball, its files, and room left
            raise UpdateError("pas assez de place sur la carte", transient=True)
        check_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"pycam-updater/{self.current_version()}"})
        h, done, part = hashlib.sha256(), 0, path + ".part"
        self.save(state="downloading", message=f"Téléchargement de {release['version']}", progress=0)
        try:
            with self._opener.open(request, timeout=60) as response, open(part, "wb") as f:
                while block := response.read(1 << 16):
                    done += len(block)
                    if done > MAX_BYTES:
                        raise UpdateError("fichier trop grand")
                    h.update(block)
                    f.write(block)
                    if release["size"] and time.monotonic() - self._saved > 1:
                        self.save(progress=round(min(done / release["size"], 1), 2))
        except (OSError, http.client.HTTPException) as e:
            raise UpdateError(f"téléchargement interrompu ({e})", transient=True) from e
        if h.hexdigest() != release["sha256"]:
            raise UpdateError("empreinte SHA-256 différente", transient=True)
        os.replace(part, path)

    def _clean_downloads(self):
        try:
            names = os.listdir(self.downloads)
        except OSError:
            return
        for name in names:
            try:
                os.remove(os.path.join(self.downloads, name))
            except OSError:
                pass

    # --- install: the release prepared, switched to, checked

    def install(self):
        status = self.load()
        version = (status.get("available") or {}).get("version")
        if status.get("state") != "ready" or not version:
            return self.save(message="Aucune mise à jour à installer")
        path = os.path.join(self.releases, version)
        if self.release_version(path) != version:
            return self.save(state="error", available=None, message="Mise à jour introuvable : à chercher de nouveau")
        self.save(state="installing", message=f"Installation de {version}", progress=0)
        try:
            self._try_engine(path, version)  # the system may have changed since (apt)
        except UpdateError as e:
            self._prune()
            return self.save(state="failed", available=None, failed=status.get("failed", []) + [version],
                             message=f"{version} écartée : {e}")  # fmt: skip
        return self._switch(path, version, check_health=True)

    def install_file(self, tarball, user):
        """The first installation (tools/install.sh), from a release's tarball."""
        if not self.as_root:
            raise SystemExit("à lancer en root")
        os.makedirs(self.state, exist_ok=True)
        _write_json(os.path.join(self.state, "config.json"), {"user": user})
        version = self._tarball_version(tarball)
        if version == self.current_version():  # installed again: only the services set up again
            self.install_units(force=True)
            self.systemctl("restart", *SERVICES)
            return self.save(state="installed", message=f"pycam {version} déjà installé : services réinstallés")
        status = self._install(tarball, version)
        if status.get("state") != "installed":
            raise SystemExit(status.get("message") or "installation impossible")
        return status

    def _install(self, tarball, version):
        """A release's tarball unpacked, prepared, and run at once (the first installation)."""
        self.save(state="installing", message=f"Installation de {version}", progress=0)
        try:
            self._prepare(self._unpack(tarball, version), version)
        except UpdateError as e:
            self._prune()
            return self.save(state="failed", message=f"{version} non installée : {e}")
        return self._switch(os.path.join(self.releases, version), version, check_health=False)

    @staticmethod
    def _tarball_version(tarball):
        with tarfile.open(tarball) as tar:
            for member in tar:
                top = member.name.lstrip("./").split("/")[0]
                if top.startswith("pycam-") and parse_version(top[len("pycam-"):]):
                    return top[len("pycam-"):]
                break
        raise SystemExit(f"{tarball} : pas une release de pycam")

    def _unpack(self, tarball, version):
        dest = os.path.join(self.releases, version)
        if os.path.realpath(dest) in (self.target("current"), self.target("previous")) and os.path.isdir(dest):
            raise UpdateError(f"{version} est déjà installée", transient=True)
        tmp = dest + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            with tarfile.open(tarball) as tar:
                tar.extractall(tmp, filter="data")  # nothing outside, no device, no setuid
        except (OSError, tarfile.TarError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise UpdateError(f"archive illisible ({e})") from e
        top = os.path.join(tmp, f"pycam-{version}")
        needed = ("VERSION", "RELEASE.json", "engine/build/pycam-engine", "app/pycam/__main__.py", "system/pycam.service.in")
        missing = [n for n in needed if not os.path.isfile(os.path.join(top, n))]
        if missing or self.release_version(top) != version:
            shutil.rmtree(tmp, ignore_errors=True)
            raise UpdateError(f"archive incomplète ({', '.join(missing) or 'VERSION'})")
        if self.as_root:  # root's: the camera user cannot change what root runs
            for dirpath, dirnames, filenames in os.walk(top):
                for name in [dirpath] + [os.path.join(dirpath, n) for n in dirnames + filenames]:
                    os.lchown(name, 0, 0)
                    if not os.path.islink(name):
                        os.chmod(name, stat.S_IMODE(os.lstat(name).st_mode) & ~0o022)
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(top, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        return dest

    def _run(self, cmd, timeout=60, as_user=False, **kwargs):
        if as_user and self.as_root:
            cmd = ["runuser", "-u", self.config()["user"], "--"] + cmd
        try:
            return self.runner(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))

    def _prepare(self, path, version):
        """Before anything is stopped: what it needs installed, and itself tried."""
        try:
            with open(os.path.join(path, "RELEASE.json"), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError) as e:
            raise UpdateError(f"RELEASE.json illisible ({e})") from e
        missing = self.missing_packages(info.get("depends", []), info.get("built_with", {}))
        if missing:
            self.save(message=f"Installation des paquets : {' '.join(missing)}")
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            for cmd in (["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends"] + missing):
                result = self._run(cmd, timeout=APT_SECONDS, env=env)
                if result.returncode:
                    raise UpdateError(f"paquets non installés ({_tail(result.stderr)})", transient=True)
        self._try_engine(path, version)
        app = os.path.join(path, "app")
        self._run([sys.executable, "-m", "compileall", "-q", app], timeout=300)  # root's code: compiled once
        result = self._run([sys.executable, "-m", "pycam", "--check"], timeout=120, as_user=True, cwd=app)
        if result.returncode or version not in result.stdout:
            raise UpdateError(f"l'application ne se charge pas ({_tail(result.stderr or result.stdout)})")

    def _try_engine(self, path, version):
        """Every library and symbol of the engine resolved now (LD_BIND_NOW): a library that
        changed shows here, not at the camera's first photo."""
        engine = os.path.join(path, "engine", "build", "pycam-engine")
        result = self._run([engine, "--version"], timeout=30, as_user=True, env=dict(os.environ, LD_BIND_NOW="1"))
        if result.returncode or version not in result.stdout:
            raise UpdateError(f"le moteur ne démarre pas sur ce système ({_tail(result.stderr or result.stdout)})")

    def installed_version(self, package):
        result = self._run(["dpkg-query", "-W", "-f=${Status}\t${Version}", package])
        state, _, version = result.stdout.partition("\t")
        return version if result.returncode == 0 and state.endswith(" installed") else None

    def missing_packages(self, depends, built_with):
        """Debian packages to install: those it needs, and the libraries it was compiled
        against when they are older here (a newer one is tried by --version)."""
        missing = [p for p in depends if self.installed_version(p) is None]
        for package, version in built_with.items():
            have = self.installed_version(package)
            if have is None or self._run(["dpkg", "--compare-versions", have, "lt", version]).returncode == 0:
                missing.append(package)
        return sorted(set(missing))

    def _switch(self, path, version, check_health):
        before, before_previous = self.target("current"), self.target("previous")
        was_ready = self.ready_version() is not None
        self.systemctl("stop", *SERVICES)
        self._remove_ready()
        self._point("current", path)
        if before and before != path:
            self._point("previous", before)
        self.install_units()
        self.systemctl("start", *SERVICES)
        # Not checked if the camera was not working before either (no camera module, say).
        if not check_health or not was_ready or before is None or self.wait_ready(version):
            self._prune()
            self._clean_downloads()
            return self.save(state="installed", available=None, previous=self.release_version(before) if before else None,
                             progress=1, message=f"pycam {version} installé")  # fmt: skip
        # Back to the release that worked.
        self.systemctl("stop", *SERVICES)
        self._remove_ready()
        self._point("current", before)
        if before_previous:  # the links as they were
            self._point("previous", before_previous)
        elif os.path.lexists(self.link("previous")):
            os.remove(self.link("previous"))
        self.install_units()
        self.systemctl("start", *SERVICES)
        shutil.rmtree(path, ignore_errors=True)
        previous = self.release_version(before)
        return self.save(state="failed", available=None, failed=self.load().get("failed", []) + [version],
                         message=f"{version} n'a pas démarré : retour à {previous}")  # fmt: skip

    def _point(self, name, target):
        link, tmp = self.link(name), self.link(name) + ".new"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.relpath(target, self.root), tmp)
        os.replace(tmp, link)  # atomic: one release or the other, never none

    def _prune(self):
        """The releases other than the current and the previous ones, removed (one prepared
        and never installed, one half unpacked)."""
        keep = {self.target("current"), self.target("previous")}
        try:
            names = os.listdir(self.releases)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.releases, name)
            if os.path.realpath(path) not in keep:
                shutil.rmtree(path, ignore_errors=True)

    # --- The services

    def install_units(self, force=False):
        """The systemd units of the current release, and its other system files, filled in.
        One a release does not have (an older one, gone back to) is left as it is."""
        current, user = self.link("current"), self.config()["user"]
        changed = force
        files = [(u, os.path.join(self.systemd, u)) for u in UNITS]
        files += [("pycam.tmpfiles", self.tmpfiles), ("pycam.polkit.rules", self.polkit)]
        for unit, dest in files:
            try:
                with open(os.path.join(current, "system", unit + ".in"), encoding="utf-8") as f:
                    text = f.read().replace("@USER@", user).replace("@PYCAM@", current)
            except FileNotFoundError:
                continue
            try:
                with open(dest, encoding="utf-8") as f:
                    if f.read() == text:
                        continue
            except OSError:
                pass
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, dest)
            changed = True
        if changed:
            self.systemctl("daemon-reload")
            self.systemctl("enable", *SERVICES, "pycam-update.path")
            self.systemctl("start", "pycam-update.path")
            self._run(["systemd-tmpfiles", "--create", self.tmpfiles])

    def systemctl(self, *args):
        result = self._run(["systemctl", *args], timeout=120)
        if result.returncode:
            print(f"systemctl {' '.join(args)}: {_tail(result.stderr)}", file=sys.stderr, flush=True)
        return result

    def ready_version(self):
        """The version of the application running, once it is working (camera.py)."""
        try:
            with open(os.path.join(self.run_dir, "ready"), encoding="utf-8") as f:
                return json.load(f).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    def _remove_ready(self):
        try:
            os.remove(os.path.join(self.run_dir, "ready"))
        except OSError:
            pass

    def wait_ready(self, version):
        deadline = time.monotonic() + self.health_seconds
        while time.monotonic() < deadline:
            if self.ready_version() == version:
                return True
            time.sleep(1)
        return False

    # --- The application's requests

    def handle_requests(self):
        """Each request written in /run/pycam/update-request, until there is none left."""
        path = os.path.join(self.run_dir, "update-request")
        for _ in range(10):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return
            except OSError:  # a link, or something else: not from the application
                os.remove(path)
                continue
            with os.fdopen(fd, "rb") as f:
                action = f.read(64).decode("ascii", "replace").strip() if stat.S_ISREG(os.fstat(fd).st_mode) else ""
            os.remove(path)
            if action == "check":
                self.check()
            elif action == "install":
                self.install()
            else:
                print(f"unknown request {action!r}", file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="updater.py", description="Mises à jour de pycam (en root).")
    parser.add_argument("action", choices=("request", "check", "install", "install-file", "status"))
    parser.add_argument("tarball", nargs="?", help="install-file : l'archive d'une release")
    parser.add_argument("--user", help="install-file : l'utilisateur de l'appareil photo")
    args = parser.parse_args(argv)
    # The tests, or a local release server: a drop-in of pycam-update.service (root's).
    updater = Updater(api=os.environ.get("PYCAM_UPDATE_API"))
    if args.action == "request":
        updater.handle_requests()
    elif args.action == "check":
        updater.check()
    elif args.action == "install":
        updater.install()
    elif args.action == "install-file":
        if not args.tarball or not args.user:
            parser.error("install-file demande l'archive et --user")
        updater.install_file(args.tarball, args.user)
    if args.action != "request":  # the service's journal has each step already
        print(json.dumps(updater.load(), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
