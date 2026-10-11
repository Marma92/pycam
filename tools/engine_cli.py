#!/usr/bin/env python3
"""Petit client de test pour pycam-engine.

Exemples (sur le Pi) :
    python3 tools/engine_cli.py ping
    python3 tools/engine_cli.py state
    python3 tools/engine_cli.py controls
    python3 tools/engine_cli.py set ExposureTime=8000 AnalogueGain=2.0 AwbMode=Daylight
    python3 tools/engine_cli.py capture ~/DCIM/test/PYC_0001 --formats jpeg dng
    python3 tools/engine_cli.py events --seconds 5
    python3 tools/engine_cli.py snapshot /tmp/ecran.jpg     # l'image de visée affichée, aides comprises
    python3 tools/engine_cli.py raw '{"cmd": "configure", "strategy": "zsl"}'
"""

import argparse
import json
import os
import socket
import sys
import time

DEFAULT_SOCKET = "/run/pycam/engine.sock"


class Engine:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 1

    def read(self, timeout):
        """Next message, or None after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("engine closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, message, timeout=5.0, on_event=None):
        """Send a command and return its reply; events received meanwhile go to on_event."""
        message = dict(message, id=self.next_id)
        self.next_id += 1
        self.sock.sendall((json.dumps(message) + "\n").encode())
        deadline = time.monotonic() + timeout
        while True:
            msg = self.read(max(0.0, deadline - time.monotonic()))
            if msg is None:
                raise TimeoutError(f"no reply to {message['cmd']}")
            if msg.get("id") == message["id"]:
                return msg
            if on_event:
                on_event(msg)


def parse_value(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text  # enum names: AwbMode=Daylight


def show(msg):
    if msg.get("event") == "stats":
        msg = dict(msg, histogram=f"[{len(msg.get('histogram', []))} classes]")
    print(json.dumps(msg, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=os.environ.get("PYCAM_ENGINE_SOCKET", DEFAULT_SOCKET))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("state")
    sub.add_parser("controls")
    p = sub.add_parser("set")
    p.add_argument("assignments", nargs="+", help="Nom=valeur (valeur JSON ou nom d'enum)")
    p = sub.add_parser("capture")
    p.add_argument("path", help="chemin sans extension")
    p.add_argument("--formats", nargs="+", default=["jpeg", "dng"])
    p.add_argument("--quality", type=int, default=93)
    p.add_argument("--exif", nargs="*", default=[])
    p.add_argument("--timeout", type=float, default=30.0)
    p = sub.add_parser("events")
    p.add_argument("--seconds", type=float, default=3.0)
    p = sub.add_parser("snapshot")
    p.add_argument("path", help="fichier JPEG à écrire")
    p = sub.add_parser("raw")
    p.add_argument("json")
    args = parser.parse_args()

    engine = Engine(args.socket)

    if args.command == "ping":
        show(engine.call({"cmd": "ping"}))
    elif args.command == "state":
        show(engine.call({"cmd": "get_state"}))
    elif args.command == "controls":
        reply = engine.call({"cmd": "list_controls"})
        for name, info in sorted(reply.get("controls", {}).items()):
            print(f"{name:28} {json.dumps(info)}")
    elif args.command == "set":
        controls = {}
        for assignment in args.assignments:
            name, _, value = assignment.partition("=")
            controls[name] = parse_value(value)
        show(engine.call({"cmd": "set_controls", "controls": controls}))
    elif args.command == "capture":
        path = os.path.expanduser(args.path)
        reply = engine.call({"cmd": "capture", "path": path, "formats": args.formats,
                             "quality": args.quality, "exif": args.exif})
        show(reply)
        if not reply.get("ok"):
            sys.exit(1)
        capture_id = reply["capture_id"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            msg = engine.read(deadline - time.monotonic())
            if msg and msg.get("capture_id") == capture_id:
                show(msg)
                if msg.get("event") in ("capture_done", "capture_error"):
                    sys.exit(0 if msg["event"] == "capture_done" else 1)
        sys.exit("timeout waiting for capture_done")
    elif args.command == "snapshot":
        path = os.path.abspath(os.path.expanduser(args.path))
        reply = engine.call({"cmd": "snapshot", "path": path})
        if not reply.get("ok"):
            show(reply)
            sys.exit(1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            msg = engine.read(deadline - time.monotonic())
            if msg and msg.get("path") == path:
                show(msg)
                sys.exit(0 if msg.get("event") == "snapshot_done" else 1)
        sys.exit("timeout waiting for snapshot_done")
    elif args.command == "events":
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            msg = engine.read(deadline - time.monotonic())
            if msg:
                show(msg)
    elif args.command == "raw":
        show(engine.call(json.loads(args.json)))


if __name__ == "__main__":
    main()
