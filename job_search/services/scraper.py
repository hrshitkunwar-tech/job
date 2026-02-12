from __future__ import annotations
import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import urllib.parse
import hashlib

from playwright.async_api import async_playwright, Page, BrowserContext

from job_search.config import settings

logger = logging.getLogger(__name__)

# Stealth script to avoid bot detection — hides the navigator.webdriver flag
# and patches other fingerprinting vectors that LinkedIn checks.
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""


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

    async def scrape_jobs(self, query: str, location: str = "United States", limit: int = 10, filters: dict = None, check_cancelled: Callable[[], bool] = None) -> List[Dict[str, Any]]:
        """Main entry point to scrape jobs."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)

            # Use saved state and random user agent
            user_agents = [
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ]
            context_args = {
                "user_agent": random.choice(user_agents),
                "viewport": {"width": 1280, "height": 800}
            }
            if self.storage_state.exists():
                context_args["storage_state"] = str(self.storage_state)

            context = await browser.new_context(**context_args)
            page = await context.new_page()

            # Inject stealth script to avoid bot detection
            await page.add_init_script(STEALTH_SCRIPT)

            try:
                # Check if logged in
                await page.goto(self.base_url, timeout=30000)
                await self._get_random_delay(1, 2)
                logged_in = await self._is_logged_in(page)

                if not logged_in:
                    if self.email and self.password:
                        await self._login(page)
                        logged_in = await self._is_logged_in(page)
                    else:
                        logger.warning("Not logged in and no credentials provided. Using guest access.")

                # Construct Search URL
                if not query:
                    logger.warning("Empty search query provided. Skipping search.")
                    return []

                safe_query = urllib.parse.quote(query)
                safe_location = urllib.parse.quote(location) if location else ""

                # Build filter params
                filter_params = []
                if filters:
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

                search_url = f"{self.base_url}/jobs/search/?keywords={safe_query}"
                if safe_location:
                    search_url += f"&location={safe_location}"

                if filter_params:
                    search_url += "&" + "&".join(filter_params)

                logger.info(f"Navigating to LinkedIn Search: {search_url}")
                await page.goto(search_url, wait_until="load", timeout=60000)
                await self._get_random_delay(3, 5)

                # Verification: Are we on a login, authwall, or checkpoint page?
                current_url = page.url
                logger.info(f"Current Page URL: {current_url}")

                if "checkpoint" in current_url:
                    logger.error("Scraper stuck at Security Checkpoint.")
                    if not self.headless:
                        logger.info("Waiting for manual CAPTCHA resolution (120s timeout)...")
                        for _ in range(60):
                            if "checkpoint" not in page.url:
                                logger.info("Checkpoint passed! Saving session state.")
                                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                                await context.storage_state(path=str(self.storage_state))
                                break
                            await asyncio.sleep(2)
                    else:
                        logger.warning("In headless mode — cannot solve CAPTCHA. Returning empty.")
                        return []

                # Detect login/authwall redirect — fall back to guest search
                if any(x in current_url for x in ["/login", "/authwall", "/signup"]):
                    logger.warning(f"Redirected to auth page: {current_url}. Retrying with guest URL...")
                    jobs = await self._scrape_guest(page, query, location, limit, filter_params)
                    return await self._fetch_details_batch(context, jobs, limit, check_cancelled)

                # Fallback: If LinkedIn redirected us to the home feed or 'jobs' home without searching
                if "/search" not in current_url and "keywords" not in current_url:
                    logger.info("Direct URL search failed to load results. Attempting on-page search...")
                    await self._perform_on_page_search(page, query, location)
                    await self._get_random_delay(4, 6)

                # Basic job extraction
                jobs = await self._extract_job_list(page, limit)

                # If authenticated search found nothing, try guest fallback
                if not jobs:
                    logger.warning(f"Authenticated search found 0 job cards. Page title: '{await page.title()}'. Trying guest fallback...")
                    jobs = await self._scrape_guest(page, query, location, limit, filter_params)

                if not jobs:
                    logger.error("No jobs found after all attempts. LinkedIn may be blocking this request.")
                    return []

                return await self._fetch_details_batch(context, jobs, limit, check_cancelled)

            finally:
                # Save state for next time
                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await context.storage_state(path=str(self.storage_state))
                except Exception:
                    pass
                await browser.close()

    async def _scrape_guest(self, page: Page, query: str, location: str, limit: int, filter_params: list) -> List[Dict[str, Any]]:
        """
        Fallback: scrape LinkedIn's public/guest job search page.
        This page renders differently (uses .base-card selectors) and
        doesn't require authentication.
        """
        safe_query = urllib.parse.quote(query)
        safe_location = urllib.parse.quote(location) if location else ""

        guest_url = f"{self.base_url}/jobs/search/?keywords={safe_query}"
        if safe_location:
            guest_url += f"&location={safe_location}"
        if filter_params:
            guest_url += "&" + "&".join(filter_params)

        logger.info(f"Guest fallback: navigating to {guest_url}")
        try:
            await page.goto(guest_url, wait_until="networkidle", timeout=30000)
        except Exception:
            # networkidle can timeout on heavy pages — try domcontentloaded
            await page.goto(guest_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for dynamic content to render
        await asyncio.sleep(5)

        # Scroll to trigger lazy loading
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)

        current_url = page.url
        page_title = await page.title()
        logger.info(f"Guest page loaded: title='{page_title}', url={current_url}")

        # If we still got redirected to auth, we're blocked
        if any(x in current_url for x in ["/login", "/authwall", "/signup"]):
            logger.error("Guest access also redirected to auth. LinkedIn is blocking this IP/browser.")
            return []

        return await self._extract_job_list(page, limit)

    async def _fetch_details_batch(self, context: BrowserContext, jobs: list, limit: int, check_cancelled) -> List[Dict[str, Any]]:
        """Fetch full job details for a list of scraped job stubs."""
        sem = asyncio.Semaphore(3)

        async def fetch_detail_with_sem(job):
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

        logger.info(f"Fetching details for {len(jobs)} jobs...")
        tasks = [fetch_detail_with_sem(job) for job in jobs[:limit]]
        detailed_jobs = await asyncio.gather(*tasks)

        # Filter out jobs that failed completely
        final_jobs = [j for j in detailed_jobs if j.get("description")]
        logger.info(f"Successfully scraped {len(final_jobs)} jobs with full details.")
        return final_jobs

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
            logger.warning("Continuing without login - results may be limited")
            return

        if "checkpoint" in page.url:
            logger.warning("Security checkpoint encountered. Manual intervention might be needed.")
            await asyncio.sleep(30)

    async def _extract_job_list(self, page: Page, limit: int) -> List[Dict[str, Any]]:
        jobs = []

        # Scroll to load more jobs
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # Target the main search results list — try both logged-in and guest selectors
        selectors = [
            "li.jobs-search-results__list-item",
            ".scaffold-layout__list-item",
            ".job-card-container",
            ".base-card.base-card--link",          # Guest page job card
            "li.result-card",                       # Older guest layout
            ".base-card",
            "div[data-job-id]",
            "li[data-occludable-job-id]",
            ".jobs-search-results-list li",         # Generic list item
            "ul.jobs-search__results-list > li",    # Guest page list items
        ]

        job_cards = []
        for selector in selectors:
            job_cards = await page.query_selector_all(selector)
            if job_cards:
                logger.info(f"Found {len(job_cards)} job cards using selector: {selector}")
                break

        if not job_cards:
            logger.warning("No job cards found with any known selectors.")
            # Last ditch effort: any link that looks like a job link
            all_links = await page.query_selector_all("a[href*='/jobs/view/']")
            logger.info(f"Found {len(all_links)} potential job view links in last-ditch effort")
            if all_links:
                unique_links = []
                seen_urls = set()
                for link in all_links:
                    url = await link.get_attribute("href")
                    if url and "/jobs/view/" in url:
                        clean_url = url.split("?")[0]
                        if clean_url not in seen_urls:
                            seen_urls.add(clean_url)
                            unique_links.append(link)
                job_cards = unique_links

            if not job_cards:
                # Log page state for debugging
                page_title = await page.title()
                body_text = await page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")
                logger.warning(f"Page title: '{page_title}', body preview: '{body_text[:200]}'")

        for card in job_cards[:limit]:
            try:
                # Robust multi-selector approach for job cards
                title_selectors = [
                    ".job-card-list__title",
                    ".base-search-card__title",
                    "h3.base-search-card__title",
                    ".job-card-container__link",
                    "a.job-card-list__title",
                    "h3",  # Generic fallback within card
                ]
                company_selectors = [
                    ".job-card-container__company-name",
                    ".base-search-card__subtitle",
                    "h4.base-search-card__subtitle",
                    ".job-card-container__primary-description",
                    "h4",  # Generic fallback within card
                ]
                link_selectors = [
                    "a.job-card-list__title",
                    "a.base-card__full-link",
                    "a.base-search-card__full-link",
                    "a.job-card-container__link",
                    ".base-search-card__title-link",
                    "a[href*='/jobs/view/']",  # Generic job link
                ]

                title_elem = None
                for selector in title_selectors:
                    title_elem = await card.query_selector(selector)
                    if title_elem: break

                company_elem = None
                for selector in company_selectors:
                    company_elem = await card.query_selector(selector)
                    if company_elem: break

                link_elem = None
                for selector in link_selectors:
                    link_elem = await card.query_selector(selector)
                    if link_elem: break

                if not title_elem or not link_elem:
                    # If this "card" IS the link (from last ditch effort)
                    if not link_elem and await card.evaluate("node => node.tagName === 'A'"):
                         link_elem = card
                         title = (await card.inner_text()).strip().split('\n')[0]
                         company = "Unknown"
                    else:
                        continue
                else:
                    title = (await title_elem.inner_text()).strip()
                    company = (await company_elem.inner_text()).strip() if company_elem else "Unknown"

                url = await link_elem.get_attribute("href")
                if url and not url.startswith("http"):
                    url = self.base_url + url

                # Clean URL (remove tracking params)
                if url and "?" in url:
                    url = url.split("?")[0]

                if not url: continue

                # Extract external ID from URL robustly
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

        logger.info(f"Extracted {len(jobs)} jobs from page")
        return jobs

    async def _get_job_details(self, context: BrowserContext, url: str) -> Dict[str, Any]:
        page = await context.new_page()
        await page.add_init_script(STEALTH_SCRIPT)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)  # Wait for dynamic content

            # Try multiple selectors for job description
            desc_selectors = [
                 ".jobs-description",
                 "#job-details",
                 ".jobs-box__html-content",
                 ".show-more-less-html__markup",
                 ".description__text",
                 "section.description",
                 "div.job-description",
                 ".core-section-container__content",
            ]

            description = ""
            description_html = ""
            for selector in desc_selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        description = await elem.inner_text()
                        description_html = await elem.inner_html()
                        if description.strip(): break
                except Exception: continue

            if not description:
                # Fallback: get largest text block
                description = await page.evaluate("""() => {
                    const texts = Array.from(document.querySelectorAll('div, section, article'))
                        .map(el => el.innerText)
                        .filter(t => t.length > 500);
                    return texts.sort((a, b) => b.length - a.length)[0] || '';
                }""")

            # Location extraction - broader search
            location = ""
            loc_selectors = [
                ".jobs-unified-top-card__bullet",
                ".top-card-layout__first-subline",
                ".job-details-jobs-unified-top-card__bullet",
                "span.jobs-unified-top-card__bullet",
                ".topcard__flavor--bullet",
                ".topcard__flavor:first-child",
            ]
            for selector in loc_selectors:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        text = (await elem.inner_text()).strip()
                        if text and not any(x in text.lower() for x in ["applied", "applicants", "ago"]):
                            location = text
                            break
                except Exception: continue

            # Detect Easy Apply
            is_easy_apply = await page.query_selector(".jobs-apply-button--easy-apply, .apply-button--easy-apply") is not None

            # extract apply url
            apply_url = None
            if not is_easy_apply:
                apply_button = await page.query_selector(".jobs-apply-button")
                if apply_button:
                    try:
                        async with page.expect_popup() as popup_info:
                            await apply_button.click()
                        popup = await popup_info.value
                        apply_url = popup.url
                        await popup.close()
                    except:
                        apply_url = url

            # extract work type
            work_type = "onsite"
            workplace_elem = await page.query_selector(".jobs-unified-top-card__workplace-type, .topcard__flavor--bullet:last-child")
            if workplace_elem:
                wt_text = (await workplace_elem.inner_text()).lower()
                if "remote" in wt_text: work_type = "remote"
                elif "hybrid" in wt_text: work_type = "hybrid"

            if work_type == "onsite" and "remote" in description.lower()[:500]:
                work_type = "remote"

            return {
                "description": description.strip(),
                "description_html": description_html.strip(),
                "location": location.strip(),
                "work_type": work_type,
                "is_easy_apply": is_easy_apply,
                "apply_url": apply_url,
                "scraped_at": datetime.now()
            }
        except Exception as e:
            logger.error(f"Error in _get_job_details for {url}: {e}")
            return {
                "description": "",
                "description_html": "",
                "location": "",
                "work_type": "unknown",
                "scraped_at": datetime.now()
            }
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
                # Standardize URL - remove hash fragments for listing pages
                if not url.startswith("http"):
                    url = "https://" + url

                # For Google Careers, navigate to the main listings page
                is_google_careers = "google.com/about/careers" in url or "careers.google.com" in url
                if is_google_careers:
                    url = "https://www.google.com/about/careers/applications/jobs/results"
                    logger.info(f"Detected Google Careers - using main listing page: {url}")

                logger.info(f"Navigating to: {url}")
                await page.goto(url, wait_until="networkidle", timeout=45000)

                # Special handling for SPAs like Google Careers
                if is_google_careers:
                    logger.info("Google Careers detected - applying SPA scraping strategy")
                    await asyncio.sleep(5)  # Initial wait for JS

                    # Scroll to trigger lazy loading
                    for _ in range(3):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)

                    # Wait for job cards to appear
                    try:
                        await page.wait_for_selector("div[role='listitem'], .gc-card, [data-job-id]", timeout=10000)
                        logger.info("Job cards detected on page")
                    except:
                        logger.warning("No job cards found with standard selectors")
                else:
                    await asyncio.sleep(4)  # Wait for JS rendering and dynamic content

                # Log page title for debugging
                page_title = await page.title()
                logger.info(f"Page loaded: {page_title}")

                # Evaluation script to find job-like links
                found_links = await page.evaluate("""({keywords, locations}) => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const jobs = [];
                    const seen = new Set();

                    console.log(`Total links found: ${links.length}`);

                    links.forEach(link => {
                        const text = link.innerText.trim();
                        const href = link.href;
                        if (!text || !href) return;

                        // Heuristics for job links - more permissive
                        const looksLikeJob = (
                            text.length > 5 &&
                            text.length < 200 &&
                            (
                                /job|career|position|opening|role|apply|details|vacancy|opportunities/i.test(href) ||
                                /job|career|position|opening|role|vacancy|opportunities/i.test(text)
                            ) &&
                            !/login|signup|privacy|term|cookie|blog|about|contact|press|help|faq|support/i.test(href)
                        );

                        // Check keywords - case insensitive partial match
                        const matchesKeyword = keywords.some(k => {
                            const keyword = k.toLowerCase();
                            const textLower = text.toLowerCase();
                            // Split keyword into words for better matching
                            const words = keyword.split(' ');
                            return words.every(word => textLower.includes(word));
                        });

                        // Check locations if provided
                        let matchesLocation = true;
                        if (locations && locations.length > 0) {
                            matchesLocation = locations.some(l =>
                                text.toLowerCase().includes(l.toLowerCase()) ||
                                href.toLowerCase().includes(l.toLowerCase().replace(/ /g, '-'))
                            );
                        }

                        if (href && !seen.has(href) && (looksLikeJob || matchesKeyword)) {
                            seen.add(href);
                            jobs.push({
                                title: text,
                                url: href,
                                matchesKeyword,
                                matchesLocation
                            });
                        }
                    });

                    console.log(`Job-like links found: ${jobs.length}`);
                    return jobs;
                }""", {"keywords": keywords, "locations": locations})

                logger.info(f"Heuristic scanner found {len(found_links)} potential candidates")

                if len(found_links) == 0:
                    # Try alternative approach - look for common job listing patterns
                    logger.warning("No links found with standard heuristics. Trying alternative selectors...")

                    # Get page content for debugging
                    content_sample = await page.evaluate("""() => {
                        const body = document.body.innerText;
                        return body.substring(0, 500);
                    }""")
                    logger.info(f"Page content sample: {content_sample[:200]}...")

                    # Try to find job cards or listings
                    job_cards = await page.query_selector_all("div[class*='job'], li[class*='job'], article[class*='job']")
                    logger.info(f"Found {len(job_cards)} potential job card elements")

                # Filter and score - make location optional
                scored_candidates = []
                for link in found_links:
                    score = 0
                    if link['matchesKeyword']: score += 3  # Keyword match is most important
                    if link['matchesLocation']: score += 2  # Location match is a bonus

                    # Don't exclude jobs that don't match location - just give them lower priority
                    # Only require keyword match OR job-like appearance
                    if score > 0 or link.get('matchesKeyword', False):
                        scored_candidates.append((score, link))

                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                final_candidates = [j[1] for j in scored_candidates[:limit]]

                logger.info(f"After filtering: {len(final_candidates)} candidates selected for detail scraping")

                if len(final_candidates) == 0:
                    logger.error(f"No job candidates found matching criteria. Keywords: {keywords}, Locations: {locations}")
                    await browser.close()
                    return []

                # Fetch details for found candidates
                scraped_jobs = []
                for idx, candidate in enumerate(final_candidates):
                    try:
                        logger.info(f"Scraping detail {idx+1}/{len(final_candidates)}: {candidate['url']}")
                        detail_page = await context.new_page()
                        await detail_page.goto(candidate['url'], wait_until="domcontentloaded", timeout=20000)

                        description = await detail_page.evaluate("""() => {
                            // Try to find the container with the most text
                            const containers = Array.from(document.querySelectorAll('div, section, article'))
                                .filter(el => el.innerText.length > 300);
                            return containers.sort((a, b) => b.innerText.length - a.innerText.length)[0]?.innerText || '';
                        }""")

                        description_html = await detail_page.evaluate("""() => {
                            const containers = Array.from(document.querySelectorAll('div, section, article'))
                                .filter(el => el.innerText.length > 300);
                            return containers.sort((a, b) => b.innerText.length - a.innerText.length)[0]?.innerHTML || '';
                        }""")

                        # Simple location/company detection
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
                            "work_type": "onsite" if "remote" not in description.lower() else "remote",
                            "scraped_at": datetime.now()
                        })
                        await detail_page.close()
                    except Exception as e:
                        logger.error(f"Failed to scrape detail for {candidate['url']}: {e}")

                logger.info(f"Successfully scraped {len(scraped_jobs)} jobs from {url}")
                return scraped_jobs

            except Exception as e:
                logger.error(f"Error during custom URL scraping: {e}")
                return []
            finally:
                await browser.close()
