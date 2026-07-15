"""Mocked tests for the Home Assistant-independent NuHeat client."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import ClientConnectionError

from chemelex_nuheat import (
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatClient,
    NuHeatDataError,
    ScheduleMode,
    decode_temperature,
    encode_temperature,
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
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.released = False

    async def json(self) -> Any:
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
        FakeResponse(200, {"userName": "Owner@Example.com", "language": "en"})
    )
    account = await client.get_account()
    assert account.username == "Owner@Example.com"
    assert account.language == "en"


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
        "temperatureType": 0,
    }
    assert command_response.released is True
    with pytest.raises(ValueError, match="requires Hold or Manual"):
        await client.set_target_temperature("ABC123", 22.5, mode=ScheduleMode.AUTO)


@pytest.mark.asyncio
async def test_auto_and_hold_payloads() -> None:
    client, session, _ = make_client(
        FakeResponse(204),
        FakeResponse(200, {**THERMOSTAT, "mode": 1}),
        FakeResponse(204),
        FakeResponse(200, THERMOSTAT),
    )
    await client.set_schedule_mode("ABC123", ScheduleMode.AUTO)
    await client.set_schedule_mode(
        "ABC123",
        ScheduleMode.HOLD,
        temperature=24.0,
        hold_until=datetime(2026, 7, 8, 1, tzinfo=UTC),
    )
    assert session.requests[0][2]["json"] == {"serialNumber": "ABC123"}
    assert session.requests[2][2]["json"] == {
        "serialNumber": "ABC123",
        "temperature": 2400,
        "temperatureType": 0,
        "holdUntil": "2026-07-08T01:00:00Z",
    }


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
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_http_errors(status: int) -> None:
    client, _, _ = make_client(FakeResponse(status))
    with pytest.raises(NuHeatApiError, match=str(status)):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ClientConnectionError()])
async def test_network_failures_are_retryable(failure: Exception) -> None:
    client, _, _ = make_client(failure)
    with pytest.raises(NuHeatApiError, match="Unable to communicate"):
        await client.list_thermostats()


def test_temperature_codec_rejects_invalid_values() -> None:
    assert encode_temperature(21.125) == 2112
    assert decode_temperature(2112) == 21.12
    for value in (float("nan"), float("inf"), 101.0):
        with pytest.raises(NuHeatDataError):
            encode_temperature(value)


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
