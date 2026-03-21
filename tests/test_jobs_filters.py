from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from job_search.app import create_app
from job_search.routes.dashboard import _search_time_bounds


def test_search_time_bounds_today():
    now = datetime(2026, 2, 16, 15, 30, 0)
    start, end = _search_time_bounds("today", now=now)
    assert start == datetime(2026, 2, 16, 0, 0, 0)
    assert end is None


def test_search_time_bounds_yesterday():
    now = datetime(2026, 2, 16, 10, 0, 0)
    start, end = _search_time_bounds("yesterday", now=now)
    assert start == datetime(2026, 2, 15, 0, 0, 0)
    assert end == datetime(2026, 2, 16, 0, 0, 0)


def test_search_time_bounds_last_week():
    now = datetime(2026, 2, 16, 10, 0, 0)
    start, end = _search_time_bounds("last_week", now=now)
    assert start == now - timedelta(days=7)
    assert end is None


def test_jobs_page_exposes_new_search_time_filter():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/jobs?show_all=true")
    assert resp.status_code == 200
    assert "Search Time" in resp.text
    assert 'option value="today"' in resp.text
    assert 'option value="yesterday"' in resp.text
    assert 'option value="last_week"' in resp.text


def test_jobs_page_marks_server_side_filter_selection():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/jobs?show_all=true&work_type=remote&app_status=submitted&search_time=today")
    assert resp.status_code == 200
    assert 'option value="remote" selected' in resp.text
    assert 'option value="submitted" selected' in resp.text
    assert 'option value="today" selected' in resp.text


def test_jobs_page_has_kanban_and_delete_controls():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/jobs?show_all=true")
    assert resp.status_code == 200
    assert "Kanban" in resp.text
    assert "Delete Selected" in resp.text
    assert 'id="view-kanban-btn"' in resp.text
    assert 'id="batch-delete-btn"' in resp.text


def test_bulk_delete_endpoint_handles_missing_ids_gracefully():
    app = create_app()
    client = TestClient(app)
    resp = client.post("/api/jobs/bulk-delete", json={"job_ids": [999999]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["deleted"] == 0
    assert payload["deleted_ids"] == []
