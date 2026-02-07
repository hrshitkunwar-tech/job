from __future__ import annotations
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import urllib.parse

from playwright.async_api import async_playwright, Page, BrowserContext

from job_search.config import settings

logger = logging.getLogger(__name__)

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

    async def scrape_jobs(self, query: str, location: str = "", limit: int = 10) -> List[Dict[str, Any]]:
        """Main entry point to scrape jobs."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            
            # Use saved state if it exists
            context_args = {}
            if self.storage_state.exists():
                context_args["storage_state"] = str(self.storage_state)
            
            context = await browser.new_context(**context_args)
            page = await context.new_page()
            
            try:
                # Check if logged in
                await page.goto(self.base_url)
                if not await self._is_logged_in(page):
                    if self.email and self.password:
                        await self._login(page)
                    else:
                        logger.warning("Not logged in and no credentials provided. Scrapes may be limited.")

                # Construct Search URL
                # Using a more robust URL format for LinkedIn jobs
                if not query:
                    logger.warning("Empty search query provided. Skipping search.")
                    return []

                safe_query = urllib.parse.quote(query)
                safe_location = urllib.parse.quote(location) if location else ""
                
                # Try the clean search URL first
                search_url = f"{self.base_url}/jobs/search/?keywords={safe_query}"
                if safe_location:
                    search_url += f"&location={safe_location}"
                
                logger.info(f"Navigating to LinkedIn Search: {search_url}")
                await page.goto(search_url, wait_until="load", timeout=60000)
                await self._get_random_delay(3, 5)

                # Verification: Are we on a login or checkpoint page?
                current_url = page.url
                logger.info(f"Current Page URL: {current_url}")

                if "checkpoint" in current_url:
                    logger.error("Scraper stuck at Security Checkpoint. Please solve the puzzle in the browser window!")
                    # Wait for manual intervention if headless=False
                    if not self.headless:
                        await asyncio.sleep(60)
                    else:
                        return []

                # Fallback: If LinkedIn redirected us to the home feed or 'jobs' home without searching
                if "/search" not in current_url and "keywords" not in current_url:
                    logger.info("Direct URL search failed to load results. Attempting on-page search...")
                    await self._perform_on_page_search(page, query, location)
                    await self._get_random_delay(4, 6)

                # Basic job extraction
                jobs = await self._extract_job_list(page, limit)
                
                # Full description extraction
                detailed_jobs = []
                for job in jobs:
                    try:
                        detail = await self._get_job_details(context, job["url"])
                        job.update(detail)
                        detailed_jobs.append(job)
                        await self._get_random_delay(1, 3) # Inter-job delay
                    except Exception as e:
                        logger.error(f"Failed to get details for {job['url']}: {e}")
                        detailed_jobs.append(job) # Keep what we have

                return detailed_jobs

            finally:
                # Save state for next time
                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(self.storage_state))
                await browser.close()

    async def _is_logged_in(self, page: Page) -> bool:
        return "feed" in page.url or "jobs" in page.url or await page.query_selector(".nav-item--profile") is not None

    async def _perform_on_page_search(self, page: Page, query: str, location: str):
        """Use the LinkedIn search bar directly on the page."""
        try:
            # Find keywords input
            # Selectors for keywords/location inputs vary
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

            # Clear and type keywords
            kw_found = False
            for selector in kw_selectors:
                if await page.query_selector(selector):
                    await page.click(selector, click_count=3)
                    await page.keyboard.press("Backspace")
                    await page.fill(selector, query)
                    kw_found = True
                    break
            
            # Clear and type location
            if location:
                for selector in loc_selectors:
                    if await page.query_selector(selector):
                        await page.click(selector, click_count=3)
                        await page.keyboard.press("Backspace")
                        await page.fill(selector, location)
                        break

            if kw_found:
                # Press Enter or click search button
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
            
            # Wait for navigation with longer timeout
            try:
                await page.wait_for_load_state("networkidle", timeout=60000)
            except Exception as e:
                logger.warning(f"Network idle timeout during login: {e}")
                # Try waiting for a specific element instead
                try:
                    await page.wait_for_selector(".global-nav", timeout=10000)
                except Exception:
                    # If still fails, check if we're on the feed page
                    if "feed" in page.url or "jobs" in page.url:
                        logger.info("Login appears successful despite timeout")
                    else:
                        raise
            
            logger.info("Login successful")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            # Don't crash, continue with limited access
            logger.warning("Continuing without login - results may be limited")
            return # Exit login attempt if it failed
        
        if "checkpoint" in page.url:
            logger.warning("Security checkpoint encountered. Manual intervention might be needed.")
            # In a real app, we might wait for the user to solve it or use a solver service.
            await asyncio.sleep(30) 

    async def _extract_job_list(self, page: Page, limit: int) -> List[Dict[str, Any]]:
        jobs = []
        
        # Scroll to load more jobs
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # Target the main search results list specifically
        # LinkedIn search results use 'ul.jobs-search-results__list'
        job_cards = await page.query_selector_all("li.jobs-search-results__list-item, .scaffold-layout__list-item")
        
        # If no results with those specific classes, fall back but log it
        if not job_cards:
            logger.info("Specific search item classes not found, trying broader selectors...")
            job_cards = await page.query_selector_all(".job-card-container, .base-card")

        for card in job_cards[:limit]:
            try:
                # Optimized selectors for actual search result cards
                title_elem = await card.query_selector(".job-card-list__title, .base-search-card__title, .job-card-container__link")
                company_elem = await card.query_selector(".job-card-container__company-name, .base-search-card__subtitle, .job-card-container__primary-description")
                link_elem = await card.query_selector("a.job-card-list__title, a.base-card__full-link, a.job-card-container__link")
                
                if not title_elem or not link_elem:
                    continue

                title = (await title_elem.inner_text()).strip()
                company = (await company_elem.inner_text()).strip() if company_elem else "Unknown"
                url = await link_elem.get_attribute("href")
                if url and not url.startswith("http"):
                    url = self.base_url + url
                
                # Clean URL (remove tracking params)
                if "?" in url:
                    url = url.split("?")[0]
                
                external_id = url.split("/")[-2] if url.endswith("/") else url.split("/")[-1]

                jobs.append({
                    "external_id": external_id,
                    "title": title,
                    "company": company,
                    "url": url,
                    "source": "linkedin"
                })
            except Exception as e:
                logger.error(f"Error extracting card: {e}")

        return jobs

    async def _get_job_details(self, context: BrowserContext, url: str) -> Dict[str, Any]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Try multiple selectors for job description
            desc_selectors = [
                ".jobs-description", 
                "#job-details", 
                ".jobs-description-content", 
                ".jobs-box__html-content",
                ".show-more-less-html__markup"
            ]
            
            description = ""
            description_html = ""
            for selector in desc_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    elem = await page.query_selector(selector)
                    if elem:
                        description = await elem.inner_text()
                        description_html = await elem.inner_html()
                        if description.strip():
                            break
                except Exception:
                    continue

            # Location extraction
            loc_selectors = [
                ".jobs-unified-top-card__bullet", 
                ".topcard__flavor--bullet",
                ".job-details-jobs-unified-top-card__bullet",
                ".jobs-unified-top-card__workplace-type"
            ]
            location = ""
            for selector in loc_selectors:
                elem = await page.query_selector(selector)
                if elem:
                    location = await elem.inner_text()
                    if location.strip():
                        break
            
            # Extract work type from description and location
            work_type = "onsite"  # default
            combined_text = (description + " " + location).lower()
            
            if "remote" in combined_text:
                work_type = "remote"
            elif "hybrid" in combined_text:
                work_type = "hybrid"
            
            return {
                "description": description.strip(),
                "description_html": description_html.strip(),
                "location": location.strip(),
                "work_type": work_type,
                "scraped_at": datetime.now()
            }
        except Exception as e:
            logger.error(f"Error in _get_job_details for {url}: {e}")
            return {
                "description": "",
                "description_html": "",
                "location": "",
                "work_type": "unknown", # Default for error case
                "scraped_at": datetime.now()
            }
        finally:
            await page.close()
