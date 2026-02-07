from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from playwright.async_api import async_playwright, Page

from job_search.config import settings
from job_search.models import Application, ApplicationStatus, Job, UserProfile, Resume, ResumeVersion
from job_search.database import SessionLocal

logger = logging.getLogger(__name__)


class JobApplier:
    def __init__(self):
        self.headless = settings.browser_headless

    # ------------------------------------------------------------------
    # Resume tailoring (runs BEFORE browser launch)
    # ------------------------------------------------------------------

    async def _tailor_resume_for_job(
        self, resume: Optional[Resume], job: Job, app: Application, db
    ) -> str:
        """Tailor the resume for a specific job and return the file path to upload.

        Attempts LLM-based tailoring first (45 s timeout), then falls back to
        keyword-only reordering, and finally returns the original file if
        everything fails.
        """
        if not resume or not resume.parsed_data:
            return resume.file_path if resume else ""

        app.automation_log += "Tailoring resume for this job...\n"
        db.commit()

        try:
            from job_search.services.llm_client import get_llm_client
            from job_search.services.resume_tailor import ResumeTailor
            from job_search.services.resume_generator import ResumeGenerator
            from job_search.utils.text_processing import extract_keywords

            llm_client = get_llm_client()
            result = None

            if llm_client:
                tailor = ResumeTailor(llm_client)
                try:
                    result = await asyncio.wait_for(
                        tailor.tailor(
                            parsed_resume=resume.parsed_data,
                            job_description=job.description or "",
                            job_title=job.title,
                            company=job.company,
                        ),
                        timeout=45.0,
                    )
                    app.automation_log += "LLM tailoring succeeded.\n"
                except asyncio.TimeoutError:
                    app.automation_log += "LLM tailoring timed out — keyword fallback.\n"
                except Exception as e:
                    app.automation_log += f"LLM tailoring error ({e}) — keyword fallback.\n"

            # Keyword-only fallback
            if result is None:
                jd_keywords = extract_keywords(job.description or "")
                # ResumeTailor.tailor_keywords_only is a sync method, no LLM needed
                tailor = ResumeTailor(llm_client=llm_client)  # llm not used in fallback
                result = tailor.tailor_keywords_only(resume.parsed_data, jd_keywords)
                app.automation_log += "Using keyword-only tailoring.\n"

            db.commit()

            # Build tailored data by merging modified sections into original
            tailored_data = dict(resume.parsed_data)
            tailored_data.update(result.modified_sections)

            # Generate the file (PDF via WeasyPrint, or HTML fallback)
            generator = ResumeGenerator()
            output_path = generator.generate_pdf(
                tailored_data,
                output_filename=f"tailored_{resume.id}_job_{job.id}.pdf",
            )

            # Persist the version record
            version = ResumeVersion(
                base_resume_id=resume.id,
                job_id=job.id,
                file_path=str(output_path),
                tailoring_notes=result.tailoring_notes,
                keywords_added=result.keywords_added,
                sections_modified=result.sections_changed,
                llm_model_used=(llm_client.model if llm_client else "keyword-only"),
            )
            db.add(version)
            db.commit()
            db.refresh(version)

            app.resume_version_id = version.id
            kw_preview = ", ".join(result.keywords_added[:5]) if result.keywords_added else "skills reordered"
            app.automation_log += f"Tailored resume saved (version #{version.id}). [{kw_preview}]\n"
            db.commit()

            return str(output_path)

        except Exception as e:
            logger.warning(f"Resume tailoring failed, using original: {e}")
            app.automation_log += f"Tailoring failed — uploading original resume.\n"
            db.commit()
            return resume.file_path if resume else ""

    # ------------------------------------------------------------------
    # Main automation entry point
    # ------------------------------------------------------------------

    async def run_automation(self, application_id: int):
        """Main entry point: tailor resume, then open browser and apply."""
        db = SessionLocal()
        app = db.query(Application).filter(Application.id == application_id).first()
        if not app:
            logger.error(f"Application {application_id} not found")
            return

        job = app.job
        user = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
        resume = db.query(Resume).filter(Resume.is_primary == True).first()

        if not user:
            app.status = ApplicationStatus.FAILED
            app.error_message = "No user profile found. Please complete your profile first."
            db.commit()
            db.close()
            return

        try:
            app.status = ApplicationStatus.IN_PROGRESS
            app.automation_log = "Automation started...\n"
            db.commit()

            # ---- Step 1: Tailor resume (before launching browser) ----
            resume_path = await self._tailor_resume_for_job(resume, job, app, db)

            # ---- Step 2: Browser automation ----
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)

                storage_path = "data/browser_state/linkedin_state.json"
                context_params = {}
                if job.source == "linkedin" and os.path.exists(storage_path):
                    context_params["storage_state"] = storage_path

                context = await browser.new_context(**context_params)
                page = await context.new_page()

                url = job.apply_url or job.url
                app.automation_log += f"Navigating to {url}\n"
                db.commit()

                await page.goto(url, wait_until="domcontentloaded")

                if job.source == "linkedin" and job.is_easy_apply:
                    await self._handle_linkedin_easy_apply(page, user, resume_path, app, db)
                else:
                    await self._handle_generic_apply(page, user, resume_path, app, db)

                app.automation_log += "Automation finished. Browser open 60s for manual review.\n"
                db.commit()
                await asyncio.sleep(60)
                await browser.close()

        except Exception as e:
            logger.exception(f"Automation failed for application {application_id}")
            app.status = ApplicationStatus.FAILED
            app.error_message = str(e)
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # LinkedIn Easy Apply
    # ------------------------------------------------------------------

    async def _handle_linkedin_easy_apply(
        self, page: Page, user: UserProfile, resume_path: str, app: Application, db
    ):
        """Walk through the LinkedIn Easy Apply modal step-by-step."""
        try:
            apply_button = await page.wait_for_selector(
                ".jobs-apply-button--easy-apply", timeout=15000
            )
            await apply_button.click()
            await asyncio.sleep(2)

            for step in range(1, 11):
                header = await page.query_selector("h3, h2")
                header_text = (await header.inner_text()).lower() if header else ""
                app.automation_log += f"Step {step}: {header_text}\n"
                db.commit()

                # Fill text fields
                await self._fill_linkedin_fields(page, user)

                # Handle resume upload screen
                if "resume" in header_text and resume_path:
                    selected = await page.query_selector(
                        ".jobs-document-upload__container--selected"
                    )
                    if not selected:
                        file_input = await page.query_selector("input[type='file']")
                        if file_input:
                            await file_input.set_input_files(os.path.abspath(resume_path))
                            app.automation_log += f"Uploaded tailored resume: {os.path.basename(resume_path)}\n"
                            await asyncio.sleep(2)

                # Navigation
                footer = await page.query_selector(".jobs-s-apply-footer") or await page.query_selector("footer")
                if not footer:
                    app.automation_log += "No footer found. Manual intervention needed.\n"
                    break

                submit_btn = await footer.query_selector(
                    "button[aria-label*='Submit application']"
                )
                if submit_btn:
                    app.automation_log += "Reached Submit screen. Please review and click Submit in browser.\n"
                    app.notes = "Ready for final submission — review in browser."
                    break

                next_btn = await footer.query_selector(
                    "button[aria-label*='Next'], button[aria-label*='Review']"
                )
                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(2)
                else:
                    app.automation_log += "No Next button — check for missing required fields.\n"
                    break

            db.commit()

        except Exception as e:
            app.automation_log += f"LinkedIn Easy Apply error: {e}\n"
            raise

    # ------------------------------------------------------------------
    # Generic external apply
    # ------------------------------------------------------------------

    async def _handle_generic_apply(
        self, page: Page, user: UserProfile, resume_path: str, app: Application, db
    ):
        """Best-effort form filler for external career sites."""
        app.automation_log += "Identifying form fields on external site...\n"
        db.commit()

        fields = [
            {"label": "first name", "value": user.full_name.split()[0] if user.full_name else ""},
            {"label": "last name", "value": user.full_name.split()[-1] if user.full_name and len(user.full_name.split()) > 1 else ""},
            {"label": "email", "value": user.email or ""},
            {"label": "phone", "value": user.phone or ""},
            {"label": "linkedin", "value": user.linkedin_url or ""},
        ]

        for f in fields:
            try:
                sel = (
                    f"input[name*='{f['label']}'], "
                    f"input[id*='{f['label']}'], "
                    f"input[aria-label*='{f['label']}']"
                )
                el = await page.query_selector(sel)
                if el and f["value"]:
                    await el.fill(f["value"])
                    app.automation_log += f"Filled {f['label']}\n"
            except Exception:
                continue

        if resume_path:
            try:
                file_input = await page.query_selector("input[type='file']")
                if file_input:
                    await file_input.set_input_files(os.path.abspath(resume_path))
                    app.automation_log += f"Attached tailored resume: {os.path.basename(resume_path)}\n"
            except Exception:
                pass

        db.commit()

    # ------------------------------------------------------------------
    # LinkedIn field auto-fill helper
    # ------------------------------------------------------------------

    async def _fill_linkedin_fields(self, page: Page, user: UserProfile):
        """Fill common LinkedIn application text fields by label matching."""
        text_fields = {
            "first name": user.full_name.split()[0] if user.full_name else "",
            "last name": user.full_name.split()[-1] if user.full_name and len(user.full_name.split()) > 1 else "",
            "phone": user.phone or "",
            "email": user.email or "",
            "city": user.location or "",
        }

        for label, val in text_fields.items():
            if not val:
                continue
            inputs = await page.query_selector_all("input[type='text'], input[type='tel']")
            for input_el in inputs:
                id_val = (await input_el.get_attribute("id") or "").lower()
                label_el = await page.query_selector(f"label[for='{id_val}']")
                label_text = (await label_el.inner_text()).lower() if label_el else ""

                if label in label_text or label.replace(" ", "") in id_val:
                    curr_val = await input_el.input_value()
                    if not curr_val:
                        await input_el.fill(val)
