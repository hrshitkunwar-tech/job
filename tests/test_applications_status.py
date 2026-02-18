from job_search.models import ApplicationStatus
from job_search.routes.dashboard import _normalize_application_status


def test_normalize_application_status_from_enum():
    assert _normalize_application_status(ApplicationStatus.QUEUED) == "queued"
    assert _normalize_application_status(ApplicationStatus.IN_PROGRESS) == "in_progress"


def test_normalize_application_status_from_raw_strings():
    assert _normalize_application_status("QUEUED") == "queued"
    assert _normalize_application_status("in progress") == "in_progress"
    assert _normalize_application_status("ApplicationStatus.SUBMITTED") == "submitted"
