"""
Tests for job_search/services/field_resolution.py

Covers every exported function with at least one happy-path and one
edge/boundary case. All tests are pure unit tests — no Playwright, no DB,
no network. Should run in milliseconds.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from job_search.services import field_resolution
from job_search.services.defaults_config import (
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_MOBILE_NUMBER,
    DEFAULT_PHONE_COUNTRY_CODE,
    DEFAULT_POSTAL_CODE,
    DEFAULT_SOURCE_CHANNEL,
    DEFAULT_SOURCE_PLATFORM,
    DEFAULT_STATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(**kwargs) -> Any:
    """Build a minimal mock UserProfile with sensible defaults."""
    u = MagicMock()
    u.full_name = kwargs.get("full_name", "Harshit Kunwar")
    u.email = kwargs.get("email", "harshit@example.com")
    u.phone = kwargs.get("phone", "9876543210")
    u.location = kwargs.get("location", "Bangalore, Karnataka, India")
    u.linkedin_url = kwargs.get("linkedin_url", "https://linkedin.com/in/harshit")
    u.expected_ctc_lpa = kwargs.get("expected_ctc_lpa", 20)
    u.current_ctc_lpa = kwargs.get("current_ctc_lpa", 15)
    u.notice_period_days = kwargs.get("notice_period_days", 30)
    u.can_join_immediately = kwargs.get("can_join_immediately", False)
    u.willing_to_relocate = kwargs.get("willing_to_relocate", True)
    u.requires_sponsorship = kwargs.get("requires_sponsorship", False)
    u.work_authorization = kwargs.get("work_authorization", "Authorized")
    u.experience = kwargs.get("experience", [])
    u.application_answers = kwargs.get("application_answers", {})
    return u


def _job(**kwargs) -> Any:
    """Build a minimal mock Job."""
    j = MagicMock()
    j.source = kwargs.get("source", "linkedin")
    j.apply_url = kwargs.get("apply_url", "https://jobs.example.com/apply/123")
    j.url = kwargs.get("url", "https://linkedin.com/jobs/123")
    return j


# ---------------------------------------------------------------------------
# is_truthy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("yes", True),
    ("YES", True),
    ("y", True),
    ("true", True),
    ("1", True),
    ("on", True),
    ("no", False),
    ("false", False),
    ("0", False),
    ("random", False),
    (None, False),
    ([], False),
])
def test_is_truthy_all_variants(value, expected):
    assert field_resolution.is_truthy(value) is expected


# ---------------------------------------------------------------------------
# clean_value
# ---------------------------------------------------------------------------


def test_clean_value_strips_whitespace():
    assert field_resolution.clean_value("  hello  ") == "hello"


def test_clean_value_none_returns_empty_string():
    assert field_resolution.clean_value(None) == ""


def test_clean_value_coerces_int():
    assert field_resolution.clean_value(42) == "42"


# ---------------------------------------------------------------------------
# normalize_input_key
# ---------------------------------------------------------------------------


def test_normalize_input_key_lowercases_and_replaces_spaces():
    assert field_resolution.normalize_input_key("First Name") == "first_name"


def test_normalize_input_key_collapses_double_underscores():
    assert field_resolution.normalize_input_key("hello__world") == "hello_world"


def test_normalize_input_key_strips_leading_trailing_underscores():
    assert field_resolution.normalize_input_key("__key__") == "key"


def test_normalize_input_key_empty_string():
    assert field_resolution.normalize_input_key("") == ""


# ---------------------------------------------------------------------------
# as_yes_no
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (True, "Yes"),
    (False, "No"),
    ("yes", "Yes"),
    ("Yes", "Yes"),
    ("y", "Yes"),
    ("1", "Yes"),
    ("true", "Yes"),
    ("no", "No"),
    ("n", "No"),
    ("false", "No"),
    ("0", "No"),
    (None, None),
    ("maybe", None),
])
def test_as_yes_no_all_variants(value, expected):
    assert field_resolution.as_yes_no(value) == expected


# ---------------------------------------------------------------------------
# normalize_mobile_number
# ---------------------------------------------------------------------------


def test_normalize_mobile_takes_last_10_digits():
    assert field_resolution.normalize_mobile_number("+91 98765 43210") == "9876543210"


def test_normalize_mobile_short_raw_falls_back_to_default():
    result = field_resolution.normalize_mobile_number("123", default="9876543210")
    assert result == "9876543210"


def test_normalize_mobile_none_falls_back_to_default():
    result = field_resolution.normalize_mobile_number(None, default="9876543210")
    assert result == "9876543210"


def test_normalize_mobile_default_mobile_number_constant():
    # The module-level default is the configured default
    digits = "".join(c for c in DEFAULT_MOBILE_NUMBER if c.isdigit())
    assert len(digits) == 10


# ---------------------------------------------------------------------------
# extract_name_parts
# ---------------------------------------------------------------------------


def test_extract_name_parts_splits_full_name():
    user = _user(full_name="Harshit Kunwar")
    full, first, last = field_resolution.extract_name_parts(user)
    assert first == "Harshit"
    assert last == "Kunwar"
    assert full == "Harshit Kunwar"


def test_extract_name_parts_single_name_gets_default_last():
    user = _user(full_name="Candidate")
    _, first, last = field_resolution.extract_name_parts(user)
    assert first == "Candidate"
    assert last == "Kunwar"  # fallback last name


def test_extract_name_parts_falls_back_to_parsed_resume():
    user = _user(full_name="")
    _, first, last = field_resolution.extract_name_parts(user, parsed_resume={"name": "Jane Doe"})
    assert first == "Jane"
    assert last == "Doe"


# ---------------------------------------------------------------------------
# location_parts
# ---------------------------------------------------------------------------


def test_location_parts_recognises_bangalore():
    city, state, country = field_resolution.location_parts("Bangalore, Karnataka, India")
    assert city == "Bengaluru"
    assert state == "Karnataka"
    assert country == DEFAULT_COUNTRY


def test_location_parts_recognises_mumbai():
    city, state, country = field_resolution.location_parts("Mumbai, MH")
    assert city == "Mumbai"
    assert state == "Maharashtra"


def test_location_parts_detects_usa():
    _, _, country = field_resolution.location_parts("San Francisco, USA")
    assert country == "United States"


def test_location_parts_empty_returns_defaults():
    city, state, country = field_resolution.location_parts("")
    assert city == DEFAULT_CITY
    assert state == DEFAULT_STATE
    assert country == DEFAULT_COUNTRY


# ---------------------------------------------------------------------------
# postal_code_from_location_text
# ---------------------------------------------------------------------------


def test_postal_code_bengaluru_city_name():
    code = field_resolution.postal_code_from_location_text("Bengaluru")
    assert code == "560102"


def test_postal_code_extracts_explicit_6digit_code():
    code = field_resolution.postal_code_from_location_text("HSR Layout 560102")
    assert code == "560102"


def test_postal_code_unknown_location_returns_none():
    code = field_resolution.postal_code_from_location_text("Timbuktu")
    assert code is None


def test_postal_code_empty_returns_none():
    assert field_resolution.postal_code_from_location_text("") is None


# ---------------------------------------------------------------------------
# input_key_from_meta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,expected_key", [
    ("phone", "phone"),
    ("mobile", "phone"),
    ("telephone", "phone"),
    ("Phone Number", "phone"),
    ("email", "email"),
    ("Email Address", "email"),
    ("city", "city"),
    ("town", "city"),
    ("state", "state"),
    ("province", "state"),
    ("country", "country"),
    ("postal code", "postal_code"),
    ("zip code", "postal_code"),
    ("pincode", "postal_code"),
    ("first name", "first_name"),
    ("given name", "first_name"),
    ("last name", "last_name"),
    ("surname", "last_name"),
    ("family name", "last_name"),
    ("full name", "full_name"),
    ("how did you hear about us", "hear_about_us"),
    ("hear about us", "hear_about_us"),
    ("verification code", "verification_code"),
    ("otp", "verification_code"),
    ("one time password", "verification_code"),
    ("expected ctc", "expected_ctc_lpa"),
    ("expected salary", "expected_ctc_lpa"),
    ("current ctc", "current_ctc_lpa"),
    ("notice period", "notice_period_days"),
    ("linkedin", "linkedin_url"),
    ("linkedin url", "linkedin_url"),
    ("years of experience", "total_experience_years"),
    ("relocate", "willing_to_relocate"),
    ("sponsorship", "requires_sponsorship"),
    ("work authorization", "work_authorization"),
    ("applied before", "applied_before"),
    ("previously worked for", "worked_here_before"),
])
def test_input_key_from_meta_label_mapping(label, expected_key):
    assert field_resolution.input_key_from_meta(label) == expected_key


def test_input_key_from_meta_phone_extension_not_misclassified():
    # "extension" alone with type=text should be phone_extension, not phone
    key = field_resolution.input_key_from_meta("extension", input_type="text")
    assert key == "phone_extension"


def test_input_key_from_meta_previous_email_before_plain_email():
    key = field_resolution.input_key_from_meta("previous email")
    assert key == "previous_company_email"


def test_input_key_from_meta_unknown_returns_normalised_label():
    key = field_resolution.input_key_from_meta("some weird custom field xyz")
    # Should not crash; returns a normalised key
    assert isinstance(key, str)
    assert len(key) > 0


def test_input_key_from_meta_file_type_returns_resume_file():
    key = field_resolution.input_key_from_meta("upload document", input_type="file")
    assert key == "resume_file"


# ---------------------------------------------------------------------------
# input_question
# ---------------------------------------------------------------------------


def test_input_question_known_key_returns_specific_question():
    q = field_resolution.input_question("verification_code", "Verify")
    assert "verification code" in q.lower()


def test_input_question_unknown_key_includes_label():
    q = field_resolution.input_question("some_unknown_key", "My Custom Label")
    assert "My Custom Label" in q


# ---------------------------------------------------------------------------
# answer_overrides_for_application
# ---------------------------------------------------------------------------


def test_answer_overrides_prefers_app_over_user():
    user = _user(application_answers={"phone": "1111111111"})
    app = MagicMock()
    app.user_inputs = {"phone": "9999999999"}
    result = field_resolution.answer_overrides_for_application(user, app)
    assert result["phone"] == "9999999999"


def test_answer_overrides_skips_dunder_keys():
    user = _user(application_answers={"__stop_requested": True, "email": "user@test.com"})
    app = MagicMock()
    app.user_inputs = {}
    result = field_resolution.answer_overrides_for_application(user, app)
    assert "__stop_requested" not in result
    assert "email" in result


def test_answer_overrides_skips_complex_values():
    user = _user(application_answers={"skills": ["python", "go"], "city": "Bangalore"})
    app = MagicMock()
    app.user_inputs = {}
    result = field_resolution.answer_overrides_for_application(user, app)
    assert "skills" not in result
    assert "city" in result


# ---------------------------------------------------------------------------
# answer_value_for_key
# ---------------------------------------------------------------------------


def test_answer_value_uses_explicit_override_first():
    user = _user(email="real@example.com")
    result = field_resolution.answer_value_for_key("email", user, overrides={"email": "override@example.com"})
    assert result == "override@example.com"


def test_answer_value_phone_normalised_to_10_digits():
    user = _user(phone="+919876543210")
    result = field_resolution.answer_value_for_key("phone", user)
    assert result == "9876543210"


def test_answer_value_hear_about_us_returns_default_source():
    user = _user()
    result = field_resolution.answer_value_for_key("hear_about_us", user)
    assert result == DEFAULT_SOURCE_CHANNEL


def test_answer_value_hear_about_us_platform_returns_default_platform():
    user = _user()
    result = field_resolution.answer_value_for_key("hear_about_us_platform", user)
    assert result == DEFAULT_SOURCE_PLATFORM


def test_answer_value_phone_country_code_returns_default():
    user = _user()
    result = field_resolution.answer_value_for_key("phone_country_code", user)
    assert result == DEFAULT_PHONE_COUNTRY_CODE


def test_answer_value_applied_before_defaults_to_no():
    user = _user()
    result = field_resolution.answer_value_for_key("applied_before", user)
    assert result == "No"


def test_answer_value_worked_here_before_defaults_to_no():
    user = _user()
    result = field_resolution.answer_value_for_key("worked_here_before", user)
    assert result == "No"


def test_answer_value_verification_code_returns_none_without_override():
    user = _user()
    result = field_resolution.answer_value_for_key("verification_code", user)
    assert result is None


def test_answer_value_expected_ctc_from_profile():
    user = _user(expected_ctc_lpa=25)
    result = field_resolution.answer_value_for_key("expected_ctc_lpa", user)
    assert result == "25"


def test_answer_value_no_user_returns_default_postal_code():
    result = field_resolution.answer_value_for_key("postal_code", None)
    assert result == DEFAULT_POSTAL_CODE


# ---------------------------------------------------------------------------
# resolve_field_value
# ---------------------------------------------------------------------------


def test_resolve_field_value_end_to_end():
    user = _user(email="test@example.com")
    key, value = field_resolution.resolve_field_value("Email Address", "email", user)
    assert key == "email"
    assert value == "test@example.com"


def test_resolve_field_value_returns_key_and_none_when_unresolvable():
    user = _user()
    key, value = field_resolution.resolve_field_value("verification code", "text", user)
    assert key == "verification_code"
    assert value is None


# ---------------------------------------------------------------------------
# issue_context
# ---------------------------------------------------------------------------


def test_issue_context_extracts_source_and_domain():
    job = _job(source="greenhouse", apply_url="https://boards.greenhouse.io/company/jobs/123")
    source, domain = field_resolution.issue_context(job)
    assert source == "greenhouse"
    assert "greenhouse" in domain


def test_issue_context_none_job_returns_nones():
    source, domain = field_resolution.issue_context(None)
    assert source is None
    assert domain is None


# ---------------------------------------------------------------------------
# classify_issue
# ---------------------------------------------------------------------------


def test_classify_issue_verification_code():
    user = _user(expected_ctc_lpa=20, current_ctc_lpa=15, notice_period_days=30)
    cat, inputs, questions = field_resolution.classify_issue("verification code required", None, user)
    assert cat == "verification_code_required"
    assert "verification_code" in inputs


def test_classify_issue_postal_code():
    user = _user(expected_ctc_lpa=20, current_ctc_lpa=15, notice_period_days=30)
    cat, inputs, _ = field_resolution.classify_issue("postal code must be 6 digits", None, user)
    assert cat == "postal_code_required"
    assert "postal_code" in inputs


def test_classify_issue_browser_crash():
    user = _user(expected_ctc_lpa=20, current_ctc_lpa=15, notice_period_days=30)
    cat, inputs, _ = field_resolution.classify_issue("page crashed unexpectedly", None, user)
    assert cat == "browser_crash"
    assert inputs == []


def test_classify_issue_anti_bot():
    user = _user(expected_ctc_lpa=20, current_ctc_lpa=15, notice_period_days=30)
    cat, inputs, _ = field_resolution.classify_issue("cloudflare security verification required", None, user)
    assert cat == "anti_bot_challenge"


def test_classify_issue_enrich_missing_ctc():
    user = _user(expected_ctc_lpa=None, current_ctc_lpa=15, notice_period_days=30)
    _, inputs, questions = field_resolution.classify_issue("some error", None, user)
    assert "expected_ctc_lpa" in inputs
    assert any("expected CTC" in q for q in questions)


def test_classify_issue_no_duplicate_required_inputs():
    user = _user(expected_ctc_lpa=None, current_ctc_lpa=None, notice_period_days=None)
    _, inputs, _ = field_resolution.classify_issue("random error", None, user)
    assert len(inputs) == len(set(inputs))


# ---------------------------------------------------------------------------
# submission_blocker_message
# ---------------------------------------------------------------------------


def test_submission_blocker_message_known_reason():
    msg = field_resolution.submission_blocker_message("verification_code_required")
    assert "verification" in msg.lower()


def test_submission_blocker_message_unknown_reason_passthrough():
    msg = field_resolution.submission_blocker_message("some_custom_reason")
    assert msg == "some_custom_reason"


# ---------------------------------------------------------------------------
# is_auto_resolvable_submission_blocker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,expected", [
    ("required_fields_missing", True),
    ("postal_code_format_error", True),
    ("submission_error", True),
    ("verification_code_required", False),
    ("portal_login_required", False),
    ("captcha_required", False),
    (None, False),
    ("", False),
])
def test_is_auto_resolvable(reason, expected):
    assert field_resolution.is_auto_resolvable_submission_blocker(reason) is expected


# ---------------------------------------------------------------------------
# is_hard_submission_blocker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,expected", [
    ("verification_code_required", True),
    ("portal_login_required", True),
    ("captcha_required", True),
    ("required_fields_missing", False),
    (None, False),
])
def test_is_hard_blocker(reason, expected):
    assert field_resolution.is_hard_submission_blocker(reason) is expected


# ---------------------------------------------------------------------------
# default_salary_answer
# ---------------------------------------------------------------------------


def test_default_salary_answer_lpa_returns_integer_string():
    user = _user(expected_ctc_lpa=20)
    result = field_resolution.default_salary_answer("expected salary lpa", user)
    assert result == "20"


def test_default_salary_answer_monthly_converts_to_monthly():
    user = _user(expected_ctc_lpa=12)  # 12 LPA = 1,00,000/month
    result = field_resolution.default_salary_answer("salary per month", user)
    assert result == str(int(round((12 * 100000) / 12)))


def test_default_salary_answer_current_uses_current_ctc():
    user = _user(expected_ctc_lpa=20, current_ctc_lpa=15)
    result = field_resolution.default_salary_answer("current ctc lpa", user)
    assert result == "15"


def test_default_salary_answer_no_ctc_returns_zero():
    user = _user(expected_ctc_lpa=None, current_ctc_lpa=None)
    result = field_resolution.default_salary_answer("expected salary", user)
    assert result == "0"


# ---------------------------------------------------------------------------
# preferred_binary
# ---------------------------------------------------------------------------


def test_preferred_binary_applied_before_returns_no():
    user = _user()
    result = field_resolution.preferred_binary("applied before", user)
    assert result == "no"


def test_preferred_binary_sponsorship_reflects_user_flag():
    user = _user(requires_sponsorship=True)
    result = field_resolution.preferred_binary("requires sponsorship", user)
    assert result == "yes"


def test_preferred_binary_relocate_reflects_user_flag():
    user = _user(willing_to_relocate=False)
    result = field_resolution.preferred_binary("willing to relocate", user)
    assert result == "no"


def test_preferred_binary_work_authorization_no_sponsorship_returns_yes():
    user = _user(requires_sponsorship=False)
    result = field_resolution.preferred_binary("authorized to work", user)
    assert result == "yes"


def test_preferred_binary_unknown_meta_returns_none():
    user = _user()
    result = field_resolution.preferred_binary("something completely unknown xyz", user)
    assert result is None
