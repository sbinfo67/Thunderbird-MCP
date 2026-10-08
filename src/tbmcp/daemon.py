"""The broker.

Exactly one process owns the loopback listener the Thunderbird add-on dials into,
and multiplexes requests from every `tbmcp serve` process onto that single
connection. That is what lets Claude Code and Codex drive one Thunderbird at the
same time.

Layout:

    add-on  ──WebSocket──▶  AddonSession   ◀── ControlServer ◀──JSON lines── serve
                             (one)                                            (many)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import ipc
from .errors import NotConnectedError, TimeoutError_, TransportError, from_wire
from .handshake import HandshakeLog, describe_handshake
from .profile import (
    BRIDGE_FILE,
    SOURCE_LABELS,
    ThunderbirdProfile,
    find_profile,
    running_profile_dirs,
    same_path,
)

log = logging.getLogger("tbmcp.daemon")

CHUNK_LIMIT = 4 * 1024 * 1024
PING_INTERVAL = 20.0
DEFAULT_IDLE_TIMEOUT = 900.0
HELLO_TIMEOUT = 10.0
"""How long a connected add-on has to send its `hello`. A module constant so the
handshake tests can shorten it without waiting out a real ten seconds."""
FOLLOW_INTERVAL = 5.0

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Serialise a failure into the protocol's `error` object.

    Deliberately reads `.message` rather than `str(exc)`. `TbmcpError.__str__` renders
    the code and the `needs` list into the text for humans, so relaying `str(exc)`
    appended them again on every hop: a "not connected" that crossed the add-on, the
    daemon and the bridge reached the caller as "... [NOT_CONNECTED] [NOT_CONNECTED]".
    Reading the field back keeps a relayed error identical to the one that was sent.
    """
    payload: dict[str, Any] = {
        "kind": getattr(exc, "kind", "internal"),
        "code": getattr(exc, "code", None),
        "message": getattr(exc, "message", None) or str(exc),
    }
    needs = getattr(exc, "needs", None)
    if needs:
        payload["needs"] = needs
    return payload


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    method: str
    on_progress: ProgressCb | None = None
    chunks: list[bytes] = field(default_factory=list)


class AddonSession:
    """One live add-on connection."""

    def __init__(self, ws: ServerConnection, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.hello = hello
        self.app: dict[str, Any] = hello.get("app") or {}
        self.capabilities: dict[str, Any] = hello.get("capabilities") or {}
        self.addon_version: str = str(hello.get("addonVersion", "?"))
        self.connected_at = time.time()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._closed = asyncio.Event()

    @property
    def has_experiment(self) -> bool:
        return bool(self.capabilities.get("experiment"))

    def describe(self) -> dict[str, Any]:
        return {
            "addonVersion": self.addon_version,
            "app": self.app,
            "experiment": self.has_experiment,
            "namespaces": self.capabilities.get("namespaces", []),
            "connectedForSeconds": round(time.time() - self.connected_at, 1),
            "inFlight": len(self._pending),
        }

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        on_progress: ProgressCb | None = None,
    ) -> Any:
        if self._closed.is_set():
            raise NotConnectedError()
        if method.startswith("x.") and not self.has_experiment:
            raise TransportError(
                f"{method} needs the privileged half of the add-on, which did not load. "
                "Reinstall with `tbmcp install-addon` and check Thunderbird's error console.",
                code="NO_EXPERIMENT",
            )
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        pending = _Pending(future=loop.create_future(), method=method, on_progress=on_progress)
        self._pending[request_id] = pending
        frame = {
            "t": "req",
            "id": request_id,
            "method": method,
            "params": params or {},
            "deadlineMs": int(timeout * 1000),
        }
        try:
            await self.ws.send(ipc.encode_message(frame).decode("utf-8"))
        except ConnectionClosed as exc:
            self._pending.pop(request_id, None)
            raise NotConnectedError() from exc
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ConnectionClosed):
                await self.ws.send(json.dumps({"t": "cancel", "id": request_id}))
            raise TimeoutError_(method, timeout) from None
        finally:
            self._pending.pop(request_id, None)

    async def pump(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        """Read frames until the add-on goes away."""
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    log.warning("dropping malformed frame from add-on")
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._handle(frame, on_event)
        except ConnectionClosed:
            pass
        finally:
            self._closed.set()
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(NotConnectedError())
            self._pending.clear()

    async def _handle(
        self, frame: dict[str, Any], on_event: Callable[[dict[str, Any]], None]
    ) -> None:
        kind = frame.get("t")
        if kind == "ping":
            with contextlib.suppress(ConnectionClosed):
                await self.ws.send(json.dumps({"t": "pong", "ts": frame.get("ts")}))
            return
        if kind == "event":
            on_event(frame)
            return
        if kind == "progress":
            pending = self._pending.get(int(frame.get("id", -1)))
            if pending and pending.on_progress:
                with contextlib.suppress(Exception):
                    await pending.on_progress(frame)
            return
        if kind != "res":
            return

        pending = self._pending.get(int(frame.get("id", -1)))
        if pending is None or pending.future.done():
            return

        chunk = frame.get("chunked")
        if isinstance(chunk, dict):
            payload = frame.get("result")
            if isinstance(payload, str):
                pending.chunks.append(base64.b64decode(payload))
            if not chunk.get("last"):
                return
            joined = b"".join(pending.chunks)
            try:
                pending.future.set_result(json.loads(joined.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                pending.future.set_exception(
                    TransportError(f"chunked result was not valid JSON: {exc}", code="BAD_CHUNK")
                )
            return

        if frame.get("ok"):
            pending.future.set_result(frame.get("result"))
        else:
            error = frame.get("error")
            pending.future.set_exception(
                from_wire(pending.method, error if isinstance(error, dict) else {})
            )

    async def keepalive(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(PING_INTERVAL)
            try:
                await asyncio.wait_for(self.ws.ping(), timeout=PING_INTERVAL)
            except (TimeoutError, ConnectionClosed):
                with contextlib.suppress(ConnectionClosed):
                    await self.ws.close(code=1011, reason="keepalive timeout")
                return


class Daemon:
    def __init__(
        self,
        profile: ThunderbirdProfile,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        event_buffer: int = 500,
        startup_lock: ipc.DaemonLock | None = None,
    ) -> None:
        self.profile = profile
        self.idle_timeout = idle_timeout
        self.startup_lock = startup_lock
        self.addon_token = ipc.new_token()
        self.control_token = ipc.new_token()
        self.handshakes = HandshakeLog()
        self.session: AddonSession | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=event_buffer)
        self.event_seq = 0
        self.started_at = time.time()
        self._clients = 0
        self._last_client_at = time.time()
        self._inflight: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._session_ready = asyncio.Event()
        self.thunderbird_profiles: list[Path] = []
        self._followed_at = 0.0
        self._addon_port: int | None = None
        self._control_port: int | None = None

    # ------------------------------------------------------------------ add-on side

    async def _serve_addon(self, ws: ServerConnection) -> None:
        """One add-on connection, from the upgrade to whatever ends it.

        Every exit records its own outcome in `self.handshakes`. Splitting what used
        to be one `except` is the point: "the socket closed before the hello" and
        "the hello was not JSON" are different faults with different remedies, and
        collapsing them left the user with nothing but a 4002 close code.
        """
        peer = ws.remote_address[0] if ws.remote_address else "?"
        if peer not in ("127.0.0.1", "::1", "localhost"):
            self.handshakes.resolve(ws, "non-loopback", close_code=4003, detail=str(peer))
            await ws.close(code=4003, reason="non-loopback peer")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT)
        except TimeoutError:
            self.handshakes.resolve(
                ws, "no-hello-timeout", close_code=4002, detail=f"{HELLO_TIMEOUT:g}s"
            )
            await ws.close(code=4002, reason="expected a hello frame")
            return
        except ConnectionClosed:
            # Already gone: there is nothing left to close, only to record.
            self.handshakes.resolve(ws, "closed-before-hello", close_code=ws.close_code)
            return
        try:
            hello = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            await self._reject_hello(ws, detail=str(exc))
            return
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            await self._reject_hello(ws, detail=f"first frame was {type(hello).__name__}")
            return
        if hello.get("token") != self.addon_token:
            self.handshakes.resolve(ws, "bad-token", close_code=4001)
            await ws.close(code=4001, reason="bad token")
            return
        try:
            protocol = int(hello.get("protocol", 0))
        except (TypeError, ValueError):
            # A non-numeric version used to raise straight out of the handler, so
            # websockets logged a traceback and the add-on was told nothing at all.
            protocol = -1
        if protocol != ipc.PROTOCOL_VERSION:
            self.handshakes.resolve(
                ws, "protocol-mismatch", close_code=4002, detail=f"protocol {protocol}"
            )
            await ws.close(code=4002, reason="protocol mismatch")
            return

        if self.session is not None:
            # A restarted Thunderbird, or a second window; the newest wins.
            self.handshakes.resolve(self.session.ws, "superseded", close_code=1012)
            with contextlib.suppress(ConnectionClosed):
                await self.session.ws.close(code=1012, reason="superseded")

        session = AddonSession(ws, hello)
        self.session = session
        self._session_ready.set()
        await ws.send(
            json.dumps({"t": "welcome", "protocol": ipc.PROTOCOL_VERSION, "server": "tbmcp"})
        )
        self.handshakes.resolve(ws, "welcomed", addon_version=session.addon_version)
        log.info(
            "add-on connected: %s %s (experiment=%s)",
            session.app.get("name", "Thunderbird"),
            session.app.get("version", "?"),
            session.has_experiment,
        )
        keepalive = asyncio.create_task(session.keepalive())
        try:
            await session.pump(self._record_event)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            if self.session is session:
                self.session = None
                self._session_ready.clear()
            # A no-op for a session we superseded: that outcome is already final.
            self.handshakes.resolve(ws, "disconnected", close_code=ws.close_code)

    async def _reject_hello(self, ws: ServerConnection, *, detail: str) -> None:
        self.handshakes.resolve(ws, "bad-hello", close_code=4002, detail=detail)
        await ws.close(code=4002, reason="expected a hello frame")

    def _connection_class(self) -> type[ServerConnection]:
        """A `ServerConnection` that reports the whole life of the TCP connection.

        `_serve_addon` only runs once the HTTP upgrade has succeeded, so a client
        that connects and never upgrades — which is exactly what Thunderbird did for
        half an hour — is invisible to it. These two hooks are the only place that
        sees it happen.
        """
        handshakes = self.handshakes

        class TelemetryConnection(ServerConnection):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                # After super(): it installs the transport that `remote_address`,
                # and so the peer port, is read from.
                super().connection_made(transport)
                handshakes.opened(self)

            def connection_lost(self, exc: Exception | None) -> None:
                super().connection_lost(exc)
                handshakes.resolve(self, "no-upgrade")

        return TelemetryConnection

    def _on_upgrade_request(self, conn: ServerConnection, request: Any) -> None:
        """`process_request`: note who is dialling in, then accept by returning None."""
        # Nothing here may raise: websockets turns an exception from process_request
        # into a 500 and rejects the connection, which would make the telemetry the
        # outage it is meant to explain.
        with contextlib.suppress(Exception):
            self.handshakes.note_request(
                conn,
                path=getattr(request, "path", None),
                origin=request.headers.get("Origin"),
                user_agent=request.headers.get("User-Agent"),
            )
        return None

    def _addon_server(self, **overrides: Any) -> serve:
        """The add-on listener, factored out so tests exercise the server `run` runs."""
        kwargs: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 0,
            "max_size": CHUNK_LIMIT * 2,
            "create_connection": self._connection_class(),
            "process_request": self._on_upgrade_request,
        }
        kwargs.update(overrides)
        return serve(self._serve_addon, **kwargs)

    def _record_event(self, frame: dict[str, Any]) -> None:
        self.event_seq += 1
        self.events.append(
            {
                "seq": self.event_seq,
                "at": time.time(),
                "name": frame.get("name"),
                "data": frame.get("data"),
            }
        )

    # ------------------------------------------------------------------ control side

    async def _serve_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] not in ("127.0.0.1", "::1"):
            writer.close()
            return
        authenticated = False
        self._clients += 1
        try:
            while True:
                message = await ipc.read_message(reader)
                if message is None:
                    return
                if not authenticated:
                    if message.get("t") != "auth" or message.get("token") != self.control_token:
                        await ipc.write_message(
                            writer,
                            {
                                "t": "res",
                                "id": message.get("id"),
                                "ok": False,
                                "error": {
                                    "kind": "transport",
                                    "code": "UNAUTHORIZED",
                                    "message": "bad control token",
                                },
                            },
                        )
                        return
                    authenticated = True
                    await ipc.write_message(writer, {"t": "ready", "id": message.get("id")})
                    continue
                # Dispatch concurrently so one slow call cannot block the rest of
                # this client's requests. Keeping a reference matters: a task held
                # only by the event loop can be garbage-collected mid-flight, which
                # would look like Thunderbird silently dropping a request.
                task = asyncio.create_task(self._dispatch(message, writer))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        except TransportError as exc:
            log.warning("control connection error: %s", exc)
        finally:
            self._clients -= 1
            self._last_client_at = time.time()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(self, message: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        timeout = float(message.get("timeout") or 30.0)
        wants_progress = bool(message.get("wantsProgress"))

        async def forward_progress(frame: dict[str, Any]) -> None:
            await ipc.write_message(
                writer,
                {
                    "t": "progress",
                    "id": request_id,
                    "done": frame.get("done"),
                    "total": frame.get("total"),
                    "message": frame.get("message"),
                },
            )

        try:
            result = await self._invoke(
                method,
                params,
                timeout=timeout,
                on_progress=forward_progress if wants_progress else None,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await ipc.write_message(
                    writer,
                    {"t": "res", "id": request_id, "ok": False, "error": error_payload(exc)},
                )
            return
        with contextlib.suppress(Exception):
            await ipc.write_message(
                writer, {"t": "res", "id": request_id, "ok": True, "result": result}
            )

    async def _invoke(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        on_progress: ProgressCb | None,
    ) -> Any:
        """Daemon-local methods first, then anything the add-on handles."""
        if method == "daemon.status":
            await self._follow_thunderbird()
            return self.status()
        if method == "daemon.events":
            since = int(params.get("since") or 0)
            limit = int(params.get("limit") or 100)
            selected = [e for e in self.events if e["seq"] > since][:limit]
            return {"events": selected, "latestSeq": self.event_seq}
        if method == "daemon.waitForThunderbird":
            wait = float(params.get("timeout") or 30.0)
            deadline = time.monotonic() + wait
            while not self._session_ready.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._not_connected()
                # The profile lookup can outlast the wait, so attachment and the
                # deadline both end it; a cancelled follow never publishes a move.
                racers = [
                    asyncio.ensure_future(self._session_ready.wait()),
                    asyncio.ensure_future(self._follow_then_pause()),
                ]
                try:
                    done, _ = await asyncio.wait(
                        racers, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                finally:
                    for task in racers:
                        task.cancel()
            return self.status()
        if method == "daemon.shutdown":
            self._stop.set()
            return {"stopping": True}

        session = self.session
        if session is None:
            raise self._not_connected()
        return await session.call(method, params, timeout=timeout, on_progress=on_progress)

    def _not_connected(self) -> NotConnectedError:
        """ "Not connected", carrying the diagnosis when we have one.

        Every tool call that fails this way is someone asking why, so the answer
        travels with the failure rather than waiting for them to run `doctor`.
        """
        return NotConnectedError(describe_handshake(self.handshakes.summary()))

    def status(self) -> dict[str, Any]:
        return {
            "daemon": {
                "pid": os.getpid(),
                "uptimeSeconds": round(time.time() - self.started_at, 1),
                "clients": self._clients,
                "eventsBuffered": len(self.events),
                "latestEventSeq": self.event_seq,
            },
            "profile": {
                "path": str(self.profile.path),
                "name": self.profile.name,
                "source": self.profile.source,
                "thunderbirdProfiles": [str(p) for p in self.thunderbird_profiles],
            },
            "thunderbird": self.session.describe() if self.session else None,
            "connected": self.session is not None,
            "handshake": self.handshakes.summary(),
            # Nothing the daemon logs reaches a terminal — it is spawned detached
            # with its streams on DEVNULL — so every reporter needs the path.
            "logFile": str(ipc.daemon_log_path()),
        }

    # ------------------------------------------------------------------ lifecycle

    def _write_bridge_file(self, port: int) -> None:
        payload = {
            "version": ipc.PROTOCOL_VERSION,
            "port": port,
            "token": self.addon_token,
            "pid": os.getpid(),
        }
        path = self.profile.path / BRIDGE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        ipc._restrict_permissions(path)
        self._addon_port = port
        log.info("pairing file written to %s (port %d)", path, port)

    def _remove_bridge_file(self, path: Path | None = None) -> None:
        path = path or self.profile.path / BRIDGE_FILE
        if _bridge_file_pid(path) == os.getpid():
            with contextlib.suppress(OSError):
                path.unlink()

    def _advertise(self) -> None:
        if self._control_port is None:
            return
        ipc.DaemonInfo(
            version=ipc.PROTOCOL_VERSION,
            port=self._control_port,
            token=self.control_token,
            pid=os.getpid(),
            profile=str(self.profile.path),
        ).write()

    async def _follow_thunderbird(self) -> None:
        """Move an auto-picked pairing file to the running Thunderbird profile."""
        if self.session is not None or time.monotonic() - self._followed_at < FOLLOW_INTERVAL:
            return
        self._followed_at = time.monotonic()
        self.thunderbird_profiles = await asyncio.to_thread(running_profile_dirs)
        # An add-on can attach while the lookup runs; its profile then stays put.
        if (
            self.session is not None
            or self.profile.source == "explicit"
            or not self.thunderbird_profiles
            or any(same_path(self.profile.path, p) for p in self.thunderbird_profiles)
            or self._addon_port is None
        ):
            return
        old = self.profile
        path = self.thunderbird_profiles[0]
        log.warning("moving pairing file from %s to running profile %s", old.path, path)
        self.profile = ThunderbirdProfile(path, path.name, False, path.parent.parent, "running")
        try:
            self._write_bridge_file(self._addon_port)
            self._advertise()
        except OSError:
            self._remove_bridge_file()
            self.profile = old
            log.exception("could not publish pairing file for %s", path)
            return
        self._remove_bridge_file(old.bridge_file)

    async def _follow_then_pause(self) -> None:
        """One follow attempt and the pause before the next, for `waitForThunderbird`."""
        await self._follow_thunderbird()
        await asyncio.sleep(FOLLOW_INTERVAL)

    def _cleanup(self) -> None:
        """Remove what we published — and only what is still ours.

        A superseded daemon runs this on its way out too. Unlinking unconditionally
        deleted the *winner's* pairing file and advertisement, which left the add-on
        holding a token nothing was listening for and `serve` with nothing to find.
        """
        self._remove_bridge_file()
        ipc.DaemonInfo.clear_if_owned(os.getpid())

    async def _watch_idle(self) -> None:
        if self.idle_timeout <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(30.0)
            if self._clients == 0 and (time.time() - self._last_client_at) > self.idle_timeout:
                log.info("no clients for %.0fs — shutting down", self.idle_timeout)
                self._stop.set()

    async def _watch_superseded(self) -> None:
        """Stand down if another daemon has taken over the advertisement.

        Only one daemon can hold the add-on's connection, but nothing stops two from
        binding their own sockets. When that happened the add-on stayed glued to the
        older one while `serve` talked to the newer, and every tool reported
        "Thunderbird is not connected" — with both processes looking healthy. Losing
        the advertisement is the signal to exit and let the winner have the add-on.
        """
        while not self._stop.is_set():
            await asyncio.sleep(20.0)
            winner = self.superseded_by()
            if winner is not None:
                log.warning("daemon %d has taken over the advertisement — standing down", winner)
                self._stop.set()

    @staticmethod
    def superseded_by() -> int | None:
        """The pid that owns the advertisement, if it is not us."""
        advertised = ipc.DaemonInfo.load()
        if advertised is None or advertised.pid == os.getpid():
            return None
        return advertised.pid

    async def run(self) -> None:
        async with (
            await asyncio.start_server(
                self._serve_control, host="127.0.0.1", port=0, limit=ipc.MAX_LINE
            ) as control,
            self._addon_server() as addon_server,
        ):
            control_port = control.sockets[0].getsockname()[1]
            addon_port = addon_server.sockets[0].getsockname()[1]
            self._control_port = control_port
            self._write_bridge_file(addon_port)
            self._advertise()
            # Published: the startup race is over, so stop blocking other starts.
            if self.startup_lock is not None:
                self.startup_lock.release()
                self.startup_lock = None
            log.info("daemon ready (control %d, add-on %d)", control_port, addon_port)
            watchdogs = [
                asyncio.create_task(self._watch_idle()),
                asyncio.create_task(self._watch_superseded()),
            ]
            try:
                await self._stop.wait()
            finally:
                for task in watchdogs:
                    task.cancel()
                for task in watchdogs:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self._cleanup()


def _bridge_file_pid(path) -> int | None:
    """The pid named by a pairing file, or None if it names nothing we can read."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload["pid"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def run_daemon(
    profile_hint: str | None = None,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    force: bool = False,
) -> int:
    # One daemon per user, because only one can hold the add-on's connection.
    #
    # The lock covers *startup only*, and is released as soon as the advertisement is
    # published. Holding it for the daemon's whole life looked tidier but could wedge
    # the system: if the holder ever stopped advertising, every later daemon stood
    # down without publishing anything, and the bridge was stuck until that process
    # died. The advertisement is the source of truth; the lock only stops two daemons
    # binding in the same instant, and `_watch_superseded` handles the rest.
    lock: ipc.DaemonLock | None = None
    if not force:
        lock = ipc.DaemonLock.acquire()
        if lock is None:
            # Someone is starting up. Give them a moment to advertise before deciding.
            existing = await _await_advertisement(timeout=6.0)
            if existing is not None:
                log.info(
                    "a daemon is already running (pid %d, control port %d); nothing to do",
                    existing.pid,
                    existing.port,
                )
                return 0
            log.warning("a stale start-up lock is in the way; taking it over")
            lock = ipc.DaemonLock.acquire_forcibly()

    profile = find_profile(profile_hint)
    if profile is None:
        log.error("no Thunderbird profile found; pass --profile with an explicit path")
        if lock is not None:
            lock.release()
        return 2

    log.info("selected profile %s (%s)", profile.path, SOURCE_LABELS[profile.source])

    daemon = Daemon(profile, idle_timeout=idle_timeout, startup_lock=lock)
    try:
        await daemon.run()
    except asyncio.CancelledError:
        daemon._cleanup()
        raise
    finally:
        if lock is not None:
            lock.release()
    return 0


async def _await_advertisement(*, timeout: float) -> ipc.DaemonInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = ipc.DaemonInfo.load()
        if info is not None:
            return info
        await asyncio.sleep(0.25)
    return ipc.DaemonInfo.load()
