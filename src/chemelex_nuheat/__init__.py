"""Typed async client for the Chemelex NuHeat OpenAPI v2."""

from .client import (
    API_BASE_URL,
    AccessTokenProvider,
    Account,
    HoldUntilStatus,
    NuHeatApiError,
    NuHeatAuthError,
    NuHeatClient,
    NuHeatDataError,
    ScheduleMode,
    Thermostat,
    ThermostatState,
    classify_thermostat_state,
    decode_temperature,
    encode_temperature,
    parse_thermostat,
)

__all__ = [
    "API_BASE_URL",
    "AccessTokenProvider",
    "Account",
    "HoldUntilStatus",
    "NuHeatApiError",
    "NuHeatAuthError",
    "NuHeatClient",
    "NuHeatDataError",
    "ScheduleMode",
    "Thermostat",
    "ThermostatState",
    "classify_thermostat_state",
    "decode_temperature",
    "encode_temperature",
    "parse_thermostat",
]
