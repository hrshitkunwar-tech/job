"""
Tests for job_search/services/portal_detection.py

All tests use the DummyPage/DummyLocator pattern established in
test_blocker_intake.py — no real browser, no Playwright binaries required.
Each DummyPage only implements the methods the function under test actually calls.

Runs in ~0.1s total.
"""

from __future__ import annotations

from typing import Any, Optional
import pytest

from job_search.services import portal_detection


# ---------------------------------------------------------------------------
# DummyPage helpers
# ---------------------------------------------------------------------------


class _Locator:
    """Minimal async locator stub."""
    def __init__(self, count: int = 0, visible: bool = False, value: str = ""):
        self._count = count
        self._visible = visible
        self._value = value

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible

    async def input_value(self) -> str:
        return self._value

    def nth(self, i: int) -> "_Locator":
        return self

    @property
    def first(self) -> "_Locator":
        return self

    async def get_attribute(self, name: str) -> Optional[str]:
        return None


class _EmptyLocator(_Locator):
    """Locator that always returns empty/falsy."""
    def __init__(self):
        super().__init__(count=0, visible=False, value="")


class _PresentLocator(_Locator):
    """Locator that reports count=1, visible=True."""
    def __init__(self, value: str = "filled"):
        super().__init__(count=1, visible=True, value=value)


# ---------------------------------------------------------------------------
# iter_scopes / scope_url (synchronous, no mocks needed)
# ---------------------------------------------------------------------------


class _FakePage:
    url = "https://jobs.example.com/apply/123"
    main_frame = object()

    class _FakeFrame:
        url = "https://ats.greenhouse.io/apply"

    def __init__(self, frames=None):
        self.frames = frames or []


def test_iter_scopes_returns_page_when_no_frames():
    page = _FakePage(frames=[])
    scopes = portal_detection.iter_scopes(page)
    assert scopes == [page]


def test_iter_scopes_includes_non_main_frames():
    page = _FakePage()
    frame1 = _FakePage._FakeFrame()
    frame2 = _FakePage._FakeFrame()
    page.frames = [page.main_frame, frame1, frame2]
    scopes = portal_detection.iter_scopes(page)
    # main_frame is excluded; frame1 and frame2 are included
    assert page in scopes
    assert frame1 in scopes
    assert frame2 in scopes
    assert page.main_frame not in scopes


def test_scope_url_returns_page_url():
    page = _FakePage()
    assert portal_detection.scope_url(page) == page.url.lower()


def test_scope_url_returns_empty_on_exception():
    class Broken:
        @property
        def url(self):
            raise RuntimeError("broken")
    assert portal_detection.scope_url(Broken()) == ""


def test_iter_scopes_prioritized_greenhouse_iframe_ranks_first():
    page = _FakePage(frames=[])
    page.url = "https://example.com/jobs/123"

    class GHFrame:
        url = "https://boards.greenhouse.io/company/jobs/456"
    frame = GHFrame()
    page.frames = [frame]

    scopes = portal_detection.iter_scopes_prioritized(page)
    # Greenhouse iframe should score higher than the parent page
    assert scopes[0] is frame


# ---------------------------------------------------------------------------
# detect_linkedin_job_state
# ---------------------------------------------------------------------------


class _InnerTextPage:
    def __init__(self, text: str):
        self._text = text

    async def inner_text(self, selector: str) -> str:
        return self._text


async def test_detect_linkedin_job_state_already_applied():
    page = _InnerTextPage("application submitted successfully.")
    result = await portal_detection.detect_linkedin_job_state(page)
    assert result == "already_applied"


async def test_detect_linkedin_job_state_closed():
    page = _InnerTextPage("This job is no longer available.")
    result = await portal_detection.detect_linkedin_job_state(page)
    assert result == "closed"


async def test_detect_linkedin_job_state_unknown_for_normal_page():
    page = _InnerTextPage("Senior Software Engineer at Acme Corp.")
    result = await portal_detection.detect_linkedin_job_state(page)
    assert result == "unknown"


async def test_detect_linkedin_job_state_handles_exception():
    class BrokenPage:
        async def inner_text(self, s: str) -> str:
            raise RuntimeError("page crashed")

    result = await portal_detection.detect_linkedin_job_state(BrokenPage())
    assert result == "unknown"


# ---------------------------------------------------------------------------
# looks_like_application_form
# ---------------------------------------------------------------------------


class _UrlPage:
    def __init__(self, url: str = "", locator_count: int = 0):
        self.url = url
        self._count = locator_count

    def locator(self, selector: str) -> _Locator:
        if self._count > 0:
            return _PresentLocator()
        return _EmptyLocator()


async def test_looks_like_application_form_apply_in_url():
    page = _UrlPage(url="https://company.com/apply/job123")
    assert await portal_detection.looks_like_application_form(page) is True


async def test_looks_like_application_form_candidate_in_url():
    page = _UrlPage(url="https://ats.example.com/candidate/apply")
    assert await portal_detection.looks_like_application_form(page) is True


async def test_looks_like_application_form_false_for_listing_page():
    class ListingPage:
        url = "https://example.com/jobs"

        def locator(self, selector: str) -> _Locator:
            return _EmptyLocator()

    assert await portal_detection.looks_like_application_form(ListingPage()) is False


# ---------------------------------------------------------------------------
# detect_anti_bot_challenge
# ---------------------------------------------------------------------------


class _AntiBotPage:
    def __init__(self, title: str = "", body: str = "", html: str = "", frames=None):
        self._title = title
        self._body = body
        self._html = html
        self.frames = frames or []

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return self._body

    async def content(self) -> str:
        return self._html


async def test_detect_anti_bot_cloudflare_in_body():
    page = _AntiBotPage(body="Please wait... cloudflare is checking your connection.")
    reason = await portal_detection.detect_anti_bot_challenge(page)
    assert reason == "cloudflare_verification"


async def test_detect_anti_bot_hcaptcha_in_body():
    page = _AntiBotPage(body="verify you are not a bot by completing the challenge.")
    reason = await portal_detection.detect_anti_bot_challenge(page)
    assert reason == "bot_verification"


async def test_detect_anti_bot_just_a_moment_title():
    page = _AntiBotPage(title="Just a moment...", body="")
    reason = await portal_detection.detect_anti_bot_challenge(page)
    assert reason == "security_interstitial"


async def test_detect_anti_bot_returns_none_for_clean_page():
    page = _AntiBotPage(
        title="Apply for Software Engineer",
        body="Please fill in your details below.",
        html="<form><input name='name'></form>",
    )
    reason = await portal_detection.detect_anti_bot_challenge(page)
    assert reason is None


async def test_detect_anti_bot_scans_embedded_frames():
    class Frame:
        url = "https://challenges.cloudflare.com/challenge"

        async def inner_text(self, s: str) -> str:
            return ""

        async def content(self) -> str:
            return ""

    page = _AntiBotPage(body="Normal apply page content", frames=[Frame()])
    reason = await portal_detection.detect_anti_bot_challenge(page)
    # "cloudflare" in the frame URL matches before "challenge-platform"
    assert reason == "cloudflare_verification"


# ---------------------------------------------------------------------------
# detect_workday_login_wall
# ---------------------------------------------------------------------------


class _WorkdayPage:
    def __init__(self, url: str, body: str = "", locator_count: int = 0):
        self.url = url
        self._body = body
        self._count = locator_count
        self.frames = []
        self.main_frame = object()

    def locator(self, selector: str) -> _Locator:
        if self._count > 0:
            return _PresentLocator()
        return _EmptyLocator()

    async def inner_text(self, selector: str) -> str:
        return self._body


async def test_detect_workday_login_wall_returns_false_for_non_workday():
    page = _WorkdayPage(url="https://jobs.greenhouse.io/apply/123")
    assert await portal_detection.detect_workday_login_wall(page) is False


async def test_detect_workday_login_wall_detects_sign_in_selector():
    page = _WorkdayPage(
        url="https://company.myworkdayjobs.com/apply/job",
        locator_count=1,
    )
    assert await portal_detection.detect_workday_login_wall(page) is True


async def test_detect_workday_login_wall_detects_body_text():
    page = _WorkdayPage(
        url="https://company.myworkdayjobs.com/apply/job",
        body="Create account or sign in to continue.",
        locator_count=0,
    )
    assert await portal_detection.detect_workday_login_wall(page) is True


async def test_detect_workday_login_wall_returns_false_for_clean_form():
    page = _WorkdayPage(
        url="https://company.myworkdayjobs.com/apply/job",
        body="Please enter your work experience details.",
        locator_count=0,
    )
    assert await portal_detection.detect_workday_login_wall(page) is False


# ---------------------------------------------------------------------------
# detect_external_submission_success
# ---------------------------------------------------------------------------


async def test_detect_success_thank_you_for_applying():
    class P:
        async def inner_text(self, s):
            return "Thank you for applying! We will be in touch."

    assert await portal_detection.detect_external_submission_success(P()) is True


async def test_detect_success_application_submitted():
    class P:
        async def inner_text(self, s):
            return "Your application has been submitted successfully."

    assert await portal_detection.detect_external_submission_success(P()) is True


async def test_detect_success_already_applied():
    class P:
        async def inner_text(self, s):
            return "You already applied for this job on Jan 5."

    assert await portal_detection.detect_external_submission_success(P()) is True


async def test_detect_success_returns_false_for_normal_form():
    class P:
        async def inner_text(self, s):
            return "Please fill in all required fields."

    assert await portal_detection.detect_external_submission_success(P()) is False


async def test_detect_success_handles_exception():
    class BrokenPage:
        async def inner_text(self, s):
            raise RuntimeError("page crashed")

    # Should return False gracefully
    assert await portal_detection.detect_external_submission_success(BrokenPage()) is False


# ---------------------------------------------------------------------------
# detect_external_submission_blocker
# ---------------------------------------------------------------------------


class _BlockerPage:
    def __init__(self, body: str = "", locator_count: int = 0, field_value: str = "filled"):
        self._body = body
        self._count = locator_count
        self._value = field_value

    async def inner_text(self, selector: str) -> str:
        return self._body

    def locator(self, selector: str) -> _Locator:
        if self._count > 0:
            return _Locator(count=self._count, visible=True, value=self._value)
        return _EmptyLocator()


async def test_detect_blocker_video_processing():
    page = _BlockerPage(
        body="Please wait for your video answers to finish processing before submitting your application."
    )
    assert await portal_detection.detect_external_submission_blocker(page) == "video_processing_pending"


async def test_detect_blocker_required_field():
    page = _BlockerPage(body="Please complete this required field.")
    assert await portal_detection.detect_external_submission_blocker(page) == "required_fields_missing"


async def test_detect_blocker_source_missing():
    page = _BlockerPage(body="how did you hear about us is required to continue.")
    assert await portal_detection.detect_external_submission_blocker(page) == "required_source_missing"


async def test_detect_blocker_postal_code_format():
    page = _BlockerPage(body="postal code must be 6 digits.")
    assert await portal_detection.detect_external_submission_blocker(page) == "postal_code_format_error"


async def test_detect_blocker_verification_code():
    page = _BlockerPage(body="Please enter the verification code sent to your email.")
    assert await portal_detection.detect_external_submission_blocker(page) == "verification_code_required"


async def test_detect_blocker_portal_login():
    page = _BlockerPage(body="You must sign in to continue and log in to apply.")
    assert await portal_detection.detect_external_submission_blocker(page) == "portal_login_required"


async def test_detect_blocker_captcha():
    page = _BlockerPage(body="Please solve the captcha to proceed.")
    assert await portal_detection.detect_external_submission_blocker(page) == "captcha_required"


async def test_detect_blocker_returns_none_for_clean_page():
    page = _BlockerPage(body="")
    assert await portal_detection.detect_external_submission_blocker(page) is None


async def test_detect_blocker_returns_none_for_irrelevant_body():
    page = _BlockerPage(
        body="We have received your information. The team will review it shortly.",
        locator_count=0,
    )
    assert await portal_detection.detect_external_submission_blocker(page) is None
