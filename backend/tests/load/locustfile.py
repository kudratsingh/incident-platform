"""
Locust load test suite for the Incident Platform API.

Simulates three user archetypes that reflect real traffic patterns:

  RegularUser   — logs in, submits jobs, polls status (bulk of traffic)
  AdminUser     — browses all jobs, replays failures (low frequency, wider reads)
  ReadHeavy     — hammers GET /jobs/{id} to exercise the Redis cache path

Run locally against a live stack (docker-compose up):

    locust -f backend/tests/load/locustfile.py \
        --host http://localhost:8000 \
        --users 50 --spawn-rate 5 --run-time 60s --headless

Or open the web UI (omit --headless) and drive it interactively.

`--host` is the origin only. Every path below is built from `ROUTES`
under `API_PREFIX` (`/api/v1`), which is where all routers are mounted;
backend/tests/api/test_locustfile_paths.py sends each one at the real app
so the suite cannot go back to 404ing every request unnoticed.

Environment variables (optional overrides):
    LOAD_USER_EMAIL    default: loadtest@example.com
    LOAD_USER_PASSWORD default: LoadTest123!
    LOAD_ADMIN_EMAIL   default: loadtest-admin@example.com
    LOAD_ADMIN_PASSWORD default: LoadTest123!
    LOAD_API_PREFIX    default: /api/v1
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
import uuid

from locust import HttpUser, between, task

# Make `app` importable no matter where locust is launched from: backend/ is
# two parents up from tests/load/, so the __file__-relative insert keeps
# `locust -f backend/tests/load/locustfile.py` working from any cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.models.enums import JobType  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_PAYLOAD = {"filename": "data.csv", "rows": 100}
# Derived from the enum so the load suite cannot drift from the API contract
# (a hardcoded copy once 422'd on three of the four types). Pinned by
# backend/tests/unit/test_locustfile_job_types.py.
_JOB_TYPES = [t.value for t in JobType]

# Every router is mounted under `Settings.api_v1_prefix`. Without this the
# documented `--host http://localhost:8000` sends every simulated request to
# a path that does not exist, so a "successful" run measures nothing but the
# 404 handler. Overridable for a stack mounted somewhere else.
API_PREFIX = os.getenv("LOAD_API_PREFIX", "/api/v1")

# The route templates this suite exercises, as name -> (method, path). The
# tasks build their URLs from here and nowhere else, and
# backend/tests/unit/test_locustfile_paths.py resolves every entry against
# the real FastAPI route table — so a dropped prefix, a renamed route or a
# remounted router fails a test instead of silently 404ing the whole run.
ROUTES: dict[str, tuple[str, str]] = {
    "login": ("POST", "/auth/login"),
    "create_job": ("POST", "/jobs"),
    "list_jobs": ("GET", "/jobs"),
    "get_job": ("GET", "/jobs/{job_id}"),
    "admin_list_jobs": ("GET", "/admin/jobs"),
    "admin_get_job": ("GET", "/admin/jobs/{job_id}"),
    "admin_list_users": ("GET", "/admin/users"),
}


def url(route: str, **params: object) -> str:
    """The absolute request path for a named route."""
    return API_PREFIX + ROUTES[route][1].format(**params)


def label(route: str, suffix: str = "") -> str:
    """The Locust stat label: the un-substituted template, so every id
    collapses into one row instead of one row per job."""
    text = API_PREFIX + ROUTES[route][1]
    return f"{text} {suffix}".strip()


def _login(client, email: str, password: str) -> str | None:
    """Return a Bearer token or None on failure."""
    with client.post(
        url("login"),
        json={"email": email, "password": password},
        catch_response=True,
        name=label("login"),
    ) as resp:
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            resp.success()
            return token
        resp.failure(f"login failed: {resp.status_code}")
        return None


# ---------------------------------------------------------------------------
# User archetypes
# ---------------------------------------------------------------------------


class RegularUser(HttpUser):
    """Simulates a normal operator: login → create jobs → poll status."""

    wait_time = between(1, 3)
    weight = 6  # 60 % of simulated users

    _email = os.getenv("LOAD_USER_EMAIL", "loadtest@example.com")
    _password = os.getenv("LOAD_USER_PASSWORD", "LoadTest123!")

    def on_start(self) -> None:
        self._token: str | None = _login(self.client, self._email, self._password)
        self._job_ids: list[str] = []

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    @task(3)
    def create_job(self) -> None:
        job_type = random.choice(_JOB_TYPES)
        resp = self.client.post(
            url("create_job"),
            json={
                "type": job_type,
                "payload": _DEFAULT_PAYLOAD,
                "idempotency_key": str(uuid.uuid4()),
                "priority": random.randint(1, 5),
            },
            headers=self._auth(),
            name=label("create_job", "[POST]"),
        )
        if resp.status_code == 201:
            job_id = resp.json().get("id")
            if job_id:
                self._job_ids.append(job_id)
                # keep list bounded
                if len(self._job_ids) > 50:
                    self._job_ids.pop(0)

    @task(5)
    def get_job(self) -> None:
        if not self._job_ids:
            return
        job_id = random.choice(self._job_ids)
        self.client.get(
            url("get_job", job_id=job_id),
            headers=self._auth(),
            name=label("get_job", "[GET]"),
        )

    @task(2)
    def list_jobs(self) -> None:
        page = random.randint(1, 3)
        self.client.get(
            f"{url('list_jobs')}?page={page}&page_size=20",
            headers=self._auth(),
            name=label("list_jobs", "[GET]"),
        )

    @task(1)
    def get_nonexistent_job(self) -> None:
        """Deliberately hits a 404 — exercises error-path latency."""
        with self.client.get(
            url("get_job", job_id=uuid.uuid4()),
            headers=self._auth(),
            catch_response=True,
            name=label("get_job", "[GET 404]"),
        ) as resp:
            if resp.status_code == 404:
                resp.success()  # expected — don't count as failure


class AdminUser(HttpUser):
    """Simulates an admin: browse all jobs, replay failures."""

    wait_time = between(2, 5)
    weight = 2  # 20 % of simulated users

    _email = os.getenv("LOAD_ADMIN_EMAIL", "loadtest-admin@example.com")
    _password = os.getenv("LOAD_ADMIN_PASSWORD", "LoadTest123!")

    def on_start(self) -> None:
        self._token: str | None = _login(self.client, self._email, self._password)
        self._job_ids: list[str] = []

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    @task(4)
    def admin_list_jobs(self) -> None:
        page = random.randint(1, 5)
        self.client.get(
            f"{url('admin_list_jobs')}?page={page}&page_size=20",
            headers=self._auth(),
            name=label("admin_list_jobs", "[GET]"),
        )

    @task(2)
    def admin_list_users(self) -> None:
        self.client.get(
            url("admin_list_users"),
            headers=self._auth(),
            name=label("admin_list_users", "[GET]"),
        )

    @task(1)
    def admin_get_job(self) -> None:
        if not self._job_ids:
            # seed from a list request
            resp = self.client.get(
                f"{url('admin_list_jobs')}?page=1&page_size=10",
                headers=self._auth(),
                name=label("admin_list_jobs", "[GET]"),
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                self._job_ids = [j["id"] for j in items]
            return
        job_id = random.choice(self._job_ids)
        self.client.get(
            url("admin_get_job", job_id=job_id),
            headers=self._auth(),
            name=label("admin_get_job", "[GET]"),
        )


class ReadHeavyUser(HttpUser):
    """
    Hammers the same job IDs repeatedly to exercise the Redis cache.
    Represents monitoring dashboards or polling clients that re-fetch
    the same resources many times per second.
    """

    wait_time = between(0.1, 0.5)
    weight = 2  # 20 % of simulated users

    _email = os.getenv("LOAD_USER_EMAIL", "loadtest@example.com")
    _password = os.getenv("LOAD_USER_PASSWORD", "LoadTest123!")

    # Shared across all ReadHeavyUser instances so they all hit the same IDs
    _hot_job_ids: list[str] = []

    def on_start(self) -> None:
        self._token: str | None = _login(self.client, self._email, self._password)

        # Seed hot job IDs once (only first instance does real work)
        if not ReadHeavyUser._hot_job_ids:
            resp = self.client.get(
                f"{url('list_jobs')}?page=1&page_size=20",
                headers={"Authorization": f"Bearer {self._token}"},
                name=label("list_jobs", "[GET]"),
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                ReadHeavyUser._hot_job_ids = [j["id"] for j in items]

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    @task
    def poll_hot_job(self) -> None:
        if not ReadHeavyUser._hot_job_ids:
            return
        job_id = random.choice(ReadHeavyUser._hot_job_ids)
        self.client.get(
            url("get_job", job_id=job_id),
            headers=self._auth(),
            name=label("get_job", "[GET cache]"),
        )
