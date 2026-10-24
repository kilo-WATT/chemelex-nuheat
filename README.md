# chemelex-nuheat

`chemelex-nuheat` is a community-maintained, typed asynchronous Python client
for the North American Chemelex/NuHeat API v2. It is not an official Chemelex
product and is not affiliated with or endorsed by Chemelex.

The client targets `https://api.nam.mynuheat.com/api/v2`. Chemelex has stated
that API v2 supports both NuHeat Signature and NuHeat Conductor thermostats.
The read model and documented Auto, Hold, and Manual writes have been validated
against live Conductor responses. Signature and production Home Assistant Cloud
Account Linking validation are still pending.

## Maintenance status

This package is being published by `kilo-WATT` on an interim basis to help
restore the Home Assistant NuHeat integration. The current publisher does not
intend to be the long-term owner or maintainer. An established maintainer or
organization willing to take responsibility for the repository and PyPI
releases is actively sought.

## Installation

```console
python -m pip install chemelex-nuheat
```

Python 3.13 or newer is required.

## Usage

The caller owns OAuth. Pass an existing `aiohttp.ClientSession` and an async
token provider. The provider receives `force_refresh=True` after one HTTP 401
response and must return a valid access token. The library does not bundle an
OAuth Client ID or Client Secret.

```python
import aiohttp

from chemelex_nuheat import NuHeatClient, ScheduleMode


async def access_token(force_refresh: bool) -> str:
    """Return a token, refreshing it when force_refresh is true."""
    ...


async with aiohttp.ClientSession() as session:
    client = NuHeatClient(session, access_token)
    account = await client.get_account()
    thermostats = await client.list_thermostats()
    thermostat = await client.get_thermostat(thermostats[0].serial_number)
    await client.set_target_temperature(
        thermostat.serial_number,
        22.5,
        mode=ScheduleMode.MANUAL,
    )
    await client.set_standby(thermostat.serial_number)
```

## Supported operations

- Retrieve account information.
- List thermostats and retrieve one thermostat by serial number.
- Read current temperature, target temperature, online state, heating state,
  room name, raw numeric mode, raw setpoint, hold end, and derived state.
- Set target temperatures using Hold or Manual mode.
- Send the documented Auto, Hold, or Manual commands.
- Command the official app's Standby state.

Nonzero temperatures exposed by the public API are Celsius `float` values. The
NuHeat API's centi-Celsius integers are encoded and decoded at the HTTP
boundary. A zero or missing read value is exposed as `None`; live responses use
zero as an unavailable setpoint sentinel. Account Fahrenheit preference does
not change this wire decoding. Outbound values use decimal half-up rounding;
for example, 45°F converted by a caller to approximately 7.2222°C is sent as
the integer `722`.

`Thermostat.state` conservatively derives scheduled operation, timed hold,
Standby, ambiguous Manual-or-permanent-hold, or unknown from the numeric mode,
raw setpoint, and hold end together. Controlled live validation after the
vendor's September 2026 GET fix established mode 1 as Auto, mode 2 as Hold,
and mode 3 as the Manual-family response. Mode 3 with a 5°C target is the
official app's Standby state. Other mode-3 targets remain ambiguous between
physical Manual and an indefinite hold in v2. The client preserves all raw
fields so callers can handle future vendor clarifications without losing
information.

Live write validation established these command semantics:

- Auto resumes the schedule and exits a hold, physical Manual mode, or Standby.
  A GET response can remain identical before and after exiting Standby.
- Standby uses the documented Manual command at 5°C/41°F. The official app
  labels this state Standby while the physical thermostat reports Manual at
  41°F. It is a frost-protected standby state, not a hard power disconnect.
- Hold with an explicit timezone-aware end creates a timed hold. The client
  serializes the end in UTC and preserves seconds; the service may normalize
  returned seconds to the minute.
- Hold without an end means hold until the next scheduled event. It does not
  create an indefinite hold. `ScheduleMode.HOLD` remains a compatibility alias
  for `ScheduleMode.HOLD_UNTIL_NEXT_SCHEDULE`.
- Manual selects the physical thermostat's Manual operating mode. Non-Standby
  mode-3 targets still overlap an Auto operating mode with indefinite hold in
  the v2 read model, so that state remains deliberately ambiguous.

Standby is both writable and readable: mode 3 with raw target `500` represents
5°C/41°F Standby. Auto at the same 41°F target remains distinguishable because
it reports mode 1.

The optional 0.1.2 extension creates indefinite hold using the documented v1
thermostat command; v2 has no separate Permanent Hold command endpoint.
Successful mode writes return an empty HTTP 204; the client does not decode
that body and follows it with a GET.
The `temperatureType` enum meanings remain unverified, so the client omits the
field and rejects attempts to set it.

`NuHeatAuthError` indicates rejected authorization. `NuHeatApiError` indicates
a transport, rate-limit, or server failure that may be retryable.
`NuHeatDataError` indicates invalid caller input or an unexpected API response.
Exceptions never include access tokens or response bodies.

## Development

```console
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
ruff format --check .
mypy
python -m build
twine check dist/*
```

Report Home Assistant integration problems in
[`home-assistant/core`](https://github.com/home-assistant/core/issues), not in
this library's issue tracker. Library transport, model, and API parsing issues
belong here.

## API resilience (0.1.3)

HTTP 403 now raises `NuHeatPermissionError`, a `NuHeatApiError` subclass; it no
longer represents an expired login. Optional v1-detail 403 retains conservative
v2 data. Persistent 401 still forces one token refresh/retry and then raises
`NuHeatAuthError`. Do not infer Permanent Hold from unavailable v1 permissions.

HTTP 429 raises `NuHeatRateLimitError` with remaining cooldown and the original
Retry-After header. Delta-seconds and HTTP dates are accepted; unusable headers
use 60 seconds. `cooldown_remaining` and `raise_if_cooling_down()` expose the
same monotonic deadline for every endpoint/device on that client. New requests
fail without waiting; optional-detail fallback retains the cooldown. Separate
processes/client instances do not share this state. Writes are never queued.

Requests have a 15-second header/body budget and public operations have a
30-second total budget including token waits and nested readbacks. Constructor
`request_timeout` and `operation_timeout` allow finite positive budgets for
isolated testing. HA's shared session configuration is not changed. Cancellation
propagates and responses are released. A successful PUT followed by an API-error
readback sets the exception's `write_applied` flag; its write must not be repeated
automatically. The existing single retry after an explicit 401 is unchanged.

These contracts are introduced in 0.1.3.
No new mode payloads, registration values, or hardware validation are included.

## License

Apache License 2.0.

## Permanent Hold extension (0.1.2)

`NuHeatClient(..., include_schedule_details=True)` supplements mode-3 reads with
correlated v1 operating/schedule fields. Matching Auto/schedule-mode-3 responses
report `ThermostatState.PERMANENT_HOLD`; physical Manual reports `MANUAL`.
Other callers keep the existing v2-only behavior by default. Missing/conflicting
metadata retains the conservative v2 state, never a guessed Permanent Hold.

`set_schedule_mode(serial, ScheduleMode.PERMANENT_HOLD, temperature=celsius)` uses
the documented minimal v1 thermostat update. Temperature is mandatory; expiration
and temperatureType are rejected. `set_target_temperature` accepts the same mode.
The command was verified from Manual and scheduled Auto in an approved isolated
hardware comparison, with the original state restored. Older notes about an unknown indefinite-hold request describe v2-only
behavior and are superseded by this explicit v1 command.
