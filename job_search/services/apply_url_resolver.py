from __future__ import annotations

import re
import time
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

BOARD_DOMAINS = {
    "remotive.com",
    "remoteok.com",
    "arbeitnow.com",
    "himalayas.app",
}

TRUSTED_AUTOMATION_SOURCES = {"linkedin", "greenhouse", "lever"}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies to continue",
    "cf-chl",
    "challenge-platform",
)

_RESOLUTION_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 900


def _domain(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_board_domain(value: str) -> bool:
    host = _domain(value)
    return any(host == d or host.endswith(f".{d}") for d in BOARD_DOMAINS)


def is_official_submission_target(url: str, source: str) -> bool:
    src = (source or "").lower()
    if src in TRUSTED_AUTOMATION_SOURCES:
        return bool(_domain(url))
    return bool(_domain(url)) and not is_board_domain(url)


def _is_candidate_apply_anchor(text: str, href: str) -> bool:
    text_l = (text or "").lower()
    href_l = (href or "").lower()
    if not href_l or href_l.startswith("#") or href_l.startswith("javascript:"):
        return False
    tokens = (
        "apply",
        "application",
        "careers",
        "job",
        "position",
        "opening",
        "greenhouse",
        "lever",
        "workday",
        "icims",
        "smartrecruiters",
        "ashbyhq",
    )
    return any(tok in text_l or tok in href_l for tok in tokens)


def extract_external_apply_links(html: str, base_url: str) -> list[str]:
    links: list[tuple[int, str]] = []
    seen: set[str] = set()

    # Anchor extraction with inner text scoring.
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in anchor_re.finditer(html or ""):
        href = unescape((m.group(1) or "").strip())
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith(("http://", "https://")):
            continue
        if is_board_domain(abs_url):
            continue
        if abs_url in seen:
            continue
        text = re.sub(r"<[^>]+>", " ", m.group(2) or "")
        text = re.sub(r"\s+", " ", unescape(text)).strip()
        if not _is_candidate_apply_anchor(text, abs_url):
            continue
        score = 1
        lowered = f"{text} {abs_url}".lower()
        if "apply" in lowered:
            score += 3
        if any(k in lowered for k in ("greenhouse", "lever", "workday", "icims", "ashbyhq", "smartrecruiters")):
            score += 3
        if any(k in lowered for k in ("careers", "jobs", "position", "opening")):
            score += 1
        links.append((score, abs_url))
        seen.add(abs_url)

    links.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in links]


async def resolve_official_apply_url(url: str, source: str) -> dict:
    """
    Resolve a usable official apply URL.

    Returns:
      {
        "resolved_url": <str|None>,
        "reason": <str>,
        "board_source": <bool>,
        "warnings": [..],
      }
    """
    target = (url or "").strip()
    src = (source or "").lower()
    if not target:
        return {
            "resolved_url": None,
            "reason": "missing_apply_url",
            "board_source": False,
            "warnings": ["Job has no apply URL."],
        }

    cache_key = f"{src}|{target}"
    now = time.time()
    cached = _RESOLUTION_CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return dict(cached[1])

    if src in TRUSTED_AUTOMATION_SOURCES:
        result = {
            "resolved_url": target,
            "reason": "trusted_source",
            "board_source": False,
            "warnings": [],
        }
        _RESOLUTION_CACHE[cache_key] = (now, dict(result))
        return result

    if not is_board_domain(target):
        result = {
            "resolved_url": target,
            "reason": "already_official",
            "board_source": False,
            "warnings": [],
        }
        _RESOLUTION_CACHE[cache_key] = (now, dict(result))
        return result

    warnings: list[str] = []
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers=HTTP_HEADERS,
        ) as client:
            resp = await client.get(target)
            final_url = str(resp.url)

            # Common case: board redirects straight to ATS/company site.
            if final_url and not is_board_domain(final_url):
                result = {
                    "resolved_url": final_url,
                    "reason": "redirected_to_official",
                    "board_source": True,
                    "warnings": warnings,
                }
                _RESOLUTION_CACHE[cache_key] = (time.time(), dict(result))
                return result

            text = (resp.text or "")[:1_500_000]
            lower_text = text.lower()
            if any(marker in lower_text for marker in _CHALLENGE_MARKERS):
                warnings.append("Board page blocked by anti-bot challenge.")
                result = {
                    "resolved_url": None,
                    "reason": "board_challenge_blocked",
                    "board_source": True,
                    "warnings": warnings,
                }
                _RESOLUTION_CACHE[cache_key] = (time.time(), dict(result))
                return result

            candidates = extract_external_apply_links(text, final_url or target)
            if candidates:
                result = {
                    "resolved_url": candidates[0],
                    "reason": "extracted_external_apply_link",
                    "board_source": True,
                    "warnings": warnings,
                }
                _RESOLUTION_CACHE[cache_key] = (time.time(), dict(result))
                return result

            result = {
                "resolved_url": None,
                "reason": "no_external_apply_link_found",
                "board_source": True,
                "warnings": warnings,
            }
            _RESOLUTION_CACHE[cache_key] = (time.time(), dict(result))
            return result
    except Exception as e:
        warnings.append(f"URL resolution failed: {e}")
        result = {
            "resolved_url": None,
            "reason": "resolution_error",
            "board_source": True,
            "warnings": warnings,
        }
        _RESOLUTION_CACHE[cache_key] = (time.time(), dict(result))
        return result
