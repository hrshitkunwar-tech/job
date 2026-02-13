"""Test HTML parsing logic for LinkedIn and free job API mappers with mock data."""

from job_search.services.scraper import (
    _parse_job_cards_from_html,
    _strip_tags,
    WebJobScraper,
)


# Mock HTML that mimics LinkedIn's public search results page
MOCK_LINKEDIN_HTML = """
<html><body>
<ul class="jobs-search__results-list">
  <li>
    <div class="base-card relative w-full base-card--link base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:1111111111">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1111111111?tracking=abc">
        <span class="sr-only">Customer Success Manager</span>
      </a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title">Customer Success Manager</h3>
        <h4 class="base-search-card__subtitle">
          <a href="/company/acme">Acme Corp</a>
        </h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location">Bangalore, India</span>
        </div>
      </div>
    </div>
  </li>
  <li>
    <div class="base-card relative w-full base-card--link base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:2222222222">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/2222222222">
        <span class="sr-only">Senior CSM</span>
      </a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title">Senior CSM</h3>
        <h4 class="base-search-card__subtitle">
          <a href="/company/globex">Globex Inc</a>
        </h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location">Mumbai, India</span>
        </div>
      </div>
    </div>
  </li>
  <li>
    <div class="base-card relative w-full base-card--link base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:3333333333">
      <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/3333333333?refId=xyz">
        <span class="sr-only">Account Manager</span>
      </a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title">Account Manager</h3>
        <h4 class="base-search-card__subtitle">
          <a href="/company/initech">Initech</a>
        </h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location">Remote</span>
        </div>
      </div>
    </div>
  </li>
</ul>
</body></html>
"""


def test_parse_job_cards_extracts_all_jobs():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    assert len(jobs) == 3


def test_parse_job_cards_extracts_title():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    assert jobs[0]["title"] == "Customer Success Manager"
    assert jobs[1]["title"] == "Senior CSM"
    assert jobs[2]["title"] == "Account Manager"


def test_parse_job_cards_extracts_company():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    assert jobs[0]["company"] == "Acme Corp"
    assert jobs[1]["company"] == "Globex Inc"
    assert jobs[2]["company"] == "Initech"


def test_parse_job_cards_extracts_location():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    assert jobs[0]["location"] == "Bangalore, India"
    assert jobs[2]["location"] == "Remote"


def test_parse_job_cards_extracts_url_and_cleans_tracking():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    # Tracking params should be stripped
    assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/1111111111"
    assert "tracking" not in jobs[0]["url"]
    assert jobs[2]["url"] == "https://in.linkedin.com/jobs/view/3333333333"
    assert "refId" not in jobs[2]["url"]


def test_parse_job_cards_extracts_external_id():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=10)
    assert jobs[0]["external_id"] == "1111111111"
    assert jobs[1]["external_id"] == "2222222222"


def test_parse_job_cards_respects_limit():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=2)
    assert len(jobs) == 2


def test_parse_job_cards_sets_source():
    jobs = _parse_job_cards_from_html(MOCK_LINKEDIN_HTML, limit=1)
    assert jobs[0]["source"] == "linkedin"


def test_parse_empty_html():
    jobs = _parse_job_cards_from_html("<html><body>No jobs here</body></html>", limit=10)
    assert jobs == []


def test_strip_tags():
    assert _strip_tags("<b>Hello</b> <i>World</i>") == "Hello World"
    assert _strip_tags("Plain text") == "Plain text"
    assert _strip_tags("&amp; &lt;test&gt;") == "& <test>"


# ── Remotive API mapping tests ──────────────────────────────────────────

MOCK_REMOTIVE_JOB = {
    "id": 99999,
    "title": "Customer Success Lead",
    "company_name": "TechCo",
    "url": "https://remotive.com/remote-jobs/customer-support/customer-success-lead-99999",
    "candidate_required_location": "Worldwide",
    "description": "<p>We are looking for a <b>CSM</b> to join our team.</p>",
    "tags": ["customer-success"],
}


def test_remotive_mapping_title():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert job["title"] == "Customer Success Lead"


def test_remotive_mapping_company():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert job["company"] == "TechCo"


def test_remotive_mapping_source():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert job["source"] == "remotive"


def test_remotive_mapping_description_strips_html():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert "<p>" not in job["description"]
    assert "<b>" not in job["description"]
    assert "CSM" in job["description"]


def test_remotive_mapping_preserves_description_html():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert "<p>" in job["description_html"]


def test_remotive_mapping_external_id():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert job["external_id"] == "remotive-99999"


def test_remotive_mapping_work_type_is_remote():
    job = WebJobScraper._map_remotive_job(MOCK_REMOTIVE_JOB)
    assert job["work_type"] == "remote"


# ── Arbeitnow API mapping tests ───────────────────────────────────────

MOCK_ARBEITNOW_JOB = {
    "slug": "account-manager-acme-123",
    "title": "Account Manager",
    "company_name": "Acme Corp",
    "url": "https://www.arbeitnow.com/view/account-manager-acme-123",
    "location": "Remote, Europe",
    "remote": True,
    "description": "<p>Manage key accounts and drive <b>revenue growth</b>.</p>",
}


def test_arbeitnow_mapping_title():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert job["title"] == "Account Manager"


def test_arbeitnow_mapping_company():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert job["company"] == "Acme Corp"


def test_arbeitnow_mapping_source():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert job["source"] == "arbeitnow"


def test_arbeitnow_mapping_external_id():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert job["external_id"] == "arbeitnow-account-manager-acme-123"


def test_arbeitnow_mapping_remote_detection():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert job["work_type"] == "remote"


def test_arbeitnow_mapping_strips_html():
    job = WebJobScraper._map_arbeitnow_job(MOCK_ARBEITNOW_JOB)
    assert "<p>" not in job["description"]
    assert "revenue growth" in job["description"]


# ── RemoteOK API mapping tests ───────────────────────────────────────

MOCK_REMOTEOK_JOB = {
    "id": 55555,
    "position": "Senior Account Manager",
    "company": "GlobalTech",
    "url": "https://remoteok.com/remote-jobs/55555",
    "apply_url": "https://globaltech.com/apply/55555",
    "location": "Worldwide",
    "description": "<p>Looking for an experienced <b>account manager</b>.</p>",
}


def test_remoteok_mapping_title():
    job = WebJobScraper._map_remoteok_job(MOCK_REMOTEOK_JOB)
    assert job["title"] == "Senior Account Manager"


def test_remoteok_mapping_company():
    job = WebJobScraper._map_remoteok_job(MOCK_REMOTEOK_JOB)
    assert job["company"] == "GlobalTech"


def test_remoteok_mapping_source():
    job = WebJobScraper._map_remoteok_job(MOCK_REMOTEOK_JOB)
    assert job["source"] == "remoteok"


def test_remoteok_mapping_external_id():
    job = WebJobScraper._map_remoteok_job(MOCK_REMOTEOK_JOB)
    assert job["external_id"] == "remoteok-55555"


def test_remoteok_mapping_apply_url():
    job = WebJobScraper._map_remoteok_job(MOCK_REMOTEOK_JOB)
    assert job["apply_url"] == "https://globaltech.com/apply/55555"


# ── Himalayas API mapping tests ──────────────────────────────────────

MOCK_HIMALAYAS_JOB = {
    "id": "h-77777",
    "title": "Account Manager",
    "companyName": "StartupXYZ",
    "applicationLink": "https://himalayas.app/jobs/h-77777/apply",
    "location": "Remote",
    "description": "We need an account manager to handle enterprise clients.",
}


def test_himalayas_mapping_title():
    job = WebJobScraper._map_himalayas_job(MOCK_HIMALAYAS_JOB)
    assert job["title"] == "Account Manager"


def test_himalayas_mapping_company():
    job = WebJobScraper._map_himalayas_job(MOCK_HIMALAYAS_JOB)
    assert job["company"] == "StartupXYZ"


def test_himalayas_mapping_source():
    job = WebJobScraper._map_himalayas_job(MOCK_HIMALAYAS_JOB)
    assert job["source"] == "himalayas"


def test_himalayas_mapping_external_id():
    job = WebJobScraper._map_himalayas_job(MOCK_HIMALAYAS_JOB)
    assert job["external_id"] == "himalayas-h-77777"


def test_himalayas_mapping_apply_url():
    job = WebJobScraper._map_himalayas_job(MOCK_HIMALAYAS_JOB)
    assert job["apply_url"] == "https://himalayas.app/jobs/h-77777/apply"


# ── Deduplication tests ──────────────────────────────────────────────

def test_deduplicate_removes_exact_duplicates():
    jobs = [
        {"title": "Account Manager", "company": "Acme", "description": "Short"},
        {"title": "Account Manager", "company": "Acme", "description": "Much longer description here"},
    ]
    result = WebJobScraper._deduplicate(jobs)
    assert len(result) == 1
    assert result[0]["description"] == "Much longer description here"


def test_deduplicate_keeps_different_jobs():
    jobs = [
        {"title": "Account Manager", "company": "Acme", "description": "x"},
        {"title": "Account Manager", "company": "Globex", "description": "y"},
    ]
    result = WebJobScraper._deduplicate(jobs)
    assert len(result) == 2


def test_deduplicate_case_insensitive():
    jobs = [
        {"title": "Account Manager", "company": "ACME CORP", "description": "a"},
        {"title": "account manager", "company": "Acme Corp", "description": "abc"},
    ]
    result = WebJobScraper._deduplicate(jobs)
    assert len(result) == 1
