"""Synthetic quota, permission and transport-deadline contracts."""

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import patch

import pytest
from test_client import THERMOSTAT, FakeResponse, FakeSession, make_client

from chemelex_nuheat import (
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatClient,
    NuHeatPermissionError,
    NuHeatRateLimitError,
    ScheduleMode,
    ThermostatState,
)


async def test_permission_does_not_refresh_and_keeps_api_base():
    client, session, refreshes = make_client(FakeResponse(403))
    with pytest.raises(NuHeatPermissionError) as error:
        await client.set_schedule_mode(
            "SYNTHETIC", ScheduleMode.PERMANENT_HOLD, temperature=24
        )
    assert isinstance(error.value, NuHeatApiError)
    assert not isinstance(error.value, NuHeatAuthError)
    assert error.value.status == 403
    assert refreshes == [False] and len(session.requests) == 1


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("status", [403, 429])
async def test_optional_detail_fallback_preserves_v2_and_quota(single, status):
    row = {**THERMOSTAT, "mode": 3, "holdUntil": None}
    client, session, _ = make_client(
        FakeResponse(200, row if single else [row]),
        FakeResponse(status, headers={"Retry-After": "120"}),
    )
    client._include_schedule_details = True
    result = (
        await client.get_thermostat("SYNTHETIC")
        if single
        else (await client.list_thermostats())[0]
    )
    assert result.state is ThermostatState.AMBIGUOUS_MANUAL_OR_PERMANENT_HOLD
    assert result.schedule_details_requested and result.target_temperature == 23
    assert result.serial_number == THERMOSTAT["serialNumber"]
    assert (client.cooldown_remaining > 100) == (status == 429)
    assert len(session.requests) == 2


@pytest.mark.parametrize(
    "header,expected",
    [
        ("120", 120),
        (None, 60),
        ("bad", 60),
        ("-1", 60),
        ("0", 0),
        ("31536000000", 31536000000),
        ("9" * 400, 60),
    ],
)
async def test_retry_after_headers_and_expiry(header, expected):
    client, session, _ = make_client(
        FakeResponse(429, headers={} if header is None else {"Retry-After": header}),
        FakeResponse(200, [THERMOSTAT]),
    )
    with patch("chemelex_nuheat.client.time.monotonic", return_value=100):
        with pytest.raises(NuHeatRateLimitError):
            await client.list_thermostats()
        assert client.cooldown_remaining == expected
        if expected:
            with pytest.raises(NuHeatRateLimitError):
                await client.get_thermostat("SECOND")
            assert len(session.requests) == 1
    with patch("chemelex_nuheat.client.time.monotonic", return_value=101 + expected):
        assert len(await client.list_thermostats()) == 1


@pytest.mark.parametrize("delta,expected", [(120, 120), (-120, 0)])
def test_http_date_and_nonshortening_deadlines(delta, expected):
    client, _, _ = make_client()
    wall = 1800000000
    header = format_datetime(datetime.fromtimestamp(wall + delta, UTC), usegmt=True)
    with (
        patch("chemelex_nuheat.client.time.time", return_value=wall),
        patch("chemelex_nuheat.client.time.monotonic", return_value=100),
    ):
        client._set_cooldown(header)
        assert client.cooldown_remaining == expected
        client._set_cooldown("0")
        assert client.cooldown_remaining == expected


async def test_auth_wait_rechecks_new_cooldown_and_other_clients_are_independent():
    client, session, _ = make_client(FakeResponse(200, [THERMOSTAT]))

    async def token(force):
        client._set_cooldown("120")
        return "synthetic"

    client._access_token_provider = token
    with pytest.raises(NuHeatRateLimitError):
        await client.list_thermostats()
    assert not session.requests
    other, _, _ = make_client(FakeResponse(200, [THERMOSTAT]))
    assert len(await other.list_thermostats()) == 1


async def test_concurrent_requests_do_not_shorten_cooldown():
    client, session, _ = make_client(tokens=["synthetic", "synthetic"])
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return FakeResponse(429, headers={"Retry-After": "60"})
        return FakeResponse(429, headers={"Retry-After": "120"})

    session.request = request
    first = asyncio.create_task(client.list_thermostats())
    await entered.wait()
    try:
        with pytest.raises(NuHeatRateLimitError):
            await client.list_thermostats()
    finally:
        release.set()
    with pytest.raises(NuHeatRateLimitError):
        await first
    assert client.cooldown_remaining > 110


@pytest.mark.parametrize("stage", ["authorization", "headers", "body", "post_write"])
async def test_deadlines_and_response_release(stage):
    response = FakeResponse(200, THERMOSTAT)
    session = FakeSession(FakeResponse(204), response)

    async def token(force):
        if stage == "authorization":
            await asyncio.Event().wait()
        return "synthetic"

    client = NuHeatClient(session, token, request_timeout=0.01, operation_timeout=0.03)
    writes = 0

    async def request(method, url, **kwargs):
        nonlocal writes
        assert kwargs["timeout"].total == 0.01
        if method == "PUT":
            writes += 1
            if stage == "headers":
                await asyncio.Event().wait()
            return FakeResponse(204)
        if stage == "post_write":
            await asyncio.Event().wait()
        return response

    async def body():
        await asyncio.Event().wait()

    if stage == "body":
        response.json = body
    session.request = request
    with pytest.raises(NuHeatApiError):
        await client.set_target_temperature("SYNTHETIC", 24, mode=ScheduleMode.HOLD)
    assert writes == (0 if stage == "authorization" else 1)
    if stage == "body":
        assert response.released


async def test_cancellation_propagates_and_releases_body():
    entered = asyncio.Event()
    response = FakeResponse(200, THERMOSTAT)

    async def body():
        entered.set()
        await asyncio.Event().wait()

    response.json = body
    client, _, _ = make_client(response)
    task = asyncio.create_task(client.get_thermostat("SYNTHETIC"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.released


async def test_nested_readback_does_not_extend_operation_budget():
    response = FakeResponse(204)
    calls = []
    client, session, _ = make_client(response, FakeResponse(200, THERMOSTAT))
    client._request_timeout = 1
    client._operation_timeout = 0.4

    async def request(method, url, **kwargs):
        calls.append(method)
        await asyncio.sleep(0.1 if method == "PUT" else 0.4)
        return response if method == "PUT" else FakeResponse(200, THERMOSTAT)

    session.request = request
    with pytest.raises(NuHeatApiError, match="timed out") as raised:
        await client.set_target_temperature("SYNTHETIC", 24, mode=ScheduleMode.HOLD)
    assert calls == ["PUT", "GET"] and response.released
    assert raised.value.write_applied


async def test_readback_throttle_records_accepted_write():
    client, session, _ = make_client(
        FakeResponse(204), FakeResponse(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(NuHeatRateLimitError) as error:
        await client.set_target_temperature("SYNTHETIC", 24, mode=ScheduleMode.HOLD)
    assert error.value.write_applied
    assert [r[0] for r in session.requests] == ["PUT", "GET"]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_timeouts_rejected(value):
    with pytest.raises(ValueError):
        NuHeatClient(FakeSession(), None, request_timeout=value)
