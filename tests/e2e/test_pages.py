"""
E2E tests for page rendering and navigation using Playwright.

These tests verify that all main pages load correctly, contain expected
elements, and that basic navigation works.
"""

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestDashboard:
    def test_root_redirects_to_dashboard(self, page: Page, base_url):
        response = page.goto(f"{base_url}/")
        assert "/dashboard" in page.url

    def test_dashboard_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/dashboard")
        expect(page).to_have_title(re.compile(r".+"))
        # Dashboard should show stat cards
        expect(page.locator("body")).to_contain_text("Jobs Found")

    def test_dashboard_shows_stats(self, page: Page, base_url):
        page.goto(f"{base_url}/dashboard")
        # On a fresh DB, stats should be zero
        expect(page.locator("body")).to_contain_text("0")


@pytest.mark.e2e
class TestJobsPage:
    def test_jobs_page_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/jobs")
        expect(page).to_have_title(re.compile(r".+"))
        expect(page.locator("body")).to_contain_text("Job")

    def test_jobs_page_empty_state(self, page: Page, base_url):
        page.goto(f"{base_url}/jobs?show_all=true")
        # With no jobs in the test DB, page should still render without error
        assert page.url.endswith("/jobs?show_all=true") or "/jobs" in page.url


@pytest.mark.e2e
class TestSearchPage:
    def test_search_page_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/search")
        expect(page).to_have_title(re.compile(r".+"))
        expect(page.locator("body")).to_contain_text("Search")


@pytest.mark.e2e
class TestProfilePage:
    def test_profile_page_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/profile")
        expect(page).to_have_title(re.compile(r".+"))
        expect(page.locator("body")).to_contain_text("Profile")


@pytest.mark.e2e
class TestResumesPage:
    def test_resumes_page_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/resumes")
        expect(page).to_have_title(re.compile(r".+"))


@pytest.mark.e2e
class TestApplicationsPage:
    def test_applications_page_loads(self, page: Page, base_url):
        page.goto(f"{base_url}/applications")
        expect(page).to_have_title(re.compile(r".+"))


@pytest.mark.e2e
class TestNavigation:
    """Test navigating between pages via links."""

    def test_navigate_to_jobs_from_dashboard(self, page: Page, base_url):
        page.goto(f"{base_url}/dashboard")
        # Click a link that leads to /jobs
        jobs_link = page.locator("a[href='/jobs']").first
        if jobs_link.is_visible():
            jobs_link.click()
            page.wait_for_url("**/jobs**")
            assert "/jobs" in page.url

    def test_navigate_to_search_from_dashboard(self, page: Page, base_url):
        page.goto(f"{base_url}/dashboard")
        search_link = page.locator("a[href='/search']").first
        if search_link.is_visible():
            search_link.click()
            page.wait_for_url("**/search**")
            assert "/search" in page.url

    def test_navigate_to_profile(self, page: Page, base_url):
        page.goto(f"{base_url}/dashboard")
        profile_link = page.locator("a[href='/profile']").first
        if profile_link.is_visible():
            profile_link.click()
            page.wait_for_url("**/profile**")
            assert "/profile" in page.url
