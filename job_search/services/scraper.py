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
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            ]
            context_args = {
                "user_agent": random.choice(user_agents),
                "viewport": {"width": 1280, "height": 800}
            }
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

                search_url = f"{self.base_url}/jobs/search/?keywords={safe_query}"
                if safe_location:
                    search_url += f"&location={safe_location}"
                
                if filter_params:
                    search_url += "&" + "&".join(filter_params)
                
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
                        # Poll for resolution
                        logger.info("Waiting for manual CAPTCHA resolution (120s timeout)...")
                        for _ in range(60): # 60 * 2s = 120s
                            if "checkpoint" not in page.url:
                                logger.info("Checkpoint passed! Saving session state immediately.")
                                # Ensure directory exists
                                self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                                await context.storage_state(path=str(self.storage_state))
                                break
                            await asyncio.sleep(2)
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
                sem = asyncio.Semaphore(3) # Limit concurrency to avoid blocking

                async def fetch_detail_with_sem(job):
                    if check_cancelled and check_cancelled():
                        return job

                    async with sem:
                        try:
                            # Add random small delay to jitter
                            await self._get_random_delay(0.5, 1.5)
                            detail = await self._get_job_details(context, job["url"])
                            job.update(detail)
                        except Exception as e:
                            logger.error(f"Failed to get details for {job['url']}: {e}")
                        return job

                # Execute in parallel with limited concurrency
                logger.info(f"Fetching details for {len(jobs)} jobs...")
                tasks = [fetch_detail_with_sem(job) for job in jobs]
                detailed_jobs = await asyncio.gather(*tasks)
                
                # Filter out jobs that failed completely
                final_jobs = [j for j in detailed_jobs if j.get("description")]
                logger.info(f"Successfully scraped {len(final_jobs)} jobs with full details.")

                return final_jobs

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
        selectors = [
            "li.jobs-search-results__list-item",
            ".scaffold-layout__list-item",
            ".job-card-container",
            ".base-card",
            "div[data-job-id]",
            "li[data-occludable-job-id]"
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
            # This is harder to extract company/title from, but we could try to get the parent card
            if all_links:
                # Deduplicate and take parents
                unique_links = []
                seen_urls = set()
                for link in all_links:
                    url = await link.get_attribute("href")
                    if url and "/jobs/view/" in url:
                        clean_url = url.split("?")[0]
                        if clean_url not in seen_urls:
                            seen_urls.add(clean_url)
                            unique_links.append(link)
                
                # Treat these links as "cards" for the loop below, it will try to find title/company inside/near them
                job_cards = unique_links

        for card in job_cards[:limit]:
            try:
                # Robust multi-selector approach for job cards
                title_selectors = [
                    ".job-card-list__title", 
                    ".base-search-card__title", 
                    ".job-card-container__link",
                    "h3.base-search-card__title",
                    "a.job-card-list__title"
                ]
                company_selectors = [
                    ".job-card-container__company-name", 
                    ".base-search-card__subtitle", 
                    ".job-card-container__primary-description",
                    "h4.base-search-card__subtitle"
                ]
                link_selectors = [
                    "a.job-card-list__title", 
                    "a.base-card__full-link", 
                    "a.job-card-container__link",
                    ".base-search-card__title-link"
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
                         title = (await card.inner_text()).strip()
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

        return jobs

    async def _get_job_details(self, context: BrowserContext, url: str) -> Dict[str, Any]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            # Try multiple selectors for job description
            desc_selectors = [
                 ".jobs-description", 
                 "#job-details", 
                 ".jobs-box__html-content",
                 ".show-more-less-html__markup",
                 ".description__text",
                 "section.description",
                 "div.job-description"
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
                ".topcard__flavor--bullet"
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
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()
            
            try:
                # Standardize URL
                if not url.startswith("http"):
                    url = "https://" + url
                
                await page.goto(url, wait_until="load", timeout=30000)
                await asyncio.sleep(2) # Wait for JS rendering
                
                # Evaluation script to find job-like links
                found_links = await page.evaluate("""({keywords, locations}) => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const jobs = [];
                    const seen = new Set();
                    
                    links.forEach(link => {
                        const text = link.innerText.trim();
                        const href = link.href;
                        if (!text || !href) return;

                        // Heuristics for job links
                        const looksLikeJob = (
                            text.length > 5 && 
                            text.length < 150 &&
                            /job|career|position|opening|role|apply|details|vacancy/i.test(href) &&
                            !/login|signup|privacy|term|cookie|blog|about|contact|press|help/i.test(href)
                        );

                        // Check keywords
                        const matchesKeyword = keywords.some(k => 
                            text.toLowerCase().includes(k.toLowerCase())
                        );

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
                    return jobs;
                }""", {"keywords": keywords, "locations": locations})

                logger.info(f"Heuristic scanner found {len(found_links)} potential candidates")
                
                # Filter and score
                scored_candidates = []
                for link in found_links:
                    score = 0
                    if link['matchesKeyword']: score += 2
                    if link['matchesLocation']: score += 3
                    
                    # If we have locations but this doesn't match, penalize or skip
                    if locations and not link['matchesLocation']:
                        score -= 1
                    
                    if score > 0:
                        scored_candidates.append((score, link))

                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                final_candidates = [j[1] for j in scored_candidates[:limit]]
                
                # Fetch details for found candidates
                scraped_jobs = []
                for candidate in final_candidates:
                    try:
                        detail_page = await context.new_page()
                        await detail_page.goto(candidate['url'], wait_until="domcontentloaded", timeout=15000)
                        
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
                            "title": candidate['title'].split('\\n')[0].strip(),
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

                return scraped_jobs

            finally:
                await browser.close()
