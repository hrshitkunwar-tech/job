import pytest

from job_search.services.apply_url_resolver import (
    extract_external_apply_links,
    is_board_domain,
    is_official_submission_target,
    resolve_official_apply_url,
)


def test_is_board_domain_recognizes_known_boards():
    assert is_board_domain("https://himalayas.app/jobs/123")
    assert is_board_domain("https://www.remoteok.com/remote-jobs/abc")
    assert not is_board_domain("https://careers.acme.com/jobs/1")


def test_is_official_submission_target_respects_source_and_domain():
    assert is_official_submission_target("https://www.linkedin.com/jobs/view/1", "linkedin")
    assert is_official_submission_target("https://careers.acme.com/jobs/1", "himalayas")
    assert not is_official_submission_target("https://himalayas.app/jobs/1", "himalayas")


def test_extract_external_apply_links_prefers_apply_like_links():
    html = """
    <html>
      <body>
        <a href="https://example.com/about">About</a>
        <a href="https://boards.greenhouse.io/acme/jobs/123">Apply on Greenhouse</a>
        <a href="https://careers.acme.com/jobs/123">Careers</a>
      </body>
    </html>
    """
    links = extract_external_apply_links(html, "https://himalayas.app/jobs/123")
    assert links
    assert links[0].startswith("https://boards.greenhouse.io")


@pytest.mark.asyncio
async def test_resolve_official_apply_url_returns_existing_official_without_network():
    result = await resolve_official_apply_url("https://careers.acme.com/jobs/1", "himalayas")
    assert result["resolved_url"] == "https://careers.acme.com/jobs/1"
    assert result["reason"] == "already_official"
