"""Subject extraction tests using synthetic, non-authenticating tokens only."""

import base64
import json

import pytest

from chemelex_nuheat import (
    InvalidAccountSubjectError,
    account_subject_from_access_token,
)


def synthetic_token(payload: object) -> str:
    """Encode a fixture payload, not a signed or usable credential."""
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"e30.{encoded}.synthetic-signature"


@pytest.mark.parametrize("subject", ["synthetic-account", "synthetic-用户", "001234"])
def test_account_subject(subject: str) -> None:
    """Return the exact provider identifier rather than normalize it."""
    assert (
        account_subject_from_access_token(synthetic_token({"sub": subject})) == subject
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"sub": None}, {"sub": ""}, {"sub": "  "}, {"sub": 42}, [], "fixture", 42],
)
def test_invalid_subject(payload: object) -> None:
    with pytest.raises(
        InvalidAccountSubjectError, match="Invalid OAuth account subject"
    ):
        account_subject_from_access_token(synthetic_token(payload))


@pytest.mark.parametrize(
    "token",
    ["", "synthetic", "header.payload", "a.b.c.d", "a.%%%.c", "a._w.c", "a.eA.c"],
)
def test_malformed_token(token: str) -> None:
    with pytest.raises(
        InvalidAccountSubjectError, match="Invalid OAuth account subject"
    ):
        account_subject_from_access_token(token)


def test_identity_error_privacy() -> None:
    """Neither the public error nor displayed exception chain exposes payload data."""
    token = synthetic_token({"private_fixture": "synthetic-sensitive-value"})
    with pytest.raises(InvalidAccountSubjectError) as error:
        account_subject_from_access_token(token)
    assert str(error.value) == "Invalid OAuth account subject"
    assert error.value.__cause__ is None


def test_malformed_error_privacy() -> None:
    with pytest.raises(InvalidAccountSubjectError) as error:
        account_subject_from_access_token("synthetic-token-without-segments")
    assert str(error.value) == "Invalid OAuth account subject"
    assert error.value.__suppress_context__
