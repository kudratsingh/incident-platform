"""API contract tests for /api/v1/auth endpoints."""

import pytest
from httpx import AsyncClient


async def test_register_returns_201(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "newuser@example.com", "password": "securepass"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "newuser@example.com"
    assert body["role"] == "user"
    assert "id" in body
    assert "hashed_password" not in body


async def test_register_duplicate_email_returns_409(client: AsyncClient) -> None:
    payload = {"email": "dup@example.com", "password": "securepass"}
    await client.post("/api/v1/auth/register", json=payload)
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "conflict"


async def test_register_short_password_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "abc"},
    )
    assert resp.status_code == 422


async def test_login_success_returns_tokens(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "login@example.com", "password": "securepass"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "securepass"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


async def test_login_wrong_password_returns_401(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "wrongpw@example.com", "password": "securepass"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "wrongpw@example.com", "password": "badpassword"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "authentication_failed"


async def test_me_returns_current_user(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "user@example.com"


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_refresh_returns_new_tokens(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "refresh@example.com", "password": "securepass"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "refresh@example.com", "password": "securepass"},
    )
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()
