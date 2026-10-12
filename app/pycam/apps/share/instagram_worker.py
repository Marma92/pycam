"""Instagram through instagrapi, in a process of its own (publishers.Instagram).

instagrapi and its dependencies (pydantic...) are installed apart from the system's Python
(tools/install-share.sh), in versions newer than Debian's. Loaded in the camera's process,
they meet the system's older ones (typing_extensions, already imported) and break; here
they come first. The camera does not keep them in memory either.

stdin: one JSON request {"action": "check" | "upload", "settings", "user", "password",
"sessionid", "image", "caption"}; stdout: one JSON answer, {"ok": true, "username"} or
{"ok": true, "code"}, or {"ok": false, "error": the exception's class, "message"}.
"""

import json
import os
import sys


def vendor_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "pycam", "vendor")


def dump(client, path):
    """instagrapi's device and session: kept after every attempt, successful or not, since
    Instagram recognises the camera by it (as SPP does)."""
    client.dump_settings(path)
    os.chmod(path, 0o600)


def run(request):
    from instagrapi import Client

    client = Client()
    path = request["settings"]
    if os.path.exists(path):
        client.load_settings(path)
    else:
        dump(client, path)  # the device made now, kept before a verification aborts the login
    try:
        if request.get("sessionid"):
            client.login_by_sessionid(request["sessionid"])
        else:
            client.login(request["user"], request["password"])
        if request["action"] == "check":
            return {"ok": True, "username": client.username or request["user"]}
        media = client.photo_upload(request["image"], request["caption"])
        return {"ok": True, "code": media.code}
    finally:
        dump(client, path)


def main():
    request = json.load(sys.stdin)
    sys.path.insert(0, vendor_dir())
    try:
        answer = run(request)
    except Exception as e:  # instagrapi's, requests', or its absence
        answer = {"ok": False, "error": type(e).__name__, "message": str(e)[:500]}
    sys.stdout.write("\n" + json.dumps(answer) + "\n")


if __name__ == "__main__":
    main()
