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

Environment variables (optional overrides):
    LOAD_USER_EMAIL    default: loadtest@example.com
    LOAD_USER_PASSWORD default: LoadTest123!
    LOAD_ADMIN_EMAIL   default: loadtest-admin@example.com
    LOAD_ADMIN_PASSWORD default: LoadTest123!
"""

from __future__ import annotations

import os
import random
import uuid

from locust import HttpUser, between, task

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_PAYLOAD = {"filename": "data.csv", "rows": 100}
_JOB_TYPES = ["csv_upload", "report_generation", "bulk_sync", "document_analysis"]


def _login(client, email: str, password: str) -> str | None:
    """Return a Bearer token or None on failure."""
    with client.post(
        "/auth/login",
        json={"email": email, "password": password},
        catch_response=True,
        name="/auth/login",
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
            "/jobs",
            json={
                "type": job_type,
                "payload": _DEFAULT_PAYLOAD,
                "idempotency_key": str(uuid.uuid4()),
                "priority": random.randint(1, 5),
            },
            headers=self._auth(),
            name="/jobs [POST]",
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
            f"/jobs/{job_id}",
            headers=self._auth(),
            name="/jobs/{id} [GET]",
        )

    @task(2)
    def list_jobs(self) -> None:
        page = random.randint(1, 3)
        self.client.get(
            f"/jobs?page={page}&page_size=20",
            headers=self._auth(),
            name="/jobs [GET]",
        )

    @task(1)
    def get_nonexistent_job(self) -> None:
        """Deliberately hits a 404 — exercises error-path latency."""
        with self.client.get(
            f"/jobs/{uuid.uuid4()}",
            headers=self._auth(),
            catch_response=True,
            name="/jobs/{id} [GET 404]",
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
            f"/admin/jobs?page={page}&page_size=20",
            headers=self._auth(),
            name="/admin/jobs [GET]",
        )

    @task(2)
    def admin_list_users(self) -> None:
        self.client.get(
            "/admin/users",
            headers=self._auth(),
            name="/admin/users [GET]",
        )

    @task(1)
    def admin_get_job(self) -> None:
        if not self._job_ids:
            # seed from a list request
            resp = self.client.get(
                "/admin/jobs?page=1&page_size=10",
                headers=self._auth(),
                name="/admin/jobs [GET]",
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                self._job_ids = [j["id"] for j in items]
            return
        job_id = random.choice(self._job_ids)
        self.client.get(
            f"/admin/jobs/{job_id}",
            headers=self._auth(),
            name="/admin/jobs/{id} [GET]",
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
                "/jobs?page=1&page_size=20",
                headers={"Authorization": f"Bearer {self._token}"},
                name="/jobs [GET]",
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
            f"/jobs/{job_id}",
            headers=self._auth(),
            name="/jobs/{id} [GET cache]",
        )
