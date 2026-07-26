"""Mocked tests for the Home Assistant-independent NuHeat client."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import ClientConnectionError

from chemelex_nuheat import (
    HoldUntilStatus,
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatClient,
    NuHeatDataError,
    ScheduleMode,
    ThermostatState,
    decode_temperature,
    encode_temperature,
    parse_thermostat,
)

THERMOSTAT = {
    "serialNumber": "ABC123",
    "name": "Bathroom",
    "currentTemperature": 2150,
    "online": True,
    "isHeating": True,
    "setPointTemperature": 2300,
    "holdUntil": "2026-07-08T01:00:00Z",
    "mode": 2,
    "errorState": None,
}


class FakeResponse:
    """Minimal aiohttp response double."""

    def __init__(
        self,
        status: int,
        payload: Any = None,
        *,
        json_error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.headers = headers or {}
        self.released = False
        self.json_calls = 0

    async def json(self) -> Any:
        self.json_calls += 1
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def release(self) -> None:
        self.released = True


class FakeSession:
    """Record requests and return mocked responses or failures."""

    def __init__(
        self,
        *results: FakeResponse | Exception | Callable[..., FakeResponse],
    ) -> None:
        self.results = list(results)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(method, url, kwargs)
        return result


def make_client(
    *results: FakeResponse | Exception | Callable[..., FakeResponse],
    tokens: list[str] | None = None,
) -> tuple[NuHeatClient, FakeSession, list[bool]]:
    session = FakeSession(*results)
    refreshes: list[bool] = []
    token_values = iter(tokens or ["access-token"] * max(1, len(results)))

    async def access_token(force_refresh: bool) -> str:
        refreshes.append(force_refresh)
        return next(token_values)

    return NuHeatClient(session, access_token), session, refreshes  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_and_get_thermostats_parse_centi_celsius() -> None:
    client, session, _ = make_client(
        FakeResponse(200, [THERMOSTAT]), FakeResponse(200, THERMOSTAT)
    )

    thermostats = await client.list_thermostats()
    thermostat = await client.get_thermostat("ABC123")

    assert thermostats == [thermostat]
    assert thermostat.current_temperature == 21.5
    assert thermostat.target_temperature == 23.0
    assert thermostat.room == "Bathroom"
    assert session.requests[1][1].endswith("/api/v2/Thermostat/ABC123")


@pytest.mark.asyncio
async def test_get_account() -> None:
    client, _, _ = make_client(
        FakeResponse(
            200,
            {
                "userName": "Owner@Example.com",
                "temperatureScale": "Celsius",
                "language": "en",
            },
        )
    )
    account = await client.get_account()
    assert account.username == "Owner@Example.com"
    assert account.temperature_scale == "Celsius"
    assert account.language == "en"


@pytest.mark.asyncio
async def test_nullable_account_fields() -> None:
    client, _, _ = make_client(
        FakeResponse(
            200,
            {"userName": None, "temperatureScale": None, "language": None},
        )
    )
    account = await client.get_account()
    assert account.username is None
    assert account.temperature_scale is None
    assert account.language is None


@pytest.mark.asyncio
async def test_setpoint_requires_explicit_mode_and_encodes_centi_celsius() -> None:
    command_response = FakeResponse(204)
    client, session, _ = make_client(
        command_response, FakeResponse(200, {**THERMOSTAT, "mode": 3})
    )

    thermostat = await client.set_target_temperature(
        "ABC123", 22.5, mode=ScheduleMode.MANUAL
    )

    assert thermostat.mode == 3
    assert session.requests[0][0] == "PUT"
    assert session.requests[0][1].endswith("/api/v2/Mode/Manual")
    assert session.requests[0][2]["json"] == {
        "serialNumber": "ABC123",
        "temperature": 2250,
    }
    assert command_response.released is True
    with pytest.raises(ValueError, match="requires Hold or Manual"):
        await client.set_target_temperature("ABC123", 22.5, mode=ScheduleMode.AUTO)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "temperature", "hold_until", "endpoint", "payload"),
    [
        (
            ScheduleMode.AUTO,
            None,
            None,
            "Auto",
            {"serialNumber": "ABC123"},
        ),
        (
            ScheduleMode.HOLD_UNTIL_NEXT_SCHEDULE,
            7.2222222222,
            datetime.fromisoformat("2026-07-25T23:11:09-04:00"),
            "Hold",
            {
                "serialNumber": "ABC123",
                "temperature": 722,
                "holdUntil": "2026-07-26T03:11:09Z",
            },
        ),
        (
            ScheduleMode.HOLD_UNTIL_NEXT_SCHEDULE,
            7.2222222222,
            None,
            "Hold",
            {"serialNumber": "ABC123", "temperature": 722},
        ),
        (
            ScheduleMode.MANUAL,
            7.2222222222,
            None,
            "Manual",
            {"serialNumber": "ABC123", "temperature": 722},
        ),
    ],
)
async def test_documented_writes_accept_empty_204_and_refresh_with_get(
    mode: ScheduleMode,
    temperature: float | None,
    hold_until: datetime | None,
    endpoint: str,
    payload: dict[str, Any],
) -> None:
    """All verified mode writes accept an empty 204 before their GET refresh."""
    command_response = FakeResponse(204, json_error=AssertionError("JSON not expected"))
    client, session, _ = make_client(
        command_response,
        FakeResponse(200, THERMOSTAT),
    )

    result = await client.set_schedule_mode(
        "ABC123", mode, temperature=temperature, hold_until=hold_until
    )

    assert result.serial_number == "ABC123"
    assert session.requests[0][0] == "PUT"
    assert session.requests[0][1].endswith(f"/api/v2/Mode/{endpoint}")
    assert session.requests[0][2]["json"] == payload
    assert "temperatureType" not in session.requests[0][2]["json"]
    assert session.requests[1][0] == "GET"
    assert command_response.json_calls == 0
    assert command_response.released is True


@pytest.mark.asyncio
async def test_temperature_type_is_rejected_until_enum_meanings_are_verified() -> None:
    client, session, _ = make_client()

    with pytest.raises(ValueError, match="enum meanings are unverified"):
        await client.set_schedule_mode(
            "ABC123", ScheduleMode.MANUAL, temperature=22.0, temperature_type=1
        )
    assert session.requests == []


@pytest.mark.asyncio
async def test_hold_end_is_rejected_for_non_hold_commands() -> None:
    client, session, _ = make_client()

    with pytest.raises(ValueError, match="only for Hold"):
        await client.set_schedule_mode(
            "ABC123",
            ScheduleMode.MANUAL,
            temperature=22.0,
            hold_until=datetime(2026, 7, 8, 1, tzinfo=UTC),
        )
    assert session.requests == []


@pytest.mark.asyncio
async def test_write_accepts_successful_200_before_follow_up_get() -> None:
    command_response = FakeResponse(200, {"ignored": "write response"})
    client, _, _ = make_client(command_response, FakeResponse(200, THERMOSTAT))

    result = await client.set_schedule_mode("ABC123", ScheduleMode.AUTO)

    assert result.serial_number == "ABC123"
    assert command_response.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, NuHeatDataError),
        (403, NuHeatAuthError),
        (503, NuHeatApiError),
    ],
)
async def test_mode_write_non_success_still_raises_typed_error(
    status: int, error: type[Exception]
) -> None:
    client, _, _ = make_client(FakeResponse(status))

    with pytest.raises(error):
        await client.set_schedule_mode("ABC123", ScheduleMode.AUTO)


@pytest.mark.asyncio
async def test_401_forces_one_refresh_and_retries_once() -> None:
    client, session, refreshes = make_client(
        FakeResponse(401), FakeResponse(200, THERMOSTAT), tokens=["old", "new"]
    )
    await client.get_thermostat("ABC123")
    assert refreshes == [False, True]
    assert session.requests[0][2]["headers"]["Authorization"] == "Bearer old"
    assert session.requests[1][2]["headers"]["Authorization"] == "Bearer new"


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [(401, 401), (403,)])
async def test_rejected_authorization(statuses: tuple[int, ...]) -> None:
    client, _, _ = make_client(*(FakeResponse(status) for status in statuses))
    with pytest.raises(NuHeatAuthError, match="authorization was rejected"):
        await client.get_thermostat("ABC123")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_retryable_http_errors(status: int) -> None:
    client, _, _ = make_client(FakeResponse(status))
    with pytest.raises(NuHeatApiError, match=str(status)):
        await client.list_thermostats()


@pytest.mark.asyncio
async def test_rate_limit_preserves_retry_after() -> None:
    client, _, _ = make_client(FakeResponse(429, headers={"Retry-After": "58"}))
    with pytest.raises(NuHeatApiError) as raised:
        await client.list_thermostats()
    assert raised.value.status == 429
    assert raised.value.retry_after == "58"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 405, 501])
async def test_request_or_unsupported_errors_are_data_failures(status: int) -> None:
    client, _, _ = make_client(FakeResponse(status))
    with pytest.raises(NuHeatDataError, match=str(status)):
        await client.list_thermostats()


@pytest.mark.asyncio
async def test_malformed_json_is_sanitized() -> None:
    client, _, _ = make_client(
        FakeResponse(200, json_error=ValueError("body contained private material"))
    )
    with pytest.raises(NuHeatDataError, match="invalid JSON") as raised:
        await client.list_thermostats()
    assert "private material" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in THERMOSTAT.items() if key != "serialNumber"},
        {**THERMOSTAT, "currentTemperature": "warm"},
        {**THERMOSTAT, "setPointTemperature": float("nan")},
        {**THERMOSTAT, "online": "yes"},
    ],
)
async def test_invalid_thermostat_data(payload: dict[str, Any]) -> None:
    client, _, _ = make_client(FakeResponse(200, [payload]))
    with pytest.raises(NuHeatDataError):
        await client.list_thermostats()


@pytest.mark.parametrize("mode", [1, 2, 3, 999])
def test_mode_values_are_preserved_without_client_side_relabeling(mode: int) -> None:
    assert parse_thermostat({**THERMOSTAT, "mode": mode}).mode == mode


@pytest.mark.parametrize(
    ("mode", "hold_until", "raw_target", "expected"),
    [
        (2, None, 0, ThermostatState.SCHEDULED),
        (
            2,
            "2026-07-08T01:00:00Z",
            740,
            ThermostatState.TIMED_HOLD,
        ),
        (3, None, 740, ThermostatState.PERMANENT_HOLD),
        (3, None, 0, ThermostatState.AMBIGUOUS_MANUAL_OR_STANDBY),
        (999, None, 0, ThermostatState.UNKNOWN),
        (2, None, 740, ThermostatState.UNKNOWN),
        (2, "2026-07-08T01:00:00Z", 0, ThermostatState.UNKNOWN),
        (3, "2026-07-08T01:00:00Z", 740, ThermostatState.UNKNOWN),
    ],
)
def test_live_validated_state_classification(
    mode: int,
    hold_until: str | None,
    raw_target: int,
    expected: ThermostatState,
) -> None:
    thermostat = parse_thermostat(
        {
            **THERMOSTAT,
            "mode": mode,
            "holdUntil": hold_until,
            "setPointTemperature": raw_target,
        }
    )

    assert thermostat.numeric_mode == mode
    assert thermostat.raw_target_temperature == raw_target
    assert thermostat.state is expected


@pytest.mark.parametrize(
    ("hold_until", "expected_status"),
    [
        ("not-a-timestamp", HoldUntilStatus.INVALID),
        ("2026-07-08T01:00:00", HoldUntilStatus.INVALID),
        ("", HoldUntilStatus.INVALID),
        (42, HoldUntilStatus.INVALID),
    ],
)
def test_malformed_hold_until_is_unknown_without_crashing(
    hold_until: object, expected_status: HoldUntilStatus
) -> None:
    thermostat = parse_thermostat(
        {**THERMOSTAT, "holdUntil": hold_until, "setPointTemperature": 740}
    )

    assert thermostat.hold_until is None
    assert thermostat.hold_until_status is expected_status
    assert thermostat.state is ThermostatState.UNKNOWN


def test_missing_hold_until_is_unknown_without_crashing() -> None:
    payload = dict(THERMOSTAT)
    payload.pop("holdUntil")

    thermostat = parse_thermostat(payload)

    assert thermostat.hold_until is None
    assert thermostat.hold_until_status is HoldUntilStatus.MISSING
    assert thermostat.state is ThermostatState.UNKNOWN


def test_zero_and_missing_target_are_unavailable() -> None:
    zero = parse_thermostat(
        {**THERMOSTAT, "mode": 2, "holdUntil": None, "setPointTemperature": 0}
    )
    missing_payload = dict(THERMOSTAT)
    missing_payload.pop("setPointTemperature")
    missing = parse_thermostat(missing_payload)

    assert decode_temperature(0) is None
    assert decode_temperature(None) is None
    assert zero.target_temperature is None
    assert zero.raw_target_temperature == 0
    assert zero.state is ThermostatState.SCHEDULED
    assert missing.target_temperature is None
    assert missing.raw_target_temperature is None
    assert missing.state is ThermostatState.UNKNOWN


@pytest.mark.asyncio
async def test_fahrenheit_account_does_not_change_wire_decoding() -> None:
    client, _, _ = make_client(
        FakeResponse(
            200,
            {
                "userName": "synthetic@example.invalid",
                "temperatureScale": "Fahrenheit",
                "language": "en",
            },
        ),
        FakeResponse(200, {**THERMOSTAT, "setPointTemperature": 740}),
    )

    account = await client.get_account()
    thermostat = await client.get_thermostat("ABC123")

    assert account.temperature_scale == "Fahrenheit"
    assert thermostat.target_temperature == 7.4


def test_centi_celsius_precision_can_share_one_fahrenheit_display_degree() -> None:
    celsius_values = [decode_temperature(raw) for raw in (722, 726, 737, 740)]

    assert all(value is not None for value in celsius_values)
    assert {round(value * 9 / 5 + 32) for value in celsius_values if value} == {45}
    assert len(set(celsius_values)) == 4


@pytest.mark.parametrize("heating", [True, False])
def test_heating_flag_is_preserved(heating: bool) -> None:
    assert parse_thermostat({**THERMOSTAT, "isHeating": heating}).heating is heating


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("2026-07-08T01:00:00Z", datetime(2026, 7, 8, 1, tzinfo=UTC)),
    ],
)
def test_hold_expiration_parsing(value: str | None, expected: datetime | None) -> None:
    assert parse_thermostat({**THERMOSTAT, "holdUntil": value}).hold_until == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ClientConnectionError()])
async def test_network_failures_are_retryable(failure: Exception) -> None:
    client, _, _ = make_client(failure)
    with pytest.raises(NuHeatApiError, match="Unable to communicate"):
        await client.list_thermostats()


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (7.2222222222, 722),
        (7.2249, 722),
        (7.225, 723),
        (-7.225, -723),
        (21.125, 2113),
        (-100.0, -10000),
        (100.0, 10000),
    ],
)
def test_temperature_encoding_uses_decimal_half_up_rounding(
    value: float, encoded: int
) -> None:
    assert encode_temperature(value) == encoded


def test_temperature_codec_rejects_invalid_values() -> None:
    assert decode_temperature(2112) == 21.12
    for value in (float("nan"), float("inf"), -100.01, 100.01, True):
        with pytest.raises(NuHeatDataError):
            encode_temperature(value)


def test_hold_command_compatibility_alias() -> None:
    assert ScheduleMode.HOLD is ScheduleMode.HOLD_UNTIL_NEXT_SCHEDULE


@pytest.mark.asyncio
async def test_secrets_and_response_bodies_never_reach_errors_or_logs(caplog) -> None:
    secret_values = (
        "access-token-secret",
        "refresh-token-secret",
        "authorization-code-secret",
        "client-secret-value",
        "complete-response-body-secret",
    )
    session = FakeSession(FakeResponse(500, payload=secret_values[-1]))

    async def access_token(force_refresh: bool) -> str:
        return secret_values[0]

    client = NuHeatClient(session, access_token)  # type: ignore[arg-type]
    with pytest.raises(NuHeatApiError) as raised:
        await client.list_thermostats()

    output = f"{raised.value}\n{caplog.text}"
    assert all(secret not in output for secret in secret_values)
