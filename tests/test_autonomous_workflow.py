from job_search.models import Job
from job_search.services.applier import JobApplier
from job_search.models import Application
from job_search.services.workflow_agents import CoordinatorAgent, TrackerAgent


def _job(source: str, apply_url: str = "", url: str = ""):
    j = Job(
        external_id=f"x-{source}",
        source=source,
        title="Role",
        company="Acme",
        description="desc",
        url=url or apply_url or "https://example.com/apply",
        apply_url=apply_url,
    )
    return j


def test_source_mode_generic_for_web_sources():
    applier = JobApplier()
    assert applier.source_mode(_job("himalayas")) == "generic"
    assert applier.source_mode(_job("remotive")) == "generic"


def test_source_mode_infers_linkedin_from_url():
    applier = JobApplier()
    assert applier.source_mode(
        _job("web", url="https://in.linkedin.com/jobs/view/customer-success-manager-at-x-123")
    ) == "linkedin"


def test_official_submission_guard_blocks_board_domains():
    coordinator = CoordinatorAgent()
    assert coordinator._is_official_submission_target(
        _job("himalayas", apply_url="https://himalayas.app/jobs/123/apply")
    ) is False


def test_official_submission_guard_allows_official_portal_from_board_source():
    coordinator = CoordinatorAgent()
    assert coordinator._is_official_submission_target(
        _job("himalayas", apply_url="https://careers.acme.com/jobs/123")
    ) is True


def test_tracker_confirmation_includes_submission_audit():
    tracker = TrackerAgent()
    app = Application(job_id=1)
    app.user_inputs = {"__submission_audit": {"job_title": "Role", "final_submission_confirmed": True}}
    payload = tracker.record_confirmation(app)
    assert payload["submission_audit"]["job_title"] == "Role"
