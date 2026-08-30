"""Unit tests for the SSE stream token helpers in app.core.security.

A stream token is a short-lived, single-purpose credential: subject is the
JOB id (not a user), type is 'stream', and it expires after
STREAM_TOKEN_TTL_SECONDS. Expiry is tested by faking the mint-time clock —
never by extending the TTL.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from app.core.exceptions import AuthenticationError
from app.core.security import (
    STREAM_TOKEN_TTL_SECONDS,
    create_access_token,
    create_stream_token,
    decode_token,
)

JOB_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()


def test_stream_token_claims_bind_job_and_tenant() -> None:
    token = create_stream_token(JOB_ID, TENANT_ID)
    payload = decode_token(token, expected_type="stream")
    assert payload["sub"] == str(JOB_ID)
    assert payload["tenant_id"] == str(TENANT_ID)
    assert payload["type"] == "stream"


def test_stream_token_ttl_is_60_seconds() -> None:
    """The token must die fast — it rides in a URL. 60s, not minutes/days.

    The mint clock is frozen rather than read twice. `_make_token` calls
    `datetime.now(UTC)` separately for `exp` and for `iat`, and JWT
    truncates both to whole seconds — so across a tick boundary the live
    difference is 59, and the assertion below was flaky by one second.
    Freezing makes the window exact, which is the property being pinned.
    """
    assert STREAM_TOKEN_TTL_SECONDS == 60
    frozen = datetime.now(UTC)
    with mock.patch("app.core.security.datetime") as fake_dt:
        fake_dt.now.return_value = frozen
        token = create_stream_token(JOB_ID, TENANT_ID)
    payload = decode_token(token, expected_type="stream")
    assert payload["exp"] - payload["iat"] == STREAM_TOKEN_TTL_SECONDS


def test_expired_stream_token_rejected() -> None:
    """Mint with the clock faked 2 minutes into the past → decode refuses.

    The clock is faked at mint time (app.core.security.datetime) instead of
    sleeping or widening the TTL.
    """
    with mock.patch("app.core.security.datetime") as fake_dt:
        fake_dt.now.return_value = datetime.now(UTC) - timedelta(seconds=120)
        token = create_stream_token(JOB_ID, TENANT_ID)

    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="stream")


def test_access_token_is_not_a_stream_token() -> None:
    """The primary JWT must never authenticate a stream (single-purpose)."""
    access = create_access_token({"sub": str(uuid.uuid4()), "tenant_id": str(TENANT_ID)})
    with pytest.raises(AuthenticationError):
        decode_token(access, expected_type="stream")


def test_stream_token_is_not_an_access_token() -> None:
    """A leaked stream token must be worthless against the REST API."""
    token = create_stream_token(JOB_ID, TENANT_ID)
    with pytest.raises(AuthenticationError):
        decode_token(token)  # default expected_type='access'
