from __future__ import annotations
import asyncio
import logging
import random
import re
import time
from html import unescape
from pathlib import Path
from typing import Optional, List, Dict, Any
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
                return jobs
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
        return detailed_jobs

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
            "li.jobs-search-results__list-item",
            ".scaffold-layout__list-item",
            ".job-card-container",
            ".base-card.base-card--link",
            "li.result-card",
            ".base-card",
            "div[data-job-id]",
            "li[data-occludable-job-id]",
            "ul.jobs-search__results-list > li",
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

                if not title:
                    continue

                url = await link_elem.get_attribute("href")
                if url and not url.startswith("http"):
                    url = self.base_url + url
                if url and "?" in url:
                    url = url.split("?")[0]
                if not url:
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
