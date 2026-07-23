"""Async client for the documented Chemelex NuHeat OpenAPI v2."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from http import HTTPStatus
from typing import Any, Final
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError

API_BASE_URL: Final = "https://api.nam.mynuheat.com"


class NuHeatApiError(RuntimeError):
    """A retryable NuHeat transport or cloud-service failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NuHeatAuthError(RuntimeError):
    """NuHeat rejected the current authorization."""


class NuHeatDataError(RuntimeError):
    """NuHeat returned a response that does not match the documented schema."""


class ScheduleMode(StrEnum):
    """Documented OpenAPI v2 thermostat mode commands."""

    AUTO = "auto"
    HOLD = "hold"
    MANUAL = "manual"


class ThermostatMode(IntEnum):
    """Provisional v2 mode values awaiting live response validation."""

    AUTO = 1
    HOLD = 2
    MANUAL = 3


@dataclass(frozen=True, slots=True)
class Account:
    """NuHeat account metadata available from OpenAPI v2."""

    username: str | None
    temperature_scale: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Thermostat:
    """Normalized thermostat state; every temperature is degrees Celsius."""

    serial_number: str
    name: str | None
    current_temperature: float
    target_temperature: float
    heating: bool
    online: bool
    mode: int
    hold_until: datetime | None = None
    error_state: str | None = None

    @property
    def room(self) -> str | None:
        """Return the thermostat room/name alias."""
        return self.name


AccessTokenProvider = Callable[[bool], Awaitable[str]]


class NuHeatClient:
    """NuHeat API client with caller-owned HTTP and OAuth lifecycle.

    The access-token provider receives ``True`` only after the API returns one
    HTTP 401. The provider must then refresh the token before returning it.
    OAuth token storage and refresh-token rotation deliberately live outside
    this library.
    """

    def __init__(
        self,
        session: ClientSession,
        access_token_provider: AccessTokenProvider,
        *,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._access_token_provider = access_token_provider
        self._base_url = base_url.rstrip("/")

    async def get_account(self) -> Account:
        """Return account metadata exposed by OpenAPI v2."""
        payload = await self._request_json("GET", "/api/v2/Account")
        data = _mapping(payload, "account")
        return Account(
            username=_optional_string(data, "userName"),
            temperature_scale=_optional_string(data, "temperatureScale"),
            language=_optional_string(data, "language"),
        )

    async def list_thermostats(self) -> list[Thermostat]:
        """Return all thermostats currently visible to the account."""
        payload = await self._request_json("GET", "/api/v2/Thermostat")
        if not isinstance(payload, list):
            raise NuHeatDataError("NuHeat thermostat response was not a list")
        return [parse_thermostat(item) for item in payload]

    async def get_thermostat(self, serial_number: str) -> Thermostat:
        """Return one thermostat by serial number."""
        serial = _serial_path(serial_number)
        payload = await self._request_json("GET", f"/api/v2/Thermostat/{serial}")
        return parse_thermostat(payload)

    async def set_target_temperature(
        self,
        serial_number: str,
        temperature: float,
        *,
        mode: ScheduleMode,
    ) -> Thermostat:
        """Set a Celsius target using an explicit, caller-selected mode.

        NuHeat's target-setpoint semantics remain subject to live validation,
        so this method intentionally has no implicit Hold/Manual default.
        """
        if mode is ScheduleMode.AUTO:
            raise ValueError("a target temperature requires Hold or Manual mode")
        return await self.set_schedule_mode(
            serial_number, mode, temperature=temperature
        )

    async def set_schedule_mode(
        self,
        serial_number: str,
        mode: ScheduleMode | str,
        *,
        temperature: float | None = None,
        hold_until: datetime | None = None,
        temperature_type: int | None = None,
    ) -> Thermostat:
        """Send a documented Auto, Hold, or Manual mode command."""
        schedule_mode = ScheduleMode(mode)
        payload: dict[str, Any] = {"serialNumber": _serial_value(serial_number)}
        if schedule_mode is not ScheduleMode.AUTO:
            if temperature is not None:
                payload["temperature"] = encode_temperature(temperature)
            if temperature_type is not None:
                if isinstance(temperature_type, bool) or temperature_type not in (0, 1):
                    raise ValueError("temperature_type must be 0, 1, or None")
                payload["temperatureType"] = temperature_type
        if schedule_mode is ScheduleMode.HOLD and hold_until is not None:
            if hold_until.tzinfo is None:
                raise ValueError("hold_until must be timezone-aware")
            payload["holdUntil"] = (
                hold_until.astimezone(UTC).isoformat().replace("+00:00", "Z")
            )
        response = await self._request(
            "PUT", f"/api/v2/Mode/{schedule_mode.value.title()}", json=payload
        )
        response.release()
        return await self.get_thermostat(serial_number)

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        try:
            return await response.json()
        except (ContentTypeError, ClientError, ValueError, TypeError) as err:
            raise NuHeatDataError("NuHeat returned invalid JSON") from err
        finally:
            response.release()

    async def _request(self, method: str, path: str, **kwargs: Any) -> ClientResponse:
        response: ClientResponse | None = None
        for attempt in range(2):
            try:
                token = await self._access_token_provider(attempt == 1)
                response = await self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    **kwargs,
                )
            except (TimeoutError, ClientError) as err:
                raise NuHeatApiError("Unable to communicate with NuHeat") from err

            if response.status != HTTPStatus.UNAUTHORIZED:
                break
            response.release()

        if response is None:  # pragma: no cover - defensive type narrowing
            raise NuHeatApiError("NuHeat request did not return a response")
        if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            response.release()
            raise NuHeatAuthError("NuHeat authorization was rejected")
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            status = response.status
            retry_after = response.headers.get("Retry-After")
            response.release()
            raise NuHeatApiError(
                f"NuHeat service temporarily failed with HTTP {status}",
                status=status,
                retry_after=retry_after,
            )
        if response.status == HTTPStatus.NOT_IMPLEMENTED:
            response.release()
            raise NuHeatDataError(
                "NuHeat API rejected the request with HTTP "
                f"{HTTPStatus.NOT_IMPLEMENTED}"
            )
        if response.status >= 500:
            status = response.status
            response.release()
            raise NuHeatApiError(
                f"NuHeat service temporarily failed with HTTP {status}",
                status=status,
            )
        if response.status >= HTTPStatus.BAD_REQUEST:
            status = response.status
            response.release()
            raise NuHeatDataError(f"NuHeat API rejected the request with HTTP {status}")
        return response


def parse_thermostat(value: Any) -> Thermostat:
    """Parse one documented thermostat object into the public model."""
    data = _mapping(value, "thermostat")
    return Thermostat(
        serial_number=_required_string(data, "serialNumber"),
        name=_optional_string(data, "name"),
        current_temperature=decode_temperature(data.get("currentTemperature")),
        target_temperature=decode_temperature(data.get("setPointTemperature")),
        heating=_required_bool(data, "isHeating"),
        online=_required_bool(data, "online"),
        mode=_required_int(data, "mode"),
        hold_until=_parse_datetime(data.get("holdUntil")),
        error_state=_optional_string(data, "errorState"),
    )


def encode_temperature(value: float) -> int:
    """Encode Celsius as the API's integer centi-Celsius representation."""
    temperature = _valid_temperature(value)
    return round(temperature * 100)


def decode_temperature(value: Any) -> float:
    """Decode the API's integer centi-Celsius representation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NuHeatDataError("NuHeat returned an invalid temperature")
    return _valid_temperature(float(value) / 100.0)


def _valid_temperature(value: float) -> float:
    if not math.isfinite(value) or not -100.0 <= value <= 100.0:
        raise NuHeatDataError("NuHeat returned an invalid temperature")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NuHeatDataError(f"NuHeat {name} response was not an object")
    return value


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NuHeatDataError(f"NuHeat response omitted required field {key}")
    return value.strip()


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise NuHeatDataError(f"NuHeat response contained invalid field {key}")
    return value


def _required_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise NuHeatDataError(f"NuHeat response omitted required field {key}")
    return value


def _required_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NuHeatDataError(f"NuHeat response omitted required field {key}")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise NuHeatDataError("NuHeat returned an invalid hold timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise NuHeatDataError("NuHeat returned an invalid hold timestamp") from err
    if parsed.tzinfo is None:
        raise NuHeatDataError("NuHeat returned an invalid hold timestamp")
    return parsed


def _serial_value(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(char in value for char in "/?#")
    ):
        raise ValueError("invalid thermostat serial number")
    return value.strip()


def _serial_path(value: str) -> str:
    return quote(_serial_value(value), safe="")
