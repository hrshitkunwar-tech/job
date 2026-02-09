"""
E2E tests for JSON API endpoints using Playwright's API request context.

These tests exercise the FastAPI API routes through real HTTP requests
against the running test server.
"""

import pytest
from playwright.sync_api import APIRequestContext


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
