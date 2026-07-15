# Contributing

Bug reports and focused pull requests are welcome. Do not include access
tokens, refresh tokens, authorization codes, client credentials, thermostat
serial numbers, account identifiers, or complete private API responses.

Set up a development environment with Python 3.13 or 3.14:

```console
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
ruff format --check .
mypy
```

Tests must use synthetic mocked responses. Home Assistant integration issues
should be reported to the Home Assistant Core project.

