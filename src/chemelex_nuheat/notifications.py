"""Bounded SignalR JSON v1 thermostat invalidations; REST remains authoritative."""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    DummyCookieJar,
    TraceConfig,
    WSMsgType,
    WSServerHandshakeError,
)

from .client import (
    API_BASE_URL,
    AccessTokenProvider,
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatRateLimitError,
)

RECORD_SEPARATOR = "\x1e"
MAX_RECORD_BYTES = 32768


class NotificationProtocolError(RuntimeError):
    """A hub response is unsupported or malformed; contains no raw payload."""


def notification_url(base_url: str) -> str:
    """Use the configured REST origin, never query tokens or redirects."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (
            parsed.scheme == "http"
            and parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        )
    ):
        raise ValueError("Notification origin must be HTTPS or local test HTTP")
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            "/v2/notificationsHost",
            "",
            "",
        )
    )


class JsonRecords:
    """Decode bounded records across WebSocket message boundaries."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> list[dict[str, Any]]:
        parts = (self._pending + text).split(RECORD_SEPARATOR)
        self._pending = parts.pop()
        result = []
        for record in [*parts, self._pending]:
            if len(record.encode("utf-8")) > MAX_RECORD_BYTES:
                raise NotificationProtocolError("Notification record limit exceeded")
        for record in parts:
            try:
                value = json.loads(record)
            except (ValueError, RecursionError):
                raise NotificationProtocolError("Invalid notification JSON") from None
            if not isinstance(value, dict):
                raise NotificationProtocolError("Invalid notification record")
            result.append(value)
        return result


class NuHeatNotifications:
    """One cancellable account connection; callbacks only request REST reads."""

    def __init__(
        self,
        session: ClientSession,
        access_token_provider: AccessTokenProvider,
        known_devices: Callable[[], set[str]],
        invalidate: Callable[[], None],
        *,
        base_url: str = API_BASE_URL,
        cooldown_remaining: Callable[[], float] = lambda: 0.0,
        set_cooldown: Callable[[str | None], None] | None = None,
        connect_timeout: float = 30,
        silence_timeout: float = 45,
        ping_interval: float = 15,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not all(
            math.isfinite(v) and v > 0
            for v in (connect_timeout, silence_timeout, ping_interval)
        ):
            raise ValueError("Notification timeouts must be finite and positive")
        self._url = notification_url(base_url)
        trace = TraceConfig()

        async def reject_redirect(
            _session: ClientSession, _context: Any, _params: Any
        ) -> None:
            raise NotificationProtocolError("Notification redirects are not supported")

        trace.on_request_redirect.append(reject_redirect)
        # Reuse the connector, but isolate cookies and reject every redirect
        # before aiohttp follows it. Closing this session never closes HA's.
        self._session = ClientSession(
            connector=session.connector,
            connector_owner=False,
            cookie_jar=DummyCookieJar(),
            trace_configs=[trace],
        )
        self._token = access_token_provider
        self._known = known_devices
        self._invalidate = invalidate
        self._cooldown = cooldown_remaining
        self._set_cooldown = set_cooldown
        self._connect_timeout = connect_timeout
        self._silence_timeout = silence_timeout
        self._ping_interval = ping_interval
        self._sleep = sleep
        self._jitter = jitter
        self._timestamps: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._socket: ClientWebSocketResponse | None = None
        self.connected = False
        self.last_failure: str | None = None

    def start(self) -> None:
        """Start once; does not open a connection until explicitly called."""
        if self._session.closed:
            raise RuntimeError("Notification listener is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="nuheat-notifications")

    async def close(self) -> None:
        """Cancel connect/token/subscription/backoff and await socket cleanup."""
        if task := self._task:
            self._task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
        self.connected = False
        await self._session.close()

    async def _run(self) -> None:
        backoff = 5.0
        while True:
            self._subscribed = False
            try:
                remaining = self._cooldown()
                if remaining > 0:
                    await self._sleep(min(86400, remaining))
                    continue
                await self._connection()
            except asyncio.CancelledError:
                raise
            except NuHeatAuthError:
                self.last_failure = "authorization"
            except NuHeatRateLimitError:
                self.last_failure = "cooldown"
            except (
                ClientError,
                TimeoutError,
                OSError,
                NotificationProtocolError,
                NuHeatApiError,
                ValueError,
            ):
                self.last_failure = "connection"
            finally:
                self.connected = False
            # A connection that reached Subscribe resets backoff; persistent
            # failures increase it without allowing a tight reconnect loop.
            if self._subscribed:
                backoff = 5.0
            await self._sleep(
                max(self._cooldown(), min(300, backoff * (1 + self._jitter() * 0.2)))
            )
            backoff = min(300, backoff * 2)

    async def _open(self) -> ClientWebSocketResponse:
        for force in (False, True):
            token = await self._token(force)
            if remaining := self._cooldown():
                raise NuHeatRateLimitError(remaining)
            try:
                # ws_connect follows HTTP redirects internally. A per-request
                # redirect trace guard is supplied by the dedicated session.
                return await self._session.ws_connect(
                    self._url,
                    headers={"Authorization": f"Bearer {token}"},
                    max_msg_size=MAX_RECORD_BYTES,
                    autoping=True,
                )
            except WSServerHandshakeError as err:
                if err.status == 401:
                    if not force:
                        continue
                    raise NuHeatAuthError(
                        "Notification authorization rejected"
                    ) from None
                if err.status == 429 and self._set_cooldown is not None:
                    self._set_cooldown(
                        err.headers.get("Retry-After") if err.headers else None
                    )
                    raise NuHeatRateLimitError(self._cooldown()) from None
                raise NotificationProtocolError(
                    "Notification connection rejected"
                ) from None
        raise NuHeatAuthError("Notification authorization rejected")

    async def _send(
        self, socket: ClientWebSocketResponse, value: Mapping[str, Any]
    ) -> None:
        await socket.send_str(
            json.dumps(value, separators=(",", ":")) + RECORD_SEPARATOR
        )

    async def _receive(
        self, socket: ClientWebSocketResponse, decoder: JsonRecords
    ) -> list[dict[str, Any]]:
        message = await socket.receive(timeout=self._silence_timeout)
        if message.type is not WSMsgType.TEXT:
            raise NotificationProtocolError("Notification socket closed or unsupported")
        return decoder.feed(message.data)

    async def _connection(self) -> None:
        decoder = JsonRecords()
        ready = False
        try:
            async with asyncio.timeout(self._connect_timeout):
                socket = self._socket = await self._open()
                await self._send(socket, {"protocol": "json", "version": 1})
                handshaken = False
                while not ready:
                    for record in await self._receive(socket, decoder):
                        if not handshaken:
                            if record != {}:
                                raise NotificationProtocolError(
                                    "Notification handshake rejected"
                                )
                            handshaken = True
                            await self._send(
                                socket,
                                {
                                    "type": 1,
                                    "invocationId": "1",
                                    "target": "Subscribe",
                                    "arguments": [[2]],
                                },
                            )
                        elif (
                            record.get("type") == 3
                            and record.get("invocationId") == "1"
                        ):
                            if "error" in record or record.get("result") is not None:
                                raise NotificationProtocolError(
                                    "Notification subscription rejected"
                                )
                            ready = True
                        elif record.get("type") == 7:
                            raise NotificationProtocolError("Notification hub closed")
                        elif ready:
                            self._notify(record)
            self.connected = True
            self._subscribed = True
            self.last_failure = None
            self._invalidate()
            async with asyncio.TaskGroup() as group:
                ping = group.create_task(self._ping(socket))
                try:
                    while True:
                        for record in await self._receive(socket, decoder):
                            if record.get("type") == 7:
                                return
                            self._notify(record)
                finally:
                    ping.cancel()
        except ExceptionGroup as err:
            # Never propagate transport payloads or token-bearing exception URLs.
            raise NotificationProtocolError("Notification connection ended") from err
        finally:
            if self._socket is not None:
                await self._socket.close()
                self._socket = None

    async def _ping(self, socket: ClientWebSocketResponse) -> None:
        while True:
            await self._sleep(self._ping_interval)
            await self._send(socket, {"type": 6})

    def _notify(self, record: Mapping[str, Any]) -> None:
        if (
            type(record.get("type")) is not int
            or record.get("type") != 1
            or record.get("target") != "Notify"
        ):
            return
        known = self._known()
        self._timestamps = {
            serial: stamp
            for serial, stamp in self._timestamps.items()
            if serial in known
        }
        arguments = record.get("arguments")
        if not isinstance(arguments, list):
            return
        changed = False
        for event in arguments:
            if (
                not isinstance(event, dict)
                or type(event.get("type")) is not int
                or event["type"] != 2
            ):
                continue
            serial, raw = event.get("id"), event.get("timeStamp")
            if (
                not isinstance(serial, str)
                or serial not in known
                or not isinstance(raw, str)
            ):
                continue
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    continue
                stamp = stamp.astimezone(UTC)
            except (ValueError, OverflowError):
                continue
            if (prior := self._timestamps.get(serial)) is None or stamp > prior:
                self._timestamps[serial] = stamp
                changed = True
        if changed:
            self._invalidate()
