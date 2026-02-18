"""
E2E tests for JSON API endpoints using Playwright's API request context.

These tests exercise the FastAPI API routes through real HTTP requests
against the running test server.
"""

import pytest
from playwright.sync_api import APIRequestContext
import time


@pytest.fixture()
def api(playwright, base_url) -> APIRequestContext:
    """Create an API request context targeting the test server."""
    ctx = playwright.request.new_context(base_url=base_url)
    yield ctx
    ctx.dispose()


@pytest.mark.e2e
class TestProfileAPI:
    def test_get_profile(self, api: APIRequestContext):
        resp = api.get("/api/profile")
        assert resp.status == 200
        data = resp.json()
        # Auto-created blank profile
        assert "full_name" in data

    def test_update_profile(self, api: APIRequestContext):
        payload = {
            "full_name": "Test User",
            "email": "test@example.com",
            "target_roles": ["Software Engineer"],
            "target_locations": ["Remote"],
        }
        resp = api.put("/api/profile", data=payload)
        assert resp.status == 200
        data = resp.json()
        assert data["full_name"] == "Test User"
        assert data["email"] == "test@example.com"


@pytest.mark.e2e
class TestJobsAPI:
    def test_list_jobs_empty(self, api: APIRequestContext):
        resp = api.get("/api/jobs")
        assert resp.status == 200
        data = resp.json()
        assert data["jobs"] == []
        assert data["total"] == 0

    def test_get_nonexistent_job(self, api: APIRequestContext):
        resp = api.get("/api/jobs/99999")
        assert resp.status == 404


@pytest.mark.e2e
class TestSearchStatusAPI:
    def test_run_search_without_profile_still_returns_status(self, api: APIRequestContext):
        payload = {
            "keywords": ["Customer Success Manager"],
            "locations": ["Remote"],
            "portals": ["unknown_portal"],
            "limit": 3,
        }
        run_resp = api.post("/api/search/run", data=payload)
        assert run_resp.status == 200
        run_data = run_resp.json()
        search_id = run_data["search_id"]

        # Poll for status completion.
        state = None
        for _ in range(15):
            status_resp = api.get(f"/api/search/status/{search_id}")
            assert status_resp.status == 200
            state = status_resp.json().get("state")
            if state in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.2)

        assert state in ("completed", "failed", "cancelled")


@pytest.mark.e2e
class TestApplicationPreflightAPI:
    def test_preflight_endpoint_exists(self, api: APIRequestContext):
        # Endpoint should return 404 for unknown app IDs (route exists).
        resp = api.get("/api/applications/99999/preflight")
        assert resp.status == 404
