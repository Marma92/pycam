"""The updates, root's side (updater.py): against a fake GitHub on 127.0.0.1, a fake
systemctl, and releases made here. Symbolic links: not on Windows."""

import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

from pycam import updater
from pycam.updater import UNITS, Updater, newer

REPO_PATH = "/repos/test/pycam/releases/latest"


def make_release(version, engine="ok", app="ok", extra=None):
    """A release's tarball, in memory: a script for the engine, a package for the application."""
    top = f"pycam-{version}"
    engine_script = {"ok": f"#!/bin/sh\necho pycam-engine {version}\n", "broken": "#!/bin/sh\necho 'libcamera.so.0.9: not found' >&2\nexit 127\n"}
    main = {"ok": f"print('pycam {version}')\n", "broken": "raise SystemExit('SyntaxError: invalid syntax')\n"}
    files = {
        "VERSION": f"{version}\n",
        "RELEASE.json": json.dumps({"version": version, "depends": ["python3-pil"], "built_with": {"librpicam-app1": "1.13.0-1"}}),
        "engine/build/pycam-engine": engine_script[engine],
        "app/pycam/__init__.py": "",
        "app/pycam/__main__.py": main[app],
    }
    for unit in UNITS + ("pycam.tmpfiles",):
        files[f"system/{unit}.in"] = f"# {unit} {version}\nUser=@USER@\nExecStart=@PYCAM@/engine/build/pycam-engine\n"
    files.update(extra or {})
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name if name.startswith("../") else f"{top}/{name}")
            info.size, info.mode = len(data), 0o755 if name.endswith("pycam-engine") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class FakeGitHub(http.server.ThreadingHTTPServer):
    """releases/latest, and its files (behind a redirect, as GitHub does)."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.release = None  # (version, tarball, sums)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def publish(self, version, tarball, sums=None):
        name = f"pycam-{version}-arm64.tar.gz"
        sums = sums or f"{hashlib.sha256(tarball).hexdigest()}  {name}\n"
        self.release = (version, {name: tarball, "SHA256SUMS": sums.encode()})


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server = self.server
        version, files = server.release or (None, {})
        if self.path == REPO_PATH and version:
            assets = [{"name": n, "browser_download_url": f"{server.url}/download/{n}", "size": len(d)} for n, d in files.items()]
            body = {"tag_name": f"v{version}", "body": f"Nouveau dans {version}", "published_at": "2026-10-13T10:00:00Z", "assets": assets}
            return self.send(200, json.dumps(body).encode(), [("Content-Type", "application/json")])
        if self.path.startswith("/download/"):
            return self.send(302, headers=[("Location", f"{server.url}/files/{self.path[len('/download/'):]}")])
        if self.path.startswith("/files/") and self.path[len("/files/") :] in files:
            return self.send(200, files[self.path[len("/files/") :]])
        self.send(404)


class FakeSystem:
    """systemctl, dpkg and apt-get as the updater calls them; the rest runs."""

    def __init__(self, updater):
        self.updater = updater
        self.calls = []
        self.packages = {"python3-pil": "11.1.0-5", "librpicam-app1": "1.13.0-1"}
        self.app_works = lambda version: True  # does the application start on this release?

    def __call__(self, cmd, **kwargs):
        name = cmd[0]
        if name in ("systemctl", "systemd-tmpfiles", "apt-get", "dpkg", "dpkg-query"):
            self.calls.append(list(cmd))
        if name == "systemctl" and cmd[1] == "start":
            version = self.updater.current_version()
            if self.app_works(version):
                with open(os.path.join(self.updater.run_dir, "ready"), "w") as f:
                    json.dump({"version": version}, f)
        if name == "dpkg-query":
            have = self.packages.get(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0 if have else 1, f"install ok installed\t{have}" if have else "", "")
        if name == "dpkg":  # --compare-versions a lt b, on these simple versions
            return subprocess.CompletedProcess(cmd, 0 if cmd[2] < cmd[4] else 1, "", "")
        if name == "apt-get" and cmd[1] == "install":
            for p in cmd[4:]:
                self.packages[p] = "99"
        if name in ("systemctl", "systemd-tmpfiles", "apt-get"):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kwargs)

    def called(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@unittest.skipIf(os.name == "nt", "symbolic links")
class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.github = FakeGitHub()
        self.addCleanup(self.github.server_close)
        self.addCleanup(self.github.shutdown)
        d = self.tmp.name
        run_dir = os.path.join(d, "run")
        os.makedirs(run_dir)
        self.updater = Updater(root=os.path.join(d, "opt"), state=os.path.join(d, "state"), run_dir=run_dir,
                               systemd=os.path.join(d, "systemd"), tmpfiles=os.path.join(d, "tmpfiles", "pycam.conf"),
                               api=self.github.url + REPO_PATH)  # fmt: skip
        self.updater.as_root = False  # the tests may run as root (the CI's container)
        self.updater.health_seconds = 2
        self.system = FakeSystem(self.updater)
        self.updater.runner = self.system
        os.makedirs(self.updater.state)
        with open(os.path.join(self.updater.state, "config.json"), "w") as f:
            json.dump({"user": "camera"}, f)
        # 0.1.0 installed, the camera working on it.
        self.install_file("0.1.0")

    def install_file(self, version, **kwargs):
        path = os.path.join(self.tmp.name, f"pycam-{version}.tar.gz")
        with open(path, "wb") as f:
            f.write(make_release(version, **kwargs))
        return self.updater._install(path, version)

    def status(self):
        return self.updater.load()

    def test_versions(self):
        self.assertTrue(newer("0.10.0", "0.9.9"))
        self.assertTrue(newer("v1.0.0", None))
        self.assertFalse(newer("0.1.0", "0.1.0"))
        self.assertFalse(newer("0.2.0-rc1", "0.1.0"))  # not a release the camera takes

    def test_first_installation(self):
        u = self.updater
        self.assertEqual((u.current_version(), self.status()["state"]), ("0.1.0", "installed"))
        with open(os.path.join(u.systemd, "pycam.service")) as f:
            self.assertEqual(f.read(), f"# pycam.service 0.1.0\nUser=camera\nExecStart={u.link('current')}/engine/build/pycam-engine\n")
        self.assertTrue(os.path.exists(u.tmpfiles))
        self.assertTrue(self.system.called("systemctl", "enable", "pycam-engine.service", "pycam.service", "pycam-update.path"))
        self.assertTrue(self.system.called("systemctl", "start", "pycam-engine.service", "pycam.service"))

    def test_up_to_date(self):
        status = self.updater.check()  # nothing published yet: GitHub answers 404
        self.assertEqual((status["state"], status["message"]), ("up_to_date", "À jour (0.1.0) : aucune version publiée"))
        self.github.publish("0.1.0", make_release("0.1.0"))
        self.assertEqual(self.updater.check()["state"], "up_to_date")

    def test_a_newer_release_is_downloaded(self):
        tarball = make_release("0.2.0")
        self.github.publish("0.2.0", tarball)
        self.system.calls.clear()
        status = self.updater.check()
        self.assertEqual((status["state"], status["available"]["version"], status["available"]["notes"]), ("ready", "0.2.0", "Nouveau dans 0.2.0"))
        # Prepared at once: unpacked and tried, while the camera goes on.
        self.assertEqual(self.updater.release_version(os.path.join(self.updater.releases, "0.2.0")), "0.2.0")
        self.assertEqual((self.updater.current_version(), os.listdir(self.updater.downloads)), ("0.1.0", []))
        self.assertEqual(self.system.called("systemctl", "stop"), [])
        self.github.release = None  # already there: not again
        self.github.publish("0.2.0", b"not downloaded again", sums=f"{hashlib.sha256(tarball).hexdigest()}  pycam-0.2.0-arm64.tar.gz\n")
        self.assertEqual(self.updater.check()["state"], "ready")
        self.updater.api = "http://127.0.0.1:9/nothing"  # offline: still ready
        self.assertEqual(self.updater.check()["state"], "ready")

    def test_a_damaged_download_is_refused(self):
        self.github.publish("0.2.0", make_release("0.2.0"), sums=f"{'0' * 64}  pycam-0.2.0-arm64.tar.gz\n")
        status = self.updater.check()
        self.assertEqual(status["state"], "error")
        self.assertIn("SHA-256", status["message"])
        self.assertEqual((os.listdir(self.updater.downloads), os.listdir(self.updater.releases)), ([], ["0.1.0"]))

    def test_no_network(self):
        self.updater.api = "http://127.0.0.1:9/nothing"
        self.assertEqual(self.updater.check()["state"], "error")
        self.updater.api = "http://example.com/releases"  # not over HTTPS
        self.assertIn("refusée", self.updater.check()["message"])

    def test_installed_and_checked(self):
        self.github.publish("0.2.0", make_release("0.2.0"))
        self.updater.check()
        self.system.calls.clear()
        status = self.updater.install()
        u = self.updater
        self.assertEqual((status["state"], status["current"], status["previous"]), ("installed", "0.2.0", "0.1.0"))
        self.assertEqual((u.current_version(), u.release_version(u.target("previous"))), ("0.2.0", "0.1.0"))
        with open(os.path.join(u.systemd, "pycam.service")) as f:
            self.assertIn("0.2.0", f.read())
        names = [c[1] for c in self.system.called("systemctl")]
        self.assertLess(names.index("stop"), names.index("daemon-reload"))
        self.assertEqual(self.system.called("apt-get"), [])  # nothing missing
        self.assertEqual(os.listdir(u.downloads), [])
        # A third: the first one goes.
        self.github.publish("0.3.0", make_release("0.3.0"))
        u.check()
        u.install()
        self.assertEqual(sorted(os.listdir(u.releases)), ["0.2.0", "0.3.0"])

    def test_back_to_the_previous_release_if_the_camera_does_not_start(self):
        self.github.publish("0.2.0", make_release("0.2.0"))
        self.updater.check()
        self.system.app_works = lambda version: version != "0.2.0"
        status = self.updater.install()
        self.assertEqual((status["state"], status["current"], status["failed"]), ("failed", "0.1.0", ["0.2.0"]))
        self.assertIn("retour à 0.1.0", status["message"])
        self.assertFalse(os.path.exists(os.path.join(self.updater.releases, "0.2.0")))
        with open(os.path.join(self.updater.systemd, "pycam.service")) as f:
            self.assertIn("0.1.0", f.read())  # its units back too
        self.assertEqual(self.updater.ready_version(), "0.1.0")
        self.assertEqual(self.updater.target("previous"), None)  # as it was before
        self.assertEqual(self.updater.check()["state"], "up_to_date")  # never proposed again
        self.github.publish("0.2.1", make_release("0.2.1"))  # the next one is
        self.assertEqual(self.updater.check()["state"], "ready")
        self.assertEqual(self.updater.install()["state"], "installed")
        # Once more, with a previous release: kept as it was.
        self.github.publish("0.3.0", make_release("0.3.0"))
        self.updater.check()
        self.system.app_works = lambda version: version != "0.3.0"
        self.assertEqual(self.updater.install()["current"], "0.2.1")
        self.assertEqual(self.updater.release_version(self.updater.target("previous")), "0.1.0")

    def test_not_checked_when_the_camera_did_not_work_before(self):
        os.remove(os.path.join(self.updater.run_dir, "ready"))  # no camera module, say
        self.github.publish("0.2.0", make_release("0.2.0"))
        self.updater.check()
        self.system.app_works = lambda version: False
        self.assertEqual(self.updater.install()["state"], "installed")

    def test_a_release_that_cannot_run_here_stops_nothing(self):
        # Found out as soon as it is downloaded: never proposed.
        for version, broken, why in (("0.2.0", {"engine": "broken"}, "libcamera.so.0.9"), ("0.2.1", {"app": "broken"}, "SyntaxError")):
            self.github.publish(version, make_release(version, **broken))
            self.system.calls.clear()
            status = self.updater.check()
            self.assertEqual((status["state"], status["current"], status["available"]), ("failed", "0.1.0", None))
            self.assertIn(version, status["failed"])
            self.assertIn(why, status["message"])
            self.assertEqual(self.system.called("systemctl", "stop"), [])  # the camera went on
            self.assertEqual(os.listdir(self.updater.releases), ["0.1.0"])
        # Or when the system changed between the download and the installation.
        self.github.publish("0.2.2", make_release("0.2.2"))
        self.updater.check()
        engine = os.path.join(self.updater.releases, "0.2.2", "engine", "build", "pycam-engine")
        with open(engine, "w") as f:
            f.write("#!/bin/sh\nexit 127\n")
        status = self.updater.install()
        self.assertEqual((status["state"], status["current"]), ("failed", "0.1.0"))
        self.assertEqual(self.system.called("systemctl", "stop"), [])

    def test_what_it_needs_from_debian(self):
        self.system.packages = {"librpicam-app1": "1.12.0-1"}  # older than the engine's, and no Pillow
        self.github.publish("0.2.0", make_release("0.2.0"))
        self.updater.check()
        self.assertEqual(self.updater.install()["state"], "installed")
        self.assertEqual(self.system.called("apt-get", "install")[0][4:], ["librpicam-app1", "python3-pil"])

    def test_an_archive_writing_outside_is_refused(self):
        self.github.publish("0.2.0", make_release("0.2.0", extra={"../../evil": "x"}))
        self.assertEqual(self.updater.check()["state"], "failed")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil")))

    def test_requests_from_the_application(self):
        request = os.path.join(self.updater.run_dir, "update-request")
        self.github.publish("0.2.0", make_release("0.2.0"))
        with open(request, "w") as f:
            f.write("check\n")
        self.updater.handle_requests()
        self.assertFalse(os.path.exists(request))
        self.assertEqual(self.status()["state"], "ready")
        os.symlink("/etc/passwd", request)  # not from the application: removed, not read
        self.updater.handle_requests()
        self.assertFalse(os.path.lexists(request))
        self.assertEqual(self.status()["state"], "ready")

    def test_run_as_a_script(self):
        # python3 -I updater.py: the standard library only, nothing from the package.
        result = subprocess.run([sys.executable, "-I", updater.__file__, "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
