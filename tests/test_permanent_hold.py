"""Regression cases grounded in the approved paired v1/v2 hardware comparison."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_client import THERMOSTAT, FakeResponse, FakeSession

from chemelex_nuheat import (
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatClient,
    ScheduleMode,
    ThermostatState,
    parse_thermostat,
)
from chemelex_nuheat.client import with_schedule_details

V2 = {**THERMOSTAT, "mode": 3, "holdUntil": None}
V1 = {
    "serialNumber": "ABC123",
    "operatingMode": 1,
    "scheduleMode": 3,
    "setPointTemp": 2300,
    "holdSetPointDateTime": None,
    "online": True,
}


async def token(_force):
    return "synthetic-token"


@pytest.mark.parametrize(
    ("operating", "target", "expected"),
    [
        (1, 2300, ThermostatState.PERMANENT_HOLD),
        (2, 2300, ThermostatState.MANUAL),
        (1, 500, ThermostatState.PERMANENT_HOLD),
        (2, 500, ThermostatState.STANDBY),
    ],
)
def test_live_mode3_disambiguation(operating, target, expected):
    original = parse_thermostat({**V2, "setPointTemperature": target})
    result = with_schedule_details(
        original, {**V1, "operatingMode": operating, "setPointTemp": target}
    )
    assert result.state is expected
    assert result.serial_number == original.serial_number


@pytest.mark.parametrize(
    "changes",
    [
        {"serialNumber": "OTHER"},
        {"operatingMode": True},
        {"operatingMode": 9},
        {"scheduleMode": 1},
        {"setPointTemp": 2200},
        {"setPointTemp": 0},
        {"setPointTemp": False},
        {"holdSetPointDateTime": "2026-10-22T15:00:00Z"},
        {"holdSetPointDateTime": "broken"},
    ],
)
def test_conflicting_metadata_does_not_invent_preset(changes):
    original = parse_thermostat(V2)
    assert with_schedule_details(original, {**V1, **changes}) is original


@pytest.mark.parametrize(
    "field", ["operatingMode", "scheduleMode", "setPointTemp", "holdSetPointDateTime"]
)
def test_missing_metadata_leaves_v2_state(field):
    original = parse_thermostat(V2)
    detail = dict(V1)
    detail.pop(field)
    assert with_schedule_details(original, detail) is original


async def test_permanent_hold_minimal_request_and_readback():
    session = FakeSession(
        FakeResponse(204), FakeResponse(200, V2), FakeResponse(200, V1)
    )
    client = NuHeatClient(session, token, include_schedule_details=True)
    result = await client.set_schedule_mode(
        "ABC123", ScheduleMode.PERMANENT_HOLD, temperature=23
    )
    assert result.state is ThermostatState.PERMANENT_HOLD
    method, url, kwargs = session.requests[0]
    assert method == "PUT" and url.endswith("/api/v1/Thermostat")
    assert kwargs["json"] == {
        "serialNumber": "ABC123",
        "setPointTemp": 2300,
        "scheduleMode": 3,
    }
    assert [x[0] for x in session.requests] == ["PUT", "GET", "GET"]


async def test_setpoint_retains_permanent_hold_command():
    session = FakeSession(
        FakeResponse(204),
        FakeResponse(200, {**V2, "setPointTemperature": 2400}),
        FakeResponse(200, {**V1, "setPointTemp": 2400}),
    )
    client = NuHeatClient(session, token, include_schedule_details=True)
    result = await client.set_target_temperature(
        "ABC123", 24, mode=ScheduleMode.PERMANENT_HOLD
    )
    assert result.target_temperature == 24
    assert result.state is ThermostatState.PERMANENT_HOLD
    assert session.requests[0][2]["json"]["scheduleMode"] == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"temperature": 23, "hold_until": datetime.now(UTC)},
        {"temperature": 23, "temperature_type": 1},
    ],
)
async def test_invalid_permanent_write_never_sends_request(kwargs):
    session = FakeSession()
    client = NuHeatClient(session, token)
    with pytest.raises(ValueError):
        await client.set_schedule_mode("ABC123", ScheduleMode.PERMANENT_HOLD, **kwargs)
    assert session.requests == []


async def test_failed_write_never_retried():
    session = FakeSession(FakeResponse(500))
    client = NuHeatClient(session, token, include_schedule_details=True)
    with pytest.raises(NuHeatApiError):
        await client.set_schedule_mode(
            "ABC123", ScheduleMode.PERMANENT_HOLD, temperature=23
        )
    assert len(session.requests) == 1


async def test_uncertain_post_write_read_does_not_repeat_command():
    session = FakeSession(FakeResponse(204), FakeResponse(500))
    client = NuHeatClient(session, token, include_schedule_details=True)
    with pytest.raises(NuHeatApiError):
        await client.set_schedule_mode(
            "ABC123", ScheduleMode.PERMANENT_HOLD, temperature=23
        )
    assert [r[0] for r in session.requests] == ["PUT", "GET"]


async def test_only_mode3_fetches_optional_details():
    session = FakeSession(FakeResponse(200, [THERMOSTAT]))
    client = NuHeatClient(session, token, include_schedule_details=True)
    assert len(await client.list_thermostats()) == 1
    assert len(session.requests) == 1


async def test_v1_list_joins_by_serial_and_retains_identity():
    session = FakeSession(
        FakeResponse(200, [V2, {**V2, "serialNumber": "SECOND"}]),
        FakeResponse(200, [{**V1, "serialNumber": "SECOND", "operatingMode": 2}, V1]),
    )
    client = NuHeatClient(session, token, include_schedule_details=True)
    result = await client.list_thermostats()
    assert [t.serial_number for t in result] == ["ABC123", "SECOND"]
    assert [t.state for t in result] == [
        ThermostatState.PERMANENT_HOLD,
        ThermostatState.MANUAL,
    ]


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(500),
        FakeResponse(200, {}),
        FakeResponse(200, [V1, V1]),
        FakeResponse(200, [None]),
    ],
)
async def test_optional_detail_failure_preserves_v2_data(response):
    session = FakeSession(FakeResponse(200, [V2]), response)
    client = NuHeatClient(session, token, include_schedule_details=True)
    result = await client.list_thermostats()
    assert result == [replace(parse_thermostat(V2), schedule_details_requested=True)]


async def test_optional_auth_error_still_requires_reauth():
    session = FakeSession(FakeResponse(200, [V2]), FakeResponse(403))
    client = NuHeatClient(session, token, include_schedule_details=True)
    with pytest.raises(NuHeatAuthError):
        await client.list_thermostats()


async def test_unenriched_default_remains_backward_compatible():
    session = FakeSession(FakeResponse(200, [V2]))
    client = NuHeatClient(session, token)
    result = await client.list_thermostats()
    assert result[0].state is ThermostatState.AMBIGUOUS_MANUAL_OR_PERMANENT_HOLD
    assert len(session.requests) == 1


async def test_optional_failure_cannot_mistake_5c_permanent_hold_for_standby():
    payload = {**V2, "setPointTemperature": 500}
    session = FakeSession(FakeResponse(200, [payload]), FakeResponse(500))
    result = await NuHeatClient(
        session, token, include_schedule_details=True
    ).list_thermostats()
    assert result[0].target_temperature == 5
    assert result[0].state is ThermostatState.AMBIGUOUS_MANUAL_OR_PERMANENT_HOLD


async def test_validated_5c_standby_remains_distinct_from_permanent_hold():
    payload = {**V2, "setPointTemperature": 500}
    detail = {**V1, "setPointTemp": 500, "operatingMode": 2}
    session = FakeSession(FakeResponse(200, payload), FakeResponse(200, detail))
    result = await NuHeatClient(
        session, token, include_schedule_details=True
    ).get_thermostat("ABC123")
    assert result.state is ThermostatState.STANDBY
