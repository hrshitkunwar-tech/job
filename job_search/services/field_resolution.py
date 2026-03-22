"""
Pure field-resolution logic for job application automation.

All functions here are side-effect free: no Playwright, no SQLAlchemy, no file
I/O. They operate only on plain Python values and the model objects passed in.

Extracted from JobApplier to make this layer independently testable. JobApplier
delegates to these functions via thin wrapper methods, so external callers are
unaffected.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional

from job_search.services.defaults_config import (
    CITY_POSTAL_MAP,
    CITY_STATE_MAP,
    DEFAULT_ADDRESS_LINE_1,
    DEFAULT_ADDRESS_LINE_2,
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_MOBILE_NUMBER,
    DEFAULT_PHONE_COUNTRY_CODE,
    DEFAULT_PHONE_EXTENSION,
    DEFAULT_PHONE_TYPE,
    DEFAULT_POSTAL_CODE,
    DEFAULT_SOURCE_CHANNEL,
    DEFAULT_SOURCE_PLATFORM,
    DEFAULT_STATE,
)

# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def is_truthy(value: Any) -> bool:
    """Return True for values that represent an affirmative boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def clean_value(value: Any) -> str:
    """Strip and stringify a value; return empty string for None."""
    if value is None:
        return ""
    return str(value).strip()


def normalize_input_key(raw: str) -> str:
    """Convert an arbitrary field name to a stable snake_case key."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in (raw or ""))
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def as_yes_no(value: Any) -> Optional[str]:
    """Map a boolean-like value to 'Yes' or 'No'. Returns None if ambiguous."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip().lower()
    if text in {"yes", "y", "true", "1"}:
        return "Yes"
    if text in {"no", "n", "false", "0"}:
        return "No"
    return None


def normalize_mobile_number(
    raw: Optional[str],
    default: str = DEFAULT_MOBILE_NUMBER,
) -> str:
    """Return a 10-digit mobile number. Falls back to *default* if raw is short."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    default_digits = "".join(ch for ch in default if ch.isdigit())
    if len(default_digits) == 10:
        return default_digits
    return "0000000000"


# ---------------------------------------------------------------------------
# Name / location parsing
# ---------------------------------------------------------------------------


def extract_name_parts(
    user: Any,
    parsed_resume: Optional[dict[str, Any]] = None,
) -> tuple[str, str, str]:
    """
    Return (full_name, first_name, last_name) derived from *user* and
    optionally a *parsed_resume* dict.
    """
    parsed_resume = parsed_resume or {}
    full_name = (
        clean_value(getattr(user, "full_name", None))
        or clean_value(parsed_resume.get("name"))
        or clean_value(parsed_resume.get("full_name"))
        or "Candidate Kunwar"
    )
    parts = [p for p in full_name.split() if p]
    first_name = (parts[0] if parts else "Candidate").title()
    last_name = (
        parts[-1].title()
        if len(parts) > 1
        else ("Kunwar" if first_name.lower() == "candidate" else first_name.title())
    )
    full_name = " ".join(parts).strip() or f"{first_name} {last_name}"
    return full_name, first_name, last_name


def location_parts(location_text: str) -> tuple[str, str, str]:
    """
    Parse a location string into (city, state, country).
    Falls back to module-level defaults when values cannot be inferred.
    """
    text = clean_value(location_text)
    if not text:
        return DEFAULT_CITY, DEFAULT_STATE, DEFAULT_COUNTRY

    lowered = text.lower()
    city_guess = ""
    state_guess = ""

    for city, state in CITY_STATE_MAP.items():
        if city in lowered:
            city_guess = city.title().replace("Bangalore", "Bengaluru")
            state_guess = state
            break

    if not city_guess:
        city_guess = text.split(",")[0].strip().title() or DEFAULT_CITY
    if not state_guess:
        state_guess = DEFAULT_STATE

    country_guess = DEFAULT_COUNTRY
    if any(tok in lowered for tok in ("usa", "united states", "us,", "u.s.")):
        country_guess = "United States"
    elif any(tok in lowered for tok in ("uk", "united kingdom", "england")):
        country_guess = "United Kingdom"

    return city_guess, state_guess, country_guess


def postal_code_from_location_text(location_text: str) -> Optional[str]:
    """
    Extract or infer a 6-digit postal code from a location string.
    Returns None when no match is found.
    """
    text = (location_text or "").strip()
    if not text:
        return None

    # Prefer an explicit 6-digit code already present in the string.
    direct = re.search(r"\b(\d{6})\b", text)
    if direct:
        return direct.group(1)

    lowered = text.lower()
    for city, code in CITY_POSTAL_MAP.items():
        if city in lowered:
            return code

    return None


# ---------------------------------------------------------------------------
# Field-key normalisation
# ---------------------------------------------------------------------------


def input_key_from_meta(meta: str, input_type: str = "") -> str:
    """
    Map an ATS field label / placeholder / name to a canonical key.

    The if/elif chain is a priority-ordered lookup table: more specific
    patterns must appear before more general ones (e.g. "previous email"
    before plain "email").
    """
    text = (meta or "").lower()
    i_type = (input_type or "").lower()

    # "pincode" must be checked before bare "pin" to avoid misclassifying postal fields.
    if any(tok in text for tok in ("postal code", "zip code", "zipcode", "pin code", "pincode")):
        return "postal_code"
    if any(tok in text for tok in ("verification code", "otp", "one time password", "security code", "passcode")) or \
            ("pin" in text and "pincode" not in text):
        return "verification_code"
    if "password" in text:
        return "password"
    if any(tok in text for tok in ("expected ctc", "expected salary", "expected compensation", "expected pay")):
        return "expected_ctc_lpa"
    if any(tok in text for tok in ("current ctc", "current salary", "current compensation", "present ctc", "present salary")):
        return "current_ctc_lpa"
    if any(tok in text for tok in ("notice period", "notice in days")):
        return "notice_period_days"
    if any(tok in text for tok in ("address line 1", "address1", "street address", "street", "line1 address")):
        return "address_line_1"
    if any(tok in text for tok in ("address line 2", "address2", "apartment", "suite", "flat number")):
        return "address_line_2"
    if any(tok in text for tok in ("city", "town")):
        return "city"
    if any(tok in text for tok in ("state", "province", "region")):
        return "state"
    if any(tok in text for tok in ("country", "nationality country")):
        return "country"
    if any(
        tok in text
        for tok in (
            "which social media",
            "social media platform",
            "platform used",
            "social channel",
        )
    ):
        return "hear_about_us_platform"
    if any(tok in text for tok in ("join immediately", "immediate joining", "availability to join", "available to join")):
        return "can_join_immediately"
    if any(
        tok in text
        for tok in (
            "previous email",
            "email in trend micro",
            "former email",
            "old email",
        )
    ):
        return "previous_company_email"
    if any(tok in text for tok in ("email", "e-mail", "mail")):
        return "email"
    if any(
        tok in text
        for tok in (
            "previous employee id",
            "employee id in trend micro",
            "former employee id",
        )
    ):
        return "previous_employee_id"
    if any(
        tok in text
        for tok in (
            "previous manager name",
            "manager name in trend micro",
            "former manager name",
        )
    ):
        return "previous_manager_name"
    if any(
        tok in text
        for tok in (
            "applied in the past",
            "applied before",
            "previously applied",
            "have you applied",
        )
    ):
        return "applied_before"
    if any(
        tok in text
        for tok in (
            "have you previously worked for",
            "previously worked for",
            "worked here before",
            "worked for this company",
            "worked at this company",
            "worked for subsidiary",
            "worked for any subsidiary",
            "employed by subsidiary",
        )
    ):
        return "worked_here_before"
    if any(tok in text for tok in ("relocate", "relocation")):
        return "willing_to_relocate"
    if any(tok in text for tok in ("sponsor", "sponsorship", "visa support")):
        return "requires_sponsorship"
    if any(tok in text for tok in ("authorized to work", "work authorization", "work permit")):
        return "work_authorization"
    if any(tok in text for tok in ("phone type", "contact type", "type of phone", "number type")):
        return "phone_type"
    if any(tok in text for tok in ("phone device type", "device type")) and "phone" in text:
        return "phone_type"
    if any(tok in text for tok in ("phone extension", "extension")) and "phone" in text:
        return "phone_extension"
    if "extension" in text and i_type in {"text", "number", "tel"}:
        return "phone_extension"
    if any(tok in text for tok in ("country code", "dial code", "phone country", "mobile country code")) and any(
        tok in text for tok in ("phone", "mobile", "contact", "dial")
    ):
        return "phone_country_code"
    if any(
        tok in text
        for tok in (
            "how did you hear",
            "hear about us",
            "where did you hear",
            "source of application",
            "how did you find",
            "referral source",
            "job source",
        )
    ):
        return "hear_about_us"
    if any(tok in text for tok in ("linkedin", "linkedin url", "linkedin profile")):
        return "linkedin_url"
    if any(tok in text for tok in ("phone", "mobile", "telephone", "contact number")):
        return "phone"
    if any(tok in text for tok in ("years of experience", "experience in years", "total experience")):
        return "total_experience_years"
    if any(tok in text for tok in ("full name", "applicant name", "candidate name")):
        return "full_name"
    if any(tok in text for tok in ("local given name", "given name local")):
        return "local_given_name"
    if any(tok in text for tok in ("local family name", "family name local")):
        return "local_family_name"
    if any(tok in text for tok in ("legal first name", "legal given name")):
        return "first_name"
    if any(tok in text for tok in ("legal last name", "legal family name", "legal surname")):
        return "last_name"
    if any(tok in text for tok in ("first name", "given name")):
        return "first_name"
    if any(tok in text for tok in ("last name", "surname", "family name")):
        return "last_name"
    if "address" in text and "email" not in text:
        return "address_line_1"
    if any(tok in text for tok in ("city", "location")):
        return "location"
    if "username" in text or "user id" in text:
        return "email"
    if i_type == "file":
        return "resume_file"

    return normalize_input_key(text[:80]) or "required_input"


def input_question(key: str, label: str) -> str:
    """Return a user-facing question string for a given canonical field key."""
    k = (key or "").lower()
    questions = {
        "verification_code": "Enter the verification code sent to your official email/phone.",
        "password": "Portal password is required to continue this application.",
        "expected_ctc_lpa": "What is your expected CTC (LPA) for this application?",
        "current_ctc_lpa": "What is your current CTC (LPA)?",
        "notice_period_days": "What is your notice period in days?",
        "postal_code": "What postal code should be used for this application?",
        "address_line_1": "Provide address line 1 for this application.",
        "city": "Provide city for this application.",
        "state": "Provide state/province for this application.",
        "country": "Provide country for this application.",
        "hear_about_us_platform": "Which social media platform should be selected?",
        "can_join_immediately": "Can you join immediately? (yes/no)",
        "applied_before": "Have you applied to this company before? (yes/no)",
        "worked_here_before": "Have you worked for this company or subsidiary before? (yes/no)",
        "previous_company_email": "Provide your previous company email (use official email format).",
        "previous_employee_id": "Provide previous employee ID if requested.",
        "previous_manager_name": "Provide previous manager name if requested.",
        "local_given_name": "Provide local given name if required by portal.",
        "local_family_name": "Provide local family name if required by portal.",
        "willing_to_relocate": "Are you willing to relocate? (yes/no)",
        "requires_sponsorship": "Do you require visa/work sponsorship? (yes/no)",
        "work_authorization": "What is your work authorization status?",
        "phone_type": "Preferred phone type for this application.",
        "phone_country_code": "Preferred phone country code.",
        "hear_about_us": "How did you hear about this role?",
        "resume_file": "Upload/select a resume file for this application.",
    }
    return questions.get(k, f"Provide value for: {label or key}")


# ---------------------------------------------------------------------------
# Answer resolution
# ---------------------------------------------------------------------------


def answer_overrides_for_application(user: Any, app: Any) -> dict[str, Any]:
    """
    Merge user-level application_answers with app-level user_inputs.
    App-level answers take priority (they are more specific).
    Keys prefixed with '__' and complex values (dicts/lists) are skipped.
    """
    merged: dict[str, Any] = {}
    if user and isinstance(getattr(user, "application_answers", None), dict):
        for k, v in user.application_answers.items():
            if not isinstance(k, str) or k.startswith("__"):
                continue
            if isinstance(v, (dict, list, tuple, set)):
                continue
            nk = normalize_input_key(str(k))
            if nk:
                merged[nk] = v
    if app and isinstance(getattr(app, "user_inputs", None), dict):
        for k, v in app.user_inputs.items():
            if not isinstance(k, str) or k.startswith("__"):
                continue
            if isinstance(v, (dict, list, tuple, set)):
                continue
            nk = normalize_input_key(str(k))
            if nk:
                merged[nk] = v
    return merged


def answer_value_for_key(
    key: str,
    user: Any,
    overrides: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """
    Resolve the best answer for a canonical field *key*.

    Priority: explicit overrides → user profile fields → hardcoded defaults.
    """
    resolved_key = normalize_input_key(key)
    answers = overrides or {}

    source_keys = {
        "hear_about_us",
        "how_did_you_hear_about_us",
        "source_channel",
        "source_of_application",
        "job_source",
        "referral_source",
    }
    source_platform_keys = {
        "hear_about_us_platform",
        "social_media_platform",
        "source_platform",
    }
    phone_country_code_keys = {"phone_country_code", "country_code", "dial_code", "mobile_country_code"}
    phone_type_keys = {"phone_type", "contact_type", "number_type"}
    phone_extension_keys = {"phone_extension", "extension", "ext"}

    # --- Explicit override wins first ---
    if resolved_key in answers and answers[resolved_key] not in (None, ""):
        explicit = str(answers[resolved_key]).strip()
        if resolved_key == "phone":
            return normalize_mobile_number(explicit)
        if resolved_key in phone_country_code_keys:
            return DEFAULT_PHONE_COUNTRY_CODE
        if resolved_key in phone_type_keys:
            return DEFAULT_PHONE_TYPE
        if resolved_key in phone_extension_keys:
            return DEFAULT_PHONE_EXTENSION
        if resolved_key in source_keys:
            return DEFAULT_SOURCE_CHANNEL
        if resolved_key in source_platform_keys:
            return DEFAULT_SOURCE_PLATFORM
        return explicit

    # --- OTP / password: never fall back to defaults ---
    if resolved_key in {"verification_code", "otp", "security_code", "pin"}:
        for alt in ("verification_code", "otp", "security_code", "pin", "two_factor_code"):
            if alt in answers and answers[alt] not in (None, ""):
                return str(answers[alt])
        return None
    if resolved_key == "password":
        for alt in ("password", "portal_password", "account_password"):
            if alt in answers and answers[alt] not in (None, ""):
                return str(answers[alt])
        return None

    # Alias normalisation
    if resolved_key == "official_email":
        resolved_key = "email"
    if resolved_key == "previous_company_email":
        resolved_key = "email"

    # --- No user: fall back to module defaults ---
    if not user:
        safe_defaults: dict[str, str] = {
            "applied_before": "No",
            "worked_here_before": "No",
            "previous_employee_id": "0",
            "previous_manager_name": "NA",
            "address_line_2": DEFAULT_ADDRESS_LINE_2,
            "country": DEFAULT_COUNTRY,
            "state": DEFAULT_STATE,
            "city": DEFAULT_CITY,
            "address_line_1": DEFAULT_ADDRESS_LINE_1,
        }
        if resolved_key in phone_country_code_keys:
            return DEFAULT_PHONE_COUNTRY_CODE
        if resolved_key in phone_type_keys:
            return DEFAULT_PHONE_TYPE
        if resolved_key in phone_extension_keys:
            return DEFAULT_PHONE_EXTENSION
        if resolved_key in {"postal_code", "zip_code", "pincode"}:
            return DEFAULT_POSTAL_CODE
        return safe_defaults.get(resolved_key)

    # --- Derive from user profile ---
    if resolved_key == "email":
        return (user.email or "").strip() or None
    if resolved_key == "previous_employee_id":
        return "0"
    if resolved_key == "previous_manager_name":
        return "NA"
    if resolved_key == "full_name":
        return (user.full_name or "").strip() or None
    if resolved_key == "first_name":
        parts = (user.full_name or "").strip().split()
        return parts[0].title() if parts else None
    if resolved_key == "last_name":
        parts = (user.full_name or "").strip().split()
        return parts[-1].title() if len(parts) > 1 else None
    if resolved_key == "local_given_name":
        parts = (user.full_name or "").strip().split()
        return parts[0].title() if parts else None
    if resolved_key == "local_family_name":
        parts = (user.full_name or "").strip().split()
        return parts[-1].title() if len(parts) > 1 else (parts[0].title() if parts else None)
    if resolved_key == "phone":
        return normalize_mobile_number((user.phone or "").strip())
    if resolved_key in phone_country_code_keys:
        return DEFAULT_PHONE_COUNTRY_CODE
    if resolved_key in phone_type_keys:
        return DEFAULT_PHONE_TYPE
    if resolved_key in phone_extension_keys:
        return DEFAULT_PHONE_EXTENSION
    if resolved_key in source_keys:
        return DEFAULT_SOURCE_CHANNEL
    if resolved_key in source_platform_keys:
        return DEFAULT_SOURCE_PLATFORM
    if resolved_key == "location":
        return (user.location or "").strip() or "NA"
    if resolved_key == "address_line_1":
        return DEFAULT_ADDRESS_LINE_1
    if resolved_key == "address_line_2":
        return DEFAULT_ADDRESS_LINE_2
    if resolved_key == "city":
        city, _, _ = location_parts(user.location or "")
        return city
    if resolved_key == "state":
        _, state, _ = location_parts(user.location or "")
        return state
    if resolved_key == "country":
        _, _, country = location_parts(user.location or "")
        return country
    if resolved_key == "linkedin_url":
        return (user.linkedin_url or "").strip() or "https://linkedin.com"
    if resolved_key == "expected_ctc_lpa" and user.expected_ctc_lpa is not None:
        return str(user.expected_ctc_lpa)
    if resolved_key == "current_ctc_lpa" and user.current_ctc_lpa is not None:
        return str(user.current_ctc_lpa)
    if resolved_key == "notice_period_days" and user.notice_period_days is not None:
        return str(user.notice_period_days)
    if resolved_key == "can_join_immediately":
        return as_yes_no(user.can_join_immediately)
    if resolved_key in {"applied_before", "worked_here_before"}:
        return "No"
    if resolved_key == "willing_to_relocate":
        return as_yes_no(user.willing_to_relocate)
    if resolved_key == "requires_sponsorship":
        return as_yes_no(user.requires_sponsorship)
    if resolved_key == "work_authorization":
        return (user.work_authorization or "").strip() or None
    if resolved_key == "postal_code":
        return DEFAULT_POSTAL_CODE
    if resolved_key == "total_experience_years":
        if isinstance(user.experience, list) and user.experience:
            return str(max(1, len(user.experience)))
        return "1"

    return None


def resolve_field_value(
    meta: str,
    input_type: str,
    user: Any,
    overrides: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    """
    Top-level entry point: label/meta → (canonical_key, resolved_value).
    Returns (key, None) when no value could be resolved.
    """
    key = input_key_from_meta(meta, input_type)
    explicit = answer_value_for_key(key, user, overrides=overrides)
    if explicit not in (None, ""):
        return key, str(explicit)
    return key, None


# ---------------------------------------------------------------------------
# Issue classification
# ---------------------------------------------------------------------------


def issue_context(job: Any) -> tuple[Optional[str], Optional[str]]:
    """Return (source, domain) from a Job object. Both may be None."""
    if not job:
        return None, None
    source = (job.source or "").lower() or None
    url = job.apply_url or job.url or ""
    try:
        domain = (urllib.parse.urlparse(url).hostname or "").lower() or None
    except Exception:
        domain = None
    return source, domain


def classify_issue(
    message: str,
    job: Any,
    user: Any,
) -> tuple[str, list[str], list[str]]:
    """
    Categorise an automation error message into (category, required_inputs, questions).

    The return values feed directly into AutomationIssueEvent rows and the
    UI blocker-resolution flow.
    """
    text = (message or "").lower()
    category = "automation_issue"
    required_inputs: list[str] = []
    questions: list[str] = []

    if "anti-bot" in text or "cloudflare" in text or "security verification" in text:
        category = "anti_bot_challenge"
        required_inputs = ["manual_challenge_verification"]
        questions = ["Can you complete anti-bot verification in the opened apply window when prompted?"]
    elif "page crashed" in text or "target closed" in text or "browser has disconnected" in text:
        category = "browser_crash"
    elif "already applied" in text or "application already submitted" in text:
        category = "already_applied_detected"
    elif "automation completed" in text or "submitted successfully" in text:
        category = "submission_success"
    elif "linkedin login required" in text:
        category = "linkedin_login_required"
        required_inputs = ["linkedin_authenticated_session"]
        questions = ["Are you logged into LinkedIn in the automation popup window?"]
    elif "requires sign-in" in text or "sign-in not completed" in text or "account creation required" in text:
        category = "portal_login_required"
        required_inputs = ["portal_authenticated_session"]
        questions = [
            "Does this application portal require sign-in/account creation before applying?",
            "Can you complete the sign-in once in the opened automation window so we can save the session for future runs?",
        ]
    elif "no linkedin apply action found" in text:
        category = "linkedin_apply_action_missing"
        required_inputs = ["posting_state_confirmation"]
        questions = ["Does the posting show 'Applied/Application submitted' or 'No longer accepting applications'?"]
    elif "apply button was not interactable" in text:
        category = "linkedin_apply_interaction_blocked"
        required_inputs = ["linkedin_visibility_state"]
        questions = ["After opening the job, do you see an enabled Apply/Easy Apply button?"]
    elif "could not detect final submit button" in text or "no final submit control detected" in text:
        category = "final_submit_detection_failed"
        required_inputs = ["screening_answers", "portal_specific_submit_label"]
        questions = [
            "Which button label appears on the last step (Submit, Apply, Send, Complete)?",
            "Are there any required unanswered screening fields visible before final submit?",
        ]
    elif "verification code" in text or "one-time password" in text or "otp" in text:
        category = "verification_code_required"
        required_inputs = ["verification_code"]
        questions = ["Enter the verification code sent by the employer portal so automation can continue."]
    elif "how did you hear about us" in text or "hear about us is required" in text:
        category = "required_source_missing"
        required_inputs = ["hear_about_us"]
        questions = ["What source should be used for 'How did you hear about us?'"]
    elif "postal code must be 6 digits" in text or ("postal code" in text and "required" in text):
        category = "postal_code_required"
        required_inputs = ["postal_code"]
        questions = ["Provide a valid 6-digit postal code for this application."]
    elif "profile missing required fields" in text:
        category = "profile_missing_required_fields"
        required_inputs = ["full_name", "email"]
        questions = ["Please provide your full name and primary email for applications."]
    elif "score" in text and "below threshold" in text:
        category = "threshold_skip"
        required_inputs = ["min_score_preference"]
        questions = ["Should automation include jobs below your current minimum score threshold?"]
    elif "unsupported source for automation" in text:
        category = "unsupported_source"
        required_inputs = ["source_preference"]
        questions = ["Should we skip unsupported sources automatically or keep them for manual apply?"]

    # Enrich with profile-driven inputs when user data is incomplete.
    if user:
        if user.expected_ctc_lpa is None:
            required_inputs.append("expected_ctc_lpa")
            questions.append("What is your expected CTC in LPA?")
        if user.current_ctc_lpa is None:
            required_inputs.append("current_ctc_lpa")
            questions.append("What is your current CTC in LPA?")
        if user.notice_period_days is None:
            required_inputs.append("notice_period_days")
            questions.append("What is your notice period in days?")

    # Deduplicate while preserving order.
    required_inputs = sorted(set(required_inputs))
    seen_q: set[str] = set()
    dedup_questions: list[str] = []
    for q in questions:
        if q not in seen_q:
            seen_q.add(q)
            dedup_questions.append(q)

    return category, required_inputs, dedup_questions


# ---------------------------------------------------------------------------
# Submission blocker helpers
# ---------------------------------------------------------------------------


def submission_blocker_message(reason: str) -> str:
    """Return a human-readable description for a submission blocker reason code."""
    mapping = {
        "video_processing_pending": "Portal is still processing video answers; retry after processing completes.",
        "required_fields_missing": "Required form fields are still missing.",
        "required_questions_missing": "Required screening questions are still unanswered.",
        "required_source_missing": "A required application source field is missing.",
        "postal_code_format_error": "Postal code format is invalid for this portal.",
        "verification_code_required": "A verification code is required before submit.",
        "portal_login_required": "Portal sign-in/account creation must be completed before submit.",
        "submission_error": "Portal reported a submission error.",
        "captcha_required": "CAPTCHA/verification is required before submit.",
    }
    return mapping.get(reason, reason)


def is_auto_resolvable_submission_blocker(reason: Optional[str]) -> bool:
    """Return True when the automation can attempt to self-heal this blocker."""
    return (reason or "") in {
        "required_fields_missing",
        "required_questions_missing",
        "required_source_missing",
        "postal_code_format_error",
        "submission_error",
    }


def is_hard_submission_blocker(reason: Optional[str]) -> bool:
    """Return True when the blocker requires human intervention and cannot auto-recover."""
    return (reason or "") in {
        "verification_code_required",
        "portal_login_required",
        "captcha_required",
    }


# ---------------------------------------------------------------------------
# Salary / binary-choice helpers
# ---------------------------------------------------------------------------


def default_salary_answer(meta: str, user: Any) -> str:
    """
    Generate a salary answer string from the user's expected/current CTC.
    Converts LPA to monthly or absolute INR when the field label implies it.
    """
    expected_lpa = user.expected_ctc_lpa if user and user.expected_ctc_lpa is not None else None
    current_lpa = user.current_ctc_lpa if user and user.current_ctc_lpa is not None else None
    use_lpa = expected_lpa
    if any(k in meta for k in ("current", "present", "existing")):
        use_lpa = current_lpa if current_lpa is not None else expected_lpa
    if use_lpa is None:
        return "0"
    if any(k in meta for k in ("monthly", "per month", "/month")):
        return str(int(round((use_lpa * 100000) / 12)))
    if any(k in meta for k in ("inr", "rupee", "per annum", "annual", "yearly")):
        return str(int(round(use_lpa * 100000)))
    if any(k in meta for k in ("lpa", "lakh", "salary", "ctc", "compensation")):
        return str(int(round(use_lpa)))
    return str(int(round(use_lpa)))


def preferred_binary(meta: str, user: Any) -> Optional[str]:
    """
    Return 'yes' or 'no' for binary-choice fields (relocate, sponsorship, etc.).
    Returns None when the field cannot be confidently mapped.
    """
    if any(
        k in meta
        for k in (
            "applied in the past",
            "applied before",
            "previously applied",
            "have you applied",
            "have you previously worked for",
            "previously worked for",
            "worked here before",
            "worked for this company",
            "worked for any subsidiary",
            "subsidiary",
        )
    ):
        return "no"
    if any(k in meta for k in ("sponsor", "sponsorship")):
        if user and user.requires_sponsorship is not None:
            return "yes" if user.requires_sponsorship else "no"
        return "no"
    if any(k in meta for k in ("authorized", "work authorization", "legally")):
        if user and user.requires_sponsorship is not None:
            return "no" if user.requires_sponsorship else "yes"
        return "yes"
    if any(k in meta for k in ("relocate", "relocation")):
        if user and user.willing_to_relocate is not None:
            return "yes" if user.willing_to_relocate else "no"
        return "yes"
    if any(k in meta for k in ("immediate", "join now", "available to join")):
        if user and user.can_join_immediately is not None:
            return "yes" if user.can_join_immediately else "no"
        if user and user.notice_period_days == 0:
            return "yes"
        return "no"
    if any(k in meta for k in ("experience", "comfortable", "do you have", "are you able")):
        return "yes"
    return None
