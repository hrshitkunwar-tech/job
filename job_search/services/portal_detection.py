"""
Portal detection functions — pure page-state readers with no form writes.

All functions here READ page state and return facts about what the page looks
like. They accept a Page (or Frame) and return bool/str/list. No form fills,
no Playwright interactions that mutate state (except waiting).

Extracted from JobApplier so this layer can be tested independently with
DummyPage objects, without running a real browser.

JobApplier delegates to these functions via thin async wrapper methods.
Callers are unaffected.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def iter_scopes(page: Any) -> list[Any]:
    """
    Return a flat list of [page, ...frames] for external form searches.
    Many portals embed apply forms in iframes; scanning all scopes avoids misses.
    """
    scopes: list[Any] = [page]
    try:
        frames = list(page.frames)
        for fr in frames:
            if fr == page.main_frame:
                continue
            scopes.append(fr)
    except Exception:
        pass
    return scopes


def scope_url(scope: Any) -> str:
    """Return the URL of a Page or Frame as a lowercase string."""
    try:
        return (getattr(scope, "url", "") or "").lower()
    except Exception:
        return ""


def iter_scopes_prioritized(page: Any) -> list[Any]:
    """
    Sort scopes so the most likely application-form scope comes first.
    Reduces false positives from header search boxes or language menus on
    job listing pages.
    """
    scopes = iter_scopes(page)

    def score(scope: Any) -> int:
        url_l = scope_url(scope)
        s = 0
        if "apply.talemetry.com" in url_l or "talemetry" in url_l:
            s += 50
        if "myworkdayjobs.com" in url_l and "/apply" in url_l:
            s += 45
        if any(
            tok in url_l
            for tok in (
                "greenhouse.io",
                "lever.co",
                "icims.com",
                "smartrecruiters.com",
                "ashbyhq.com",
            )
        ):
            s += 40
        if any(tok in url_l for tok in ("/apply", "application", "candidate", "applicant")):
            s += 20
        if scope is not page:
            s += 5
        return s

    scopes.sort(key=score, reverse=True)
    return scopes


async def scope_has_fillable_controls(scope: Any, minimum_visible: int = 1) -> bool:
    """
    Return True when *scope* exposes at least *minimum_visible* visible form controls.
    Helps avoid clicking navigation on a parent listing page when an embedded form exists.
    """
    try:
        controls = scope.locator(
            "input:not([type='hidden']), textarea, select, "
            "[role='textbox'], [role='combobox'], [contenteditable='true'], "
            "input[type='radio'], input[type='checkbox']"
        )
        count = min(await controls.count(), 80)
        visible = 0
        for idx in range(count):
            try:
                if await controls.nth(idx).is_visible():
                    visible += 1
                    if visible >= minimum_visible:
                        return True
            except Exception:
                continue
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# LinkedIn detection
# ---------------------------------------------------------------------------


async def detect_linkedin_job_state(page: Any) -> str:
    """
    Best-effort state detection for a LinkedIn posting page.
    Returns: already_applied | closed | unknown
    """
    try:
        text = ((await page.inner_text("body")) or "").lower()
    except Exception:
        return "unknown"

    if any(
        token in text
        for token in (
            "application submitted",
            "you've applied",
            "you already applied",
            "applied ",
        )
    ):
        return "already_applied"

    if any(
        token in text
        for token in (
            "no longer accepting applications",
            "job is no longer available",
            "this job is no longer available",
            "position has been filled",
        )
    ):
        return "closed"

    return "unknown"


async def pick_visible_linkedin_apply_button(page: Any) -> tuple[Any, str]:
    """
    Return (element_handle, normalised_label) for the best visible LinkedIn apply CTA.
    Returns (None, '') when no suitable button is found.
    Prefers "Easy Apply" when available.
    """
    selectors = (
        "a.jobs-apply-button, button.jobs-apply-button, "
        "a[data-control-name*='jobdetails_topcard_inapply'], button[data-control-name*='jobdetails_topcard_inapply'], "
        "button.apply-button, a.apply-button, .jobs-apply-button--easy-apply"
    )
    candidates = await page.query_selector_all(selectors)
    fallback: tuple[Any, str] = (None, "")

    for candidate in candidates:
        try:
            if not await candidate.is_visible():
                continue
            if await candidate.get_attribute("disabled"):
                continue
            box = await candidate.bounding_box()
            if not box or box.get("width", 0) < 20 or box.get("height", 0) < 20:
                continue
            text = ((await candidate.inner_text()) or "").strip()
            aria = (await candidate.get_attribute("aria-label") or "").strip()
            label = f"{text} {aria}".strip().lower()
            if "apply" not in label:
                continue
            if "easy apply" in label:
                return candidate, label
            if fallback[0] is None:
                fallback = (candidate, label)
        except Exception:
            continue

    return fallback


# ---------------------------------------------------------------------------
# Form presence heuristics
# ---------------------------------------------------------------------------


async def looks_like_application_form(page: Any) -> bool:
    """
    Return True when the page appears to already be an application form view.
    Avoids re-clicking an "Apply" CTA on a job detail page.
    """
    url_l = (page.url or "").lower()
    if any(tok in url_l for tok in ("/apply", "/application", "candidate", "applicant")):
        return True

    try:
        loc = page.locator("input[type='file']")
        if await loc.count() > 0 and await loc.first.is_visible():
            return True
    except Exception:
        pass

    try:
        inputs = page.locator("input:not([type='hidden']), textarea, select")
        n = min(await inputs.count(), 80)
        visible = 0
        for i in range(n):
            try:
                if await inputs.nth(i).is_visible():
                    visible += 1
                    if visible >= 6:
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# Anti-bot / challenge detection
# ---------------------------------------------------------------------------


async def detect_anti_bot_challenge(page: Any) -> Optional[str]:
    """
    Detect anti-bot / verification interstitials that block automation.
    Returns a short reason string when detected, otherwise None.
    Also scans embedded frames.
    """

    def _match_reason(text: str) -> Optional[str]:
        checks = [
            ("cloudflare", "cloudflare_verification"),
            ("performing security verification", "security_verification"),
            ("security verification", "security_verification"),
            ("just a moment", "security_interstitial"),
            ("cf-chl", "cloudflare_challenge"),
            ("challenge-platform", "challenge_platform"),
            ("ray id", "challenge_ray_id"),
            ("enable javascript and cookies", "js_cookie_challenge"),
            ("enable javascript", "js_cookie_challenge"),
            ("verify you are not a bot", "bot_verification"),
            ("verify you are human", "human_verification"),
            ("are you human", "human_verification"),
            ("checking your browser", "security_interstitial"),
        ]
        for token, reason in checks:
            if token in text:
                return reason
        return None

    try:
        title = ((await page.title()) or "").lower()
    except Exception:
        title = ""
    try:
        body = ((await page.inner_text("body")) or "").lower()
    except Exception:
        body = ""
    try:
        html = ((await page.content()) or "").lower()
    except Exception:
        html = ""

    text = " ".join([title, body[:20000], html[:20000]])
    reason = _match_reason(text)
    if reason:
        return reason

    # Scan embedded frames — some portals render bot checks in iframes.
    try:
        frames = list(page.frames or [])
    except Exception:
        frames = []

    for fr in frames[:10]:
        try:
            fr_url = (fr.url or "").lower()
        except Exception:
            fr_url = ""
        reason = _match_reason(fr_url)
        if reason:
            return reason
        fr_body = ""
        fr_html = ""
        try:
            fr_body = ((await fr.inner_text("body")) or "").lower()
        except Exception:
            fr_body = ""
        try:
            fr_html = ((await fr.content()) or "").lower()
        except Exception:
            fr_html = ""
        reason = _match_reason(" ".join([fr_body[:15000], fr_html[:15000]]))
        if reason:
            return reason

    return None


# ---------------------------------------------------------------------------
# Workday-specific detection
# ---------------------------------------------------------------------------


async def detect_workday_login_wall(page: Any) -> bool:
    """
    Detect the Workday sign-in / create-account overlay.
    Returns True when login is required before the application can proceed.
    """
    try:
        url_l = (page.url or "").lower()
        if "myworkdayjobs.com" not in url_l:
            return False
    except Exception:
        return False

    selectors = [
        "[data-automation-id='signInContent']",
        "[data-automation-id='signInFormo']",
        "input[data-automation-id='password']",
        "input[data-automation-id='verifyPassword']",
        "[data-automation-id='noCaptchaWrapper']",
        "[data-automation-id='createAccountSubmitButton']",
    ]
    # Scan all scopes — auth fragments sometimes render in nested frames.
    scopes = iter_scopes_prioritized(page)
    for scope in scopes:
        for sel in selectors:
            try:
                loc = scope.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
                if await loc.count() > 0:
                    return True
            except Exception:
                continue

    try:
        body_text = ((await page.inner_text("body")) or "").lower()
        if any(
            tok in body_text
            for tok in (
                "create account",
                "sign in",
                "already have an account",
                "enter your password",
            )
        ):
            return True
    except Exception:
        pass

    return False


async def wait_for_workday_hydration(
    page: Any,
    app: Any = None,
    db: Any = None,
    max_wait_seconds: float = 18.0,
) -> None:
    """
    Wait for Workday's SPA to finish rendering CTA / form elements.
    Workday job pages spend a few seconds on a loading shell before hydrating.

    *app* and *db* are optional — when provided, hydration status is logged
    to app.automation_log and committed. Safe to call with (page,) only.
    """
    try:
        parsed = urllib.parse.urlparse(page.url or "")
        hostname = (parsed.hostname or "").lower()
        if "myworkdayjobs.com" not in hostname:
            return
    except Exception:
        return

    hydration_selectors = [
        "[data-automation-id='adventureButton']",
        "[data-automation-id='applyAdventurePage']",
        "[data-automation-id='applyFlowPage']",
        "[data-automation-id='signInContent']",
        "[data-automation-id='createAccountSubmitButton']",
        "input[data-automation-id='email']",
        "button[data-automation-id='bottom-navigation-next-button']",
    ]

    wait_step = 1.5
    waited = 0.0
    announced_wait = False

    while waited < max_wait_seconds:
        try:
            ready = False
            for sel in hydration_selectors:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    ready = True
                    break
            if ready:
                if waited >= 1.5 and app is not None and db is not None:
                    app.automation_log += f"Workday page hydrated after {waited:.1f}s.\n"
                    db.commit()
                return
        except Exception:
            pass

        try:
            loading_loc = page.locator("[data-automation-id='loading']")
            if (
                not announced_wait
                and await loading_loc.count() > 0
                and app is not None
                and db is not None
            ):
                app.automation_log += "Workday page is still loading; waiting for hydration...\n"
                db.commit()
                announced_wait = True
        except Exception:
            pass

        await asyncio.sleep(wait_step)
        waited += wait_step

    if app is not None and db is not None:
        app.automation_log += (
            f"Workday hydration timeout after {max_wait_seconds:.0f}s; continuing best-effort.\n"
        )
        db.commit()


async def has_workday_apply_navigation(page: Any) -> bool:
    """
    Return True when Workday is on an actual application step
    (not the job details shell or a login wall).
    """
    try:
        url_l = (page.url or "").lower()
        if "myworkdayjobs.com" not in url_l:
            return False
    except Exception:
        return False

    try:
        if await detect_workday_login_wall(page):
            return False
    except Exception:
        return False

    selectors = [
        "button[data-automation-id='bottom-navigation-next-button']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='bottom-navigation-submit-button']",
        "[data-automation-id='bottom-navigation-submit-button']",
        "button[data-automation-id='bottom-navigation-back-button']",
        "[data-automation-id='bottom-navigation-back-button']",
        "[data-automation-id='applyFlowPage']",
        "[data-automation-id='click_filter']",
        "[data-automation-id='click_filter'][aria-label*='save and continue' i]",
        "[data-automation-id='click_filter'][aria-label*='review' i]",
        "[data-automation-id='click_filter'][aria-label*='submit' i]",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                return True
        except Exception:
            continue
    return False


async def find_workday_navigation_control(page: Any) -> tuple[Any, str]:
    """
    Return (locator, label) for the Workday next/submit navigation control.
    Returns (None, '') when no actionable navigation control is found.

    Workday often hides native buttons and exposes clickable overlays
    (data-automation-id='click_filter') with labels like "Save and Continue".
    """
    selectors = [
        "[data-automation-id='click_filter'][aria-label]",
        "[data-automation-id='click_filter']",
        "button[data-automation-id='bottom-navigation-next-button']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='bottom-navigation-submit-button']",
        "[data-automation-id='bottom-navigation-submit-button']",
        "button[aria-label*='save and continue' i]",
        "button[aria-label*='review and submit' i]",
        "button[aria-label*='submit' i]",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(await loc.count(), 20)
        except Exception:
            continue
        for i in range(count):
            try:
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                if (await el.get_attribute("disabled")) is not None:
                    continue
                if ((await el.get_attribute("aria-disabled")) or "").strip().lower() == "true":
                    continue
                label = (
                    (await el.get_attribute("aria-label"))
                    or (await el.get_attribute("title"))
                    or (await el.inner_text())
                    or ""
                ).strip().lower()
                if not label:
                    try:
                        label = (
                            (
                                await el.evaluate(
                                    """(node) => {
                                        const candidates = [
                                            node,
                                            node.closest('button'),
                                            node.parentElement,
                                            node.parentElement && node.parentElement.querySelector('button'),
                                            node.closest('[data-automation-id]'),
                                        ].filter(Boolean);
                                        for (const c of candidates) {
                                            const txt =
                                                (c.getAttribute && (c.getAttribute('aria-label') || c.getAttribute('title'))) ||
                                                (c.innerText || c.textContent || '');
                                            if (txt && txt.trim()) return txt.trim();
                                        }
                                        return '';
                                    }"""
                                )
                            )
                            or ""
                        ).strip().lower()
                    except Exception:
                        label = ""
                if not label:
                    continue
                if any(tok in label for tok in ("sign in", "log in", "create account", "continue editing")):
                    continue
                if any(
                    tok in label
                    for tok in (
                        "save and continue",
                        "continue to next",
                        "next",
                        "review",
                        "submit",
                        "finish",
                        "complete",
                        "send application",
                    )
                ):
                    return el, label
            except Exception:
                continue
    return None, ""


# ---------------------------------------------------------------------------
# Submission state detection
# ---------------------------------------------------------------------------


async def detect_external_submission_success(page: Any) -> bool:
    """
    Return True when the page shows confirmation of a completed submission.
    Also handles "already applied" states.
    """
    try:
        text = ((await page.inner_text("body")) or "").lower()
    except Exception:
        text = ""

    success_tokens = (
        "thank you for applying",
        "application submitted",
        "successfully applied",
        "we have received your application",
        "application received",
        "your application has been submitted",
        "submission confirmed",
        "thanks for applying",
        "thank you, your application has been received",
        "your application is complete",
        "application complete",
        "you already applied for this job",
        "you've already applied for this job",
        "already applied for this job",
    )
    return any(tok in text for tok in success_tokens)


async def detect_external_submission_blocker(page: Any) -> Optional[str]:
    """
    Detect common post-submit blockers that prevent final submission.
    Returns a machine-friendly reason string, or None for a clean page.

    Reasons: video_processing_pending | required_fields_missing |
             required_questions_missing | required_source_missing |
             postal_code_format_error | verification_code_required |
             portal_login_required | submission_error | captcha_required
    """
    try:
        text = ((await page.inner_text("body")) or "").lower()
    except Exception:
        text = ""
    if not text:
        return None

    if "video answers to finish processing before submitting your application" in text:
        return "video_processing_pending"
    if any(
        tok in text
        for tok in (
            "please complete this required field",
            "this field is required",
            "required fields are missing",
            "can't be blank",
        )
    ):
        return "required_fields_missing"
    if "this question is required" in text:
        return "required_questions_missing"
    if "how did you hear about us" in text and "required" in text:
        return "required_source_missing"
    if "invalid phone" in text or "phone number is invalid" in text or "enter a valid phone number" in text:
        return "required_fields_missing"
    if "postal code must be 6 digits" in text or ("postal code" in text and "must be" in text and "digits" in text):
        return "postal_code_format_error"
    if "zip code" in text and any(tok in text for tok in ("invalid", "required", "must be")):
        return "postal_code_format_error"
    if any(tok in text for tok in ("verification code", "one-time password", "one time password", "otp", "enter code sent")):
        return "verification_code_required"
    if any(tok in text for tok in ("sign in to continue", "create account", "log in to apply")):
        return "portal_login_required"
    if "there was a problem submitting" in text or "unable to submit" in text:
        return "submission_error"
    if "captcha" in text or "verify you are human" in text:
        return "captcha_required"

    # DOM-level fallback: aria-invalid / error class markers.
    try:
        invalid = page.locator(
            "[aria-invalid='true'], .input-wrapper--error, .helper-text--error, .application-error, [data-testid$='-error']"
        )
        if await invalid.count() > 0:
            return "required_fields_missing"
    except Exception:
        pass

    try:
        empty_required = page.locator(
            "input[required]:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
        )
        total = await empty_required.count()
        for idx in range(min(total, 40)):
            field = empty_required.nth(idx)
            try:
                value = (await field.input_value() or "").strip()
                if not value:
                    return "required_fields_missing"
            except Exception:
                continue
    except Exception:
        pass

    try:
        required_selects = page.locator("select[required]")
        total = await required_selects.count()
        for idx in range(min(total, 40)):
            field = required_selects.nth(idx)
            try:
                value = (await field.input_value() or "").strip()
                if not value:
                    return "required_fields_missing"
            except Exception:
                continue
    except Exception:
        pass

    return None
