"""Read account identity claims from provider-issued OAuth access tokens."""

import base64
import binascii
import json


class InvalidAccountSubjectError(ValueError):
    """The access token does not contain a usable account subject."""


def account_subject_from_access_token(access_token: str) -> str:
    """Decode a non-empty subject without authenticating the token.

    This does not verify the signature, issuer, audience, or expiry. Callers
    must obtain the token through a trusted OAuth flow and validate API access
    before relying on the subject. Error messages never include token content.
    """
    try:
        segments = access_token.split(".")
        if len(segments) != 3:
            raise ValueError
        payload_segment = segments[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.b64decode(payload_segment, altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise InvalidAccountSubjectError("Invalid OAuth account subject") from None

    subject = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidAccountSubjectError("Invalid OAuth account subject")
    return subject
