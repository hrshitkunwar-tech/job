"""Test LinkedIn HTML parsing logic with mock data."""

from job_search.services.scraper import _parse_job_cards_from_html, _strip_tags


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
