"""Client of pycam-engine (docs/protocol.md): asyncio, reconnects by itself."""

import asyncio
import itertools
import json
import logging
import socket

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/pycam/engine.sock"


class EngineError(Exception):
    """The engine refused a command; `code` is the protocol error code ("busy"…)."""

    def __init__(self, reply):
        super().__init__(f"{reply.get('error')}: {reply.get('detail', '')}")
        self.code = reply.get("error")


class EngineClient:
    """Connection to the engine, re-established whenever it is lost.

    on_event(msg) is called for each event, from the event loop: it must not block.
    on_connect() is awaited after each (re)connection, to push the whole state again.
    """

    def __init__(self, path=DEFAULT_SOCKET, on_event=None, on_connect=None, on_disconnect=None):
        self.path = path
        self.on_event = on_event or (lambda msg: None)
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._writer = None
        self._pending = {}
        self._ids = itertools.count(1)

    @property
    def connected(self):
        return self._writer is not None

    async def call(self, cmd, timeout=10.0, **params):
        """Sends a command and returns its reply; raises EngineError if it is refused."""
        if not self._writer:
            raise ConnectionError("engine not connected")
        msg_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            self._writer.write((json.dumps({"id": msg_id, "cmd": cmd, **params}) + "\n").encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)
        if not reply.get("ok"):
            raise EngineError(reply)
        return reply

    async def run(self):
        """Connects, reads, reconnects. Runs until cancelled."""
        warned = False
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path, limit=1 << 20)
            except OSError as e:  # not there yet, refused, or no permission
                if not warned:
                    log.warning("engine not reachable at %s (%s), retrying", self.path, e)
                    warned = True
                await asyncio.sleep(0.5)
                continue
            warned = False
            log.info("connected to the engine")
            self._writer = writer
            sync = asyncio.create_task(self._sync()) if self.on_connect else None
            try:
                await self._read(reader)
            finally:
                if sync:
                    sync.cancel()
                self._writer = None
                writer.close()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("engine connection lost"))
                self._pending.clear()
                if self.on_disconnect:
                    self.on_disconnect()
            log.warning("engine connection lost")
            await asyncio.sleep(0.5)

    async def _sync(self):
        try:
            await self.on_connect()
        except Exception:
            log.exception("engine synchronisation failed")

    async def _read(self, reader):
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.error("invalid message from the engine: %r", line[:200])
                    continue
                if "event" in msg:
                    try:
                        self.on_event(msg)
                    except Exception:  # an event handled badly: the next ones still are
                        log.exception("event %s failed", msg.get("event"))
                else:
                    future = self._pending.get(msg.get("id"))
                    if future and not future.done():
                        future.set_result(msg)
        except ConnectionError:
            pass
        except ValueError as e:  # a line beyond the limit: connect again
            log.error("engine connection: %s", e)


def call_blocking(path, cmd, timeout=2.0, **params):
    """One command on a short-lived connection, outside asyncio (e.g. while shutting down)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"id": "x", "cmd": cmd, **params}) + "\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("engine closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                if msg.get("id") == "x":
                    if not msg.get("ok"):
                        raise EngineError(msg)
                    return msg
