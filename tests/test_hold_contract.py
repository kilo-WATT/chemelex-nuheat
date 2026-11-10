"""Vendor-confirmed expiry and definite-rejection retry contract; no live API."""

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientConnectionError
from test_client import THERMOSTAT, FakeResponse, make_client

from chemelex_nuheat import (
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatDataError,
    ScheduleMode,
    normalize_hold_until,
    validate_hold_until,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("hours", [23, 24, 25, 168])
async def test_no_upper_cap_and_whole_second_payload(hours):
    end = NOW + timedelta(hours=hours, microseconds=123456)
    canonical = end.replace(microsecond=0)
    client, session, _ = make_client(
        FakeResponse(204),
        FakeResponse(200, {**THERMOSTAT, "holdUntil": canonical.isoformat()}),
    )
    client._utcnow = lambda: NOW
    result = await client.set_schedule_mode(
        "SYNTHETIC", ScheduleMode.HOLD, temperature=24, hold_until=end
    )
    assert session.requests[0][2]["json"]["holdUntil"] == canonical.isoformat().replace(
        "+00:00", "Z"
    )
    assert result.hold_until == normalize_hold_until(end)


@pytest.mark.parametrize("seconds,accepted", [(1, False), (2, True), (3, True)])
def test_canonical_minimum_lead(seconds, accepted):
    end = NOW + timedelta(seconds=seconds, microseconds=900000)
    if accepted:
        assert validate_hold_until(end, now=NOW) == end.replace(microsecond=0)
    else:
        with pytest.raises(ValueError, match="two seconds"):
            validate_hold_until(end, now=NOW)


def test_offsets_and_missing_timezone():
    assert (
        normalize_hold_until(datetime.fromisoformat("2030-01-01T02:00:00.12+02:00"))
        == NOW
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_hold_until(datetime(2030, 1, 1))


@pytest.mark.parametrize("reject_on_retry", [False, True])
async def test_expiry_rechecked_after_token_wait_and_401(reject_on_retry):
    client, session, _ = make_client(FakeResponse(401))
    current = NOW
    client._utcnow = lambda: current

    async def token(force):
        nonlocal current
        if force or not reject_on_retry:
            current += timedelta(seconds=5)
        return "synthetic"

    client._access_token_provider = token
    with pytest.raises(ValueError, match="two seconds"):
        await client.set_schedule_mode(
            "SYNTHETIC",
            ScheduleMode.HOLD,
            temperature=24,
            hold_until=NOW + timedelta(seconds=6),
        )
    assert len(session.requests) == int(reject_on_retry)


@pytest.mark.parametrize("readback_401", [False, True])
async def test_definite_401_retries_only_rejected_request(readback_401):
    responses = (
        [FakeResponse(204), FakeResponse(401)]
        if readback_401
        else [FakeResponse(401), FakeResponse(204)]
    )
    client, session, refreshes = make_client(*responses, FakeResponse(200, THERMOSTAT))
    await client.set_schedule_mode("SYNTHETIC", ScheduleMode.HOLD, temperature=24)
    assert [r[0] for r in session.requests] == (
        ["PUT", "GET", "GET"] if readback_401 else ["PUT", "PUT", "GET"]
    )
    assert sum(refreshes) == 1
    if not readback_401:
        assert session.requests[0][2]["json"] == session.requests[1][2]["json"]
    assert all(response.released for response in responses)


async def test_repeated_401_write_stops():
    client, session, _ = make_client(FakeResponse(401), FakeResponse(401))
    with pytest.raises(NuHeatAuthError):
        await client.set_schedule_mode("SYNTHETIC", ScheduleMode.HOLD, temperature=24)
    assert [r[0] for r in session.requests] == ["PUT", "PUT"]


@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize(
    "failure", [ClientConnectionError("synthetic"), TimeoutError(), FakeResponse(503)]
)
async def test_uncertainty_never_repeats_put(after_write, failure):
    results = [FakeResponse(204), failure] if after_write else [failure]
    client, session, _ = make_client(*results)
    with pytest.raises(NuHeatApiError):
        await client.set_schedule_mode("SYNTHETIC", ScheduleMode.HOLD, temperature=24)
    assert [r[0] for r in session.requests].count("PUT") == 1


async def test_server_400_not_retried_or_shifted():
    client, session, _ = make_client(FakeResponse(400))
    with pytest.raises(NuHeatDataError):
        await client.set_schedule_mode(
            "SYNTHETIC",
            ScheduleMode.HOLD,
            temperature=24,
            hold_until=NOW + timedelta(hours=48),
        )
    assert len(session.requests) == 1
