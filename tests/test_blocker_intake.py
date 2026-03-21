import pytest

from job_search.config import settings
from job_search.models import Application, UserProfile, Resume
from job_search.services.applier import JobApplier


def test_input_key_detection_for_verification_code():
    applier = JobApplier()
    key = applier._input_key_from_meta("Enter OTP / verification code sent to email", "text")
    assert key == "verification_code"


def test_answer_overrides_merge_prefers_application_specific():
    applier = JobApplier()
    profile = UserProfile(full_name="Candidate", email="candidate@example.com")
    profile.application_answers = {"expected_ctc_lpa": 40}
    app = Application(job_id=1)
    app.user_inputs = {"expected_ctc_lpa": 45}

    merged = applier._answer_overrides_for_application(profile, app)
    assert merged["expected_ctc_lpa"] == 45


def test_resolve_field_value_uses_official_email():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="official@example.com")
    key, value = applier._resolve_field_value("username/login email", "text", user, {})
    assert key == "email"
    assert value == "official@example.com"


def test_classify_issue_for_verification_message():
    applier = JobApplier()
    category, required_inputs, questions = applier._classify_issue(
        "Submission blocked: verification code is required before submit.",
        job=None,
        user=None,
    )
    assert category == "verification_code_required"
    assert "verification_code" in required_inputs
    assert any("verification code" in q.lower() for q in questions)


def test_phone_answer_normalized_to_ten_digits():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="candidate@example.com", phone="+91 93191 35101")
    assert applier._answer_value_for_key("phone", user, {}) == "9319135101"


def test_hear_about_us_defaults_to_linkedin():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    key = applier._input_key_from_meta("How did you hear about us?", "select")
    assert key == "hear_about_us"
    assert applier._answer_value_for_key(key, user, {}) == "Social Media"


def test_hear_about_platform_defaults_to_linkedin():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    key = applier._input_key_from_meta("Which social media platform?", "select")
    assert key == "hear_about_us_platform"
    assert applier._answer_value_for_key(key, user, {}) == "LinkedIn"


def test_postal_code_defaults_to_six_digits():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    key = applier._input_key_from_meta("Postal code", "text")
    assert key == "postal_code"
    assert applier._answer_value_for_key(key, user, {}) == "110001"


def test_classify_issue_for_postal_code_error():
    applier = JobApplier()
    category, required_inputs, _ = applier._classify_issue(
        "Error - Page Error. Enter a postal code in the valid format: Postal code must be 6 digits",
        job=None,
        user=None,
    )
    assert category == "postal_code_required"
    assert "postal_code" in required_inputs


def test_postal_code_inferred_from_job_location():
    applier = JobApplier()
    assert applier._postal_code_from_location_text("Mumbai, Maharashtra, India") == "400001"
    assert applier._postal_code_from_location_text("Work From Home") is None


@pytest.mark.asyncio
async def test_external_success_detects_already_applied_phrase():
    class DummyPage:
        async def inner_text(self, selector: str):
            return "You already applied for this job."

    applier = JobApplier()
    assert await applier._detect_external_submission_success(DummyPage()) is True


def test_phone_extension_is_not_misclassified_as_source_field():
    applier = JobApplier()
    key = applier._input_key_from_meta("Phone Extension", "text")
    assert key == "phone_extension"
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    assert applier._answer_value_for_key(key, user, {}) == "0"


def test_runtime_overrides_generate_contact_defaults_when_missing():
    applier = JobApplier()
    user = UserProfile(full_name="Test User", email="test@example.com", location="Bangalore, India")
    overrides, sources = applier._build_runtime_answer_overrides(
        user=user,
        resume=None,
        job=None,
        app=None,
        db=None,
        base_overrides={},
    )
    assert overrides["phone_type"] == "mobile"
    assert overrides["phone_country_code"] == "+91"
    assert overrides["phone_extension"] == "0"
    assert overrides["postal_code"] == "560001"
    assert overrides["city"] == "Bengaluru"
    assert sources["postal_code"] in {"generated_placeholder", "profile", "resume", "previous_applications"}


def test_applied_before_key_defaults_to_no():
    applier = JobApplier()
    key = applier._input_key_from_meta("Have you applied in the past?", "select")
    assert key == "applied_before"
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    assert applier._answer_value_for_key(key, user, {}) == "No"


def test_best_learned_values_prefers_highest_frequency():
    applier = JobApplier()
    user = UserProfile(full_name="Candidate", email="candidate@example.com")
    user.application_answers = {
        "__learning": {
            "field_success": {
                "hear_about_us": {"Social Media": 2, "LinkedIn": 5},
                "phone_type": {"mobile": 10, "landline": 1},
            }
        }
    }
    best = applier._best_learned_values(user)
    assert best["hear_about_us"] == "LinkedIn"
    assert best["phone_type"] == "mobile"


@pytest.mark.asyncio
async def test_resume_tailoring_disabled_uses_original_resume_file():
    class DummyDB:
        def commit(self):
            return None

    applier = JobApplier()
    app = Application(job_id=1)
    resume = Resume(id=1, name="CV", file_path="/tmp/cv.pdf", file_type="pdf")
    original = settings.resume_tailoring_enabled
    settings.resume_tailoring_enabled = False
    try:
        output = await applier._tailor_resume_for_job(resume, job=None, app=app, db=DummyDB())
        assert output == "/tmp/cv.pdf"
        assert "Resume tailoring disabled" in (app.automation_log or "")
    finally:
        settings.resume_tailoring_enabled = original
