from __future__ import annotations
import asyncio
import logging
import random
import re
import time
import os
from html import unescape
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
import urllib.parse
import hashlib

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext

from job_search.config import settings

logger = logging.getLogger(__name__)

# Stealth script to avoid bot detection
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""

# Headers that mimic a real browser for HTTP requests
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def _strip_tags(html: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_job_cards_from_html(html: str, limit: int) -> List[Dict[str, Any]]:
    """
    Parse LinkedIn public search HTML and extract job listings.
    LinkedIn's public pages contain .base-card elements with structured job data.
    """
    jobs = []

    # Pattern 1: Match individual job card blocks by finding job view links
    # LinkedIn public pages embed cards like:
    #   <a ... href="https://www.linkedin.com/jobs/view/NNNN...">
    # near <h3 class="base-search-card__title">Title</h3>
    # and  <h4 class="base-search-card__subtitle">Company</h4>
    # and  <span class="job-search-card__location">Location</span>

    # Split HTML into chunks around each job card
    card_splits = re.split(r'(?=<div[^>]*class="[^"]*base-card[^"]*base-search-card[^"]*")', html)

    for chunk in card_splits[1:]:  # Skip first chunk (before first card)
        if len(jobs) >= limit:
            break
        if re.search(r"(?i)\bpromoted\b", chunk):
            continue

        # Extract URL
        url_match = re.search(
            r'href="(https?://[^"]*linkedin\.com/jobs/view/[^"]*)"',
            chunk
        )
        if not url_match:
            continue
        raw_url = url_match.group(1)
        # Clean tracking params
        url = raw_url.split("?")[0]

        # Extract title
        title_match = re.search(
            r'class="base-search-card__title[^"]*"[^>]*>(.*?)</(?:h3|span|a)',
            chunk, re.DOTALL
        )
        title = _strip_tags(title_match.group(1)) if title_match else ""
        title = re.sub(r"\s+with verification.*$", "", title, flags=re.IGNORECASE).strip()

        # Extract company
        company_match = re.search(
            r'class="base-search-card__subtitle[^"]*"[^>]*>(.*?)</(?:h4|span|a)',
            chunk, re.DOTALL
        )
        company = _strip_tags(company_match.group(1)) if company_match else "Unknown"

        # Extract location
        location_match = re.search(
            r'class="job-search-card__location[^"]*"[^>]*>(.*?)</(?:span|div)',
            chunk, re.DOTALL
        )
        location = _strip_tags(location_match.group(1)) if location_match else ""

        if not title or not url:
            continue

        # Extract external ID from URL
        parts = url.strip("/").split("/")
        external_id = parts[-1] if parts else "unknown"

        jobs.append({
            "external_id": external_id,
            "title": title,
            "company": company,
            "url": url,
            "location": location,
            "source": "linkedin",
        })

    # Fallback: if card-based parsing failed, try finding any /jobs/view/ links
    if not jobs:
        link_pattern = re.compile(
            r'href="(https?://[^"]*linkedin\.com/jobs/view/(\d+)[^"]*)"[^>]*>([^<]+)',
        )
        seen = set()
        for match in link_pattern.finditer(html):
            if len(jobs) >= limit:
                break
            raw_url = match.group(1).split("?")[0]
            external_id = match.group(2)
            title_text = _strip_tags(match.group(3))
            title_text = re.sub(r"\s+with verification.*$", "", title_text, flags=re.IGNORECASE).strip()
            if re.search(r"(?i)\bpromoted\b", title_text):
                continue
            if external_id in seen or not title_text or len(title_text) < 3:
                continue
            seen.add(external_id)
            jobs.append({
                "external_id": external_id,
                "title": title_text,
                "company": "Unknown",
                "url": raw_url,
                "location": "",
                "source": "linkedin",
            })

    return jobs


class LinkedInScraper:
    GENERIC_ROLE_TOKENS = {
        "senior", "junior", "lead", "principal", "staff", "associate",
        "manager", "executive", "specialist", "intern", "consultant",
        "developer", "engineer", "stack", "full", "part", "time", "remote",
        "role", "job", "position",
    }

    QUERY_ALIASES = {
        "mern": {"mern", "fullstack", "full stack", "react", "node", "mongodb", "express"},
        "fullstack": {"fullstack", "full stack", "software engineer", "react", "node", "frontend", "backend"},
        "full stack": {"fullstack", "full stack", "software engineer", "react", "node", "frontend", "backend"},
        "data engineer": {"data engineer", "etl", "pipeline", "data platform", "big data"},
        "customer success": {"customer success", "csm", "account manager", "client success", "success manager"},
    }

    def __init__(self):
        self.headless = settings.browser_headless
        self.email = settings.linkedin_email
        self.password = settings.linkedin_password
        self.base_url = "https://www.linkedin.com"
        self.storage_state = Path("data/browser_state/linkedin.json")

    async def _get_random_delay(self, min_s: float = None, max_s: float = None):
        if min_s is None: min_s = settings.scrape_delay_min
        if max_s is None: max_s = settings.scrape_delay_max
        await asyncio.sleep(random.uniform(min_s, max_s))

    def _build_filter_params(self, filters: dict) -> list:
        """Build LinkedIn URL filter parameters."""
        filter_params = []
        if not filters:
            return filter_params

        # Date Posted
        date_map = {"past_24h": "r86400", "past_week": "r604800", "past_month": "r2592000"}
        if filters.get("date_posted") in date_map:
            filter_params.append(f"f_TPR={date_map[filters['date_posted']]}")

        # Experience Levels (f_E)
        exp_map = {"internship": "1", "entry": "2", "associate": "3", "mid-senior": "4", "director": "5", "executive": "6"}
        exp_filters = filters.get("experience_levels") or []
        exp_codes = [exp_map[e] for e in exp_filters if e in exp_map]
        if exp_codes:
            filter_params.append(f"f_E={urllib.parse.quote(','.join(exp_codes))}")

        # Work Types (f_WT)
        wt_map = {"onsite": "1", "remote": "2", "hybrid": "3"}
        wt_filters = filters.get("work_types") or []
        wt_codes = [wt_map[w] for w in wt_filters if w in wt_map]
        if wt_codes:
            filter_params.append(f"f_WT={urllib.parse.quote(','.join(wt_codes))}")

        # Easy Apply (f_AL)
        if filters.get("easy_apply_only"):
            filter_params.append("f_AL=true")

        return filter_params

    def _build_search_url(self, query: str, location: str, filter_params: list) -> str:
        """Build LinkedIn job search URL."""
        safe_query = urllib.parse.quote(query)
        url = f"{self.base_url}/jobs/search/?keywords={safe_query}"
        if location:
            url += f"&location={urllib.parse.quote(location)}"
        if filter_params:
            url += "&" + "&".join(filter_params)
        return url

    @staticmethod
    def _sanitize_job_title(title: str) -> str:
        text = (title or "").strip()
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s+with verification.*$", "", text, flags=re.IGNORECASE).strip()
        return text

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        text = (text or "").lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _query_core_terms(self, query_norm: str) -> set[str]:
        role_parts = [p.strip() for p in re.split(r",|/|\||\band\b", query_norm) if p.strip()]
        core_tokens: set[str] = set()
        for part in role_parts:
            part_tokens = [t for t in part.split() if len(t) > 2]
            filtered = [t for t in part_tokens if t not in self.GENERIC_ROLE_TOKENS]
            if filtered:
                core_tokens.update(filtered)
            elif part_tokens:
                core_tokens.add(part_tokens[0])
        return core_tokens

    def _query_alias_terms(self, query_norm: str) -> set[str]:
        aliases: set[str] = set()
        for phrase, extras in self.QUERY_ALIASES.items():
            if phrase in query_norm:
                aliases.update(extras)
        return aliases

    def _linkedin_relevance_score(self, job: Dict[str, Any], query: str) -> float:
        title_raw = self._sanitize_job_title(job.get("title", ""))
        title = self._normalize_for_match(title_raw)
        company = self._normalize_for_match(job.get("company", ""))
        description = self._normalize_for_match(job.get("description", ""))
        haystack = f"{title} {company} {description}"
        query_norm = self._normalize_for_match(query)
        query_terms = [t for t in query_norm.split() if len(t) > 2]
        if not query_terms:
            return 0.0

        core_tokens = self._query_core_terms(query_norm)
        alias_terms = self._query_alias_terms(query_norm)

        technical_markers = (
            "developer",
            "engineer",
            "fullstack",
            "full stack",
            "frontend",
            "front end",
            "backend",
            "back end",
            "mern",
            "react",
            "node",
            "software",
        )
        is_technical_query = (
            any(marker in query_norm for marker in technical_markers)
            or bool({"mern", "fullstack", "full stack"} & alias_terms)
        )
        if is_technical_query and not (
            any(marker in title for marker in technical_markers)
            or any(term in title for term in alias_terms)
        ):
            return 0.0

        core_or_alias = core_tokens | alias_terms
        core_in_title = any(tok in title for tok in core_or_alias) if core_or_alias else False
        core_in_haystack = any(tok in haystack for tok in core_or_alias) if core_or_alias else False
        if core_or_alias and not core_in_title:
            if not (
                is_technical_query
                and core_in_haystack
                and (any(marker in title for marker in technical_markers) or any(term in title for term in alias_terms))
            ):
                return 0.0

        if company in ("", "unknown") and not core_in_title:
            return 0.0

        title_matches = sum(1 for term in query_terms if term in title)
        body_matches = sum(1 for term in query_terms if term in haystack and term not in title)
        score = (title_matches * 2.5) + (body_matches * 0.5)

        if query_norm and query_norm in title:
            score += 5.0
        elif query_norm and query_norm in haystack:
            score += 2.0

        alias_hits_in_title = sum(1 for term in alias_terms if term in title)
        if alias_hits_in_title:
            score += min(4.0, alias_hits_in_title * 1.2)

        if is_technical_query and title_matches == 0 and alias_hits_in_title == 0:
            score -= 4.0
        return score

    def _filter_relevant_jobs(self, jobs: List[Dict[str, Any]], query: str, limit: int) -> List[Dict[str, Any]]:
        query_norm = self._normalize_for_match(query)
        is_technical_query = any(
            marker in query_norm
            for marker in ("developer", "engineer", "stack", "mern", "frontend", "backend", "fullstack")
        )
        min_score = 3.5 if is_technical_query else 2.0

        ranked: List[tuple[float, Dict[str, Any]]] = []
        for job in jobs:
            score = self._linkedin_relevance_score(job, query)
            if score < min_score:
                continue
            ranked.append((score, job))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [row[1] for row in ranked[:limit]]

    async def scrape_jobs(self, query: str, location: str = "United States", limit: int = 10, filters: dict = None, check_cancelled: Callable[[], bool] = None) -> List[Dict[str, Any]]:
        """Main entry point to scrape jobs."""
        filter_params = self._build_filter_params(filters)
        has_credentials = bool(self.email and self.password)
        has_saved_session = self.storage_state.exists()

        # Strategy:
        # 1. If we have credentials or a saved session, try browser-based scraping
        # 2. Otherwise, use HTTP-based public scraping (no browser needed)
        # 3. If browser scraping finds nothing, fall back to HTTP

        if has_credentials or has_saved_session:
            jobs = await self._scrape_with_browser(query, location, limit, filter_params, check_cancelled)
            if jobs:
                filtered = self._filter_relevant_jobs(jobs, query, limit)
                # Keep browser path when quality is good; otherwise recover via HTTP.
                min_needed = max(2, min(limit, 5))
                if len(filtered) >= min_needed:
                    return filtered
                logger.warning(
                    f"Browser scraping yielded low relevance ({len(filtered)}/{len(jobs)}). "
                    "Falling back to HTTP public scraping..."
                )
            else:
                logger.warning("Browser-based scraping found 0 jobs. Falling back to HTTP public scraping...")

        # HTTP-based public scraping — works without login, no browser bot detection
        jobs = await self._scrape_public_http(query, location, limit, filter_params)

        if not jobs:
            logger.error(
                f"No jobs found for '{query}' in '{location}' after all methods. "
                "LinkedIn may be rate-limiting or the query returned no results."
            )
            return []

        # Fetch full descriptions via HTTP for each job
        detailed_jobs = await self._fetch_details_http(jobs, limit)
        return self._filter_relevant_jobs(detailed_jobs, query, limit)

    async def _scrape_public_http(self, query: str, location: str, limit: int, filter_params: list) -> List[Dict[str, Any]]:
        """
        Scrape LinkedIn's public job search page via plain HTTP requests.
        LinkedIn serves SEO-friendly HTML that doesn't require authentication
        or a real browser. This is the most reliable method for guest access.
        """
        search_url = self._build_search_url(query, location, filter_params)
        logger.info(f"HTTP public scraping: {search_url}")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers=HTTP_HEADERS,
            ) as client:
                # First, visit LinkedIn homepage to establish session cookies
                # LinkedIn returns 403 without proper cookies
                try:
                    await client.get("https://www.linkedin.com/", headers=HTTP_HEADERS)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass  # Not critical if this fails

                resp = await client.get(search_url)
                logger.info(f"HTTP response: status={resp.status_code}, url={resp.url}, length={len(resp.text)}")

                if resp.status_code == 403:
                    logger.warning("LinkedIn returned 403. Retrying with alternate headers...")
                    # Try with minimal headers (some WAFs block Sec-Fetch headers)
                    alt_headers = {
                        "User-Agent": HTTP_HEADERS["User-Agent"],
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    resp = await client.get(search_url, headers=alt_headers)
                    logger.info(f"Retry response: status={resp.status_code}")

                if resp.status_code != 200:
                    logger.error(f"LinkedIn returned HTTP {resp.status_code}")
                    return []

                html = resp.text

                # Check if we got a real search results page
                if "base-search-card" not in html and "jobs/view" not in html:
                    # Might be an auth wall or error page
                    title_match = re.search(r'<title>(.*?)</title>', html)
                    page_title = _strip_tags(title_match.group(1)) if title_match else "unknown"
                    logger.warning(f"Page doesn't contain job cards. Title: '{page_title}'")

                    # Try fetching more results pages
                    for start in [25, 50]:
                        paginated_url = search_url + f"&start={start}"
                        resp2 = await client.get(paginated_url)
                        if resp2.status_code == 200 and "base-search-card" in resp2.text:
                            html = resp2.text
                            break
                    else:
                        return []

                jobs = _parse_job_cards_from_html(html, limit)
                logger.info(f"HTTP scraping found {len(jobs)} jobs")
                return jobs

        except httpx.TimeoutException:
            logger.error("HTTP request to LinkedIn timed out")
            return []
        except Exception as e:
            logger.error(f"HTTP scraping failed: {e}")
            return []

    async def _fetch_details_http(self, jobs: List[Dict], limit: int) -> List[Dict[str, Any]]:
        """Fetch full job descriptions via HTTP for each job."""
        detailed = []

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers=HTTP_HEADERS,
        ) as client:
            for job in jobs[:limit]:
                try:
                    await self._get_random_delay(0.5, 1.5)
                    resp = await client.get(job["url"])
                    if resp.status_code != 200:
                        logger.warning(f"Failed to fetch {job['url']}: HTTP {resp.status_code}")
                        continue

                    html = resp.text

                    # Extract description
                    desc_match = re.search(
                        r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
                        html, re.DOTALL
                    )
                    if not desc_match:
                        desc_match = re.search(
                            r'class="description__text[^"]*"[^>]*>(.*?)</section>',
                            html, re.DOTALL
                        )
                    if not desc_match:
                        # Broader fallback
                        desc_match = re.search(
                            r'class="[^"]*description[^"]*"[^>]*>(.*?)</(?:section|div>)',
                            html, re.DOTALL
                        )

                    description = _strip_tags(desc_match.group(1)) if desc_match else ""
                    description_html = desc_match.group(1).strip() if desc_match else ""

                    if not description or len(description) < 50:
                        logger.warning(f"Short/empty description for {job['url']}")
                        # Still include the job but with whatever we have
                        if not description:
                            description = f"{job['title']} at {job['company']}"

                    # Extract location if not already present
                    if not job.get("location"):
                        loc_match = re.search(
                            r'class="topcard__flavor--bullet"[^>]*>(.*?)</span>',
                            html, re.DOTALL
                        )
                        if loc_match:
                            job["location"] = _strip_tags(loc_match.group(1))

                    # Detect Easy Apply
                    is_easy_apply = "Easy Apply" in html

                    # Detect work type
                    work_type = "onsite"
                    if re.search(r'(?i)remote', html[:3000]):
                        work_type = "remote"
                    elif re.search(r'(?i)hybrid', html[:3000]):
                        work_type = "hybrid"

                    job.update({
                        "description": description,
                        "description_html": description_html,
                        "work_type": work_type,
                        "is_easy_apply": is_easy_apply,
                        "apply_url": None,
                        "scraped_at": datetime.now(),
                    })
                    detailed.append(job)

                except Exception as e:
                    logger.error(f"Failed to fetch details for {job['url']}: {e}")

        logger.info(f"HTTP detail fetch: {len(detailed)}/{len(jobs)} jobs with descriptions")
        return detailed

    async def _scrape_with_browser(self, query: str, location: str, limit: int, filter_params: list, check_cancelled) -> List[Dict[str, Any]]:
        """Browser-based scraping for authenticated users with saved sessions."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)

            user_agents = [
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            ]
            context_args = {
                "user_agent": random.choice(user_agents),
                "viewport": {"width": 1280, "height": 800}
            }
            if self.storage_state.exists():
                context_args["storage_state"] = str(self.storage_state)

            context = await browser.new_context(**context_args)
            page = await context.new_page()
            await page.add_init_script(STEALTH_SCRIPT)

            try:
                # Check if logged in
                await page.goto(self.base_url, timeout=30000)
                await self._get_random_delay(1, 2)
                logged_in = await self._is_logged_in(page)

                if not logged_in and self.email and self.password:
                    await self._login(page)
                    logged_in = await self._is_logged_in(page)

                if not logged_in:
                    logger.warning("Browser-based scraping: not logged in. Returning empty to trigger HTTP fallback.")
                    return []

                search_url = self._build_search_url(query, location, filter_params)
                logger.info(f"Browser scraping (authenticated): {search_url}")
                await page.goto(search_url, wait_until="load", timeout=60000)
                await self._get_random_delay(3, 5)

                current_url = page.url
                logger.info(f"Current Page URL: {current_url}")

                if "checkpoint" in current_url:
                    logger.error("Security Checkpoint encountered.")
                    if not self.headless:
                        logger.info("Waiting for manual CAPTCHA resolution (120s)...")
                        for _ in range(60):
                            if "checkpoint" not in page.url:
                                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                                await context.storage_state(path=str(self.storage_state))
                                break
                            await asyncio.sleep(2)
                    else:
                        return []

                if any(x in current_url for x in ["/login", "/authwall", "/signup"]):
                    logger.warning(f"Redirected to auth: {current_url}")
                    return []

                if "/search" not in current_url and "keywords" not in current_url:
                    await self._perform_on_page_search(page, query, location)
                    await self._get_random_delay(4, 6)

                jobs = await self._extract_job_list(page, limit)
                if not jobs:
                    return []

                # Fetch details with browser
                return await self._fetch_details_browser(context, jobs, limit, check_cancelled)

            finally:
                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await context.storage_state(path=str(self.storage_state))
                except Exception:
                    pass
                await browser.close()

    async def _fetch_details_browser(self, context: BrowserContext, jobs: list, limit: int, check_cancelled) -> List[Dict[str, Any]]:
        """Fetch full job details using browser context."""
        sem = asyncio.Semaphore(3)

        async def fetch_one(job):
            if check_cancelled and check_cancelled():
                return job
            async with sem:
                try:
                    await self._get_random_delay(0.5, 1.5)
                    detail = await self._get_job_details(context, job["url"])
                    if not detail.get("title"):
                        detail.pop("title", None)
                    if not detail.get("company"):
                        detail.pop("company", None)
                    job.update(detail)
                except Exception as e:
                    logger.error(f"Failed to get details for {job['url']}: {e}")
                return job

        tasks = [fetch_one(job) for job in jobs[:limit]]
        detailed = await asyncio.gather(*tasks)
        final = [j for j in detailed if j.get("description")]
        logger.info(f"Browser detail fetch: {len(final)} jobs with descriptions")
        return final

    async def _is_logged_in(self, page: Page) -> bool:
        url = page.url
        if any(x in url for x in ["/feed", "/mynetwork", "/messaging"]):
            return True
        nav = await page.query_selector(".global-nav__me, .nav-item--profile")
        return nav is not None

    async def _perform_on_page_search(self, page: Page, query: str, location: str):
        """Use the LinkedIn search bar directly on the page."""
        try:
            kw_selectors = [
                "input[aria-label='Search by title, skill, or company']",
                ".jobs-search-box__text-input[aria-label='Search by title, skill, or company']",
                "input[name='keywords']"
            ]
            loc_selectors = [
                "input[aria-label='City, state, or zip code']",
                ".jobs-search-box__text-input[aria-label='City, state, or zip code']",
                "input[name='location']"
            ]

            kw_found = False
            for selector in kw_selectors:
                if await page.query_selector(selector):
                    await page.click(selector, click_count=3)
                    await page.keyboard.press("Backspace")
                    await page.fill(selector, query)
                    kw_found = True
                    break

            if location:
                for selector in loc_selectors:
                    if await page.query_selector(selector):
                        await page.click(selector, click_count=3)
                        await page.keyboard.press("Backspace")
                        await page.fill(selector, location)
                        break

            if kw_found:
                await page.keyboard.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception as e:
            logger.warning(f"On-page search failed: {e}")

    async def _login(self, page: Page):
        logger.info("Attempting login...")
        try:
            await page.goto(f"{self.base_url}/login", timeout=60000)
            await page.fill("#username", self.email)
            await page.fill("#password", self.password)
            await page.click('button[type="submit"]')

            try:
                await page.wait_for_load_state("networkidle", timeout=60000)
            except Exception as e:
                logger.warning(f"Network idle timeout during login: {e}")
                try:
                    await page.wait_for_selector(".global-nav", timeout=10000)
                except Exception:
                    if "feed" in page.url or "jobs" in page.url:
                        logger.info("Login appears successful despite timeout")
                    else:
                        raise

            logger.info("Login successful")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            logger.warning("Continuing without login")
            return

        if "checkpoint" in page.url:
            logger.warning("Security checkpoint encountered.")
            await asyncio.sleep(30)

    async def _extract_job_list(self, page: Page, limit: int) -> List[Dict[str, Any]]:
        jobs = []

        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        selectors = [
            "ul.jobs-search-results__list > li",
            "ul.jobs-search__results-list > li",
            "li.jobs-search-results__list-item",
            ".scaffold-layout__list-item",
            ".job-card-container",
            "li.result-card",
            "div[data-job-id]",
            "li[data-occludable-job-id]",
        ]

        job_cards = []
        for selector in selectors:
            job_cards = await page.query_selector_all(selector)
            if job_cards:
                logger.info(f"Found {len(job_cards)} cards with: {selector}")
                break

        if not job_cards:
            all_links = await page.query_selector_all("a[href*='/jobs/view/']")
            if all_links:
                unique, seen = [], set()
                for link in all_links:
                    url = await link.get_attribute("href")
                    if url and "/jobs/view/" in url:
                        clean = url.split("?")[0]
                        if clean not in seen:
                            seen.add(clean)
                            unique.append(link)
                job_cards = unique

        for card in job_cards[:limit]:
            try:
                card_text = ((await card.inner_text()) or "").strip()
                if re.search(r"(?i)\bpromoted\b", card_text):
                    continue

                title_selectors = [".job-card-list__title", ".base-search-card__title", "h3.base-search-card__title", "h3", ".job-card-container__link"]
                company_selectors = [".job-card-container__company-name", ".base-search-card__subtitle", "h4.base-search-card__subtitle", "h4"]
                link_selectors = ["a.job-card-list__title", "a.base-card__full-link", "a.base-search-card__full-link", "a[href*='/jobs/view/']"]

                title_elem = None
                for s in title_selectors:
                    title_elem = await card.query_selector(s)
                    if title_elem: break

                company_elem = None
                for s in company_selectors:
                    company_elem = await card.query_selector(s)
                    if company_elem: break

                link_elem = None
                for s in link_selectors:
                    link_elem = await card.query_selector(s)
                    if link_elem: break

                if not link_elem:
                    if await card.evaluate("node => node.tagName === 'A'"):
                        link_elem = card
                        title = (await card.inner_text()).strip().split('\n')[0]
                        company = "Unknown"
                    else:
                        continue
                else:
                    title = (await title_elem.inner_text()).strip() if title_elem else ""
                    company = (await company_elem.inner_text()).strip() if company_elem else "Unknown"

                title = self._sanitize_job_title(title.split("\n")[0] if title else "")
                company = (company.split("\n")[0].strip() if company else "Unknown")

                if not title:
                    continue

                url = await link_elem.get_attribute("href")
                if url and not url.startswith("http"):
                    url = self.base_url + url
                if url and "?" in url:
                    url = url.split("?")[0]
                if not url:
                    continue
                if not re.search(r"/jobs/view/\d+", url):
                    continue

                parts = url.strip("/").split("/")
                external_id = parts[-1] if parts else "unknown"

                jobs.append({
                    "external_id": external_id,
                    "title": title,
                    "company": company,
                    "url": url,
                    "source": "linkedin"
                })
            except Exception as e:
                logger.error(f"Error extracting card: {e}")

        logger.info(f"Browser extracted {len(jobs)} jobs")
        return jobs

    async def _get_job_details(self, context: BrowserContext, url: str) -> Dict[str, Any]:
        page = await context.new_page()
        await page.add_init_script(STEALTH_SCRIPT)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            title = ""
            company = ""
            for selector in [
                "h1.top-card-layout__title",
                ".jobs-unified-top-card h1",
                "h1[data-test-id='job-details-job-title']",
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        text = self._sanitize_job_title((await el.inner_text()) or "")
                        if text:
                            title = text
                            break
                except Exception:
                    continue
            for selector in [
                ".topcard__org-name-link",
                ".jobs-unified-top-card__company-name a",
                "a[data-test-id='job-details-company-name']",
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        text = ((await el.inner_text()) or "").strip()
                        if text:
                            company = text
                            break
                except Exception:
                    continue

            desc_selectors = [".jobs-description", "#job-details", ".jobs-box__html-content", ".show-more-less-html__markup", ".description__text", "section.description", ".core-section-container__content"]
            description = ""
            description_html = ""
            for selector in desc_selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        description = await elem.inner_text()
                        description_html = await elem.inner_html()
                        if description.strip(): break
                except Exception:
                    continue

            if not description:
                description = await page.evaluate("""() => {
                    const texts = Array.from(document.querySelectorAll('div, section, article'))
                        .map(el => el.innerText).filter(t => t.length > 500);
                    return texts.sort((a, b) => b.length - a.length)[0] || '';
                }""")

            location = ""
            for selector in [".jobs-unified-top-card__bullet", ".top-card-layout__first-subline", ".topcard__flavor--bullet", ".topcard__flavor:first-child"]:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        text = (await elem.inner_text()).strip()
                        if text and not any(x in text.lower() for x in ["applied", "applicants", "ago"]):
                            location = text
                            break
                except Exception:
                    continue

            is_easy_apply = await page.query_selector(".jobs-apply-button--easy-apply, .apply-button--easy-apply") is not None

            work_type = "onsite"
            workplace_elem = await page.query_selector(".jobs-unified-top-card__workplace-type")
            if workplace_elem:
                wt_text = (await workplace_elem.inner_text()).lower()
                if "remote" in wt_text: work_type = "remote"
                elif "hybrid" in wt_text: work_type = "hybrid"
            elif description and "remote" in description.lower()[:500]:
                work_type = "remote"

            return {
                "title": title,
                "company": company,
                "description": description.strip(),
                "description_html": description_html.strip(),
                "location": location.strip(),
                "work_type": work_type,
                "is_easy_apply": is_easy_apply,
                "apply_url": None,
                "scraped_at": datetime.now()
            }
        except Exception as e:
            logger.error(f"Error in _get_job_details for {url}: {e}")
            return {"description": "", "description_html": "", "location": "", "work_type": "unknown", "scraped_at": datetime.now()}
        finally:
            await page.close()


class GeneralWebScraper:
    def __init__(self):
        self.headless = settings.browser_headless

    async def scrape_custom_url(self, url: str, keywords: List[str], locations: List[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Scrape jobs from an arbitrary career site URL."""
        logger.info(f"Starting custom scrape for: {url} with keywords: {keywords}, locations: {locations}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()
            await page.add_init_script(STEALTH_SCRIPT)

            try:
                if not url.startswith("http"):
                    url = "https://" + url

                is_google_careers = "google.com/about/careers" in url or "careers.google.com" in url
                if is_google_careers:
                    url = "https://www.google.com/about/careers/applications/jobs/results"
                    logger.info(f"Detected Google Careers: {url}")

                logger.info(f"Navigating to: {url}")
                await page.goto(url, wait_until="networkidle", timeout=45000)

                if is_google_careers:
                    await asyncio.sleep(5)
                    for _ in range(3):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)
                    try:
                        await page.wait_for_selector("div[role='listitem'], .gc-card, [data-job-id]", timeout=10000)
                    except:
                        pass
                else:
                    await asyncio.sleep(4)

                page_title = await page.title()
                logger.info(f"Page loaded: {page_title}")

                found_links = await page.evaluate("""({keywords, locations}) => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const jobs = [];
                    const seen = new Set();

                    links.forEach(link => {
                        const text = link.innerText.trim();
                        const href = link.href;
                        if (!text || !href) return;

                        const looksLikeJob = (
                            text.length > 5 &&
                            text.length < 200 &&
                            (
                                /job|career|position|opening|role|apply|details|vacancy|opportunities/i.test(href) ||
                                /job|career|position|opening|role|vacancy|opportunities/i.test(text)
                            ) &&
                            !/login|signup|privacy|term|cookie|blog|about|contact|press|help|faq|support/i.test(href)
                        );

                        const matchesKeyword = keywords.some(k => {
                            const words = k.toLowerCase().split(' ');
                            return words.every(word => text.toLowerCase().includes(word));
                        });

                        let matchesLocation = true;
                        if (locations && locations.length > 0) {
                            matchesLocation = locations.some(l =>
                                text.toLowerCase().includes(l.toLowerCase()) ||
                                href.toLowerCase().includes(l.toLowerCase().replace(/ /g, '-'))
                            );
                        }

                        if (href && !seen.has(href) && (looksLikeJob || matchesKeyword)) {
                            seen.add(href);
                            jobs.push({title: text, url: href, matchesKeyword, matchesLocation});
                        }
                    });
                    return jobs;
                }""", {"keywords": keywords, "locations": locations})

                logger.info(f"Heuristic scanner found {len(found_links)} candidates")

                scored = []
                for link in found_links:
                    score = 0
                    if link['matchesKeyword']: score += 3
                    if link['matchesLocation']: score += 2
                    if score > 0:
                        scored.append((score, link))

                scored.sort(key=lambda x: x[0], reverse=True)
                final = [j[1] for j in scored[:limit]]

                if not final:
                    logger.error(f"No job candidates found. Keywords: {keywords}")
                    return []

                scraped_jobs = []
                for idx, candidate in enumerate(final):
                    try:
                        detail_page = await context.new_page()
                        await detail_page.goto(candidate['url'], wait_until="domcontentloaded", timeout=20000)

                        description = await detail_page.evaluate("""() => {
                            const c = Array.from(document.querySelectorAll('div, section, article')).filter(el => el.innerText.length > 300);
                            return c.sort((a, b) => b.innerText.length - a.innerText.length)[0]?.innerText || '';
                        }""")
                        description_html = await detail_page.evaluate("""() => {
                            const c = Array.from(document.querySelectorAll('div, section, article')).filter(el => el.innerText.length > 300);
                            return c.sort((a, b) => b.innerText.length - a.innerText.length)[0]?.innerHTML || '';
                        }""")

                        domain = urllib.parse.urlparse(url).netloc.replace('www.', '').split('.')[0].capitalize()
                        scraped_jobs.append({
                            "external_id": hashlib.md5(candidate['url'].encode()).hexdigest(),
                            "title": candidate['title'].split('\\n')[0].strip()[:200],
                            "company": domain,
                            "url": candidate['url'],
                            "source": "custom_url",
                            "description": description.strip(),
                            "description_html": description_html.strip(),
                            "location": "See description",
                            "work_type": "remote" if "remote" in description.lower() else "onsite",
                            "scraped_at": datetime.now()
                        })
                        await detail_page.close()
                    except Exception as e:
                        logger.error(f"Failed to scrape detail for {candidate['url']}: {e}")

                logger.info(f"Scraped {len(scraped_jobs)} jobs from {url}")
                return scraped_jobs

            except Exception as e:
                logger.error(f"Error during custom URL scraping: {e}")
                return []
            finally:
                await browser.close()



class WebJobScraper:
    """
    Scrapes jobs from free/public APIs.
    Sources: Remotive, Arbeitnow, RemoteOK, Himalayas, Greenhouse, Lever.
    """

    REMOTIVE_API = "https://remotive.com/api/remote-jobs"
    ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"
    REMOTEOK_API = "https://remoteok.com/api"
    HIMALAYAS_API = "https://himalayas.app/jobs/api"

    GREENHOUSE_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    LEVER_BOARD_URL = "https://api.lever.co/v0/postings/{company}"

    # Public ATS boards to improve recall without user configuration.
    DEFAULT_GREENHOUSE_BOARDS = ["openai", "stripe", "airbnb", "notion", "coinbase"]
    DEFAULT_LEVER_COMPANIES = ["netflix", "shopify", "figma", "canva", "uber"]

    ROLE_SYNONYMS = {
        "csm": ["customer", "success", "manager"],
        "customer success": ["customer", "success", "account", "retention"],
        "account manager": ["account", "manager", "customer", "success"],
        "software engineer": ["software", "engineer", "developer", "backend", "frontend"],
        "fullstack": ["fullstack", "full", "stack", "developer", "engineer", "frontend", "backend"],
        "full stack": ["fullstack", "full", "stack", "developer", "engineer", "frontend", "backend"],
        "mern": ["mern", "mongodb", "express", "react", "node", "developer", "engineer"],
        "data engineer": ["data", "engineer", "etl", "pipeline", "sql", "python"],
    }

    def __init__(self):
        self._last_warnings: List[str] = []
        self._last_sources: List[str] = []
        self._last_source_breakdown: Dict[str, int] = {}

    async def scrape_jobs(self, query: str, location: str = "", limit: int = 10, filters: dict = None) -> List[Dict[str, Any]]:
        """Scrape jobs from multiple web/ATS APIs in parallel with ranked relevance."""
        filters = filters or {}
        sources = ["remotive", "arbeitnow", "remoteok", "himalayas", "greenhouse", "lever"]

        results = await asyncio.gather(
            self._scrape_remotive(query, limit * 8),
            self._scrape_arbeitnow(query, limit * 8),
            self._scrape_remoteok(query, limit * 8),
            self._scrape_himalayas(query, limit * 8),
            self._scrape_greenhouse(query, limit * 8),
            self._scrape_lever(query, limit * 8),
            return_exceptions=True,
        )

        all_jobs: List[Dict[str, Any]] = []
        warnings: List[str] = []
        source_breakdown: Dict[str, int] = {}
        sources_succeeded: List[str] = []

        for source_name, result in zip(sources, results):
            if isinstance(result, Exception):
                warnings.append(f"{source_name} failed: {result}")
                logger.error(f"{source_name} raised exception: {result}")
                continue

            if not isinstance(result, list):
                warnings.append(f"{source_name} returned unexpected payload")
                continue

            source_breakdown[source_name] = len(result)
            if result:
                sources_succeeded.append(source_name)
            else:
                warnings.append(f"{source_name} returned 0 jobs")
            all_jobs.extend(result)

        ranked = self._rank_and_filter_jobs(all_jobs, query, location, filters, limit)
        deduped = self._deduplicate(ranked)
        final = deduped[:limit]

        if not final:
            location_hint = f" in '{location}'" if location else ""
            warnings.append(
                f"No relevant jobs found for '{query}'{location_hint}. "
                "Checked web sources: remotive, arbeitnow, remoteok, himalayas, greenhouse, lever. "
                "Try broader role terms (e.g. 'customer success', 'account manager') or use LinkedIn for local-only jobs."
            )

        self._last_warnings = warnings
        self._last_sources = sources_succeeded
        self._last_source_breakdown = source_breakdown

        logger.info(
            f"Web scraper: {len(final)} selected from {len(all_jobs)} candidates. "
            f"Sources={source_breakdown} warnings={warnings}"
        )
        return final

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = (text or "").lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _expanded_query_terms(self, query: str) -> set[str]:
        normalized = self._normalize_text(query)
        terms = set(normalized.split())

        for phrase, extra in self.ROLE_SYNONYMS.items():
            if phrase in normalized:
                terms.update(extra)

        if "customer" in terms and "success" in terms:
            terms.add("csm")
        return terms

    def _relevance_score(self, job: Dict[str, Any], query: str, terms: set[str], strict_pass: bool) -> float:
        title = self._normalize_text(job.get("title", ""))
        company = self._normalize_text(job.get("company", ""))
        description = self._normalize_text(job.get("description", ""))
        haystack = f"{title} {company} {description}"

        query_phrase = self._normalize_text(query)
        term_matches = sum(1 for term in terms if term and term in haystack)
        title_matches = sum(1 for term in terms if term and term in title)

        score = 0.0
        score += title_matches * 2.0
        score += term_matches * 0.8

        if query_phrase and query_phrase in title:
            score += 4.0
        elif query_phrase and query_phrase in haystack:
            score += 2.0

        if strict_pass:
            return score if (title_matches > 0 or query_phrase in haystack) else 0.0
        if title_matches == 0 and query_phrase not in title:
            score -= 2.0
        return score

    def _job_age_days(self, job: Dict[str, Any]) -> float | None:
        raw = job.get("posted_date")
        if not raw:
            return None
        if isinstance(raw, datetime):
            return max(0.0, (datetime.now() - raw).total_seconds() / 86400.0)
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace("+00:00", ""))
            return max(0.0, (datetime.now() - parsed).total_seconds() / 86400.0)
        except Exception:
            return None

    def _matches_filters(self, job: Dict[str, Any], location: str, filters: dict) -> bool:
        work_types = filters.get("work_types") or []
        if work_types:
            job_work = (job.get("work_type") or "").lower()
            if job_work and not any(w.lower() == job_work for w in work_types):
                return False

        date_posted = filters.get("date_posted")
        age_days = self._job_age_days(job)
        if date_posted and age_days is not None:
            max_age = {"past_24h": 1, "past_week": 7, "past_month": 30}.get(date_posted)
            if max_age and age_days > max_age:
                return False

        if location:
            loc_norm = self._normalize_text(location)
            job_loc = self._normalize_text(job.get("location", ""))
            broad_locs = {"india", "united states", "usa", "united kingdom", "uk", "global"}
            global_markers = {"remote", "worldwide", "global", "anywhere", "distributed", "international"}
            if loc_norm in broad_locs:
                return True
            # Keep remote/global jobs even for location filter.
            if job_loc and not any(marker in job_loc for marker in global_markers):
                loc_parts = [p for p in re.split(r"[\s,]+", loc_norm) if len(p) > 2]
                if loc_parts:
                    if not any(p in job_loc for p in loc_parts):
                        return False
                elif loc_norm not in job_loc:
                    return False

        return True

    def _rank_and_filter_jobs(self, jobs: List[Dict[str, Any]], query: str, location: str, filters: dict, limit: int) -> List[Dict[str, Any]]:
        if not jobs:
            return []

        terms = self._expanded_query_terms(query)

        ranked: List[tuple[float, Dict[str, Any]]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for strict_pass in (True, False):
            for job in jobs:
                if not self._matches_filters(job, location, filters):
                    continue
                score = self._relevance_score(job, query, terms, strict_pass=strict_pass)
                threshold = 2.4 if strict_pass else 2.0
                if score >= threshold:
                    key = (
                        (job.get("title") or "").lower().strip(),
                        (job.get("company") or "").lower().strip(),
                        (job.get("url") or "").strip(),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    ranked.append((score, job))

            if len(ranked) >= limit:
                break

        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in ranked]

    @staticmethod
    def _deduplicate(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate jobs based on normalized title + company."""
        seen: Dict[tuple, Dict[str, Any]] = {}
        for job in jobs:
            key = (
                (job.get("title") or "").lower().strip(),
                (job.get("company") or "").lower().strip(),
            )
            if key in seen:
                existing = seen[key]
                if len(job.get("description", "")) > len(existing.get("description", "")):
                    seen[key] = job
            else:
                seen[key] = job
        return list(seen.values())

    async def _scrape_remotive(self, query: str, limit: int) -> List[Dict[str, Any]]:
        logger.info(f"Remotive API: searching for '{query}'")
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=HTTP_HEADERS) as client:
                resp = await client.get(self.REMOTIVE_API, params={"search": query, "limit": min(limit, 100)})
                if resp.status_code != 200:
                    logger.warning(f"Remotive returned HTTP {resp.status_code}")
                    return []
                raw_jobs = resp.json().get("jobs", [])
                return [self._map_remotive_job(j) for j in raw_jobs[:limit]]
        except Exception as e:
            logger.error(f"Remotive scraping failed: {e}")
            return []

    @staticmethod
    def _map_remotive_job(j: dict) -> Dict[str, Any]:
        desc_html = j.get("description", "")
        desc_text = _strip_tags(desc_html) if desc_html else ""
        posted = j.get("publication_date")
        return {
            "external_id": f"remotive-{j.get('id', '')}",
            "title": j.get("title", ""),
            "company": j.get("company_name", "Unknown"),
            "url": j.get("url", ""),
            "location": j.get("candidate_required_location", "Remote"),
            "work_type": "remote",
            "is_easy_apply": False,
            "apply_url": j.get("url", ""),
            "description": desc_text,
            "description_html": desc_html,
            "source": "remotive",
            "posted_date": posted,
            "scraped_at": datetime.now(),
        }

    async def _scrape_arbeitnow(self, query: str, limit: int) -> List[Dict[str, Any]]:
        logger.info(f"Arbeitnow API: searching for '{query}'")
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=HTTP_HEADERS) as client:
                resp = await client.get(self.ARBEITNOW_API)
                if resp.status_code != 200:
                    logger.warning(f"Arbeitnow returned HTTP {resp.status_code}")
                    return []
                raw_jobs = resp.json().get("data", [])
                return [self._map_arbeitnow_job(j) for j in raw_jobs[:limit]]
        except Exception as e:
            logger.error(f"Arbeitnow scraping failed: {e}")
            return []

    @staticmethod
    def _map_arbeitnow_job(j: dict) -> Dict[str, Any]:
        desc_html = j.get("description", "")
        desc_text = _strip_tags(desc_html) if desc_html else ""
        return {
            "external_id": f"arbeitnow-{j.get('slug', '')}",
            "title": j.get("title", ""),
            "company": j.get("company_name", "Unknown"),
            "url": j.get("url", ""),
            "location": j.get("location", "Remote"),
            "work_type": "remote" if j.get("remote", False) else "onsite",
            "is_easy_apply": False,
            "apply_url": j.get("url", ""),
            "description": desc_text,
            "description_html": desc_html,
            "source": "arbeitnow",
            "posted_date": j.get("created_at"),
            "scraped_at": datetime.now(),
        }

    async def _scrape_remoteok(self, query: str, limit: int) -> List[Dict[str, Any]]:
        logger.info(f"RemoteOK API: searching for '{query}'")
        try:
            headers = {**HTTP_HEADERS, "User-Agent": "job-search-app/1.0"}
            async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
                resp = await client.get(self.REMOTEOK_API)
                if resp.status_code != 200:
                    logger.warning(f"RemoteOK returned HTTP {resp.status_code}")
                    return []
                data = resp.json()
                raw_jobs = data[1:] if isinstance(data, list) and len(data) > 1 else []
                return [self._map_remoteok_job(j) for j in raw_jobs[:limit] if isinstance(j, dict)]
        except Exception as e:
            logger.error(f"RemoteOK scraping failed: {e}")
            return []

    @staticmethod
    def _map_remoteok_job(j: dict) -> Dict[str, Any]:
        desc_html = j.get("description", "")
        desc_text = _strip_tags(desc_html) if desc_html else ""
        posted = j.get("date") or j.get("epoch")
        return {
            "external_id": f"remoteok-{j.get('id', '')}",
            "title": j.get("position", ""),
            "company": j.get("company", "Unknown"),
            "url": j.get("url", j.get("apply_url", "")),
            "location": j.get("location", "Remote"),
            "work_type": "remote",
            "is_easy_apply": False,
            "apply_url": j.get("apply_url", j.get("url", "")),
            "description": desc_text,
            "description_html": desc_html,
            "source": "remoteok",
            "posted_date": posted,
            "scraped_at": datetime.now(),
        }

    async def _scrape_himalayas(self, query: str, limit: int) -> List[Dict[str, Any]]:
        logger.info(f"Himalayas API: searching for '{query}'")
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=HTTP_HEADERS) as client:
                resp = await client.get(self.HIMALAYAS_API, params={"limit": min(limit, 100), "query": query})
                if resp.status_code != 200:
                    logger.warning(f"Himalayas returned HTTP {resp.status_code}")
                    return []
                raw_jobs = resp.json().get("jobs", [])
                return [self._map_himalayas_job(j) for j in raw_jobs[:limit]]
        except Exception as e:
            logger.error(f"Himalayas scraping failed: {e}")
            return []

    @staticmethod
    def _map_himalayas_job(j: dict) -> Dict[str, Any]:
        desc_text = j.get("description", "")
        return {
            "external_id": f"himalayas-{j.get('id', '')}",
            "title": j.get("title", ""),
            "company": j.get("companyName", "Unknown"),
            "url": j.get("applicationLink", j.get("url", "")),
            "location": j.get("location", "Remote"),
            "work_type": "remote",
            "is_easy_apply": False,
            "apply_url": j.get("applicationLink", ""),
            "description": desc_text,
            "description_html": "",
            "source": "himalayas",
            "posted_date": j.get("publishedAt") or j.get("createdAt"),
            "scraped_at": datetime.now(),
        }

    def _greenhouse_boards(self) -> List[str]:
        env_val = os.getenv("GREENHOUSE_BOARDS", "")
        if env_val.strip():
            return [s.strip() for s in env_val.split(",") if s.strip()]
        return self.DEFAULT_GREENHOUSE_BOARDS

    async def _scrape_greenhouse(self, query: str, limit: int) -> List[Dict[str, Any]]:
        boards = self._greenhouse_boards()
        jobs: List[Dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=HTTP_HEADERS) as client:
                tasks = [
                    client.get(self.GREENHOUSE_BOARD_URL.format(board=board), params={"content": "true"})
                    for board in boards
                ]
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                for board, response in zip(boards, responses):
                    if isinstance(response, Exception):
                        logger.warning(f"Greenhouse board {board} failed: {response}")
                        continue
                    if response.status_code != 200:
                        logger.warning(f"Greenhouse board {board} returned HTTP {response.status_code}")
                        continue
                    payload = response.json()
                    for item in payload.get("jobs", []):
                        jobs.append(self._map_greenhouse_job(item, board))
                    if len(jobs) >= limit:
                        break
        except Exception as e:
            logger.error(f"Greenhouse scraping failed: {e}")
            return []
        return jobs[:limit]

    @staticmethod
    def _map_greenhouse_job(j: dict, board: str) -> Dict[str, Any]:
        content = j.get("content") or ""
        abs_url = j.get("absolute_url") or ""
        metadata = j.get("metadata") or []
        location = ""
        for m in metadata:
            if str(m.get("name", "")).lower() == "location":
                location = m.get("value", "")
                break
        if not location:
            location = (j.get("location") or {}).get("name", "Remote") if isinstance(j.get("location"), dict) else "Remote"
        return {
            "external_id": f"greenhouse-{j.get('id', '')}",
            "title": j.get("title", ""),
            "company": board.capitalize(),
            "url": abs_url,
            "location": location,
            "work_type": "remote" if "remote" in location.lower() else "onsite",
            "is_easy_apply": False,
            "apply_url": abs_url,
            "description": _strip_tags(content),
            "description_html": content,
            "source": "greenhouse",
            "posted_date": j.get("updated_at"),
            "scraped_at": datetime.now(),
        }

    def _lever_companies(self) -> List[str]:
        env_val = os.getenv("LEVER_COMPANIES", "")
        if env_val.strip():
            return [s.strip() for s in env_val.split(",") if s.strip()]
        return self.DEFAULT_LEVER_COMPANIES

    async def _scrape_lever(self, query: str, limit: int) -> List[Dict[str, Any]]:
        companies = self._lever_companies()
        jobs: List[Dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=HTTP_HEADERS) as client:
                tasks = [client.get(self.LEVER_BOARD_URL.format(company=company), params={"mode": "json"}) for company in companies]
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                for company, response in zip(companies, responses):
                    if isinstance(response, Exception):
                        logger.warning(f"Lever company {company} failed: {response}")
                        continue
                    if response.status_code != 200:
                        logger.warning(f"Lever company {company} returned HTTP {response.status_code}")
                        continue
                    for item in response.json() if isinstance(response.json(), list) else []:
                        jobs.append(self._map_lever_job(item, company))
                    if len(jobs) >= limit:
                        break
        except Exception as e:
            logger.error(f"Lever scraping failed: {e}")
            return []
        return jobs[:limit]

    @staticmethod
    def _map_lever_job(j: dict, company: str) -> Dict[str, Any]:
        categories = j.get("categories") or {}
        location = categories.get("location", "Remote")
        description_html = j.get("descriptionPlain") or j.get("description") or ""
        apply_url = j.get("hostedUrl") or ""
        return {
            "external_id": f"lever-{j.get('id', '')}",
            "title": j.get("text", ""),
            "company": company.capitalize(),
            "url": apply_url,
            "location": location,
            "work_type": "remote" if "remote" in location.lower() else "onsite",
            "is_easy_apply": False,
            "apply_url": apply_url,
            "description": _strip_tags(description_html),
            "description_html": description_html,
            "source": "lever",
            "posted_date": j.get("createdAt"),
            "scraped_at": datetime.now(),
        }
