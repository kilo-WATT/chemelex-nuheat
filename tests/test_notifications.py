"""SignalR proof against loopback-only fake hubs; never the vendor service."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from chemelex_nuheat import NuHeatAuthError
from chemelex_nuheat.notifications import (
    MAX_RECORD_BYTES,
    JsonRecords,
    NotificationProtocolError,
    NuHeatNotifications,
    notification_url,
)

RS = "\x1e"


async def token(force):
    return "synthetic-refreshed" if force else "synthetic-token"


@asynccontextmanager
async def fake_hub(handler):
    app = web.Application()
    app.router.add_get("/v2/notificationsHost", handler)
    async with TestServer(app) as server:
        yield str(server.make_url("/"))


async def subscribe(request):
    assert request.query_string == ""
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    assert json.loads((await socket.receive_str()).rstrip(RS)) == {
        "protocol": "json",
        "version": 1,
    }
    await socket.send_str("{}" + RS)
    message = json.loads((await socket.receive_str()).rstrip(RS))
    assert message == {
        "type": 1,
        "invocationId": "1",
        "target": "Subscribe",
        "arguments": [[2]],
    }
    await socket.send_str(
        json.dumps({"type": 3, "invocationId": "1", "result": None}) + RS
    )
    return socket


def notify(serial="SYNTHETIC", stamp="2030-01-01T12:00:00Z"):
    return {
        "type": 1,
        "target": "Notify",
        "arguments": [{"type": 2, "id": serial, "timeStamp": stamp}],
    }


def test_record_framing_and_limits():
    decoder = JsonRecords()
    assert decoder.feed('{"type":') == []
    assert decoder.feed("6}" + RS + "{}" + RS) == [{"type": 6}, {}]
    for bad in ["[]" + RS, "bad" + RS, "x" * (MAX_RECORD_BYTES + 1)]:
        with pytest.raises(NotificationProtocolError):
            JsonRecords().feed(bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid",
        "https://user:secret@example.invalid",
        "https://example.invalid/?token=secret",
        "https://example.invalid/api/v2",
    ],
)
def test_unsafe_origin_rejected(url):
    with pytest.raises(ValueError):
        notification_url(url)


def test_direct_hub_url():
    assert (
        notification_url("https://example.invalid")
        == "wss://example.invalid/v2/notificationsHost"
    )
    assert (
        notification_url("http://127.0.0.1:1234/")
        == "ws://127.0.0.1:1234/v2/notificationsHost"
    )


async def test_header_auth_reconnect_resubscribes_and_full_refresh():
    connections = 0
    invalidations = 0
    second = asyncio.Event()
    forces = []

    async def access(force):
        forces.append(force)
        return await token(force)

    async def handler(request):
        nonlocal connections
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        connections += 1
        socket = await subscribe(request)
        if connections == 1:
            await socket.send_str('{"type":7}' + RS)
        else:
            async for _ in socket:
                pass
        return socket

    def invalidate():
        nonlocal invalidations
        invalidations += 1
        if invalidations == 2:
            second.set()

    async def fast_backoff(seconds):
        await asyncio.sleep(0.001 if seconds >= 5 else seconds)

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session,
            access,
            lambda: {"SYNTHETIC"},
            invalidate,
            base_url=url,
            sleep=fast_backoff,
        )
        try:
            listener.start()
            listener.start()
            await asyncio.wait_for(second.wait(), 3)
            assert connections == 2 and forces == [False, False]
        finally:
            await listener.close()
        assert not session.closed and not listener.connected


async def test_notifications_deduplicate_and_are_account_scoped():
    changes = []
    async with ClientSession() as session:
        listener = NuHeatNotifications(
            session, token, lambda: {"SYNTHETIC"}, lambda: changes.append(True)
        )
        other = NuHeatNotifications(
            session, token, lambda: {"OTHER"}, lambda: changes.append(False)
        )
        try:
            for event in [
                notify(),
                notify(),
                notify(stamp="2030-01-01T11:59:00Z"),
                notify(serial="UNKNOWN"),
                notify(stamp="bad"),
                notify(stamp="2030-01-01T12:00:01+00:00"),
            ]:
                listener._notify(event)
                other._notify(event)
            assert changes == [True, True]
            assert listener._timestamps == {
                "SYNTHETIC": datetime(2030, 1, 1, 12, 0, 1, tzinfo=UTC)
            }
        finally:
            await listener.close()
            await other.close()


@pytest.mark.parametrize("persistent", [False, True])
async def test_definite_401_refreshes_once(persistent):
    attempts, forces = [], []

    async def access(force):
        forces.append(force)
        return await token(force)

    async def handler(request):
        attempts.append(request.headers.get("Authorization"))
        if persistent or len(attempts) == 1:
            return web.Response(status=401)
        socket = await subscribe(request)
        await socket.send_str('{"type":7}' + RS)
        return socket

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session, access, lambda: set(), lambda: None, base_url=url
        )
        try:
            if persistent:
                with pytest.raises(NuHeatAuthError):
                    await listener._connection()
            else:
                await listener._connection()
            assert forces == [False, True] and len(attempts) == 2
        finally:
            await listener.close()


async def test_rejects_redirect_before_following():
    followed = []

    async def handler(request):
        followed.append(request.path)
        return web.Response(status=302, headers={"Location": "/must-not-follow"})

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(session, token, set, lambda: None, base_url=url)
        try:
            with pytest.raises(NotificationProtocolError, match="redirects"):
                await listener._connection()
            assert followed == ["/v2/notificationsHost"]
        finally:
            await listener.close()


async def test_partial_records_ping_and_change():
    completed = asyncio.Event()
    calls = []

    async def handler(request):
        socket = await subscribe(request)
        payload = json.dumps(notify()) + RS
        await socket.send_str(payload[:12])
        await socket.send_str(payload[12:] + '{"type":6}' + RS)
        while True:
            message = await socket.receive()
            if message.type.name != "TEXT":
                break
            if json.loads(message.data.rstrip(RS)) == {"type": 6}:
                completed.set()
        return socket

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session,
            token,
            lambda: {"SYNTHETIC"},
            lambda: calls.append(True),
            base_url=url,
            ping_interval=0.01,
        )
        try:
            listener.start()
            await asyncio.wait_for(completed.wait(), 3)
            assert calls == [True, True]  # Reconnect/initial GET plus changed event.
        finally:
            await listener.close()


@pytest.mark.parametrize("phase", ["token", "subscription", "backoff", "listen"])
async def test_close_cancels_each_wait(phase):
    waiting = asyncio.Event()
    never = asyncio.Event()

    async def access(force):
        if phase == "token":
            waiting.set()
            await never.wait()
        return await token(force)

    async def sleep(seconds):
        if phase == "backoff":
            waiting.set()
            await never.wait()
        await asyncio.sleep(seconds)

    async def handler(request):
        if phase == "backoff":
            return web.Response(status=503)
        if phase == "subscription":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_str()
            waiting.set()
        else:
            socket = await subscribe(request)
        async for _ in socket:
            pass
        return socket

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session, access, set, waiting.set, base_url=url, sleep=sleep
        )
        listener.start()
        await asyncio.wait_for(waiting.wait(), 3)
        await asyncio.wait_for(listener.close(), 2)
        assert listener._task is None and listener._socket is None


async def test_server_silence_closes_connection():
    async def handler(request):
        socket = await subscribe(request)
        async for _ in socket:
            pass
        return socket

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session, token, set, lambda: None, base_url=url, silence_timeout=0.02
        )
        try:
            with pytest.raises(NotificationProtocolError):
                await listener._connection()
            assert listener._socket is None
        finally:
            await listener.close()


@pytest.mark.parametrize("phase", ["handshake", "subscribe", "oversized"])
async def test_bad_hub_messages_close_without_raw_error(phase):
    secret_marker = "synthetic-private-frame-marker"

    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.receive_str()
        if phase == "handshake":
            await socket.send_str(json.dumps({"error": secret_marker}) + RS)
        elif phase == "oversized":
            await socket.send_str("x" * (MAX_RECORD_BYTES + 1))
        else:
            await socket.send_str("{}" + RS)
            await socket.receive_str()
            await socket.send_str(
                json.dumps({"type": 3, "invocationId": "1", "error": secret_marker})
                + RS
            )
        async for _ in socket:
            pass
        return socket

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(session, token, set, lambda: None, base_url=url)
        try:
            with pytest.raises(NotificationProtocolError) as raised:
                await listener._connection()
            assert secret_marker not in str(raised.value)
            assert listener._socket is None
        finally:
            await listener.close()


async def test_429_sets_shared_cooldown_and_prevents_new_connections():
    from chemelex_nuheat import NuHeatClient, NuHeatRateLimitError

    attempts = []

    async def handler(request):
        attempts.append(True)
        return web.Response(status=429, headers={"Retry-After": "120"})

    async with fake_hub(handler) as url, ClientSession() as session:
        client = NuHeatClient(session, token, base_url=url)
        listener = client.create_notifications(set, lambda: None)
        try:
            with pytest.raises(NuHeatRateLimitError):
                await listener._connection()
            assert client.cooldown_remaining > 100
            with pytest.raises(NuHeatRateLimitError):
                await client.list_thermostats()
            with pytest.raises(NuHeatRateLimitError):
                await listener._connection()
            assert attempts == [True]
        finally:
            await listener.close()


async def test_disabled_listener_opens_no_connection():
    attempts = []

    async def handler(request):
        attempts.append(True)
        return web.Response(status=500)

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(session, token, set, lambda: None, base_url=url)
        await asyncio.sleep(0)
        await listener.close()
        assert attempts == []


async def test_backoff_is_bounded_without_tight_retry():
    delays = []

    async def handler(request):
        return web.Response(status=503)

    async def wait(seconds):
        delays.append(seconds)
        if len(delays) == 9:
            raise asyncio.CancelledError

    async with fake_hub(handler) as url, ClientSession() as session:
        listener = NuHeatNotifications(
            session,
            token,
            set,
            lambda: None,
            base_url=url,
            sleep=wait,
            jitter=lambda: 0,
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await listener._run()
            assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
        finally:
            await listener.close()
