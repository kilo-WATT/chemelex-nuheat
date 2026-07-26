# chemelex-nuheat

`chemelex-nuheat` is a community-maintained, typed asynchronous Python client
for the North American Chemelex/NuHeat API v2. It is not an official Chemelex
product and is not affiliated with or endorsed by Chemelex.

The client targets `https://api.nam.mynuheat.com/api/v2`. Chemelex has stated
that API v2 supports both NuHeat Signature and NuHeat Conductor thermostats.
The read model has been validated against live Conductor responses. Live write,
Signature, and production Home Assistant Cloud Account Linking validation are
still pending.

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
```

## Supported operations

- Retrieve account information.
- List thermostats and retrieve one thermostat by serial number.
- Read current temperature, target temperature, online state, heating state,
  room name, raw numeric mode, raw setpoint, hold end, and derived state.
- Set target temperatures using Hold or Manual mode.
- Select Auto, Hold, or Manual schedule mode.

Nonzero temperatures exposed by the public API are Celsius `float` values. The
NuHeat API's centi-Celsius integers are encoded and decoded at the HTTP
boundary. A zero or missing read value is exposed as `None`; live responses use
zero as an unavailable setpoint sentinel. Account Fahrenheit preference does
not change this wire decoding.

`Thermostat.state` conservatively derives scheduled operation, timed hold,
permanent hold, ambiguous Manual-or-Standby, or unknown from the numeric mode,
raw setpoint, and hold end together. Numeric read modes are deliberately not
named after the Auto, Hold, and Manual write commands. The client preserves the
raw fields so callers can handle future vendor clarifications without losing
information.

Outbound encoding and command behavior have not been changed based on the read
observations. Live PUT validation remains pending.

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

## License

Apache License 2.0.

