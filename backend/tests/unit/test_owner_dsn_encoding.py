"""A generated password must survive being put in a DSN (WO-R2-65).

`random_password.db` may contain `%`, `#`, `?`, `:`, `@` and `/` — its
`override_special` set says so — and every one of those means something
inside a URL. SQLAlchemy percent-DECODES the password while parsing, so the
owner DSN handed the database a different secret than RDS was created with,
for some passwords, on the path that migrates the schema. The failure is an
authentication error with no plausible cause, appearing only after a
password rotation happens to produce one of the unlucky strings.

The composition is Terraform's, so these tests do two things: demonstrate the
property on the Python side that consumes the URL, and assert the Terraform
still spells it that way.
"""

from __future__ import annotations

import pathlib
import re
from urllib.parse import quote

import pytest
from sqlalchemy.engine import make_url

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Exactly `random_password.db`'s override_special set, plus alphanumerics.
_OVERRIDE_SPECIAL = "!#$%&*()-_=+[]{}<>:?"


def _dsn(password: str) -> str:
    return f"postgresql+asyncpg://postgres:{password}@db.internal:5432/incident_platform"


@pytest.mark.parametrize(
    "password",
    [
        "pw%4ab",          # the silent one: %4a decodes to "J"
        "pw%ffdead",
        "a#b",             # truncates at the fragment
        "a?b",
        "a@b",             # re-splits userinfo from host
        "a/b",
        "a:b",
        "".join(_OVERRIDE_SPECIAL) + "Aa0",
    ],
)
def test_encoded_password_round_trips_through_the_url(password: str) -> None:
    parsed = make_url(_dsn(quote(password, safe="")))

    assert parsed.password == password
    assert parsed.host == "db.internal"
    assert parsed.database == "incident_platform"


def test_an_unencoded_percent_password_parses_as_a_different_secret() -> None:
    """The bug itself, so the fix has something to be a fix of."""
    parsed = make_url(_dsn("pw%4ab"))

    assert parsed.password == "pwJb"
    assert parsed.password != "pw%4ab"


def test_a_literal_plus_is_not_decoded_as_a_space() -> None:
    """Terraform's `urlencode` is form-encoding: it renders a space as `+`,
    which this parser reads back as a literal `+`. No space can occur in
    `override_special`, so the composition is safe — but if a space is ever
    added to that set, this is the test that should stop it.
    """
    assert make_url(_dsn("a+b")).password == "a+b"
    assert " " not in _OVERRIDE_SPECIAL, (
        "a space in override_special would round-trip as '+' through "
        "terraform's urlencode — switch to a non-form encoder first"
    )


def _secrets_tf() -> str:
    return (_REPO_ROOT / "infra" / "secrets.tf").read_text(encoding="utf-8")


def test_every_dsn_secret_urlencodes_its_password() -> None:
    dsns = re.findall(r'secret_string\s*=\s*"(postgresql\+asyncpg://[^"]+)"', _secrets_tf())

    assert dsns, "no DSN secrets found — did secrets.tf move?"
    for dsn in dsns:
        password_interp = re.search(r"://[^:]+:\$\{([^}]+)\}@", dsn)
        assert password_interp is not None, dsn
        assert password_interp.group(1).startswith("urlencode("), (
            f"password interpolation {password_interp.group(1)!r} is not "
            "urlencode()d — a generated password containing % or # or ? "
            "will not survive the round trip"
        )


def test_the_override_special_set_is_still_the_one_these_tests_assume() -> None:
    """If the character set changes, the parametrisation above is stale."""
    match = re.search(r'override_special\s*=\s*"([^"]*)"', _secrets_tf())

    assert match is not None
    assert match.group(1) == _OVERRIDE_SPECIAL
