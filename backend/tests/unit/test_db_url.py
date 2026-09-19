"""Unit tests for app.core.db_url.is_async_url (F1-06)."""

from app.core.db_url import is_async_url


def test_psycopg2_url_is_not_async() -> None:
    assert is_async_url("postgresql+psycopg2://u:p@h/db") is False


def test_bare_postgresql_url_is_not_async() -> None:
    # No explicit driver: the default postgresql dialect is sync (psycopg2).
    assert is_async_url("postgresql://u:p@h/db") is False


def test_asyncpg_url_is_async() -> None:
    assert is_async_url("postgresql+asyncpg://u:p@h/db") is True
