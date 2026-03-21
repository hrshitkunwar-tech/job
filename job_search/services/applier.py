from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import urllib.parse
from datetime import datetime
from typing import Any, Optional

from playwright.async_api import async_playwright, Page, Frame

from job_search.config import settings
from job_search.models import (
    Application,
    ApplicationStatus,
    Job,
    AutomationIssueEvent,
    UserProfile,
    Resume,
    ResumeVersion,
    SearchQuery,
)
from job_search.database import SessionLocal
from job_search.services.apply_url_resolver import resolve_official_apply_url

logger = logging.getLogger(__name__)


class JobApplier:
    def __init__(self):
        self.headless = settings.browser_headless
        self.default_mobile_number = "9319135101"
        self.default_phone_country_code = "+91"
        self.default_source_channel = "Social Media"
        self.default_source_platform = "LinkedIn"
        self.default_source_answer = "LinkedIn"
        self.default_postal_code = "560102"
        self.default_country = "India"
        self.default_city = "Bangalore"
        self.default_state = "Karnataka"
        self.default_address_line_1 = "HSR Layout"

    @staticmethod
    def _is_truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return False

    def _stop_requested(self, app: Optional[Application]) -> bool:
        if not app or not isinstance(getattr(app, "user_inputs", None), dict):
            return False
        return self._is_truthy(app.user_inputs.get("__stop_requested"))

    def _abort_if_stop_requested(self, db, app: Optional[Application], stage: str) -> bool:
        if not app:
            return False
        try:
            db.refresh(app)
        except Exception:
            pass
        if not self._stop_requested(app):
            return False
        payload = dict(app.user_inputs or {})
        stop_reason = (
            str(payload.get("__stop_reason") or "").strip()
            or "Automation stopped from UI."
        )
        app.status = ApplicationStatus.REVIEWED
        app.error_message = None
        app.notes = stop_reason
        app.automation_log = (app.automation_log or "") + f"Stop requested. Aborting at {stage}.\n"
        db.commit()
        return True

    def _normalize_mobile_number(self, raw: Optional[str]) -> str:
        """
        Return a 10-digit mobile number with no spaces.
        Preference order:
        1) digits extracted from provided value (last 10 digits),
        2) configured default mobile number.
        """
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(digits) >= 10:
            return digits[-10:]
        default_digits = "".join(ch for ch in self.default_mobile_number if ch.isdigit())
        if len(default_digits) == 10:
            return default_digits
        return "0000000000"

    @staticmethod
    def _clean_value(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _extract_name_parts(
        self,
        user: Optional[UserProfile],
        parsed_resume: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str, str]:
        parsed_resume = parsed_resume or {}
        full_name = (
            self._clean_value(getattr(user, "full_name", None))
            or self._clean_value(parsed_resume.get("name"))
            or self._clean_value(parsed_resume.get("full_name"))
            or "Candidate Kunwar"
        )
        parts = [p for p in full_name.split() if p]
        first_name = (parts[0] if parts else "Candidate").title()
        last_name = (
            parts[-1].title()
            if len(parts) > 1
            else ("Kunwar" if first_name.lower() == "candidate" else first_name.title())
        )
        full_name = " ".join(parts).strip() or f"{first_name} {last_name}"
        return full_name, first_name, last_name

    def _location_parts(self, location_text: str) -> tuple[str, str, str]:
        text = self._clean_value(location_text)
        if not text:
            return self.default_city, self.default_state, self.default_country
        lowered = text.lower()
        city_to_state = {
            "new delhi": "Delhi",
            "delhi": "Delhi",
            "gurgaon": "Haryana",
            "gurugram": "Haryana",
            "noida": "Uttar Pradesh",
            "mumbai": "Maharashtra",
            "bengaluru": "Karnataka",
            "bangalore": "Karnataka",
            "hyderabad": "Telangana",
            "chennai": "Tamil Nadu",
            "pune": "Maharashtra",
            "kolkata": "West Bengal",
            "ahmedabad": "Gujarat",
            "jaipur": "Rajasthan",
        }
        city_guess = ""
        state_guess = ""
        for city, state in city_to_state.items():
            if city in lowered:
                city_guess = city.title().replace("Bangalore", "Bengaluru")
                state_guess = state
                break
        if not city_guess:
            # Use first tokenized location segment as city fallback.
            city_guess = text.split(",")[0].strip().title() or self.default_city
        if not state_guess:
            state_guess = self.default_state
        country_guess = self.default_country
        if any(tok in lowered for tok in ("usa", "united states", "us,", "u.s.")):
            country_guess = "United States"
        elif any(tok in lowered for tok in ("uk", "united kingdom", "england")):
            country_guess = "United Kingdom"
        return city_guess, state_guess, country_guess

    def _collect_previous_application_answers(self, db) -> dict[str, Any]:
        learned: dict[str, Any] = {}
        if db is None:
            return learned
        try:
            rows = db.query(Application).order_by(Application.id.desc()).limit(200).all()
        except Exception:
            return learned
        for row in rows:
            for payload in (
                row.user_inputs if isinstance(getattr(row, "user_inputs", None), dict) else {},
                row.blocker_details if isinstance(getattr(row, "blocker_details", None), dict) else {},
            ):
                for key, value in payload.items():
                    if not isinstance(key, str):
                        continue
                    normalized = self._normalize_input_key(key)
                    if not normalized or normalized.startswith("__"):
                        continue
                    if isinstance(value, (dict, list, tuple, set)):
                        continue
                    cleaned = self._clean_value(value)
                    if not cleaned:
                        continue
                    if normalized not in learned:
                        learned[normalized] = cleaned
        return learned

    def _best_learned_values(self, user: Optional[UserProfile]) -> dict[str, str]:
        if not user or not isinstance(getattr(user, "application_answers", None), dict):
            return {}
        root = user.application_answers or {}
        learning = root.get("__learning")
        if not isinstance(learning, dict):
            return {}
        field_success = learning.get("field_success")
        if not isinstance(field_success, dict):
            return {}
        out: dict[str, str] = {}
        for raw_key, value_map in field_success.items():
            if not isinstance(raw_key, str):
                continue
            if not isinstance(value_map, dict):
                continue
            best_value = None
            best_count = -1
            for raw_value, count in value_map.items():
                if not isinstance(raw_value, str):
                    continue
                try:
                    n = int(count)
                except Exception:
                    n = 0
                if n > best_count and raw_value.strip():
                    best_count = n
                    best_value = raw_value.strip()
            if best_value:
                out[self._normalize_input_key(raw_key)] = best_value
        return out

    def _learn_from_application_run(
        self,
        db,
        user: Optional[UserProfile],
        app: Optional[Application],
        job: Optional[Job],
        runtime_overrides: Optional[dict[str, Any]] = None,
        value_sources: Optional[dict[str, str]] = None,
    ) -> None:
        if not user or not app:
            return
        root = user.application_answers if isinstance(user.application_answers, dict) else {}
        learning = root.get("__learning")
        if not isinstance(learning, dict):
            learning = {}

        totals = learning.get("totals")
        if not isinstance(totals, dict):
            totals = {"runs": 0, "submitted": 0, "reviewed": 0, "failed": 0}
        totals["runs"] = int(totals.get("runs", 0)) + 1
        status_value = (
            app.status.value if hasattr(app.status, "value") else str(getattr(app, "status", "unknown"))
        ).lower()
        if status_value == "submitted":
            totals["submitted"] = int(totals.get("submitted", 0)) + 1
        elif status_value == "failed":
            totals["failed"] = int(totals.get("failed", 0)) + 1
        else:
            totals["reviewed"] = int(totals.get("reviewed", 0)) + 1
        learning["totals"] = totals

        blocker_counts = learning.get("blocker_counts")
        if not isinstance(blocker_counts, dict):
            blocker_counts = {}
        blocker_reason = None
        if isinstance(getattr(app, "blocker_details", None), dict):
            blocker_reason = app.blocker_details.get("reason")
        if blocker_reason:
            key = str(blocker_reason).strip().lower()
            blocker_counts[key] = int(blocker_counts.get(key, 0)) + 1
        learning["blocker_counts"] = blocker_counts

        domain_stats = learning.get("domain_stats")
        if not isinstance(domain_stats, dict):
            domain_stats = {}
        domain = None
        try:
            target_url = (job.apply_url or job.url or "") if job else ""
            domain = (urllib.parse.urlparse(target_url).hostname or "").lower().strip()
        except Exception:
            domain = None
        if domain:
            node = domain_stats.get(domain)
            if not isinstance(node, dict):
                node = {"runs": 0, "submitted": 0, "failed": 0, "reviewed": 0}
            node["runs"] = int(node.get("runs", 0)) + 1
            if status_value == "submitted":
                node["submitted"] = int(node.get("submitted", 0)) + 1
            elif status_value == "failed":
                node["failed"] = int(node.get("failed", 0)) + 1
            else:
                node["reviewed"] = int(node.get("reviewed", 0)) + 1
            domain_stats[domain] = node
        learning["domain_stats"] = domain_stats

        missing_required = learning.get("missing_required_inputs")
        if not isinstance(missing_required, dict):
            missing_required = {}
        if isinstance(getattr(app, "blocker_details", None), dict):
            required_inputs = app.blocker_details.get("required_inputs")
            if isinstance(required_inputs, list):
                for row in required_inputs:
                    if not isinstance(row, dict):
                        continue
                    raw_key = str(row.get("key", "")).strip()
                    if not raw_key:
                        continue
                    key = self._normalize_input_key(raw_key)
                    missing_required[key] = int(missing_required.get(key, 0)) + 1
        learning["missing_required_inputs"] = missing_required

        field_success = learning.get("field_success")
        if not isinstance(field_success, dict):
            field_success = {}
        if status_value == "submitted":
            for raw_key, raw_value in (runtime_overrides or {}).items():
                key = self._normalize_input_key(str(raw_key))
                if not key or key.startswith("__"):
                    continue
                if isinstance(raw_value, (dict, list, tuple, set)):
                    continue
                value = self._clean_value(raw_value)
                if not value:
                    continue
                value_map = field_success.get(key)
                if not isinstance(value_map, dict):
                    value_map = {}
                value_map[value] = int(value_map.get(value, 0)) + 1
                field_success[key] = value_map
        learning["field_success"] = field_success

        snapshots = learning.get("run_snapshots")
        if not isinstance(snapshots, list):
            snapshots = []
        snapshot = {
            "application_id": app.id,
            "job_id": app.job_id,
            "status": status_value,
            "reason": blocker_reason or (app.error_message or app.notes or ""),
            "created_at": datetime.utcnow().isoformat(),
        }
        snapshots.append(snapshot)
        learning["run_snapshots"] = snapshots[-120:]
        learning["updated_at"] = datetime.utcnow().isoformat()

        root["__learning"] = learning
        user.application_answers = root
        try:
            db.commit()
            db.refresh(user)
        except Exception:
            db.rollback()

    def _build_runtime_answer_overrides(
        self,
        user: Optional[UserProfile],
        resume: Optional[Resume],
        job: Optional[Job],
        app: Optional[Application],
        db,
        base_overrides: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """
        Resolve answers with strict priority:
        explicit overrides -> profile memory -> resume/LinkedIn context -> previous applications -> generated fallback.
        """
        overrides: dict[str, Any] = {}
        value_sources: dict[str, str] = {}

        def put(key: str, value: Any, source: str, force: bool = False):
            nk = self._normalize_input_key(key)
            cleaned = self._clean_value(value)
            if not nk or not cleaned:
                return
            if force or nk not in overrides or self._clean_value(overrides.get(nk)) == "":
                overrides[nk] = cleaned
                value_sources[nk] = source

        explicit = base_overrides or {}
        for key, value in explicit.items():
            put(str(key), value, "explicit_override", force=True)

        parsed_resume = resume.parsed_data if resume and isinstance(resume.parsed_data, dict) else {}
        full_name, first_name, last_name = self._extract_name_parts(user, parsed_resume)

        # Profile memory / LinkedIn context
        if user:
            put("full_name", user.full_name, "profile")
            put("first_name", first_name, "profile")
            put("last_name", last_name, "profile")
            put("local_given_name", first_name, "profile")
            put("local_family_name", last_name, "profile")
            put("email", user.email, "profile")
            put("official_email", user.email, "profile")
            put("phone", self._normalize_mobile_number(user.phone), "profile")
            put("linkedin_url", user.linkedin_url, "profile")
            put("location", user.location, "profile")
            put("expected_ctc_lpa", user.expected_ctc_lpa, "profile")
            put("current_ctc_lpa", user.current_ctc_lpa, "profile")
            put("notice_period_days", user.notice_period_days, "profile")
            put("work_authorization", user.work_authorization, "profile")
            put("can_join_immediately", self._as_yes_no(user.can_join_immediately), "profile")
            put("willing_to_relocate", self._as_yes_no(user.willing_to_relocate), "profile")
            put("requires_sponsorship", self._as_yes_no(user.requires_sponsorship), "profile")
            if isinstance(user.skills, list) and user.skills:
                put("skills_csv", ", ".join([str(s).strip() for s in user.skills if str(s).strip()][:15]), "linkedin_context")
            if user.summary:
                put("profile_summary", user.summary, "linkedin_context")
            if user.headline:
                put("headline", user.headline, "linkedin_context")
            if isinstance(user.experience, list) and user.experience:
                put("total_experience_years", max(1, len(user.experience)), "linkedin_context")

        # Resume context
        put("full_name", parsed_resume.get("name") or parsed_resume.get("full_name"), "resume")
        put("first_name", first_name, "resume")
        put("last_name", last_name, "resume")
        put("email", parsed_resume.get("email"), "resume")
        put("phone", self._normalize_mobile_number(parsed_resume.get("phone")), "resume")
        put("linkedin_url", parsed_resume.get("linkedin_url") or parsed_resume.get("linkedin"), "resume")
        put("location", parsed_resume.get("location"), "resume")
        if isinstance(parsed_resume.get("skills"), list) and parsed_resume.get("skills"):
            put("skills_csv", ", ".join([str(s).strip() for s in parsed_resume.get("skills", []) if str(s).strip()][:15]), "resume")

        # Learned successful values from prior completed submissions.
        learned_values = self._best_learned_values(user)
        for key, value in learned_values.items():
            put(key, value, "learned_success_pattern")

        # Historical memory from prior applications
        learned = self._collect_previous_application_answers(db)
        for key, value in learned.items():
            put(key, value, "previous_applications")

        location_seed = (
            self._clean_value(overrides.get("location"))
            or self._clean_value(parsed_resume.get("location"))
            or self._clean_value(getattr(user, "location", None))
            or self._clean_value(getattr(job, "location", None))
        )
        city, state, country = self._location_parts(location_seed)
        inferred_postal = (
            self._postal_code_from_location_text(location_seed)
            or self._postal_code_from_location_text(self._clean_value(getattr(job, "location", None)))
            or self.default_postal_code
        )

        # Generated but valid fallbacks for mandatory contact fields.
        put("full_name", full_name, "generated_placeholder")
        put("first_name", first_name, "generated_placeholder")
        put("last_name", last_name, "generated_placeholder")
        put("local_given_name", first_name, "generated_placeholder")
        put("local_family_name", last_name, "generated_placeholder")
        put("phone", self._normalize_mobile_number(overrides.get("phone")), "generated_placeholder")
        put("phone_country_code", self.default_phone_country_code, "generated_placeholder")
        put("phone_type", "mobile", "generated_placeholder")
        put("phone_extension", "0", "generated_placeholder")
        put("location", f"{city}, {state}, {country}", "generated_placeholder")
        put("address_line_1", self.default_address_line_1, "generated_placeholder")
        put("address_line_2", "NA", "generated_placeholder")
        put("city", city, "generated_placeholder")
        put("state", state, "generated_placeholder")
        put("country", country, "generated_placeholder")
        put("postal_code", inferred_postal, "generated_placeholder")
        put("zip_code", inferred_postal, "generated_placeholder")
        put("pincode", inferred_postal, "generated_placeholder")
        put("hear_about_us", self.default_source_channel, "generated_placeholder")
        put("hear_about_us_platform", self.default_source_platform, "generated_placeholder")
        put("source_of_application", self.default_source_platform, "generated_placeholder")
        if self._clean_value(overrides.get("notice_period_days")) == "":
            put("notice_period_days", "0", "generated_placeholder")
        if self._clean_value(overrides.get("can_join_immediately")) == "":
            put("can_join_immediately", "Yes", "generated_placeholder")
        if self._clean_value(overrides.get("willing_to_relocate")) == "":
            put("willing_to_relocate", "Yes", "generated_placeholder")
        if self._clean_value(overrides.get("requires_sponsorship")) == "":
            put("requires_sponsorship", "No", "generated_placeholder")
        if self._clean_value(overrides.get("applied_before")) == "":
            put("applied_before", "No", "generated_placeholder")
        if self._clean_value(overrides.get("worked_here_before")) == "":
            put("worked_here_before", "No", "generated_placeholder")

        return overrides, value_sources

    async def _infer_field_context_text(self, element) -> str:
        """Best-effort extraction of nearby label/context text for ATS widgets."""
        try:
            text = await element.evaluate(
                """
                (el) => {
                  const parts = [];
                  const directAttrs = ["aria-label", "placeholder", "name", "id"];
                  for (const a of directAttrs) {
                    const v = (el.getAttribute && el.getAttribute(a)) || "";
                    if (v) parts.push(v);
                  }
                  if (el.labels && el.labels.length) {
                    for (const l of Array.from(el.labels)) {
                      const t = (l.innerText || "").trim();
                      if (t) parts.push(t);
                    }
                  }
                  let node = el;
                  for (let i = 0; i < 5 && node; i++) {
                    const container = node.closest?.("[data-automation-id='formField'], [data-automation-id='multiselectInputContainer'], [role='group'], fieldset");
                    const scope = container || node.parentElement;
                    if (!scope) break;
                    const labelEl = scope.querySelector?.("[data-automation-id='formFieldLabel'], label, legend, [aria-label]");
                    if (labelEl) {
                      const t = (labelEl.innerText || labelEl.getAttribute?.("aria-label") || "").trim();
                      if (t) parts.push(t);
                    }
                    node = scope.parentElement;
                  }
                  return Array.from(new Set(parts.map(p => (p || "").trim()).filter(Boolean))).join(" ");
                }
                """
            )
            return (text or "").strip()
        except Exception:
            return ""

    def _postal_code_from_location_text(self, location_text: str) -> Optional[str]:
        text = (location_text or "").strip()
        if not text:
            return None
        # Prefer explicit postal code if already present.
        direct = re.search(r"\b(\d{6})\b", text)
        if direct:
            return direct.group(1)
        city_map = {
            "hsr layout": "560102",
            "new delhi": "110001",
            "delhi": "110001",
            "gurgaon": "122001",
            "gurugram": "122001",
            "noida": "201301",
            "mumbai": "400001",
            "bengaluru": "560102",
            "bangalore": "560102",
            "hyderabad": "500001",
            "chennai": "600001",
            "pune": "411001",
            "kolkata": "700001",
            "ahmedabad": "380001",
            "jaipur": "302001",
        }
        lowered = text.lower()
        for city, code in city_map.items():
            if city in lowered:
                return code
        return None

    def _augment_overrides_with_defaults(
        self,
        user: UserProfile,
        overrides: Optional[dict[str, Any]] = None,
        job: Optional[Job] = None,
    ) -> dict[str, Any]:
        merged = dict(overrides or {})
        full_name = (user.full_name or "").strip()
        name_parts = [p for p in full_name.split() if p]
        first_name = (name_parts[0] if name_parts else "Candidate").title()
        last_name = (name_parts[-1] if len(name_parts) > 1 else (name_parts[0] if name_parts else "Kunwar")).title()
        merged.setdefault("first_name", first_name)
        merged.setdefault("last_name", last_name)
        merged.setdefault("local_given_name", first_name)
        merged.setdefault("local_family_name", last_name)
        merged.setdefault("phone", self._normalize_mobile_number(user.phone))
        merged.setdefault("phone_type", "mobile")
        merged.setdefault("phone_country_code", self.default_phone_country_code)
        merged.setdefault("phone_extension", "0")
        merged.setdefault("hear_about_us", self.default_source_channel)
        merged.setdefault("hear_about_us_platform", self.default_source_platform)
        job_location = ""
        try:
            if job is not None:
                job_location = (getattr(job, "location", "") or "").strip()
        except Exception:
            job_location = ""
        inferred_postal = (
            self._postal_code_from_location_text(job_location)
            or self._postal_code_from_location_text(user.location or "")
            or self.default_postal_code
        )
        city, state, country = self._location_parts(job_location or user.location or "")
        merged.setdefault("address_line_1", self.default_address_line_1)
        merged.setdefault("address_line_2", "NA")
        merged.setdefault("city", city)
        merged.setdefault("state", state)
        merged.setdefault("country", country)
        merged.setdefault("postal_code", inferred_postal)
        merged.setdefault("zip_code", inferred_postal)
        merged.setdefault("pincode", inferred_postal)
        return merged

    async def _diagnose_and_fill_known_portal_blockers(
        self,
        page: Page,
        user: UserProfile,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Lightweight self-healing pass for common ATS blockers.
        Uses page diagnostics to pre-seed targeted answers and retries fill logic.
        """
        try:
            text = ((await page.inner_text("body")) or "").lower()
        except Exception:
            text = ""
        if not text:
            return 0

        seeded = self._augment_overrides_with_defaults(
            user,
            answer_overrides,
            job=(app.job if app else None),
        )
        diagnostics_triggered = False
        needs_hear_about_fix = False
        needs_previous_email_fix = False
        needs_worked_before_fix = False

        if any(k in text for k in ("how did you hear about us", "hear about us")):
            diagnostics_triggered = True
            needs_hear_about_fix = True
            seeded["hear_about_us"] = self.default_source_channel
            seeded["hear_about_us_platform"] = self.default_source_platform
        if any(
            k in text
            for k in (
                "have you previously worked for",
                "previously worked for",
                "worked for this company",
                "worked for any subsidiary",
                "worked here before",
            )
        ):
            diagnostics_triggered = True
            needs_worked_before_fix = True
            seeded["worked_here_before"] = "No"
        if any(
            k in text
            for k in (
                "previous email",
                "email in trend micro",
                "invalid email address format",
                "must be a valid email",
            )
        ):
            diagnostics_triggered = True
            needs_previous_email_fix = True
            seeded["previous_company_email"] = (user.email or "").strip()
        if "postal code must be 6 digits" in text or "postal code is required" in text:
            diagnostics_triggered = True
            seeded["postal_code"] = self.default_postal_code
        if "given name" in text:
            diagnostics_triggered = True
            seeded.setdefault("first_name", seeded.get("local_given_name") or "Candidate")
            seeded.setdefault("local_given_name", seeded.get("first_name") or "Candidate")
        if "family name" in text or "last name" in text:
            diagnostics_triggered = True
            seeded.setdefault("last_name", seeded.get("local_family_name") or "Kunwar")
            seeded.setdefault("local_family_name", seeded.get("last_name") or "Kunwar")
        if "phone" in text or "mobile" in text:
            diagnostics_triggered = True
            seeded["phone"] = self._normalize_mobile_number(user.phone)
            seeded["phone_type"] = "mobile"
            seeded["phone_country_code"] = self.default_phone_country_code

        if not diagnostics_triggered:
            return 0

        filled = 0
        scopes = self._iter_scopes_prioritized(page)
        # Some ATS portals keep invalid values populated; force-correct known problematic fields.
        for scope in scopes:
            try:
                if needs_hear_about_fix:
                    filled += await self._fill_prompt_dropdown_by_meta(
                        scope,
                        aliases=[
                            "how did you hear about us",
                            "hear about us",
                            "where did you hear",
                            "source of application",
                            "job source",
                            "referral source",
                        ],
                        preferred_values=[
                            self.default_source_platform,
                            "linkedin",
                            self.default_source_channel,
                            "social media",
                        ],
                    )
                if needs_worked_before_fix:
                    filled += await self._fill_prompt_dropdown_by_meta(
                        scope,
                        aliases=[
                            "have you previously worked for",
                            "previously worked for",
                            "worked for this company",
                            "worked for any subsidiary",
                            "worked here before",
                        ],
                        preferred_values=["no", "no, i have not", "never"],
                    )
                if "postal_code" in seeded and (
                    "postal code must be 6 digits" in text or "postal code is required" in text
                ):
                    filled += await self._force_fill_external_field(
                        scope,
                        ["postal code", "zip code", "zipcode", "pin code", "pincode"],
                        str(seeded["postal_code"]),
                    )
                if "phone" in seeded and ("phone" in text or "mobile" in text):
                    filled += await self._force_fill_external_field(
                        scope,
                        ["phone", "mobile", "telephone", "contact number"],
                        self._normalize_mobile_number(str(seeded["phone"])),
                    )
                if any(k in text for k in ("how did you hear about us", "hear about us")):
                    filled += await self._force_fill_external_field(
                        scope,
                        ["how did you hear about us", "hear about us", "source of application", "job source"],
                        self.default_source_platform,
                    )
                if needs_previous_email_fix and seeded.get("previous_company_email"):
                    filled += await self._force_fill_external_field(
                        scope,
                        [
                            "previous email",
                            "email in trend micro",
                            "former email",
                            "old email",
                            "previous company email",
                        ],
                        str(seeded["previous_company_email"]),
                    )
            except Exception:
                continue

        for scope in scopes:
            try:
                filled += await self._fill_linkedin_modal_minimum_fields(
                    scope, user, answer_overrides=seeded
                )
            except Exception:
                continue
        for scope in scopes:
            try:
                filled += await self._fill_non_native_dropdowns(
                    scope, user, answer_overrides=seeded
                )
            except Exception:
                continue
        if filled:
            app.automation_log += (
                f"Diagnostic resolver filled {filled} additional field(s) from portal error hints.\n"
            )
            try:
                self._record_issue_event(
                    db,
                    app,
                    app.job if app else None,
                    user,
                    "Auto-diagnosed blocker hints and applied targeted defaults.",
                    event_type="resolved",
                )
            except Exception:
                pass
            db.commit()
        return filled

    async def _fill_prompt_dropdown_by_meta(
        self,
        scope: Page | Frame,
        aliases: list[str],
        preferred_values: list[str],
    ) -> int:
        """
        Fill prompt-style dropdowns (not native <select>) used heavily in Workday.
        Matches fields by contextual meta text and picks the preferred option.
        """
        try:
            root_page = scope.page if isinstance(scope, Frame) else scope
        except Exception:
            root_page = scope  # type: ignore

        alias_tokens = [a.strip().lower() for a in aliases if a and a.strip()]
        preferred_tokens = [p.strip().lower() for p in preferred_values if p and p.strip()]
        if not alias_tokens or not preferred_tokens:
            return 0

        selectors = [
            "input[role='combobox']",
            "input[aria-autocomplete='list']",
            "[data-automation-id='promptSearchInput']",
            "[data-automation-id='searchBox']",
            "[role='combobox']",
            "button[aria-haspopup='listbox']",
            "[role='button'][aria-haspopup='listbox']",
        ]
        filled = 0
        seen_meta: set[str] = set()

        for sel in selectors:
            try:
                candidates = await scope.query_selector_all(sel)
            except Exception:
                continue
            for cand in candidates[:100]:
                try:
                    if not await cand.is_visible():
                        continue
                    meta_parts = []
                    for attr in ("name", "id", "aria-label", "placeholder", "title"):
                        try:
                            meta_parts.append((await cand.get_attribute(attr) or "").strip())
                        except Exception:
                            continue
                    try:
                        meta_parts.append(((await cand.inner_text()) or "").strip())
                    except Exception:
                        pass
                    try:
                        meta_parts.append(await self._infer_field_context_text(cand))
                    except Exception:
                        pass
                    meta = " ".join([m for m in meta_parts if m]).lower().strip()
                    if not meta:
                        continue
                    if meta in seen_meta:
                        continue
                    seen_meta.add(meta)
                    if not any(tok in meta for tok in alias_tokens):
                        continue

                    try:
                        await cand.click(timeout=1500, force=True)
                    except Exception:
                        continue
                    await asyncio.sleep(0.35)

                    # Type into prompt inputs when possible to reveal matching options.
                    cand_tag = ((await cand.evaluate("e => e.tagName")) or "").lower()
                    if cand_tag == "input":
                        try:
                            await cand.fill(preferred_values[0])
                            await asyncio.sleep(0.35)
                        except Exception:
                            pass

                    options = root_page.locator(
                        "[role='option'], [data-automation-id='promptOption'], li[role='option']"
                    )
                    option_count = min(await options.count(), 40)
                    chosen_idx = None
                    for pref in preferred_tokens:
                        for idx in range(option_count):
                            try:
                                opt = options.nth(idx)
                                if not await opt.is_visible():
                                    continue
                                txt = ((await opt.inner_text()) or "").strip().lower()
                                if txt and pref in txt:
                                    chosen_idx = idx
                                    break
                            except Exception:
                                continue
                        if chosen_idx is not None:
                            break

                    if chosen_idx is not None:
                        await options.nth(chosen_idx).click(timeout=1500, force=True)
                        filled += 1
                        await asyncio.sleep(0.3)
                        continue

                    # Fallback: commit typed value for prompt fields.
                    if cand_tag == "input":
                        try:
                            await cand.press("Enter")
                            filled += 1
                            await asyncio.sleep(0.25)
                        except Exception:
                            pass
                except Exception:
                    continue

        # Workday fallback: find field by label text and interact with the nearest prompt input/button.
        if filled == 0:
            for alias in alias_tokens:
                try:
                    xpath_candidates = await root_page.query_selector_all(
                        "xpath=//*[contains(translate(normalize-space(.),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{alias}')]"
                    )
                except Exception:
                    xpath_candidates = []
                for node in xpath_candidates[:8]:
                    try:
                        if not await node.is_visible():
                            continue
                    except Exception:
                        continue
                    for rel_sel in (
                        "xpath=following::input[1]",
                        "xpath=following::*[@role='combobox'][1]",
                        "xpath=following::button[@aria-haspopup='listbox'][1]",
                    ):
                        try:
                            target = await node.query_selector(rel_sel)
                        except Exception:
                            target = None
                        if not target:
                            continue
                        try:
                            if not await target.is_visible():
                                continue
                            await target.click(timeout=1500, force=True)
                            await asyncio.sleep(0.25)
                            tag = ((await target.evaluate("e => e.tagName")) or "").lower()
                            if tag == "input":
                                try:
                                    await target.fill(preferred_values[0])
                                    await asyncio.sleep(0.25)
                                except Exception:
                                    pass
                            options = root_page.locator(
                                "[role='option'], [data-automation-id='promptOption'], li[role='option']"
                            )
                            option_count = min(await options.count(), 40)
                            chosen_idx = None
                            for pref in preferred_tokens:
                                for idx in range(option_count):
                                    try:
                                        opt = options.nth(idx)
                                        if not await opt.is_visible():
                                            continue
                                        txt = ((await opt.inner_text()) or "").strip().lower()
                                        if txt and pref in txt:
                                            chosen_idx = idx
                                            break
                                    except Exception:
                                        continue
                                if chosen_idx is not None:
                                    break
                            if chosen_idx is not None:
                                await options.nth(chosen_idx).click(timeout=1500, force=True)
                                return 1
                            if tag == "input":
                                try:
                                    await target.press("Enter")
                                    return 1
                                except Exception:
                                    pass
                        except Exception:
                            continue

        return filled

    @staticmethod
    def _is_supported_resume_upload(path: str) -> bool:
        """Return True if file extension is usually accepted by ATS resume uploads."""
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower().strip()
        return ext in {".pdf", ".doc", ".docx", ".rtf", ".txt", ".odt"}

    def _coerce_resume_upload_path(
        self,
        preferred_path: str,
        base_resume: Optional[Resume],
        app: Optional[Application] = None,
        db=None,
    ) -> str:
        """
        Ensure we never upload unsuitable resume artifacts (e.g. generated HTML fallback files).
        """
        preferred = (preferred_path or "").strip()
        if preferred and self._is_supported_resume_upload(preferred) and os.path.exists(preferred):
            return preferred

        fallback = (base_resume.file_path or "").strip() if base_resume else ""
        if fallback and self._is_supported_resume_upload(fallback) and os.path.exists(fallback):
            if app is not None and db is not None:
                app.automation_log += (
                    f"Tailored resume artifact is not ATS-uploadable; falling back to base resume: {os.path.basename(fallback)}\n"
                )
                db.commit()
            return fallback

        if preferred and os.path.exists(preferred):
            # Last resort: keep preferred path if nothing better exists.
            return preferred
        return fallback or preferred

    def _build_submission_audit_payload(
        self,
        app: Application,
        job: Optional[Job],
        resume_path: str,
        runtime_overrides: Optional[dict[str, Any]],
        value_sources: Optional[dict[str, str]],
    ) -> dict[str, Any]:
        values = runtime_overrides or {}
        sources = value_sources or {}
        phone = self._normalize_mobile_number(values.get("phone"))
        location = self._clean_value(values.get("location"))
        city = self._clean_value(values.get("city"))
        state = self._clean_value(values.get("state"))
        country = self._clean_value(values.get("country") or self.default_country)
        postal = self._clean_value(values.get("postal_code") or values.get("zip_code") or values.get("pincode"))
        address = self._clean_value(values.get("address_line_1"))
        contact_source = "real_profile_or_history"
        if any(sources.get(k) == "generated_placeholder" for k in ("phone", "location", "postal_code", "address_line_1", "city", "state", "country")):
            contact_source = "generated_fallback_used"
        payload = {
            "job_title": (job.title if job else ""),
            "company": (job.company if job else ""),
            "contact_data_mode": contact_source,
            "contact_data_sources": {
                "phone": sources.get("phone", "unknown"),
                "location": sources.get("location", "unknown"),
                "address_line_1": sources.get("address_line_1", "unknown"),
                "city": sources.get("city", "unknown"),
                "state": sources.get("state", "unknown"),
                "country": sources.get("country", "unknown"),
                "postal_code": sources.get("postal_code", "unknown"),
            },
            "entered_values": {
                "phone_country_code": self._clean_value(values.get("phone_country_code") or self.default_phone_country_code),
                "phone_type": self._clean_value(values.get("phone_type") or "mobile"),
                "phone": phone,
                "address_line_1": address,
                "city": city,
                "state": state,
                "country": country,
                "postal_code": postal,
                "location": location,
            },
            "resume_submitted": os.path.basename(resume_path or ""),
            "resume_path": resume_path or "",
            "resume_version_id": app.resume_version_id,
            "final_submission_confirmed": bool(app.status == ApplicationStatus.SUBMITTED),
            "status": app.status.value if hasattr(app.status, "value") else str(app.status),
            "confirmation_status": app.notes or app.error_message or "",
            "captured_at": datetime.utcnow().isoformat(),
        }
        return payload

    def _persist_submission_audit(
        self,
        app: Application,
        job: Optional[Job],
        resume_path: str,
        runtime_overrides: Optional[dict[str, Any]],
        value_sources: Optional[dict[str, str]],
    ) -> None:
        payload = self._build_submission_audit_payload(
            app=app,
            job=job,
            resume_path=resume_path,
            runtime_overrides=runtime_overrides,
            value_sources=value_sources,
        )
        existing = app.user_inputs if isinstance(app.user_inputs, dict) else {}
        history = existing.get("__submission_audit_history")
        if not isinstance(history, list):
            history = []
        history.append(payload)
        existing["__submission_audit"] = payload
        existing["__submission_audit_history"] = history[-100:]
        existing["__last_runtime_values"] = dict(runtime_overrides or {})
        existing["__last_runtime_value_sources"] = dict(value_sources or {})
        app.user_inputs = existing
        app.automation_log = (app.automation_log or "") + (
            "Structured submission audit captured: "
            f"{payload['job_title']} @ {payload['company']} | "
            f"submit_confirmed={payload['final_submission_confirmed']}.\n"
        )

    @staticmethod
    def _linkedin_storage_state_path() -> Optional[str]:
        # Backward compatibility: support both historical filenames.
        candidates = [
            "data/browser_state/linkedin_state.json",
            "data/browser_state/linkedin.json",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def _external_storage_state_path(url: str) -> Optional[str]:
        """Per-domain storage state for non-LinkedIn challenge-gated portals."""
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower().strip()
        except Exception:
            host = ""
        if not host:
            return None
        safe = "".join(ch if ch.isalnum() else "_" for ch in host).strip("_")
        if not safe:
            return None
        os.makedirs("data/browser_state", exist_ok=True)
        return f"data/browser_state/external_{safe}.json"

    async def _bootstrap_linkedin_session(
        self,
        app: Application,
        db,
        timeout_seconds: int = 180,
    ) -> bool:
        """
        Open a visible LinkedIn login window and save session storage state.
        Used only when no saved session and no credentials are configured.
        """
        app.automation_log += (
            "No LinkedIn session found. Opening browser window for LinkedIn login (waiting up to 3 minutes)...\n"
        )
        db.commit()

        target_state_path = "data/browser_state/linkedin_state.json"
        legacy_state_path = "data/browser_state/linkedin.json"
        os.makedirs("data/browser_state", exist_ok=True)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

                deadline = asyncio.get_running_loop().time() + timeout_seconds
                logged_in = False
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(2)
                    if page.is_closed():
                        break
                    current = (page.url or "").lower()
                    if (
                        "linkedin.com/login" in current
                        or "checkpoint" in current
                        or "challenge" in current
                        or "signup" in current
                    ):
                        continue
                    cookies = await context.cookies("https://www.linkedin.com")
                    has_session_cookie = any(
                        c.get("name") == "li_at" and bool(c.get("value"))
                        for c in cookies
                    )
                    if has_session_cookie and "linkedin.com" in current:
                        logged_in = True
                        break

                if not logged_in:
                    app.automation_log += (
                        "LinkedIn login window timed out before sign-in completed.\n"
                    )
                    db.commit()
                    await browser.close()
                    return False

                await context.storage_state(path=target_state_path)
                try:
                    shutil.copyfile(target_state_path, legacy_state_path)
                except Exception:
                    pass

                app.automation_log += (
                    "LinkedIn session captured and saved. Continuing automation.\n"
                )
                db.commit()
                await browser.close()
                return True
        except Exception as e:
            app.automation_log += (
                f"Could not open interactive LinkedIn login window: {e}\n"
            )
            db.commit()
            return False

    async def _is_linkedin_session_valid(self, storage_state_path: str) -> bool:
        """
        Validate whether saved LinkedIn browser state still represents a logged-in session.
        """
        if not storage_state_path or not os.path.exists(storage_state_path):
            return False

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=storage_state_path)
                page = await context.new_page()
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.5)
                current_url = (page.url or "").lower()
                login_prompt = await page.query_selector(
                    "button.sign-in-form__submit, form[action*='/login'], .contextual-sign-in-modal"
                )
                await browser.close()

                if "/login" in current_url or "checkpoint/challenge" in current_url:
                    return False
                if login_prompt:
                    return False
                return "linkedin.com" in current_url
        except Exception:
            return False

    async def _dismiss_linkedin_signin_overlay(self, page: Page, app: Application, db) -> bool:
        """
        Dismiss LinkedIn sign-in overlay/modal if present.
        Returns True if any dismiss interaction succeeded.
        """
        dismiss_selectors = [
            ".contextual-sign-in-modal__modal-dismiss",
            "button[aria-label='Dismiss']",
            ".modal__dismiss-btn",
            ".artdeco-modal__dismiss",
        ]
        dismissed = False
        for sel in dismiss_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=2000, no_wait_after=True)
                    await asyncio.sleep(0.5)
                    dismissed = True
                    app.automation_log += f"Dismissed LinkedIn overlay via '{sel}'.\n"
                    db.commit()
                    break
            except Exception:
                continue
        return dismissed

    async def _detect_linkedin_job_state(self, page: Page) -> str:
        """
        Best-effort state detection for LinkedIn posting pages.
        Returns: already_applied | closed | unknown
        """
        try:
            text = ((await page.inner_text("body")) or "").lower()
        except Exception:
            return "unknown"

        if any(
            token in text
            for token in (
                "application submitted",
                "you've applied",
                "you already applied",
                "applied ",
            )
        ):
            return "already_applied"

        if any(
            token in text
            for token in (
                "no longer accepting applications",
                "job is no longer available",
                "this job is no longer available",
                "position has been filled",
            )
        ):
            return "closed"

        return "unknown"

    @staticmethod
    async def _pick_visible_linkedin_apply_button(page: Page):
        """
        Return (button_handle, normalized_label) for the best visible LinkedIn apply CTA.
        """
        selectors = (
            "a.jobs-apply-button, button.jobs-apply-button, "
            "a[data-control-name*='jobdetails_topcard_inapply'], button[data-control-name*='jobdetails_topcard_inapply'], "
            "button.apply-button, a.apply-button, .jobs-apply-button--easy-apply"
        )
        candidates = await page.query_selector_all(selectors)
        fallback = (None, "")

        for candidate in candidates:
            try:
                if not await candidate.is_visible():
                    continue
                if await candidate.get_attribute("disabled"):
                    continue
                box = await candidate.bounding_box()
                if not box or box.get("width", 0) < 20 or box.get("height", 0) < 20:
                    continue
                text = ((await candidate.inner_text()) or "").strip()
                aria = (await candidate.get_attribute("aria-label") or "").strip()
                label = f"{text} {aria}".strip().lower()
                if "apply" not in label:
                    continue
                # Prefer Easy Apply button when available.
                if "easy apply" in label:
                    return candidate, label
                if fallback[0] is None:
                    fallback = (candidate, label)
            except Exception:
                continue
        return fallback

    @staticmethod
    def source_mode(job: Job) -> str:
        source = (job.source or "").lower()
        url = (job.apply_url or job.url or "").lower()
        if "linkedin.com/jobs/" in url:
            return "linkedin"
        if source == "linkedin":
            return "linkedin"
        if source == "greenhouse":
            return "greenhouse"
        if source == "lever":
            return "lever"
        # Public web boards + external ATS/career portals use the generic form filler path.
        if source in {
            "himalayas",
            "remotive",
            "arbeitnow",
            "remoteok",
            "web",
            "custom_url",
            "workday",
            "icims",
            "smartrecruiters",
            "ashby",
            "jobvite",
            "taleo",
            "successfactors",
        }:
            return "generic"
        if url.startswith("http://") or url.startswith("https://"):
            return "generic"
        return "manual"

    @staticmethod
    def _issue_context(job: Optional[Job]) -> tuple[Optional[str], Optional[str]]:
        if not job:
            return None, None
        source = (job.source or "").lower() or None
        url = job.apply_url or job.url or ""
        try:
            domain = (urllib.parse.urlparse(url).hostname or "").lower() or None
        except Exception:
            domain = None
        return source, domain

    def _classify_issue(
        self,
        message: str,
        job: Optional[Job],
        user: Optional[UserProfile],
    ) -> tuple[str, list[str], list[str]]:
        text = (message or "").lower()
        category = "automation_issue"
        required_inputs: list[str] = []
        questions: list[str] = []

        if "anti-bot" in text or "cloudflare" in text or "security verification" in text:
            category = "anti_bot_challenge"
            required_inputs = ["manual_challenge_verification"]
            questions = [
                "Can you complete anti-bot verification in the opened apply window when prompted?"
            ]
        elif "page crashed" in text or "target closed" in text or "browser has disconnected" in text:
            category = "browser_crash"
            required_inputs = []
            questions = []
        elif "already applied" in text or "application already submitted" in text:
            category = "already_applied_detected"
            required_inputs = []
            questions = []
        elif "automation completed" in text or "submitted successfully" in text:
            category = "submission_success"
            required_inputs = []
            questions = []
        elif "linkedin login required" in text:
            category = "linkedin_login_required"
            required_inputs = ["linkedin_authenticated_session"]
            questions = [
                "Are you logged into LinkedIn in the automation popup window?"
            ]
        elif "requires sign-in" in text or "sign-in not completed" in text or "account creation required" in text:
            category = "portal_login_required"
            required_inputs = ["portal_authenticated_session"]
            questions = [
                "Does this application portal require sign-in/account creation before applying?",
                "Can you complete the sign-in once in the opened automation window so we can save the session for future runs?",
            ]
        elif "no linkedin apply action found" in text:
            category = "linkedin_apply_action_missing"
            required_inputs = ["posting_state_confirmation"]
            questions = [
                "Does the posting show 'Applied/Application submitted' or 'No longer accepting applications'?"
            ]
        elif "apply button was not interactable" in text:
            category = "linkedin_apply_interaction_blocked"
            required_inputs = ["linkedin_visibility_state"]
            questions = [
                "After opening the job, do you see an enabled Apply/Easy Apply button?"
            ]
        elif "could not detect final submit button" in text or "no final submit control detected" in text:
            category = "final_submit_detection_failed"
            required_inputs = ["screening_answers", "portal_specific_submit_label"]
            questions = [
                "Which button label appears on the last step (Submit, Apply, Send, Complete)?",
                "Are there any required unanswered screening fields visible before final submit?",
            ]
        elif "verification code" in text or "one-time password" in text or "otp" in text:
            category = "verification_code_required"
            required_inputs = ["verification_code"]
            questions = [
                "Enter the verification code sent by the employer portal so automation can continue."
            ]
        elif "how did you hear about us" in text or "hear about us is required" in text:
            category = "required_source_missing"
            required_inputs = ["hear_about_us"]
            questions = [
                "What source should be used for 'How did you hear about us?'"
            ]
        elif "postal code must be 6 digits" in text or "postal code" in text and "required" in text:
            category = "postal_code_required"
            required_inputs = ["postal_code"]
            questions = [
                "Provide a valid 6-digit postal code for this application."
            ]
        elif "profile missing required fields" in text:
            category = "profile_missing_required_fields"
            required_inputs = ["full_name", "email"]
            questions = [
                "Please provide your full name and primary email for applications."
            ]
        elif "score" in text and "below threshold" in text:
            category = "threshold_skip"
            required_inputs = ["min_score_preference"]
            questions = [
                "Should automation include jobs below your current minimum score threshold?"
            ]
        elif "unsupported source for automation" in text:
            category = "unsupported_source"
            required_inputs = ["source_preference"]
            questions = [
                "Should we skip unsupported sources automatically or keep them for manual apply?"
            ]

        # Enrich with known profile-driven screening inputs.
        if user:
            if user.expected_ctc_lpa is None:
                required_inputs.append("expected_ctc_lpa")
                questions.append("What is your expected CTC in LPA?")
            if user.current_ctc_lpa is None:
                required_inputs.append("current_ctc_lpa")
                questions.append("What is your current CTC in LPA?")
            if user.notice_period_days is None:
                required_inputs.append("notice_period_days")
                questions.append("What is your notice period in days?")

        # Keep deterministic uniqueness.
        required_inputs = sorted(set(required_inputs))
        dedup_questions: list[str] = []
        seen_q = set()
        for q in questions:
            if q in seen_q:
                continue
            seen_q.add(q)
            dedup_questions.append(q)
        return category, required_inputs, dedup_questions

    def _record_issue_event(
        self,
        db,
        app: Optional[Application],
        job: Optional[Job],
        user: Optional[UserProfile],
        message: str,
        event_type: str = "detected",
    ) -> None:
        if not message:
            return
        source, domain = self._issue_context(job)
        category, required_inputs, questions = self._classify_issue(message, job, user)
        if event_type == "detected" and app and isinstance(getattr(app, "blocker_details", None), dict):
            extra = app.blocker_details.get("required_inputs")
            if isinstance(extra, list):
                for item in extra:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key", "")).strip()
                    q = str(item.get("question", "")).strip()
                    if key:
                        required_inputs.append(key)
                    if q:
                        questions.append(q)
            required_inputs = sorted(set(required_inputs))
            dedup_questions: list[str] = []
            seen_q = set()
            for q in questions:
                if q in seen_q:
                    continue
                seen_q.add(q)
                dedup_questions.append(q)
            questions = dedup_questions
        row = AutomationIssueEvent(
            application_id=app.id if app else None,
            job_id=job.id if job else None,
            source=source,
            domain=domain,
            category=category,
            event_type=event_type,
            message=message,
            required_user_inputs=required_inputs or None,
            suggested_questions=questions or None,
        )
        db.add(row)
        db.commit()

    @staticmethod
    def _normalize_input_key(raw: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in (raw or ""))
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("_")

    def _input_key_from_meta(self, meta: str, input_type: str = "") -> str:
        text = (meta or "").lower()
        i_type = (input_type or "").lower()

        if any(tok in text for tok in ("verification code", "otp", "one time password", "security code", "passcode", "pin")):
            return "verification_code"
        if "password" in text:
            return "password"
        if any(tok in text for tok in ("expected ctc", "expected salary", "expected compensation", "expected pay")):
            return "expected_ctc_lpa"
        if any(tok in text for tok in ("current ctc", "current salary", "current compensation", "present ctc", "present salary")):
            return "current_ctc_lpa"
        if any(tok in text for tok in ("notice period", "notice in days")):
            return "notice_period_days"
        if any(tok in text for tok in ("postal code", "zip code", "zipcode", "pin code", "pincode")):
            return "postal_code"
        if any(tok in text for tok in ("address line 1", "address1", "street address", "street", "line1 address")):
            return "address_line_1"
        if any(tok in text for tok in ("address line 2", "address2", "apartment", "suite", "flat number")):
            return "address_line_2"
        if any(tok in text for tok in ("city", "town")):
            return "city"
        if any(tok in text for tok in ("state", "province", "region")):
            return "state"
        if any(tok in text for tok in ("country", "nationality country")):
            return "country"
        if any(
            tok in text
            for tok in (
                "which social media",
                "social media platform",
                "platform used",
                "social channel",
            )
        ):
            return "hear_about_us_platform"
        if any(tok in text for tok in ("join immediately", "immediate joining", "availability to join", "available to join")):
            return "can_join_immediately"
        if any(
            tok in text
            for tok in (
                "previous email",
                "email in trend micro",
                "former email",
                "old email",
            )
        ):
            return "previous_company_email"
        if any(tok in text for tok in ("email", "e-mail", "mail")):
            return "email"
        if any(
            tok in text
            for tok in (
                "previous employee id",
                "employee id in trend micro",
                "former employee id",
            )
        ):
            return "previous_employee_id"
        if any(
            tok in text
            for tok in (
                "previous manager name",
                "manager name in trend micro",
                "former manager name",
            )
        ):
            return "previous_manager_name"
        if any(
            tok in text
            for tok in (
                "applied in the past",
                "applied before",
                "previously applied",
                "have you applied",
            )
        ):
            return "applied_before"
        if any(
            tok in text
            for tok in (
                "have you previously worked for",
                "previously worked for",
                "worked here before",
                "worked for this company",
                "worked at this company",
                "worked for subsidiary",
                "worked for any subsidiary",
                "employed by subsidiary",
            )
        ):
            return "worked_here_before"
        if any(tok in text for tok in ("relocate", "relocation")):
            return "willing_to_relocate"
        if any(tok in text for tok in ("sponsor", "sponsorship", "visa support")):
            return "requires_sponsorship"
        if any(tok in text for tok in ("authorized to work", "work authorization", "work permit")):
            return "work_authorization"
        if any(tok in text for tok in ("phone type", "contact type", "type of phone", "number type")):
            return "phone_type"
        if any(tok in text for tok in ("phone device type", "device type")) and "phone" in text:
            return "phone_type"
        if any(tok in text for tok in ("phone extension", "extension")) and "phone" in text:
            return "phone_extension"
        if "extension" in text and i_type in {"text", "number", "tel"}:
            return "phone_extension"
        if any(tok in text for tok in ("country code", "dial code", "phone country", "mobile country code")) and any(
            tok in text for tok in ("phone", "mobile", "contact", "dial")
        ):
            return "phone_country_code"
        if any(
            tok in text
            for tok in (
                "how did you hear",
                "hear about us",
                "where did you hear",
                "source of application",
                "how did you find",
                "referral source",
                "job source",
            )
        ):
            return "hear_about_us"
        if any(tok in text for tok in ("linkedin", "linkedin url", "linkedin profile")):
            return "linkedin_url"
        if any(tok in text for tok in ("phone", "mobile", "telephone", "contact number")):
            return "phone"
        if any(tok in text for tok in ("years of experience", "experience in years", "total experience")):
            return "total_experience_years"
        if any(tok in text for tok in ("full name", "applicant name", "candidate name")):
            return "full_name"
        if any(tok in text for tok in ("local given name", "given name local")):
            return "local_given_name"
        if any(tok in text for tok in ("local family name", "family name local")):
            return "local_family_name"
        if any(tok in text for tok in ("legal first name", "legal given name")):
            return "first_name"
        if any(tok in text for tok in ("legal last name", "legal family name", "legal surname")):
            return "last_name"
        if any(tok in text for tok in ("first name", "given name")):
            return "first_name"
        if any(tok in text for tok in ("last name", "surname", "family name")):
            return "last_name"
        if "address" in text and "email" not in text:
            return "address_line_1"
        if any(tok in text for tok in ("city", "location")):
            return "location"
        if "username" in text or "user id" in text:
            return "email"
        if i_type == "file":
            return "resume_file"
        return self._normalize_input_key(text[:80]) or "required_input"

    def _input_question(self, key: str, label: str) -> str:
        k = (key or "").lower()
        if k == "verification_code":
            return "Enter the verification code sent to your official email/phone."
        if k == "password":
            return "Portal password is required to continue this application."
        if k == "expected_ctc_lpa":
            return "What is your expected CTC (LPA) for this application?"
        if k == "current_ctc_lpa":
            return "What is your current CTC (LPA)?"
        if k == "notice_period_days":
            return "What is your notice period in days?"
        if k == "postal_code":
            return "What postal code should be used for this application?"
        if k == "address_line_1":
            return "Provide address line 1 for this application."
        if k == "city":
            return "Provide city for this application."
        if k == "state":
            return "Provide state/province for this application."
        if k == "country":
            return "Provide country for this application."
        if k == "hear_about_us_platform":
            return "Which social media platform should be selected?"
        if k == "can_join_immediately":
            return "Can you join immediately? (yes/no)"
        if k == "applied_before":
            return "Have you applied to this company before? (yes/no)"
        if k == "worked_here_before":
            return "Have you worked for this company or subsidiary before? (yes/no)"
        if k == "previous_company_email":
            return "Provide your previous company email (use official email format)."
        if k == "previous_employee_id":
            return "Provide previous employee ID if requested."
        if k == "previous_manager_name":
            return "Provide previous manager name if requested."
        if k == "local_given_name":
            return "Provide local given name if required by portal."
        if k == "local_family_name":
            return "Provide local family name if required by portal."
        if k == "willing_to_relocate":
            return "Are you willing to relocate? (yes/no)"
        if k == "requires_sponsorship":
            return "Do you require visa/work sponsorship? (yes/no)"
        if k == "work_authorization":
            return "What is your work authorization status?"
        if k == "phone_type":
            return "Preferred phone type for this application."
        if k == "phone_country_code":
            return "Preferred phone country code."
        if k == "hear_about_us":
            return "How did you hear about this role?"
        if k == "resume_file":
            return "Upload/select a resume file for this application."
        return f"Provide value for: {label or key}"

    def _answer_overrides_for_application(
        self, user: Optional[UserProfile], app: Optional[Application]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if user and isinstance(getattr(user, "application_answers", None), dict):
            for k, v in user.application_answers.items():
                if not isinstance(k, str) or k.startswith("__"):
                    continue
                if isinstance(v, (dict, list, tuple, set)):
                    continue
                nk = self._normalize_input_key(str(k))
                if nk:
                    merged[nk] = v
        if app and isinstance(getattr(app, "user_inputs", None), dict):
            for k, v in app.user_inputs.items():
                if not isinstance(k, str) or k.startswith("__"):
                    continue
                if isinstance(v, (dict, list, tuple, set)):
                    continue
                nk = self._normalize_input_key(str(k))
                if nk:
                    merged[nk] = v
        return merged

    @staticmethod
    def _as_yes_no(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool):
            return "Yes" if value else "No"
        text = str(value).strip().lower()
        if text in {"yes", "y", "true", "1"}:
            return "Yes"
        if text in {"no", "n", "false", "0"}:
            return "No"
        return None

    def _answer_value_for_key(
        self,
        key: str,
        user: Optional[UserProfile],
        overrides: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        resolved_key = self._normalize_input_key(key)
        answers = overrides or {}
        source_keys = {
            "hear_about_us",
            "how_did_you_hear_about_us",
            "source_channel",
            "source_of_application",
            "job_source",
            "referral_source",
        }
        source_platform_keys = {
            "hear_about_us_platform",
            "social_media_platform",
            "source_platform",
        }
        phone_country_code_keys = {"phone_country_code", "country_code", "dial_code", "mobile_country_code"}
        phone_type_keys = {"phone_type", "contact_type", "number_type"}
        phone_extension_keys = {"phone_extension", "extension", "ext"}

        if resolved_key in answers and answers[resolved_key] not in (None, ""):
            explicit = str(answers[resolved_key]).strip()
            if resolved_key == "phone":
                return self._normalize_mobile_number(explicit)
            if resolved_key in phone_country_code_keys:
                return self.default_phone_country_code
            if resolved_key in phone_type_keys:
                return "mobile"
            if resolved_key in phone_extension_keys:
                return "0"
            if resolved_key in source_keys:
                return self.default_source_channel
            if resolved_key in source_platform_keys:
                return self.default_source_platform
            return explicit

        if resolved_key in {"verification_code", "otp", "security_code", "pin"}:
            for alt in ("verification_code", "otp", "security_code", "pin", "two_factor_code"):
                if alt in answers and answers[alt] not in (None, ""):
                    return str(answers[alt])
            return None
        if resolved_key == "password":
            for alt in ("password", "portal_password", "account_password"):
                if alt in answers and answers[alt] not in (None, ""):
                    return str(answers[alt])
            return None

        if resolved_key == "official_email":
            resolved_key = "email"
        if resolved_key == "previous_company_email":
            resolved_key = "email"

        if not user:
            if resolved_key in {"applied_before", "worked_here_before"}:
                return "No"
            if resolved_key == "previous_employee_id":
                return "0"
            if resolved_key == "previous_manager_name":
                return "NA"
            if resolved_key in phone_country_code_keys:
                return self.default_phone_country_code
            if resolved_key in phone_type_keys:
                return "mobile"
            if resolved_key in phone_extension_keys:
                return "0"
            if resolved_key in {"postal_code", "zip_code", "pincode"}:
                return self.default_postal_code
            if resolved_key == "country":
                return self.default_country
            if resolved_key == "state":
                return self.default_state
            if resolved_key == "city":
                return self.default_city
            if resolved_key == "address_line_1":
                return self.default_address_line_1
            if resolved_key == "address_line_2":
                return "NA"
            return None

        if resolved_key == "email":
            return (user.email or "").strip() or None
        if resolved_key == "previous_employee_id":
            return "0"
        if resolved_key == "previous_manager_name":
            return "NA"
        if resolved_key == "full_name":
            return (user.full_name or "").strip() or None
        if resolved_key == "first_name":
            parts = (user.full_name or "").strip().split()
            return parts[0].title() if parts else None
        if resolved_key == "last_name":
            parts = (user.full_name or "").strip().split()
            return parts[-1].title() if len(parts) > 1 else None
        if resolved_key == "local_given_name":
            parts = (user.full_name or "").strip().split()
            return parts[0].title() if parts else None
        if resolved_key == "local_family_name":
            parts = (user.full_name or "").strip().split()
            return parts[-1].title() if len(parts) > 1 else (parts[0].title() if parts else None)
        if resolved_key == "phone":
            return self._normalize_mobile_number((user.phone or "").strip())
        if resolved_key in phone_country_code_keys:
            return self.default_phone_country_code
        if resolved_key in phone_type_keys:
            return "mobile"
        if resolved_key in phone_extension_keys:
            return "0"
        if resolved_key in source_keys:
            return self.default_source_channel
        if resolved_key in source_platform_keys:
            return self.default_source_platform
        if resolved_key == "location":
            return (user.location or "").strip() or "NA"
        if resolved_key == "address_line_1":
            return self.default_address_line_1
        if resolved_key == "address_line_2":
            return "NA"
        if resolved_key == "city":
            city, _, _ = self._location_parts(user.location or "")
            return city
        if resolved_key == "state":
            _, state, _ = self._location_parts(user.location or "")
            return state
        if resolved_key == "country":
            _, _, country = self._location_parts(user.location or "")
            return country
        if resolved_key == "linkedin_url":
            return (user.linkedin_url or "").strip() or "https://linkedin.com"
        if resolved_key == "expected_ctc_lpa" and user.expected_ctc_lpa is not None:
            return str(user.expected_ctc_lpa)
        if resolved_key == "current_ctc_lpa" and user.current_ctc_lpa is not None:
            return str(user.current_ctc_lpa)
        if resolved_key == "notice_period_days" and user.notice_period_days is not None:
            return str(user.notice_period_days)
        if resolved_key == "can_join_immediately":
            return self._as_yes_no(user.can_join_immediately)
        if resolved_key in {"applied_before", "worked_here_before"}:
            return "No"
        if resolved_key == "willing_to_relocate":
            return self._as_yes_no(user.willing_to_relocate)
        if resolved_key == "requires_sponsorship":
            return self._as_yes_no(user.requires_sponsorship)
        if resolved_key == "work_authorization":
            return (user.work_authorization or "").strip() or None
        if resolved_key == "postal_code":
            return self.default_postal_code
        if resolved_key == "total_experience_years":
            if isinstance(user.experience, list) and user.experience:
                return str(max(1, len(user.experience)))
            return "1"
        return None

    def _resolve_field_value(
        self,
        meta: str,
        input_type: str,
        user: Optional[UserProfile],
        overrides: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Optional[str]]:
        key = self._input_key_from_meta(meta, input_type)
        explicit = self._answer_value_for_key(key, user, overrides=overrides)
        if explicit not in (None, ""):
            return key, str(explicit)
        return key, None

    async def _collect_required_inputs_from_page(
        self,
        page: Page,
        user: Optional[UserProfile],
        app: Optional[Application],
        max_items: int = 20,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        overrides = self._answer_overrides_for_application(user, app)

        try:
            elements = await page.query_selector_all(
                "input[required], textarea[required], select[required], "
                "input[aria-required='true'], textarea[aria-required='true'], select[aria-required='true']"
            )
        except Exception:
            elements = []

        placeholder_values = {"", "select", "select an option", "choose an option", "please select"}
        for el in elements[:300]:
            if len(results) >= max_items:
                break
            try:
                if not await el.is_visible():
                    continue
                if await el.get_attribute("disabled") is not None:
                    continue
                if await el.get_attribute("readonly") is not None:
                    continue

                tag = ((await el.evaluate("e => e.tagName")) or "").lower()
                i_type = (await el.get_attribute("type") or "").lower()
                if i_type in {"hidden", "submit", "button", "image"}:
                    continue

                # Skip required elements that are already satisfied.
                if i_type in {"checkbox", "radio"}:
                    try:
                        if await el.is_checked():
                            continue
                    except Exception:
                        pass
                else:
                    raw_value = (await el.input_value() or "").strip()
                    if tag == "select" and raw_value.lower() not in placeholder_values:
                        continue
                    if tag != "select" and raw_value:
                        continue

                label = ""
                try:
                    label = (
                        await el.evaluate(
                            "(e) => (e.labels && e.labels.length ? Array.from(e.labels).map(l => (l.innerText || '').trim()).join(' ') : '')"
                        )
                    ) or ""
                except Exception:
                    label = ""
                meta_parts = [
                    (await el.get_attribute("name") or "").strip(),
                    (await el.get_attribute("id") or "").strip(),
                    (await el.get_attribute("aria-label") or "").strip(),
                    (await el.get_attribute("placeholder") or "").strip(),
                    label.strip(),
                ]
                meta = " ".join([p for p in meta_parts if p]).strip().lower()
                if not meta:
                    meta = f"{tag}_{i_type or 'field'}"

                key = self._input_key_from_meta(meta, i_type)
                if key in seen:
                    continue
                seen.add(key)

                existing = self._answer_value_for_key(key, user, overrides)
                if existing not in (None, ""):
                    continue

                input_kind = "text"
                if i_type in {"number"}:
                    input_kind = "number"
                elif i_type in {"email"}:
                    input_kind = "email"
                elif i_type in {"checkbox", "radio"}:
                    input_kind = "choice"
                elif tag == "select":
                    input_kind = "select"
                elif i_type == "file":
                    input_kind = "file"

                display_label = label.strip() or meta_parts[2] or meta_parts[0] or key.replace("_", " ")
                results.append(
                    {
                        "key": key,
                        "label": display_label.strip().title(),
                        "question": self._input_question(key, display_label),
                        "type": input_kind,
                        "required": True,
                    }
                )
            except Exception:
                continue

        # Include OTP/code fields even when portals forget required attributes.
        otp_selectors = (
            "input[name*='otp' i], input[id*='otp' i], input[aria-label*='otp' i], "
            "input[name*='verification' i], input[id*='verification' i], input[placeholder*='code' i], "
            "input[name*='code' i], input[id*='code' i]"
        )
        try:
            otp_inputs = await page.query_selector_all(otp_selectors)
        except Exception:
            otp_inputs = []
        for el in otp_inputs[:20]:
            if len(results) >= max_items:
                break
            try:
                if not await el.is_visible():
                    continue
                current = (await el.input_value() or "").strip()
                if current:
                    continue
                key = "verification_code"
                if key in seen:
                    continue
                if self._answer_value_for_key(key, user, overrides) not in (None, ""):
                    continue
                seen.add(key)
                label = (await el.get_attribute("aria-label") or "").strip() or "Verification code"
                results.append(
                    {
                        "key": key,
                        "label": label.title(),
                        "question": self._input_question(key, label),
                        "type": "verification_code",
                        "required": True,
                    }
                )
            except Exception:
                continue
        return results

    async def _capture_blocker_details(
        self,
        page: Optional[Page],
        app: Application,
        user: Optional[UserProfile],
        db,
        reason: str,
        message: str,
        required_inputs: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        inputs = [item for item in (required_inputs or []) if isinstance(item, dict)]
        if not inputs:
            reason_l = (reason or "").lower()
            if "verification_code" in reason_l:
                inputs.append(
                    {
                        "key": "verification_code",
                        "label": "Verification Code",
                        "question": "Enter the verification code sent to your email/phone.",
                        "type": "verification_code",
                        "required": True,
                    }
                )
            elif "portal_login_required" in reason_l or "captcha" in reason_l or "anti_bot" in reason_l:
                inputs.append(
                    {
                        "key": "portal_authenticated_session",
                        "label": "Portal Sign-In",
                        "question": "Complete login/verification in the popup and click retry.",
                        "type": "manual_action",
                        "required": True,
                    }
                )
        if page is not None:
            try:
                detected = await self._collect_required_inputs_from_page(page, user, app)
                existing_keys = {str(i.get("key", "")) for i in inputs}
                for row in detected:
                    key = str(row.get("key", ""))
                    if key in existing_keys:
                        continue
                    existing_keys.add(key)
                    inputs.append(row)
            except Exception:
                pass

        payload = {
            "reason": reason,
            "message": message,
            "required_inputs": inputs,
            "detected_at": datetime.utcnow().isoformat(),
        }
        if page is not None:
            try:
                payload["page_url"] = page.url
            except Exception:
                pass
        if isinstance(app.user_inputs, dict):
            snapshot = app.user_inputs.get("__last_runtime_values")
            if isinstance(snapshot, dict):
                payload["last_used_values"] = snapshot
        app.blocker_details = payload
        if inputs:
            keys = ", ".join(sorted({str(i.get("key", "")) for i in inputs if i.get("key")}))
            if keys:
                app.automation_log = (app.automation_log or "") + f"Captured required inputs: {keys}\n"
        db.commit()

    @staticmethod
    def _collect_fallback_target_roles(db, limit: int = 8) -> list[str]:
        queries = db.query(SearchQuery).order_by(SearchQuery.id.desc()).limit(limit).all()
        roles: list[str] = []
        seen: set[str] = set()
        for q in queries:
            raw = q.keywords
            if not raw:
                continue
            candidates: list[str] = []
            if isinstance(raw, str):
                text = raw.strip()
                if text.startswith("[") and text.endswith("]"):
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            candidates = [str(v) for v in parsed if v]
                    except Exception:
                        candidates = [text]
                else:
                    candidates = [text]
            elif isinstance(raw, list):
                candidates = [str(v) for v in raw if v]

            for item in candidates:
                role = item.strip()
                if not role:
                    continue
                key = role.lower()
                if key in seen:
                    continue
                seen.add(key)
                roles.append(role)
        return roles

    def refresh_job_score_if_stale(
        self,
        job: Optional[Job],
        user: Optional[UserProfile],
        db,
        threshold: float,
    ) -> tuple[bool, Optional[float], Optional[float]]:
        """
        Re-score job if current score looks stale or below threshold.
        Returns (updated, old_score, new_score).
        """
        if not job or not user:
            return False, None, None

        current_score = float(job.match_score) if job.match_score is not None else None
        details = job.match_details or {}
        has_unscored_reason = isinstance(details, dict) and bool(details.get("unscored_reason"))
        should_refresh = current_score is None or current_score < threshold or has_unscored_reason
        if not should_refresh:
            return False, current_score, current_score

        from job_search.services.job_matcher import JobMatcher

        fallback_roles = self._collect_fallback_target_roles(db)
        profile_dict = {
            "skills": user.skills or [],
            "experience": user.experience or [],
            "target_roles": user.target_roles or fallback_roles,
            "target_locations": user.target_locations or [],
            "summary": user.summary or "",
            "headline": user.headline or "",
        }
        has_profile_signal = any(
            [
                profile_dict["skills"],
                profile_dict["target_roles"],
                profile_dict["experience"],
                profile_dict["summary"],
                profile_dict["headline"],
            ]
        )
        if not has_profile_signal:
            return False, current_score, current_score

        matcher = JobMatcher()
        job_dict = {
            "title": job.title,
            "description": job.description or "",
            "location": job.location or "",
            "work_type": job.work_type or "",
        }
        result = matcher.score_job(job_dict, profile_dict)

        old_score = current_score
        job.match_score = result.overall_score
        job.match_details = {
            "skill_score": result.skill_score,
            "title_score": result.title_score,
            "experience_score": result.experience_score,
            "location_score": result.location_score,
            "keyword_score": result.keyword_score,
            "matched_skills": result.matched_skills,
            "missing_skills": result.missing_skills,
            "recommendation": result.recommendation,
            "explanation": result.explanation,
        }
        job.extracted_keywords = result.extracted_keywords
        db.commit()
        return True, old_score, float(result.overall_score)

    def _hydrate_profile_from_resume_if_needed(
        self, user: Optional[UserProfile], resume: Optional[Resume], db
    ) -> Optional[UserProfile]:
        """Fill missing required profile fields from parsed resume if possible."""
        if not resume or not resume.parsed_data:
            return user

        parsed = resume.parsed_data or {}
        name = parsed.get("name") or parsed.get("full_name")
        email = parsed.get("email")
        phone = parsed.get("phone")
        location = parsed.get("location")
        linkedin = parsed.get("linkedin_url") or parsed.get("linkedin")

        if not user:
            user = UserProfile(
                full_name=name or "",
                email=email or "",
                phone=phone,
                location=location,
                linkedin_url=linkedin,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user

        updated = False
        if not user.full_name and name:
            user.full_name = name
            updated = True
        if not user.email and email:
            user.email = email
            updated = True
        if not user.phone and phone:
            user.phone = phone
            updated = True
        if not user.location and location:
            user.location = location
            updated = True
        if not user.linkedin_url and linkedin:
            user.linkedin_url = linkedin
            updated = True

        if updated:
            db.commit()
            db.refresh(user)
        return user

    # ------------------------------------------------------------------
    # Resume tailoring (runs BEFORE browser launch)
    # ------------------------------------------------------------------

    async def _tailor_resume_for_job(
        self, resume: Optional[Resume], job: Job, app: Application, db
    ) -> str:
        """Tailor the resume for a specific job and return file path.

        Falls back to original file if parsing/tailoring fails.
        """
        if not resume:
            return ""

        if not settings.resume_tailoring_enabled:
            app.automation_log = (app.automation_log or "") + (
                "Resume tailoring disabled; uploading original resume file.\n"
            )
            db.commit()
            return resume.file_path or ""

        # Chosen behavior: fallback to original resume when parsing data is absent.
        if not resume.parsed_data:
            app.automation_log = (app.automation_log or "") + "Resume not parsed; uploading original file.\n"
            db.commit()
            return resume.file_path or ""

        app.automation_log = (app.automation_log or "") + "Tailoring resume for this job...\n"
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
                    app.automation_log += "LLM tailoring timed out; keyword fallback.\n"
                except Exception as e:
                    app.automation_log += f"LLM tailoring error ({e}); keyword fallback.\n"

            if result is None:
                jd_keywords = extract_keywords(job.description or "")
                tailor = ResumeTailor(llm_client=llm_client)
                result = tailor.tailor_keywords_only(resume.parsed_data, jd_keywords)
                app.automation_log += "Using keyword-only tailoring.\n"

            db.commit()

            tailored_data = dict(resume.parsed_data)
            tailored_data.update(result.modified_sections)

            generator = ResumeGenerator()
            try:
                output_path = generator.generate_pdf(
                    tailored_data,
                    output_filename=f"tailored_{resume.id}_job_{job.id}.pdf",
                    require_pdf=True,
                )
            except Exception as pdf_err:
                # External portals usually reject HTML uploads; use original resume when PDF backend is unavailable.
                app.automation_log += (
                    "Tailored PDF generation unavailable in this environment; using original resume file for upload.\n"
                )
                db.commit()
                return resume.file_path or ""

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
            app.automation_log += "Tailoring failed; uploading original resume.\n"
            db.commit()
            return resume.file_path or ""

    # ------------------------------------------------------------------
    # Main automation entry point
    # ------------------------------------------------------------------

    async def run_automation(
        self,
        application_id: int,
        resume_id: Optional[int] = None,
        safe_mode: bool = True,
        require_confirmation: bool = True,
    ):
        """Main automation entry point with threshold gating and safe mode."""
        db = SessionLocal()
        app = db.query(Application).filter(Application.id == application_id).first()
        if not app:
            logger.error(f"Application {application_id} not found")
            db.close()
            return
        if self._abort_if_stop_requested(db, app, "startup"):
            db.close()
            return
        # Prevent duplicate triggers from racing (UI auto-start + manual retry, etc).
        try:
            if getattr(app, "status", None) == ApplicationStatus.IN_PROGRESS:
                app.automation_log = (app.automation_log or "") + "Automation already in progress; skipping duplicate trigger.\n"
                db.commit()
                db.close()
                return
        except Exception:
            pass

        job = app.job
        user = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
        resume = db.query(Resume).filter(Resume.id == resume_id).first() if resume_id else db.query(Resume).filter(Resume.is_primary == True).first()
        runtime_answer_overrides: dict[str, Any] = {}
        runtime_value_sources: dict[str, str] = {}
        resume_path: str = ""

        try:
            def record_detected(message: Optional[str]):
                if not message:
                    return
                try:
                    self._record_issue_event(db, app, job, user, message, event_type="detected")
                except Exception:
                    pass

            def record_resolved(message: Optional[str]):
                if not message:
                    return
                try:
                    self._record_issue_event(db, app, job, user, message, event_type="resolved")
                except Exception:
                    pass

            source_mode = self.source_mode(job)
            if source_mode == "manual":
                app.status = ApplicationStatus.REVIEWED
                app.error_message = None
                app.notes = f"Source '{job.source}' does not support full automation. Manual review required."
                app.automation_log = (app.automation_log or "") + "Unsupported source for automation.\n"
                db.commit()
                record_detected(app.notes)
                return
            if self._abort_if_stop_requested(db, app, "pre-flight"):
                return

            user = self._hydrate_profile_from_resume_if_needed(user, resume, db)
            if not user or not user.full_name or not user.email:
                app.status = ApplicationStatus.FAILED
                app.error_message = "Profile missing required fields (full name and email)."
                await self._capture_blocker_details(
                    page=None,
                    app=app,
                    user=user,
                    db=db,
                    reason="profile_missing_required_fields",
                    message=app.error_message,
                    required_inputs=[
                        {
                            "key": "full_name",
                            "label": "Full Name",
                            "question": "Please provide your full legal name.",
                            "type": "text",
                            "required": True,
                        },
                        {
                            "key": "official_email",
                            "label": "Official Email",
                            "question": "Please provide your official email for account creation and applications.",
                            "type": "email",
                            "required": True,
                        },
                    ],
                )
                db.commit()
                record_detected(app.error_message)
                return
            if self._abort_if_stop_requested(db, app, "profile-validation"):
                return

            threshold = float(settings.auto_apply_min_score)
            refreshed, old_score, new_score = self.refresh_job_score_if_stale(job, user, db, threshold)
            if refreshed:
                app.automation_log = (app.automation_log or "") + (
                    f"Refreshed score before threshold check: {old_score if old_score is not None else 'N/A'} -> {new_score}.\n"
                )
                db.commit()
            if job.match_score is not None and float(job.match_score) < threshold:
                app.status = ApplicationStatus.REVIEWED
                app.error_message = None
                app.notes = f"Automation skipped: score {job.match_score} below threshold {threshold}."
                app.automation_log = (app.automation_log or "") + "Skipped due to threshold gate.\n"
                db.commit()
                record_detected(app.notes)
                return

            app.status = ApplicationStatus.IN_PROGRESS
            app.error_message = None
            app.notes = None
            app.blocker_details = None
            app.automation_log = "Automation started...\n"
            app.automation_log += (
                f"Browser mode: {'headless' if self.headless else 'headed'}.\n"
            )
            db.commit()
            if self._abort_if_stop_requested(db, app, "before-session-bootstrap"):
                return
            base_answer_overrides = self._answer_overrides_for_application(user, app)
            runtime_answer_overrides, runtime_value_sources = self._build_runtime_answer_overrides(
                user=user,
                resume=resume,
                job=job,
                app=app,
                db=db,
                base_overrides=base_answer_overrides,
            )
            # Persist runtime values for blocker diagnostics/retries.
            if isinstance(app.user_inputs, dict):
                app.user_inputs["__last_runtime_values"] = dict(runtime_answer_overrides)
                app.user_inputs["__last_runtime_value_sources"] = dict(runtime_value_sources)
            else:
                app.user_inputs = {
                    "__last_runtime_values": dict(runtime_answer_overrides),
                    "__last_runtime_value_sources": dict(runtime_value_sources),
                }
            db.commit()
            if self._abort_if_stop_requested(db, app, "before-browser-launch"):
                return

            if source_mode == "linkedin":
                has_credentials = bool(settings.linkedin_email and settings.linkedin_password)
                if not has_credentials:
                    storage_path = self._linkedin_storage_state_path()
                    if storage_path:
                        session_valid = await self._is_linkedin_session_valid(storage_path)
                        if not session_valid:
                            app.automation_log += (
                                "Saved LinkedIn session appears expired. Re-authentication required.\n"
                            )
                            db.commit()
                            ensured = await self._bootstrap_linkedin_session(app, db)
                            if not ensured:
                                app.status = ApplicationStatus.REVIEWED
                                app.notes = (
                                    "LinkedIn login required. Complete login in the popup window and retry."
                                )
                                app.error_message = None
                                db.commit()
                                record_detected(app.notes)
                                return
                    else:
                        ensured = await self._bootstrap_linkedin_session(app, db)
                        if not ensured:
                            app.status = ApplicationStatus.REVIEWED
                            app.notes = (
                                "LinkedIn login required. Complete login in the popup window and retry."
                            )
                            app.error_message = None
                            db.commit()
                            record_detected(app.notes)
                            return

            resume_path = await self._tailor_resume_for_job(resume, job, app, db)
            resume_path = self._coerce_resume_upload_path(resume_path, resume, app, db)
            if self._abort_if_stop_requested(db, app, "before-navigation"):
                return

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)

                target_url = job.apply_url or job.url
                if source_mode == "generic":
                    resolution = await resolve_official_apply_url(target_url or "", job.source or "")
                    if resolution.get("warnings"):
                        app.automation_log += "Apply target warnings: " + "; ".join(resolution["warnings"]) + "\n"

                    resolved_url = resolution.get("resolved_url")
                    if not resolved_url:
                        reason = resolution.get("reason", "unknown")
                        if safe_mode or require_confirmation:
                            app.automation_log += (
                                "Could not resolve official apply URL automatically "
                                f"({reason}). Continuing in manual-assist mode on listing URL.\n"
                            )
                            resolved_url = target_url
                        else:
                            app.status = ApplicationStatus.REVIEWED
                            app.error_message = None
                            app.notes = (
                                "Could not resolve official apply URL automatically. "
                                f"Reason: {reason}. Enable safe mode to proceed with manual-assist."
                            )
                            app.automation_log += (
                                "Stopping before browser automation: official apply URL could not be resolved "
                                "for full-auto submit mode.\n"
                            )
                            db.commit()
                            record_detected(app.notes)
                            return

                    if resolved_url != target_url:
                        app.automation_log += f"Resolved official apply URL: {resolved_url}\n"
                        target_url = resolved_url
                        job.apply_url = resolved_url
                        db.commit()

                context_params = {}
                if source_mode == "linkedin":
                    storage_path = self._linkedin_storage_state_path()
                    if storage_path:
                        context_params["storage_state"] = storage_path
                else:
                    external_state_path = self._external_storage_state_path(target_url or "")
                    if external_state_path and os.path.exists(external_state_path):
                        context_params["storage_state"] = external_state_path

                context = await browser.new_context(**context_params)
                page = await context.new_page()
                if self._abort_if_stop_requested(db, app, "after-browser-open"):
                    await browser.close()
                    return

                url = target_url
                app.automation_log += f"Navigating to {url}\n"
                db.commit()

                await page.goto(url, wait_until="domcontentloaded")
                if self._abort_if_stop_requested(db, app, "after-initial-navigation"):
                    await browser.close()
                    return
                final_submitted = False

                if source_mode == "linkedin":
                    final_submitted = await self._handle_linkedin_apply(
                        page,
                        user,
                        resume_path,
                        app,
                        db,
                        answer_overrides=runtime_answer_overrides,
                        prefer_easy_apply=bool(job.is_easy_apply),
                        safe_mode=safe_mode,
                        require_confirmation=require_confirmation,
                    )
                elif source_mode == "greenhouse":
                    final_submitted = await self._handle_greenhouse_apply(
                        page,
                        user,
                        resume_path,
                        app,
                        db,
                        answer_overrides=runtime_answer_overrides,
                        safe_mode=safe_mode,
                        require_confirmation=require_confirmation,
                    )
                elif source_mode == "lever":
                    final_submitted = await self._handle_lever_apply(
                        page,
                        user,
                        resume_path,
                        app,
                        db,
                        answer_overrides=runtime_answer_overrides,
                        safe_mode=safe_mode,
                        require_confirmation=require_confirmation,
                    )
                elif source_mode == "generic":
                    final_submitted = await self._handle_generic_apply(
                        page,
                        user,
                        resume_path,
                        app,
                        db,
                        answer_overrides=runtime_answer_overrides,
                        safe_mode=safe_mode,
                        require_confirmation=require_confirmation,
                    )
                else:
                    final_submitted = await self._handle_generic_apply(
                        page,
                        user,
                        resume_path,
                        app,
                        db,
                        answer_overrides=runtime_answer_overrides,
                        safe_mode=safe_mode,
                        require_confirmation=require_confirmation,
                    )

                if safe_mode or require_confirmation:
                    if app.status == ApplicationStatus.SUBMITTED:
                        app.error_message = None
                        app.notes = app.notes or "Application already submitted."
                        if not app.applied_at:
                            app.applied_at = datetime.now()
                        app.automation_log += "Detected application already submitted.\n"
                    else:
                        if not app.notes:
                            log_lower = (app.automation_log or "").lower()
                            reached_final_review = (
                                "reached final submit control" in log_lower
                                or "reached final submit screen" in log_lower
                            )
                            if reached_final_review:
                                app.notes = "Ready for final submission; review in browser."
                            else:
                                app.notes = "Automation filled available fields but did not reach final submit step."
                        app.status = ApplicationStatus.REVIEWED
                        app.automation_log += "Safe mode active. Did not submit final application.\n"
                elif final_submitted:
                    if not app.notes:
                        app.notes = "Automation completed."
                    app.status = ApplicationStatus.SUBMITTED
                    app.blocker_details = None
                    if not app.applied_at:
                        app.applied_at = datetime.now()
                    if app.notes and "already applied" in app.notes.lower():
                        app.automation_log += "Submission already existed on target site.\n"
                    else:
                        app.automation_log += "Final submission action executed.\n"
                else:
                    if not app.notes:
                        app.notes = "Automation filled available fields but could not detect final submit button."
                        app.automation_log += "No final submit control detected. Left for manual review.\n"
                    else:
                        app.automation_log += f"Application left for review: {app.notes}\n"
                    app.status = ApplicationStatus.REVIEWED
                    if not (safe_mode or require_confirmation) and not app.blocker_details:
                        await self._capture_blocker_details(
                            page,
                            app,
                            user,
                            db,
                            reason="final_submit_detection_failed",
                            message=app.notes or "Automation stopped before final submission.",
                        )

                db.commit()
                if app.status == ApplicationStatus.SUBMITTED:
                    record_resolved(app.notes or "Application submitted successfully.")
                elif app.status == ApplicationStatus.FAILED:
                    record_detected(app.error_message or "Automation failed")
                elif app.status == ApplicationStatus.REVIEWED:
                    if app.notes and "ready for final submission" not in app.notes.lower():
                        record_detected(app.notes)

                self._persist_submission_audit(
                    app=app,
                    job=job,
                    resume_path=resume_path,
                    runtime_overrides=runtime_answer_overrides,
                    value_sources=runtime_value_sources,
                )
                db.commit()
                try:
                    self._learn_from_application_run(
                        db=db,
                        user=user,
                        app=app,
                        job=job,
                        runtime_overrides=runtime_answer_overrides,
                        value_sources=runtime_value_sources,
                    )
                except Exception:
                    pass
                # Keep browser open only for interactive non-headless review sessions.
                if (safe_mode or require_confirmation) and not self.headless:
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(1)
                await browser.close()

        except Exception as e:
            logger.exception(f"Automation failed for application {application_id}")
            app.status = ApplicationStatus.FAILED
            app.error_message = str(e)
            try:
                self._persist_submission_audit(
                    app=app,
                    job=job,
                    resume_path=resume_path,
                    runtime_overrides=runtime_answer_overrides,
                    value_sources=runtime_value_sources,
                )
            except Exception:
                pass
            db.commit()
            try:
                self._record_issue_event(db, app, job, user, str(e), event_type="detected")
            except Exception:
                pass
            try:
                self._learn_from_application_run(
                    db=db,
                    user=user,
                    app=app,
                    job=job,
                    runtime_overrides=runtime_answer_overrides,
                    value_sources=runtime_value_sources,
                )
            except Exception:
                pass
        finally:
            db.close()

    # ------------------------------------------------------------------
    # LinkedIn Easy Apply
    # ------------------------------------------------------------------

    async def _handle_linkedin_apply(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
        prefer_easy_apply: bool = True,
        safe_mode: bool = True,
        require_confirmation: bool = True,
    ) -> bool:
        """
        Handle LinkedIn jobs for both Easy Apply and external apply flows.
        """
        if self._abort_if_stop_requested(db, app, "linkedin-apply-start"):
            return False
        apply_btn, apply_label = await self._pick_visible_linkedin_apply_button(page)
        if apply_btn and ("easy apply" in apply_label or "continue applying" in apply_label):
            app.automation_log += "LinkedIn Easy Apply detected.\n"
            db.commit()
            return await self._handle_linkedin_easy_apply(
                page,
                user,
                resume_path,
                app,
                db,
                answer_overrides=answer_overrides,
                safe_mode=safe_mode,
                require_confirmation=require_confirmation,
                trigger_button=apply_btn,
                trigger_label=apply_label,
            )
        if not apply_btn:
            posting_state = await self._detect_linkedin_job_state(page)
            if posting_state == "already_applied":
                app.status = ApplicationStatus.SUBMITTED
                app.error_message = None
                if not app.applied_at:
                    app.applied_at = datetime.now()
                app.notes = "Already applied on LinkedIn (detected)."
                app.automation_log += "LinkedIn indicates application already submitted for this posting.\n"
                db.commit()
                return True
            if posting_state == "closed":
                app.notes = "LinkedIn posting is closed or no longer accepting applications."
                app.automation_log += "LinkedIn job appears closed/no longer accepting applications.\n"
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="posting_closed",
                    message=app.notes,
                )
                db.commit()
                return False
            signed_out_prompt = await page.query_selector(
                "button.sign-in-form__submit, .contextual-sign-in-modal, form[action*='/login']"
            )
            if signed_out_prompt:
                app.notes = "LinkedIn login required before automation can continue."
                app.automation_log += (
                    "LinkedIn login required. No authenticated session found. "
                    "Save LinkedIn browser state first, then retry automation.\n"
                )
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="linkedin_login_required",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "linkedin_authenticated_session",
                            "label": "LinkedIn Session",
                            "question": "Log into LinkedIn in the popup window and retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
            else:
                app.notes = "No LinkedIn apply action found on this posting."
                app.automation_log += "No LinkedIn apply button found.\n"
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="linkedin_apply_action_missing",
                    message=app.notes,
                )
            db.commit()
            return False

        app.automation_log += "LinkedIn external apply detected.\n"
        db.commit()

        external_page = page
        switched_page = False
        before_click_url = page.url
        apply_href = await apply_btn.get_attribute("href")
        try:
            async with page.context.expect_page(timeout=7000) as new_page_info:
                await apply_btn.click(timeout=7000, no_wait_after=True, force=True)
            external_page = await new_page_info.value
            switched_page = True
            await external_page.wait_for_load_state("domcontentloaded")
        except Exception:
            try:
                await apply_btn.click(timeout=7000, no_wait_after=True, force=True)
                await asyncio.sleep(2)
                # Same-tab navigation fallback (LinkedIn sometimes does not open a popup tab).
                if page.url != before_click_url:
                    external_page = page
                else:
                    # Some postings provide direct href on the apply anchor.
                    if apply_href:
                        resolved = urllib.parse.urljoin(before_click_url, apply_href)
                        if resolved and resolved != before_click_url:
                            await page.goto(resolved, wait_until="domcontentloaded", timeout=30000)
                            external_page = page
            except Exception as e:
                # LinkedIn public job pages often show a sign-in modal that intercepts clicks.
                if await self._dismiss_linkedin_signin_overlay(page, app, db):
                    try:
                        async with page.context.expect_page(timeout=7000) as new_page_info_retry:
                            await apply_btn.click(timeout=5000, no_wait_after=True, force=True)
                        external_page = await new_page_info_retry.value
                        switched_page = True
                        await external_page.wait_for_load_state("domcontentloaded")
                    except Exception as e_retry:
                        # Retry handling for same-tab redirects after dismiss.
                        try:
                            await apply_btn.click(timeout=7000, no_wait_after=True, force=True)
                            await asyncio.sleep(2)
                            if page.url != before_click_url:
                                external_page = page
                            else:
                                if apply_href:
                                    resolved = urllib.parse.urljoin(before_click_url, apply_href)
                                    if resolved and resolved != before_click_url:
                                        await page.goto(resolved, wait_until="domcontentloaded", timeout=30000)
                                        external_page = page
                                    else:
                                        raise RuntimeError("no_redirect_after_retry")
                                else:
                                    raise RuntimeError("no_redirect_after_retry")
                        except Exception:
                            app.notes = "LinkedIn apply button was not interactable (likely login wall or hidden button)."
                            app.automation_log += (
                                f"Failed to click LinkedIn apply button after dismiss/retry: {e_retry}\n"
                            )
                            await self._capture_blocker_details(
                                page,
                                app,
                                user,
                                db,
                                reason="linkedin_apply_interaction_blocked",
                                message=app.notes,
                            )
                            db.commit()
                            return False
                else:
                    app.notes = "LinkedIn apply button was not interactable (likely login wall or hidden button)."
                    app.automation_log += f"Failed to click LinkedIn apply button: {e}\n"
                    await self._capture_blocker_details(
                        page,
                        app,
                        user,
                        db,
                        reason="linkedin_apply_interaction_blocked",
                        message=app.notes,
                    )
                    db.commit()
                    return False

        current_url = external_page.url
        app.automation_log += f"LinkedIn apply target: {current_url}\n"
        db.commit()

        current_url_l = (current_url or "").lower()
        if "/login" in current_url_l or "signup" in current_url_l:
            app.notes = "LinkedIn login required before automation can continue."
            app.automation_log += (
                "LinkedIn redirected to login before application flow. "
                "Save LinkedIn browser state first, then retry automation.\n"
            )
            await self._capture_blocker_details(
                external_page,
                app,
                user,
                db,
                reason="linkedin_login_required",
                message=app.notes,
                required_inputs=[
                    {
                        "key": "linkedin_authenticated_session",
                        "label": "LinkedIn Session",
                        "question": "Log into LinkedIn in the popup window and retry.",
                        "type": "manual_action",
                        "required": True,
                    }
                ],
            )
            db.commit()
            return False

        if "linkedin.com" in current_url_l:
            # Sometimes LinkedIn opens an in-page modal even for non-easy paths.
            if await external_page.query_selector(".jobs-s-apply-footer"):
                return await self._handle_linkedin_easy_apply(
                    external_page,
                    user,
                    resume_path,
                    app,
                    db,
                    answer_overrides=answer_overrides,
                    safe_mode=safe_mode,
                    require_confirmation=require_confirmation,
                )
            login_modal = await external_page.query_selector(
                "button.sign-in-form__submit, .contextual-sign-in-modal, form[action*='/login']"
            )
            if login_modal:
                app.notes = "LinkedIn login required before automation can continue."
                app.automation_log += (
                    "LinkedIn apply requires sign-in for this posting. "
                    "Save LinkedIn browser state first, then retry automation.\n"
                )
                await self._capture_blocker_details(
                    external_page,
                    app,
                    user,
                    db,
                    reason="linkedin_login_required",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "linkedin_authenticated_session",
                            "label": "LinkedIn Session",
                            "question": "Log into LinkedIn in the popup window and retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
                db.commit()
                return False
            app.notes = "LinkedIn apply remained on listing page; manual review required."
            app.automation_log += "Still on LinkedIn page after apply click; manual review required.\n"
            await self._capture_blocker_details(
                external_page,
                app,
                user,
                db,
                reason="linkedin_apply_interaction_blocked",
                message=app.notes,
            )
            db.commit()
            return False

        # If LinkedIn routed to an intermediary board page, resolve official apply URL when possible.
        try:
            resolution = await resolve_official_apply_url(current_url, "linkedin")
            resolved = (resolution or {}).get("resolved_url")
            if resolved and resolved != current_url:
                app.automation_log += f"Resolved official external apply URL: {resolved}\n"
                db.commit()
                await external_page.goto(resolved, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        submitted = await self._handle_generic_apply(
            external_page,
            user,
            resume_path,
            app,
            db,
            answer_overrides=answer_overrides,
            safe_mode=safe_mode,
            require_confirmation=require_confirmation,
        )
        if switched_page:
            try:
                await external_page.close()
            except Exception:
                pass
        return submitted

    async def _handle_linkedin_easy_apply(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
        safe_mode: bool = True,
        require_confirmation: bool = True,
        trigger_button=None,
        trigger_label: str = "",
    ) -> bool:
        """Walk through LinkedIn Easy Apply modal and optionally submit."""
        if self._abort_if_stop_requested(db, app, "linkedin-easy-apply-start"):
            return False
        submitted = False
        try:
            apply_button = trigger_button
            apply_label = trigger_label or ""
            if not apply_button:
                for _ in range(3):
                    apply_button, apply_label = await self._pick_visible_linkedin_apply_button(page)
                    if apply_button and ("easy apply" in apply_label or "continue applying" in apply_label):
                        break
                    await asyncio.sleep(1)
            if not apply_button or not any(k in apply_label for k in ("easy apply", "continue applying")):
                app.notes = "LinkedIn Easy Apply button not found on this posting."
                app.automation_log += "Easy Apply button not detected after retries.\n"
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="linkedin_apply_action_missing",
                    message=app.notes,
                )
                db.commit()
                return False
            await apply_button.click(force=True)
            await asyncio.sleep(2)

            login_modal = await page.query_selector(
                "button.sign-in-form__submit, .contextual-sign-in-modal, form[action*='/login']"
            )
            if login_modal:
                app.notes = "LinkedIn login required before automation can continue."
                app.automation_log += (
                    "LinkedIn sign-in modal appeared before Easy Apply form. "
                    "Save LinkedIn browser state first, then retry automation.\n"
                )
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="linkedin_login_required",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "linkedin_authenticated_session",
                            "label": "LinkedIn Session",
                            "question": "Log into LinkedIn in the popup window and retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
                db.commit()
                return False

            for step in range(1, 11):
                if self._abort_if_stop_requested(db, app, f"linkedin-easy-apply-step-{step}"):
                    return False
                modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal, [role='dialog']")
                header = await (modal or page).query_selector("h3, h2")
                header_text = (await header.inner_text()).lower() if header else ""
                app.automation_log += f"Step {step}: {header_text}\n"
                db.commit()

                filled_step = await self._fill_linkedin_fields(page, user)
                filled_step += await self._fill_linkedin_modal_minimum_fields(
                    page, user, answer_overrides=answer_overrides
                )
                # Required screening questions often include radios/checkboxes (consent/yes-no).
                try:
                    filled_step += await self._fill_required_radios_and_checkboxes(
                        page, user, answer_overrides=answer_overrides
                    )
                except Exception:
                    pass
                try:
                    if await self._maybe_uncheck_linkedin_follow_company(page):
                        app.automation_log += "Unchecked LinkedIn 'Follow company/page' option.\n"
                        db.commit()
                except Exception:
                    pass
                if filled_step:
                    app.automation_log += f"Filled {filled_step} field(s) in this step.\n"
                    db.commit()

                if "resume" in header_text and resume_path:
                    selected = await page.query_selector(".jobs-document-upload__container--selected")
                    if not selected:
                        file_input = await page.query_selector("input[type='file']")
                        if file_input:
                            if self._is_supported_resume_upload(resume_path):
                                await file_input.set_input_files(os.path.abspath(resume_path))
                                app.automation_log += f"Uploaded resume: {os.path.basename(resume_path)}\n"
                                await asyncio.sleep(2)
                            else:
                                app.automation_log += (
                                    f"Skipped resume upload for unsupported file type: {os.path.basename(resume_path)}\n"
                                )

                submit_btn, _ = await self._find_clickable_button(
                    page,
                    ["submit application", "submit", "send application", "apply now"],
                )
                if submit_btn:
                    try:
                        if await self._maybe_uncheck_linkedin_follow_company(page):
                            app.automation_log += "Unchecked LinkedIn 'Follow company/page' option before submit.\n"
                            db.commit()
                    except Exception:
                        pass
                    if safe_mode or require_confirmation:
                        app.notes = "Ready for final submission; review in browser."
                        app.automation_log += "Reached final submit screen. Stopping for user review.\n"
                    else:
                        await submit_btn.click()
                        await asyncio.sleep(2)
                        app.automation_log += "Clicked LinkedIn final submit.\n"
                        submitted = True
                    break

                next_btn, _ = await self._find_clickable_button(
                    page,
                    ["continue to next step", "next", "review application", "review"],
                )
                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(2)
                else:
                    login_modal = await page.query_selector(
                        "button.sign-in-form__submit, .contextual-sign-in-modal, form[action*='/login']"
                    )
                    if login_modal:
                        app.notes = "LinkedIn login required before automation can continue."
                        app.automation_log += (
                            "LinkedIn sign-in prompt blocked Easy Apply steps.\n"
                        )
                        await self._capture_blocker_details(
                            page,
                            app,
                            user,
                            db,
                            reason="linkedin_login_required",
                            message=app.notes,
                            required_inputs=[
                                {
                                    "key": "linkedin_authenticated_session",
                                    "label": "LinkedIn Session",
                                    "question": "Log into LinkedIn in the popup window and retry.",
                                    "type": "manual_action",
                                    "required": True,
                                }
                            ],
                        )
                        db.commit()
                        return False
                    app.automation_log += "No Next/Review button. Check required fields.\n"
                    await self._capture_blocker_details(
                        page,
                        app,
                        user,
                        db,
                        reason="final_submit_detection_failed",
                        message="LinkedIn Easy Apply could not progress. Missing required fields may still exist.",
                    )
                    break

            db.commit()
            return submitted

        except Exception as e:
            app.automation_log += f"LinkedIn Easy Apply error: {e}\n"
            raise

    async def _maybe_uncheck_linkedin_follow_company(self, page: Page) -> bool:
        """
        LinkedIn Easy Apply often includes a checked "Follow company/page" checkbox.
        Uncheck it to avoid auto-following many companies.
        """
        modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal, [role='dialog']")
        container = modal or page

        # 1) Direct checkbox selectors commonly used by LinkedIn.
        selectors = [
            "input[type='checkbox'][id*='follow' i]",
            "input[type='checkbox'][name*='follow' i]",
            "input[type='checkbox'][aria-label*='follow' i]",
            "#follow-company-checkbox",
        ]
        for sel in selectors:
            try:
                boxes = await container.query_selector_all(sel)
            except Exception:
                boxes = []
            for box in boxes[:12]:
                try:
                    if not await box.is_visible():
                        continue
                    checked = await box.is_checked()
                    if checked:
                        await box.uncheck(force=True)
                        return True
                except Exception:
                    continue

        # 2) Match labels that mention follow + company/page and uncheck associated checkbox.
        try:
            labels = await container.query_selector_all("label")
        except Exception:
            labels = []
        for lab in labels[:120]:
            try:
                txt = ((await lab.inner_text()) or "").strip().lower()
                if "follow" not in txt:
                    continue
                if "company" not in txt and "page" not in txt:
                    continue

                target_id = (await lab.get_attribute("for") or "").strip()
                if target_id:
                    cb = await container.query_selector(f"input[type='checkbox']#{target_id}")
                    if cb and await cb.is_visible() and await cb.is_checked():
                        await cb.uncheck(force=True)
                        return True

                # Fallback: nested checkbox within label.
                cb2 = await lab.query_selector("input[type='checkbox']")
                if cb2 and await cb2.is_visible() and await cb2.is_checked():
                    await cb2.uncheck(force=True)
                    return True
            except Exception:
                continue

        # 3) Some builds render this as a switch.
        try:
            toggles = await container.query_selector_all(
                "[role='switch'][aria-label*='follow' i], [role='checkbox'][aria-label*='follow' i]"
            )
        except Exception:
            toggles = []
        for tg in toggles[:8]:
            try:
                if not await tg.is_visible():
                    continue
                state = (await tg.get_attribute("aria-checked") or "").strip().lower()
                if state == "true":
                    await tg.click(force=True)
                    return True
            except Exception:
                continue
        return False

    async def _handle_greenhouse_apply(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
        safe_mode: bool = True,
        require_confirmation: bool = True,
    ) -> bool:
        if self._abort_if_stop_requested(db, app, "greenhouse-start"):
            return False
        app.automation_log += "Greenhouse form mode: filling common fields.\n"
        return await self._handle_generic_apply(
            page,
            user,
            resume_path,
            app,
            db,
            answer_overrides=answer_overrides,
            safe_mode=safe_mode,
            require_confirmation=require_confirmation,
        )

    async def _handle_lever_apply(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
        safe_mode: bool = True,
        require_confirmation: bool = True,
    ) -> bool:
        if self._abort_if_stop_requested(db, app, "lever-start"):
            return False
        app.automation_log += "Lever form mode: filling common fields.\n"
        return await self._handle_generic_apply(
            page,
            user,
            resume_path,
            app,
            db,
            answer_overrides=answer_overrides,
            safe_mode=safe_mode,
            require_confirmation=require_confirmation,
        )

    async def _fill_external_field(self, page: Page | Frame, aliases: list[str], value: str) -> bool:
        """Fill first matching empty input/textarea field using common alias tokens."""
        if not value:
            return False

        selectors: list[str] = []
        for alias in aliases:
            token = alias.strip()
            if not token:
                continue
            selectors.extend(
                [
                    f"input[name*='{token}' i]:not([type='hidden'])",
                    f"input[id*='{token}' i]:not([type='hidden'])",
                    f"input[aria-label*='{token}' i]:not([type='hidden'])",
                    f"input[placeholder*='{token}' i]:not([type='hidden'])",
                    f"textarea[name*='{token}' i]",
                    f"textarea[id*='{token}' i]",
                    f"textarea[aria-label*='{token}' i]",
                    f"textarea[placeholder*='{token}' i]",
                ]
            )

        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
            except Exception:
                continue

            for el in elements[:8]:
                try:
                    if not await el.is_visible():
                        continue
                    if await el.get_attribute("disabled") is not None:
                        continue
                    if await el.get_attribute("readonly") is not None:
                        continue
                    current = (await el.input_value() or "").strip()
                    if current:
                        continue
                    await el.click(timeout=1000)
                    await el.fill(value)
                    return True
                except Exception:
                    continue

        return False

    async def _force_fill_external_field(self, page: Page | Frame, aliases: list[str], value: str) -> int:
        """
        Force-fill matching input/textarea fields even when they already contain values.
        Useful when portals report format errors (e.g., postal code) and stale values must be corrected.
        """
        if not value:
            return 0
        updated = 0
        selectors: list[str] = []
        for alias in aliases:
            token = alias.strip()
            if not token:
                continue
            selectors.extend(
                [
                    f"input[name*='{token}' i]:not([type='hidden']):not([type='password'])",
                    f"input[id*='{token}' i]:not([type='hidden']):not([type='password'])",
                    f"input[aria-label*='{token}' i]:not([type='hidden']):not([type='password'])",
                    f"input[placeholder*='{token}' i]:not([type='hidden']):not([type='password'])",
                    f"textarea[name*='{token}' i]",
                    f"textarea[id*='{token}' i]",
                    f"textarea[aria-label*='{token}' i]",
                    f"textarea[placeholder*='{token}' i]",
                ]
            )

        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
            except Exception:
                continue
            for el in elements[:10]:
                try:
                    if not await el.is_visible():
                        continue
                    if await el.get_attribute("disabled") is not None:
                        continue
                    if await el.get_attribute("readonly") is not None:
                        continue
                    current = (await el.input_value() or "").strip()
                    if current == str(value).strip():
                        continue
                    await el.click(timeout=1000)
                    await el.fill(str(value))
                    updated += 1
                except Exception:
                    continue
        return updated

    async def _find_clickable_button(self, page: Page | Frame, keywords: list[str]):
        """Find first visible enabled button-like element whose label contains any keyword."""
        lowered = [k.lower() for k in keywords]
        wants_submit = any("submit" in k or "complete" in k or "finish" in k for k in lowered)
        try:
            candidates = await page.query_selector_all(
                "button, input[type='button'], input[type='submit'], "
                "a[role='button'], [role='button'], a"
            )
        except Exception:
            # Common on SPA ATS portals: frames detach/reattach during render.
            return None, ""
        for btn in candidates[:300]:
            try:
                if not await btn.is_visible():
                    continue
                if await btn.get_attribute("disabled") is not None:
                    continue
                tag = ((await btn.evaluate("el => el.tagName")) or "").lower()
                label = ""
                if tag == "input":
                    label = (await btn.get_attribute("value") or "").strip().lower()
                if not label:
                    label = ((await btn.inner_text()) or "").strip().lower()
                if not label:
                    label = (await btn.get_attribute("aria-label") or "").strip().lower()
                if not label:
                    continue
                # Avoid false positives on auth/navigation dialogs.
                if "continue editing" in label:
                    continue
                if any(tok in label for tok in ("sign in", "log in", "create account")) and any(
                    k in lowered for k in ("submit", "next", "continue", "review", "apply")
                ):
                    continue
                if any(k in label for k in lowered):
                    return btn, label
                # Fallback: many ATS portals expose generic unlabeled submit controls.
                if wants_submit and tag in {"button", "input"}:
                    btn_type = (await btn.get_attribute("type") or "").strip().lower()
                    if btn_type == "submit":
                        return btn, label or "submit"
            except Exception:
                continue
        return None, ""

    async def _click_external_apply_cta(self, page: Page, app: Application, db) -> Page:
        """
        Best-effort: click an 'Apply/Start application' CTA on external portals.
        Some ATS pages show a job detail page first with an apply button that reveals the form.
        Returns the active page (may switch to a newly opened tab).
        """
        scopes = self._iter_scopes_prioritized(page)
        keywords = [
            "start application",
            "begin application",
            "apply now",
            "apply",
            "continue application",
        ]
        for scope in scopes:
            try:
                btn, label = await self._find_clickable_button(scope, keywords)
                if not btn:
                    continue
                if "submit" in (label or ""):
                    continue
                app.automation_log += "Clicked external Apply CTA to open the application form.\n"
                db.commit()
                before_url = page.url
                try:
                    async with page.context.expect_page(timeout=4000) as new_page_info:
                        await btn.click(timeout=7000, no_wait_after=True, force=True)
                    new_page = await new_page_info.value
                    await new_page.wait_for_load_state("domcontentloaded")
                    return new_page
                except Exception:
                    await btn.click(timeout=7000, no_wait_after=True, force=True)
                    await asyncio.sleep(2)
                    if page.url != before_url:
                        return page
                    return page
            except Exception:
                continue
        return page

    async def _maybe_dismiss_portal_popups(self, page: Page, app: Application, db) -> bool:
        """
        Best-effort dismissal for common consent/cookie/privacy overlays that block interaction.
        Returns True if we clicked something that likely dismissed an overlay.
        """
        scopes = self._iter_scopes_prioritized(page)

        # 1) Close/X buttons (preferred). Many popups can be dismissed without accepting.
        close_selectors = [
            # Common close buttons / icons
            "button[aria-label*='close' i]",
            "button[title*='close' i]",
            "a[aria-label*='close' i]",
            "[data-testid*='close' i]",
            "[data-automation-id*='close' i]",
            ".modal__dismiss-btn, .modal__close, .modal-close, .popup-close, .close",
            # OneTrust
            ".onetrust-close-btn-handler",
            # Common modal close affordances
            "button[data-dismiss='modal']",
            "button[class*='close' i]",
        ]
        host_l = self._host(page.url or "")
        is_talemetry_family = ("ttcportals.com" in host_l) or ("talemetry.com" in host_l)

        close_texts = ["×", "✕", "close", "dismiss"]
        if is_talemetry_family:
            close_texts.extend(["continue later"])

        for scope in scopes:
            for sel in close_selectors:
                try:
                    loc = scope.locator(sel)
                    if await loc.count() <= 0:
                        continue
                    el = loc.first
                    try:
                        await el.wait_for(state="visible", timeout=1500)
                    except Exception:
                        pass
                    if await el.is_visible():
                        await el.click(timeout=2000, force=True, no_wait_after=True)
                        app.automation_log += f"Closed portal popup via selector: {sel}\n"
                        db.commit()
                        await asyncio.sleep(1.0)
                        return True
                except Exception:
                    continue

            try:
                candidates = scope.locator("button, a, [role='button']")
                for txt in close_texts:
                    try:
                        loc = candidates.filter(has_text=txt)
                        if await loc.count() <= 0:
                            continue
                        el = loc.first
                        if await el.is_visible():
                            await el.click(timeout=2000, force=True, no_wait_after=True)
                            app.automation_log += f"Closed portal popup via text: {txt}\n"
                            db.commit()
                            await asyncio.sleep(1.0)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass

        # 2) Consent/cookie/privacy banners (accept/continue/reject).
        # Prefer targeted selectors first (safe) and only then attempt text-based clicks when a modal/banner is likely present.
        direct_selectors = [
            # Workday cookie banner
            "button[data-automation-id='legalNoticeAcceptButton']",
            "button[data-automation-id='legalNoticeDeclineButton']",
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            ".onetrust-close-btn-handler",
            # Generic accept/agree buttons used by privacy agreements
            "button[aria-label*='agree' i]",
            "button[aria-label*='accept' i]",
            ".cookie-accept",
            ".cookie__accept",
            ".consent-accept",
            ".consent__accept",
        ]

        accept_texts = [
            "accept all",
            "accept",
            "agree",
            "i agree",
            "allow all",
            "ok",
            "got it",
            "confirm",
            "yes, i agree",
            "i understand",
        ]
        if is_talemetry_family:
            accept_texts.extend(
                [
                    "continue later",
                    "continue application",
                    "resume application",
                ]
            )
        reject_texts = [
            "reject all",
            "reject",
            "deny",
            "no thanks",
        ]
        text_priority = accept_texts + reject_texts

        likely_overlay = False
        try:
            overlay_locators = [
                "[data-automation-id='legalNotice']",
                "#onetrust-banner-sdk",
                "[id*='consent' i]",
                "[role='dialog']",
                ".modal, .popup, .overlay, .backdrop",
                "#talemetry_apply_container, #talemetry_apply_pane",
            ]
            for scope in scopes:
                for sel in overlay_locators:
                    try:
                        loc = scope.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            likely_overlay = True
                            break
                    except Exception:
                        continue
                if likely_overlay:
                    break
        except Exception:
            likely_overlay = False

        clicked = False
        for scope in scopes:
            for sel in direct_selectors:
                try:
                    loc = scope.locator(sel)
                    if await loc.count() <= 0:
                        continue
                    el = loc.first
                    try:
                        await el.wait_for(state="visible", timeout=2000)
                    except Exception:
                        pass
                    if await el.is_visible():
                        await el.click(timeout=2000, force=True, no_wait_after=True)
                        clicked = True
                        app.automation_log += f"Dismissed portal overlay via selector: {sel}\n"
                        db.commit()
                        break
                except Exception:
                    continue
            if clicked:
                break

            if not likely_overlay:
                continue

            try:
                candidates = scope.locator("button, a, [role='button'], input[type='button'], input[type='submit']")
                for txt in text_priority:
                    try:
                        loc = candidates.filter(has_text=txt)
                        if await loc.count() <= 0:
                            continue
                        el = loc.first
                        if await el.is_visible():
                            await el.click(timeout=2000, force=True, no_wait_after=True)
                            clicked = True
                            app.automation_log += f"Dismissed portal overlay via text: {txt}\n"
                            db.commit()
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            except Exception:
                continue

        if clicked:
            try:
                self._record_issue_event(db, app, app.job if app else None, None, "Dismissed blocking portal popup/overlay.", event_type="resolved")
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return clicked

    @staticmethod
    def _iter_scopes(page: Page) -> list[Page | Frame]:
        """
        Iterate search scopes for external forms. Many portals embed apply forms in iframes.
        """
        scopes: list[Page | Frame] = [page]
        try:
            frames = list(page.frames)
            for fr in frames:
                if fr == page.main_frame:
                    continue
                scopes.append(fr)
        except Exception:
            pass
        return scopes

    @staticmethod
    def _scope_url(scope: Page | Frame) -> str:
        try:
            return (getattr(scope, "url", "") or "").lower()
        except Exception:
            return ""

    async def _scope_has_fillable_controls(self, scope: Page | Frame, minimum_visible: int = 1) -> bool:
        """
        Detect whether a scope currently exposes fillable controls.
        Helps avoid clicking navigation buttons on parent job pages instead of embedded apply forms.
        """
        try:
            controls = scope.locator(
                "input:not([type='hidden']), textarea, select, "
                "[role='textbox'], [role='combobox'], [contenteditable='true'], "
                "input[type='radio'], input[type='checkbox']"
            )
            count = min(await controls.count(), 80)
            visible = 0
            for idx in range(count):
                try:
                    if await controls.nth(idx).is_visible():
                        visible += 1
                        if visible >= minimum_visible:
                            return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _iter_scopes_prioritized(self, page: Page) -> list[Page | Frame]:
        """
        Prefer scopes likely to contain the real application form (embedded ATS iframes, /apply pages, etc).
        This reduces false positives from header search boxes / language menus.
        """
        scopes = self._iter_scopes(page)

        def score(scope: Page | Frame) -> int:
            url_l = self._scope_url(scope)
            s = 0
            if "apply.talemetry.com" in url_l or "talemetry" in url_l:
                s += 50
            if "myworkdayjobs.com" in url_l and "/apply" in url_l:
                s += 45
            if any(
                tok in url_l
                for tok in (
                    "greenhouse.io",
                    "lever.co",
                    "icims.com",
                    "smartrecruiters.com",
                    "ashbyhq.com",
                )
            ):
                s += 40
            if any(tok in url_l for tok in ("/apply", "application", "candidate", "applicant")):
                s += 20
            if scope is not page:
                s += 5
            return s

        scopes.sort(key=score, reverse=True)
        return scopes

    async def _wait_for_workday_hydration(
        self, page: Page, app: Optional[Application] = None, db=None, max_wait_seconds: float = 18.0
    ) -> None:
        """
        Workday job pages can stay on a transient loading shell for a few seconds before
        rendering CTA/form elements. Wait for hydration signals before proceeding.
        """
        try:
            host = self._host(page.url or "")
            if "myworkdayjobs.com" not in host:
                return
        except Exception:
            return

        hydration_selectors = [
            "[data-automation-id='adventureButton']",
            "[data-automation-id='applyAdventurePage']",
            "[data-automation-id='applyFlowPage']",
            "[data-automation-id='signInContent']",
            "[data-automation-id='createAccountSubmitButton']",
            "input[data-automation-id='email']",
            "button[data-automation-id='bottom-navigation-next-button']",
        ]

        wait_step = 1.5
        waited = 0.0
        announced_wait = False
        while waited < max_wait_seconds:
            try:
                ready = False
                for sel in hydration_selectors:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        ready = True
                        break
                if ready:
                    if waited >= 1.5 and app is not None and db is not None:
                        app.automation_log += f"Workday page hydrated after {waited:.1f}s.\n"
                        db.commit()
                    return
            except Exception:
                pass

            try:
                loading_loc = page.locator("[data-automation-id='loading']")
                if (
                    not announced_wait
                    and await loading_loc.count() > 0
                    and app is not None
                    and db is not None
                ):
                    app.automation_log += "Workday page is still loading; waiting for hydration...\n"
                    db.commit()
                    announced_wait = True
            except Exception:
                pass

            await asyncio.sleep(wait_step)
            waited += wait_step

        if app is not None and db is not None:
            app.automation_log += (
                f"Workday hydration timeout after {max_wait_seconds:.0f}s; continuing best-effort.\n"
            )
            db.commit()

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _path(url: str) -> str:
        try:
            return (urllib.parse.urlparse(url).path or "").lower()
        except Exception:
            return ""

    async def _looks_like_application_form(self, page: Page) -> bool:
        """
        Heuristic: True when we're likely already in an application form view,
        so we should avoid re-clicking an "Apply" CTA on a job detail page.
        """
        url_l = (page.url or "").lower()
        if any(tok in url_l for tok in ("/apply", "/application", "candidate", "applicant")):
            return True

        try:
            loc = page.locator("input[type='file']")
            if await loc.count() > 0 and await loc.first.is_visible():
                return True
        except Exception:
            pass

        try:
            inputs = page.locator("input:not([type='hidden']), textarea, select")
            n = min(await inputs.count(), 80)
            visible = 0
            for i in range(n):
                try:
                    if await inputs.nth(i).is_visible():
                        visible += 1
                        if visible >= 6:
                            return True
                except Exception:
                    continue
        except Exception:
            pass

        return False

    async def _ensure_external_apply_form_open(self, page: Page, app: Application, db) -> Page:
        """
        Many ATS pages show a job detail first and only mount the form after clicking Apply.
        This attempts to reach the actual form view (best-effort).
        """
        try:
            if await self._looks_like_application_form(page):
                return page
        except Exception:
            pass

        url = page.url or ""
        host = self._host(url)
        path = self._path(url)

        # Workday: click the "Apply" adventure button on the job posting page.
        if "myworkdayjobs.com" in host and "/apply" not in path:
            try:
                await self._wait_for_workday_hydration(page, app, db)
            except Exception:
                pass
            # Workday cookie banner often blocks navigation; accept/decline first.
            try:
                cookie = page.locator(
                    "button[data-automation-id='legalNoticeAcceptButton'], button[data-automation-id='legalNoticeDeclineButton']"
                )
                try:
                    await cookie.first.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                if await cookie.count() > 0 and await cookie.first.is_visible():
                    # Prefer Accept Cookies when available.
                    accept = page.locator("button[data-automation-id='legalNoticeAcceptButton']")
                    target = accept.first if await accept.count() > 0 else cookie.first
                    app.automation_log += "Workday: dismissing cookie banner.\n"
                    db.commit()
                    await target.click(timeout=3000, force=True, no_wait_after=True)
                    await asyncio.sleep(1.0)
            except Exception:
                pass
            try:
                await self._maybe_dismiss_portal_popups(page, app, db)
            except Exception:
                pass
            challenge_reason = await self._detect_anti_bot_challenge(page)
            if challenge_reason:
                app.notes = (
                    "Application portal blocked by anti-bot verification challenge; "
                    "complete verification manually, then retry automation."
                )
                app.automation_log += (
                    f"Blocked after popup/overlay handling by anti-bot challenge "
                    f"({challenge_reason}).\n"
                )
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="anti_bot_challenge",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "manual_challenge_verification",
                            "label": "Manual Verification",
                            "question": "Complete anti-bot verification in the opened browser window, then retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
                db.commit()
                return False
            try:
                loc = page.locator(
                    "a[data-automation-id='adventureButton'], button[data-automation-id='adventureButton']"
                )
                try:
                    await loc.first.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                if await loc.count() > 0 and await loc.first.is_visible():
                    href = ""
                    try:
                        href = (await loc.first.get_attribute("href") or "").strip()
                    except Exception:
                        href = ""
                    if not href:
                        try:
                            href = (await loc.first.evaluate("el => el.href || ''") or "").strip()
                        except Exception:
                            href = ""
                    app.automation_log += "Workday: clicking Apply CTA to open application form.\n"
                    db.commit()
                    # Workday sometimes opens a new tab; otherwise it navigates same-tab.
                    try:
                        async with page.context.expect_page(timeout=3500) as new_page_info:
                            await loc.first.click(timeout=7000, force=True, no_wait_after=True)
                        new_page = await new_page_info.value
                        await new_page.wait_for_load_state("domcontentloaded", timeout=30000)
                        page = new_page
                        app.automation_log += "Workday: apply opened in a new tab.\n"
                        db.commit()
                    except Exception:
                        try:
                            await loc.first.click(timeout=7000, force=True, no_wait_after=True)
                        except Exception:
                            # Detached/intercepted element fallback.
                            try:
                                await loc.first.evaluate("el => el.click()")
                            except Exception:
                                pass
                    try:
                        await page.wait_for_url("**/apply**", timeout=15000)
                    except Exception:
                        pass
                    # If click didn't navigate, force navigation using the explicit href.
                    try:
                        if "/apply" not in (page.url or "").lower() and href:
                            app.automation_log += "Workday: apply click did not navigate; forcing navigation via href.\n"
                            db.commit()
                            # Try opening in a dedicated page first (mirrors popup behavior and avoids same-tab instability).
                            opened = False
                            try:
                                new_page = await page.context.new_page()
                                await new_page.goto(href, wait_until="domcontentloaded", timeout=60000)
                                page = new_page
                                opened = True
                                app.automation_log += "Workday: opened apply URL in a new window.\n"
                                db.commit()
                            except Exception:
                                opened = False
                            if not opened:
                                await page.goto(href, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    try:
                        await self._wait_for_workday_hydration(page, app, db)
                    except Exception:
                        pass
                    # If still on non-apply Workday page, do not keep looping generic steps.
                    try:
                        current_l = (page.url or "").lower()
                        if "myworkdayjobs.com" in current_l and "/apply" not in current_l:
                            adv_after = page.locator(
                                "a[data-automation-id='adventureButton'], button[data-automation-id='adventureButton']"
                            )
                            if await adv_after.count() > 0:
                                app.notes = (
                                    "Workday apply action did not open the application form. "
                                    "Retry once; if it persists, open job in browser and click Apply manually, then rerun."
                                )
                                app.automation_log += "Workday apply form did not open from CTA.\n"
                                db.commit()
                    except Exception:
                        pass
                    await asyncio.sleep(2.5)
                    return page
            except Exception:
                pass

        # If the page already has the Talemetry apply iframe mounted, we're done.
        try:
            iframe = page.locator("iframe#talemetry_apply_iframe, iframe[id*='talemetry' i]")
            if await iframe.count() > 0:
                return page
        except Exception:
            pass

        # Generic fallback: click an "Apply/Start application" CTA once.
        try:
            before = page.url
            page = await self._click_external_apply_cta(page, app, db)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2.5)
            try:
                host_l = self._host(page.url or before)
                if "ttcportals.com" in host_l:
                    talemetry_iframe = page.locator(
                        "iframe#talemetry_apply_iframe, iframe[id*='talemetry' i], iframe[src*='apply.talemetry.com' i]"
                    )
                    try:
                        await talemetry_iframe.first.wait_for(state="visible", timeout=10000)
                    except Exception:
                        pass
                    if await talemetry_iframe.count() > 0:
                        app.automation_log += "Detected Talemetry apply iframe after Apply CTA.\n"
                        db.commit()
                        return page
            except Exception:
                pass
            after = page.url
            if after != before:
                return page
        except Exception:
            pass

        return page

    async def _detect_anti_bot_challenge(self, page: Page) -> Optional[str]:
        """
        Detect anti-bot / verification interstitials that block automation.
        Returns a short reason when detected, otherwise None.
        """
        def _match_reason(text: str) -> Optional[str]:
            checks = [
                ("cloudflare", "cloudflare_verification"),
                ("performing security verification", "security_verification"),
                ("security verification", "security_verification"),
                ("just a moment", "security_interstitial"),
                ("cf-chl", "cloudflare_challenge"),
                ("challenge-platform", "challenge_platform"),
                ("ray id", "challenge_ray_id"),
                ("enable javascript and cookies", "js_cookie_challenge"),
                ("enable javascript", "js_cookie_challenge"),
                ("verify you are not a bot", "bot_verification"),
                ("verify you are human", "human_verification"),
                ("are you human", "human_verification"),
                ("checking your browser", "security_interstitial"),
            ]
            for token, reason in checks:
                if token in text:
                    return reason
            return None

        try:
            title = ((await page.title()) or "").lower()
        except Exception:
            title = ""
        try:
            body = ((await page.inner_text("body")) or "").lower()
        except Exception:
            body = ""
        try:
            html = ((await page.content()) or "").lower()
        except Exception:
            html = ""

        text = " ".join([title, body[:20000], html[:20000]])
        reason = _match_reason(text)
        if reason:
            return reason

        # Some portals render bot checks inside embedded frames; scan them too.
        try:
            frames = list(page.frames or [])
        except Exception:
            frames = []

        for fr in frames[:10]:
            try:
                fr_url = (fr.url or "").lower()
            except Exception:
                fr_url = ""
            reason = _match_reason(fr_url)
            if reason:
                return reason
            fr_body = ""
            fr_html = ""
            try:
                fr_body = ((await fr.inner_text("body")) or "").lower()
            except Exception:
                fr_body = ""
            try:
                fr_html = ((await fr.content()) or "").lower()
            except Exception:
                fr_html = ""
            reason = _match_reason(" ".join([fr_body[:15000], fr_html[:15000]]))
            if reason:
                return reason

        return None

    async def _detect_workday_login_wall(self, page: Page) -> bool:
        """
        Workday frequently requires sign-in / account creation before an application can proceed.
        Detect the presence of Workday's sign-in/create account form (and often reCAPTCHA).
        """
        try:
            url_l = (page.url or "").lower()
            if "myworkdayjobs.com" not in url_l:
                return False
        except Exception:
            return False

        selectors = [
            "[data-automation-id='signInContent']",
            "[data-automation-id='signInFormo']",
            "input[data-automation-id='password']",
            "input[data-automation-id='verifyPassword']",
            "[data-automation-id='noCaptchaWrapper']",
            "[data-automation-id='createAccountSubmitButton']",
        ]
        # Workday occasionally renders auth fragments in nested frames; scan all scopes.
        scopes = self._iter_scopes_prioritized(page)
        for scope in scopes:
            for sel in selectors:
                try:
                    loc = scope.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        return True
                    # Visibility can be flaky during hydration; presence is a useful signal too.
                    if await loc.count() > 0:
                        return True
                except Exception:
                    continue
        try:
            body_text = ((await page.inner_text("body")) or "").lower()
            if any(
                tok in body_text
                for tok in (
                    "create account",
                    "sign in",
                    "already have an account",
                    "enter your password",
                )
            ):
                return True
        except Exception:
            pass
        return False

    async def _wait_for_workday_login(
        self, page: Page, app: Application, db, timeout_seconds: int
    ) -> bool:
        """
        In headed mode, wait for the user to complete Workday sign-in/create account.
        Returns True if the login wall appears cleared.
        """
        app.automation_log += (
            f"Workday login/account creation required. Waiting up to {timeout_seconds}s for you to complete sign-in...\n"
        )
        db.commit()

        deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if self._abort_if_stop_requested(db, app, "workday-login-wait"):
                return False
            await asyncio.sleep(2.0)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            try:
                if page.is_closed():
                    # Recover from page close/crash by reopening the current Workday URL.
                    reopen_url = ""
                    try:
                        reopen_url = page.url or ""
                    except Exception:
                        reopen_url = ""
                    if reopen_url:
                        try:
                            page = await page.context.new_page()
                            await page.goto(reopen_url, wait_until="domcontentloaded", timeout=60000)
                            app.automation_log += "Workday login window closed unexpectedly; reopened it.\n"
                            db.commit()
                            continue
                        except Exception:
                            return False
                    return False
            except Exception:
                pass
            try:
                if not await self._detect_workday_login_wall(page):
                    # Heuristic: user passed login wall. Give the SPA a moment.
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
                    return True
            except Exception:
                continue

        return False

    async def _has_workday_apply_navigation(self, page: Page) -> bool:
        """
        True when Workday is on an actual application step (as opposed to job details shell).
        """
        try:
            url_l = (page.url or "").lower()
            if "myworkdayjobs.com" not in url_l:
                return False
        except Exception:
            return False
        try:
            if await self._detect_workday_login_wall(page):
                return False
        except Exception:
            return False
        selectors = [
            "button[data-automation-id='bottom-navigation-next-button']",
            "[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='bottom-navigation-submit-button']",
            "[data-automation-id='bottom-navigation-submit-button']",
            "button[data-automation-id='bottom-navigation-back-button']",
            "[data-automation-id='bottom-navigation-back-button']",
            "[data-automation-id='applyFlowPage']",
            "[data-automation-id='click_filter']",
            "[data-automation-id='click_filter'][aria-label*='save and continue' i]",
            "[data-automation-id='click_filter'][aria-label*='review' i]",
            "[data-automation-id='click_filter'][aria-label*='submit' i]",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _find_workday_navigation_control(self, page: Page) -> tuple[Optional[Any], str]:
        """
        Workday often hides native buttons and exposes clickable overlays (`click_filter`)
        with labels like "Save and Continue" / "Review and Submit".
        """
        selectors = [
            "[data-automation-id='click_filter'][aria-label]",
            "[data-automation-id='click_filter']",
            "button[data-automation-id='bottom-navigation-next-button']",
            "[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='bottom-navigation-submit-button']",
            "[data-automation-id='bottom-navigation-submit-button']",
            "button[aria-label*='save and continue' i]",
            "button[aria-label*='review and submit' i]",
            "button[aria-label*='submit' i]",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(await loc.count(), 20)
            except Exception:
                continue
            for i in range(count):
                try:
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    if (await el.get_attribute("disabled")) is not None:
                        continue
                    if ((await el.get_attribute("aria-disabled")) or "").strip().lower() == "true":
                        continue
                    label = (
                        (await el.get_attribute("aria-label"))
                        or (await el.get_attribute("title"))
                        or (await el.inner_text())
                        or ""
                    ).strip().lower()
                    if not label:
                        try:
                            label = (
                                (
                                    await el.evaluate(
                                        """(node) => {
                                            const candidates = [
                                                node,
                                                node.closest('button'),
                                                node.parentElement,
                                                node.parentElement && node.parentElement.querySelector('button'),
                                                node.closest('[data-automation-id]'),
                                            ].filter(Boolean);
                                            for (const c of candidates) {
                                                const txt =
                                                    (c.getAttribute && (c.getAttribute('aria-label') || c.getAttribute('title'))) ||
                                                    (c.innerText || c.textContent || '');
                                                if (txt && txt.trim()) return txt.trim();
                                            }
                                            return '';
                                        }"""
                                    )
                                )
                                or ""
                            ).strip().lower()
                        except Exception:
                            label = ""
                    if not label:
                        continue
                    if any(tok in label for tok in ("sign in", "log in", "create account", "continue editing")):
                        continue
                    if any(
                        tok in label
                        for tok in (
                            "save and continue",
                            "continue to next",
                            "next",
                            "review",
                            "submit",
                            "finish",
                            "complete",
                            "send application",
                        )
                    ):
                        return el, label
                except Exception:
                    continue
        return None, ""

    @staticmethod
    def _default_salary_answer(meta: str, user: UserProfile) -> str:
        expected_lpa = user.expected_ctc_lpa if user and user.expected_ctc_lpa is not None else None
        current_lpa = user.current_ctc_lpa if user and user.current_ctc_lpa is not None else None
        use_lpa = expected_lpa
        if any(k in meta for k in ("current", "present", "existing")):
            use_lpa = current_lpa if current_lpa is not None else expected_lpa
        if use_lpa is None:
            return "0"
        if any(k in meta for k in ("monthly", "per month", "/month")):
            return str(int(round((use_lpa * 100000) / 12)))
        # If units are explicit INR/annual, convert to absolute amount.
        if any(k in meta for k in ("inr", "rupee", "per annum", "annual", "yearly")):
            return str(int(round(use_lpa * 100000)))
        if any(k in meta for k in ("lpa", "lakh", "salary", "ctc", "compensation")):
            return str(int(round(use_lpa)))
        return str(int(round(use_lpa)))

    @staticmethod
    def _preferred_binary(meta: str, user: UserProfile) -> Optional[str]:
        """Return 'yes' or 'no' when a binary choice can be inferred."""
        if any(
            k in meta
            for k in (
                "applied in the past",
                "applied before",
                "previously applied",
                "have you applied",
                "have you previously worked for",
                "previously worked for",
                "worked here before",
                "worked for this company",
                "worked for any subsidiary",
                "subsidiary",
            )
        ):
            return "no"
        if any(k in meta for k in ("sponsor", "sponsorship")):
            if user and user.requires_sponsorship is not None:
                return "yes" if user.requires_sponsorship else "no"
            return "no"
        if any(k in meta for k in ("authorized", "work authorization", "legally")):
            if user and user.requires_sponsorship is not None:
                return "no" if user.requires_sponsorship else "yes"
            return "yes"
        if any(k in meta for k in ("relocate", "relocation")):
            if user and user.willing_to_relocate is not None:
                return "yes" if user.willing_to_relocate else "no"
            return "yes"
        if any(k in meta for k in ("immediate", "join now", "available to join")):
            if user and user.can_join_immediately is not None:
                return "yes" if user.can_join_immediately else "no"
            if user and user.notice_period_days == 0:
                return "yes"
            return "no"
        if any(k in meta for k in ("experience", "comfortable", "do you have", "are you able")):
            return "yes"
        return None

    async def _choose_select_option(
        self,
        sel,
        meta: str,
        user: UserProfile,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        options = await sel.query_selector_all("option")
        parsed: list[tuple[str, str]] = []
        for opt in options:
            value = (await opt.get_attribute("value") or "").strip()
            text = ((await opt.inner_text()) or "").strip()
            if not value:
                continue
            low = text.lower()
            if any(tok in low for tok in ("select", "choose", "please", "none")):
                continue
            parsed.append((value, low))
        if not parsed:
            return None

        # User policy defaults to unblock submissions with deterministic answers.
        if any(
            tok in meta
            for tok in (
                "how did you hear",
                "hear about us",
                "where did you hear",
                "source of application",
                "referral source",
                "job source",
            )
        ):
            for value, low in parsed:
                if "social media" in low:
                    return value
            for value, low in parsed:
                if "linkedin" in low:
                    return value
        if any(tok in meta for tok in ("social media platform", "which social media", "source platform", "social channel")):
            for value, low in parsed:
                if "linkedin" in low:
                    return value

        if any(tok in meta for tok in ("phone type", "contact type", "type of phone", "number type")):
            for value, low in parsed:
                if "mobile" in low:
                    return value
        if any(tok in meta for tok in ("phone device type", "device type")) and "phone" in meta:
            for value, low in parsed:
                if "mobile" in low:
                    return value

        if any(tok in meta for tok in ("country code", "dial code", "phone country", "mobile country code")) and any(
            tok in meta for tok in ("phone", "mobile", "contact", "dial")
        ):
            for value, low in parsed:
                if "+91" in low or "india" in low:
                    return value
        if any(tok in meta for tok in ("country",)) and not any(tok in meta for tok in ("country code", "dial code")):
            for value, low in parsed:
                if "india" in low:
                    return value
        if any(tok in meta for tok in ("phone extension", "extension")):
            for value, low in parsed:
                if any(k in low for k in ("na", "n/a", "none", "0")):
                    return value

        explicit_key, explicit_value = self._resolve_field_value(
            meta, "select", user, answer_overrides
        )
        if explicit_value not in (None, ""):
            target = str(explicit_value).strip().lower()
            for value, low in parsed:
                if target == low or target in low:
                    return value
            if explicit_key in {"can_join_immediately", "willing_to_relocate", "requires_sponsorship"}:
                yn = self._as_yes_no(explicit_value)
                if yn:
                    yn_l = yn.lower()
                    for value, low in parsed:
                        if yn_l == "yes" and any(k in low for k in ("yes", "authorized", "immediate", "willing")):
                            return value
                        if yn_l == "no" and any(k in low for k in ("no", "not", "unwilling")):
                            return value

        binary = self._preferred_binary(meta, user)
        if binary:
            for value, low in parsed:
                if binary == "yes" and any(k in low for k in ("yes", "authorized", "immediate", "willing")):
                    return value
                if binary == "no" and any(k in low for k in ("no", "not", "unwilling")):
                    return value

        if any(k in meta for k in ("notice", "join", "availability")):
            notice = user.notice_period_days if user and user.notice_period_days is not None else 1
            if notice == 0:
                for value, low in parsed:
                    if any(k in low for k in ("0", "immediate", "join immediately", "same day")):
                        return value
            for value, low in parsed:
                if any(k in low for k in ("1", "2", "7", "15", "30")):
                    return value

        if any(k in meta for k in ("salary", "ctc", "compensation", "expected pay", "current pay")):
            target = user.expected_ctc_lpa if user and user.expected_ctc_lpa is not None else None
            if any(k in meta for k in ("current", "present", "existing")):
                target = user.current_ctc_lpa if user and user.current_ctc_lpa is not None else target
            if target is not None:
                target_int = int(round(target))
                exact_tokens = [f"{target_int}", f"{target_int}.0"]
                for value, low in parsed:
                    if any(tok in low for tok in exact_tokens):
                        return value
            # Fallback: choose the lowest non-placeholder salary band.
            return parsed[0][0]

        if any(k in meta for k in ("experience", "years", "yrs")):
            for value, low in parsed:
                if any(k in low for k in ("1", "2", "3", "3+", "2+")):
                    return value

        return parsed[0][0]

    async def _fill_non_native_dropdowns(
        self,
        scope: Page | Frame,
        user: UserProfile,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Best-effort filler for non-<select> dropdowns (combobox/listbox patterns).
        Used for LinkedIn screening questions and some ATS portals.
        """
        filled = 0
        try:
            root_page = scope.page if isinstance(scope, Frame) else scope
        except Exception:
            root_page = scope  # type: ignore

        try:
            combos = await scope.query_selector_all(
                "[role='combobox'], button[aria-haspopup='listbox'], [role='button'][aria-haspopup='listbox']"
            )
        except Exception:
            return 0

        effective_overrides = self._augment_overrides_with_defaults(user, answer_overrides)
        for combo in combos[:40]:
            try:
                if not await combo.is_visible():
                    continue
                meta_parts: list[str] = []
                for attr in ("name", "id", "aria-label", "aria-labelledby", "placeholder"):
                    try:
                        meta_parts.append((await combo.get_attribute(attr) or "").strip())
                    except Exception:
                        continue
                combo_text = ((await combo.inner_text()) or "").strip()
                meta_parts.append(combo_text)
                try:
                    meta_parts.append(await self._infer_field_context_text(combo))
                except Exception:
                    pass
                meta = " ".join([p for p in meta_parts if p]).lower()
                if not meta or any(k in meta for k in ("language", "search", "filter")):
                    continue

                # Skip if it already looks selected (avoid changing user defaults).
                if combo_text and not any(tok in combo_text.lower() for tok in ("select", "choose", "please select")):
                    if 2 <= len(combo_text.strip()) <= 80:
                        continue

                try:
                    await combo.click(timeout=1500, force=True)
                except Exception:
                    continue
                await asyncio.sleep(0.6)

                # Options often render in a portal/global overlay; search on the root page.
                options = root_page.locator("[role='option']")
                opt_n = min(await options.count(), 30)
                if opt_n <= 0:
                    continue

                option_texts: list[tuple[int, str]] = []
                for i in range(opt_n):
                    try:
                        el = options.nth(i)
                        if not await el.is_visible():
                            continue
                        txt = ((await el.inner_text()) or "").strip()
                        if not txt:
                            continue
                        low = txt.lower()
                        if any(tok in low for tok in ("select", "choose", "please select")):
                            continue
                        option_texts.append((i, low))
                    except Exception:
                        continue
                if not option_texts:
                    continue

                chosen_index = option_texts[0][0]
                if any(
                    tok in meta
                    for tok in (
                        "how did you hear",
                        "hear about us",
                        "where did you hear",
                        "source of application",
                        "referral source",
                        "job source",
                    )
                ):
                    for i, low in option_texts:
                        if "social media" in low:
                            chosen_index = i
                            break
                    for i, low in option_texts:
                        if "linkedin" in low:
                            chosen_index = i
                            break
                if any(tok in meta for tok in ("social media platform", "which social media", "source platform", "social channel")):
                    for i, low in option_texts:
                        if "linkedin" in low:
                            chosen_index = i
                            break

                if any(tok in meta for tok in ("phone type", "contact type", "type of phone", "number type")):
                    for i, low in option_texts:
                        if "mobile" in low:
                            chosen_index = i
                            break

                if any(tok in meta for tok in ("country code", "dial code", "phone country", "mobile country code")) and any(
                    tok in meta for tok in ("phone", "mobile", "contact", "dial")
                ):
                    for i, low in option_texts:
                        if "+91" in low or "india" in low:
                            chosen_index = i
                            break
                if "country" in meta and not any(tok in meta for tok in ("country code", "dial code")):
                    for i, low in option_texts:
                        if "india" in low:
                            chosen_index = i
                            break

                key, explicit = self._resolve_field_value(
                    meta, "select", user, effective_overrides
                )
                explicit_applied = False
                if explicit not in (None, ""):
                    exp = str(explicit).strip().lower()
                    matched = False
                    for i, low in option_texts:
                        if exp == low or exp in low:
                            chosen_index = i
                            matched = True
                            explicit_applied = True
                            break
                    if not matched and key in {"can_join_immediately", "willing_to_relocate", "requires_sponsorship"}:
                        yn = self._as_yes_no(explicit)
                        if yn:
                            yn_l = yn.lower()
                            for i, low in option_texts:
                                if yn_l == "yes" and any(k in low for k in ("yes", "authorized", "immediate", "willing")):
                                    chosen_index = i
                                    explicit_applied = True
                                    break
                                if yn_l == "no" and any(k in low for k in ("no", "not", "unwilling")):
                                    chosen_index = i
                                    explicit_applied = True
                                    break

                if not explicit_applied:
                    binary = self._preferred_binary(meta, user)
                    if binary:
                        for i, low in option_texts:
                            if binary == "yes" and any(k in low for k in ("yes", "y", "authorized", "immediate", "willing")):
                                chosen_index = i
                                break
                            if binary == "no" and any(k in low for k in ("no", "n", "not", "unwilling")):
                                chosen_index = i
                                break
                    elif any(k in meta for k in ("notice", "join", "availability")):
                        notice = user.notice_period_days if user and user.notice_period_days is not None else 1
                        if notice == 0:
                            for i, low in option_texts:
                                if any(k in low for k in ("0", "immediate", "join immediately", "same day")):
                                    chosen_index = i
                                    break
                    elif any(k in meta for k in ("salary", "ctc", "compensation", "expected", "current")):
                        target = user.expected_ctc_lpa if user and user.expected_ctc_lpa is not None else None
                        if any(k in meta for k in ("current", "present")):
                            target = user.current_ctc_lpa if user and user.current_ctc_lpa is not None else target
                        if target is not None:
                            t = str(int(round(target)))
                            for i, low in option_texts:
                                if t in low:
                                    chosen_index = i
                                    break

                await options.nth(chosen_index).click(timeout=2000, force=True)
                filled += 1
                await asyncio.sleep(0.4)
            except Exception:
                continue

        return filled

    async def _fill_linkedin_modal_minimum_fields(
        self,
        page: Page | Frame,
        user: UserProfile,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Fill missing modal/form fields with minimal values to progress through steps.

        Despite the name, this is reused for external portals as a generic "minimum viable form fill".
        """
        modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal")
        container = modal or page
        filled = 0
        effective_overrides = self._augment_overrides_with_defaults(user, answer_overrides)

        try:
            selects = await container.query_selector_all("select")
            for sel in selects:
                try:
                    if not await sel.is_visible():
                        continue
                    if await sel.get_attribute("disabled") is not None:
                        continue
                    current = (await sel.input_value() or "").strip()
                    placeholder_values = {"select an option", "choose an option", "please select", "select"}
                    if current and current.lower() not in placeholder_values:
                        continue
                    sel_id = (await sel.get_attribute("id") or "").strip()
                    label_text = ""
                    if sel_id:
                        try:
                            label_el = await container.query_selector(f"label[for='{sel_id}']")
                            if label_el:
                                label_text = ((await label_el.inner_text()) or "").strip()
                        except Exception:
                            label_text = ""
                    meta = " ".join(
                        [
                            (await sel.get_attribute("name") or ""),
                            sel_id,
                            (await sel.get_attribute("aria-label") or ""),
                            label_text,
                            await self._infer_field_context_text(sel),
                        ]
                    ).lower()
                    chosen = await self._choose_select_option(
                        sel, meta, user, answer_overrides=effective_overrides
                    )
                    if chosen:
                        await sel.select_option(value=chosen)
                        filled += 1
                except Exception:
                    continue
        except Exception:
            pass

        try:
            inputs = await container.query_selector_all(
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='file']):not([type='submit']):not([type='button']):not([type='image']), textarea"
            )
            for inp in inputs:
                try:
                    if not await inp.is_visible():
                        continue
                    if await inp.get_attribute("disabled") is not None:
                        continue
                    if await inp.get_attribute("readonly") is not None:
                        continue
                    current = (await inp.input_value() or "").strip()
                    if current:
                        continue
                    inp_id = (await inp.get_attribute("id") or "").strip()
                    label_text = ""
                    if inp_id:
                        try:
                            label_el = await container.query_selector(f"label[for='{inp_id}']")
                            if label_el:
                                label_text = ((await label_el.inner_text()) or "").strip()
                        except Exception:
                            label_text = ""
                    meta = " ".join(
                        [
                            (await inp.get_attribute("name") or ""),
                            inp_id,
                            (await inp.get_attribute("aria-label") or ""),
                            (await inp.get_attribute("placeholder") or ""),
                            label_text,
                            await self._infer_field_context_text(inp),
                        ]
                    ).lower()
                    if "search" in meta:
                        continue

                    input_type = (await inp.get_attribute("type") or "").lower()
                    input_key, explicit_value = self._resolve_field_value(
                        meta, input_type, user, effective_overrides
                    )
                    # Never guess credentials or honeypot fields.
                    if input_type == "password" or input_key == "password":
                        if explicit_value in (None, ""):
                            continue
                    if any(tok in meta for tok in ("for robots only", "do not enter if you're human", "honeypot")):
                        continue
                    is_required = (await inp.get_attribute("required") is not None) or (
                        ((await inp.get_attribute("aria-required") or "").strip().lower() == "true")
                    )
                    priority_keys = {
                        "first_name",
                        "last_name",
                        "full_name",
                        "local_given_name",
                        "local_family_name",
                        "email",
                        "phone",
                        "phone_type",
                        "phone_country_code",
                        "phone_extension",
                        "postal_code",
                        "zip_code",
                        "pincode",
                        "address_line_1",
                        "address_line_2",
                        "city",
                        "state",
                        "country",
                        "location",
                        "linkedin_url",
                        "hear_about_us",
                        "expected_ctc_lpa",
                        "current_ctc_lpa",
                        "notice_period_days",
                        "can_join_immediately",
                        "applied_before",
                        "worked_here_before",
                        "willing_to_relocate",
                        "requires_sponsorship",
                        "work_authorization",
                        "total_experience_years",
                        "verification_code",
                    }
                    if not is_required and input_key not in priority_keys:
                        continue
                    if explicit_value not in (None, ""):
                        default_value = str(explicit_value)
                    else:
                        # User-requested policy: minimum value for numeric and minimum text to proceed.
                        default_value = "NA"
                        if input_type == "number":
                            default_value = "1"
                        elif input_type == "email":
                            # For official employer portals always use candidate's official email.
                            default_value = user.email or ""
                        elif input_type in {"tel", "phone"}:
                            if any(
                                k in meta for k in ("country code", "dial code", "phone country", "mobile country code")
                            ):
                                default_value = self.default_phone_country_code
                            else:
                                default_value = self._normalize_mobile_number(user.phone or "")
                        elif input_type == "url":
                            default_value = user.linkedin_url or ""
                        elif input_type == "date":
                            default_value = datetime.now().strftime("%Y-%m-%d")
                        elif any(k in meta for k in ("country code", "dial code", "phone country", "mobile country code")) and any(
                            k in meta for k in ("phone", "mobile", "contact", "dial")
                        ):
                            default_value = self.default_phone_country_code
                        elif any(k in meta for k in ("phone type", "contact type", "type of phone", "number type")):
                            default_value = "mobile"
                        elif any(
                            k in meta
                            for k in (
                                "how did you hear",
                                "hear about us",
                                "where did you hear",
                                "source of application",
                                "referral source",
                                "job source",
                            )
                        ):
                            default_value = self.default_source_answer
                        elif any(k in meta for k in ("year", "experience", "yrs", "month", "notice")):
                            default_value = str(
                                user.notice_period_days if user and user.notice_period_days is not None and "notice" in meta else 1
                            )
                        elif any(k in meta for k in ("salary", "ctc", "compensation", "expected pay")):
                            default_value = self._default_salary_answer(meta, user)
                        elif any(k in meta for k in ("immediate", "join", "availability")):
                            default_value = (
                                "Yes"
                                if (user and user.can_join_immediately is True) or (user and user.notice_period_days == 0)
                                else "No"
                            )
                        elif any(k in meta for k in ("work authorization", "authorized")) and user and user.work_authorization:
                            default_value = user.work_authorization

                    # Never guess one-time verification/OTP codes.
                    if input_key in {"verification_code", "otp", "security_code", "pin"} and not explicit_value:
                        continue
                    if default_value in (None, ""):
                        continue

                    await inp.click(timeout=1000)
                    await inp.fill(default_value)
                    filled += 1
                except Exception:
                    continue
        except Exception:
            pass

        # Non-native dropdown widgets (common on LinkedIn screening questions and some ATS portals).
        # Use the original scope (Page/Frame); some dropdown option panels render outside the modal container.
        try:
            filled += await self._fill_non_native_dropdowns(
                page, user, answer_overrides=effective_overrides
            )
        except Exception:
            pass

        return filled

    async def _select_greenhouse_combobox_option(
        self,
        page: Page,
        input_id: str,
        meta: str,
        preferred_value: Optional[str] = None,
    ) -> bool:
        """Select an option from Greenhouse react-select combobox widgets."""
        try:
            combo = page.locator(f"input#{input_id}[role='combobox']")
            if await combo.count() <= 0:
                return False
            field = combo.first
            if not await field.is_visible():
                return False
            await field.click(timeout=1500, force=True)
            await asyncio.sleep(0.25)
        except Exception:
            return False

        options = page.locator(f"[id^='react-select-{input_id}-option-'][role='option']")
        try:
            if await options.count() <= 0:
                options = page.locator("[role='option']")
        except Exception:
            options = page.locator("[role='option']")

        option_rows: list[tuple[int, str]] = []
        try:
            count = min(await options.count(), 30)
        except Exception:
            count = 0
        for idx in range(count):
            try:
                opt = options.nth(idx)
                if not await opt.is_visible():
                    continue
                low = ((await opt.inner_text()) or "").strip().lower()
                if not low:
                    continue
                option_rows.append((idx, low))
            except Exception:
                continue
        if not option_rows:
            return False

        chosen_index = option_rows[0][0]
        meta_l = (meta or "").lower()
        preferred_l = (preferred_value or "").strip().lower()

        if any(
            tok in meta_l
            for tok in (
                "applied in the past",
                "applied before",
                "previously applied",
                "worked here before",
                "worked for this company",
                "worked for any subsidiary",
                "subsidiary",
                "competitor",
                "relative",
            )
        ):
            preferred_l = "no"
        elif any(
            tok in meta_l
            for tok in (
                "how did you hear",
                "hear about us",
                "where did you hear",
                "source of application",
                "referral source",
                "job source",
            )
        ):
            for idx, low in option_rows:
                if "social media" in low:
                    chosen_index = idx
                    break
            for idx, low in option_rows:
                if "linkedin" in low:
                    chosen_index = idx
                    break
        elif "work eligibility" in meta_l or "authorized" in meta_l:
            for idx, low in option_rows:
                if "eligible" in low or "without sponsorship" in low:
                    chosen_index = idx
                    break

        if preferred_l:
            for idx, low in option_rows:
                if preferred_l == low or preferred_l in low:
                    chosen_index = idx
                    break

        try:
            await options.nth(chosen_index).click(timeout=2000, force=True)
            await asyncio.sleep(0.2)
            return True
        except Exception:
            return False

    async def _fill_greenhouse_required_error_fields(
        self,
        page: Page,
        user: UserProfile,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Greenhouse-specific fixer for currently errored required fields.
        Targets `.label--error` controls and fills them deterministically.
        """
        host = self._host(page.url or "")
        if "greenhouse" not in host:
            return 0

        filled = 0
        effective_overrides = self._augment_overrides_with_defaults(user, answer_overrides)
        _, first_name, last_name = self._extract_name_parts(user)

        labels = page.locator("label.label--error, label.select__label--error")
        try:
            label_count = min(await labels.count(), 50)
        except Exception:
            label_count = 0

        for idx in range(label_count):
            try:
                lab = labels.nth(idx)
                if not await lab.is_visible():
                    continue
                label_text = ((await lab.inner_text()) or "").strip()
                field_id = (await lab.get_attribute("for") or "").strip()
                if not field_id:
                    continue
                meta = f"{label_text} {field_id}".strip().lower()

                combo = page.locator(f"input#{field_id}[role='combobox']")
                if await combo.count() > 0:
                    key, explicit = self._resolve_field_value(meta, "select", user, effective_overrides)
                    preferred = explicit
                    if key in {"applied_before", "worked_here_before", "requires_sponsorship"}:
                        preferred = "No"
                    if await self._select_greenhouse_combobox_option(page, field_id, meta, preferred):
                        filled += 1
                        continue

                text_field = page.locator(f"input#{field_id}:not([type='hidden']):not([role='combobox']), textarea#{field_id}")
                if await text_field.count() <= 0:
                    continue
                field = text_field.first
                if not await field.is_visible():
                    continue
                if await field.get_attribute("disabled") is not None:
                    continue
                if await field.get_attribute("readonly") is not None:
                    continue
                current = (await field.input_value() or "").strip()
                if current:
                    continue
                input_type = (await field.get_attribute("type") or "").lower()
                key, explicit = self._resolve_field_value(meta, input_type, user, effective_overrides)
                value = (explicit or "").strip()
                if not value:
                    if key in {"first_name", "local_given_name"}:
                        value = first_name
                    elif key in {"last_name", "local_family_name"}:
                        value = last_name
                    elif key in {"full_name"}:
                        value = f"{first_name} {last_name}".strip()
                    elif key in {"address_line_1", "location"}:
                        value = self.default_address_line_1
                    elif key in {"city"}:
                        value = self.default_city
                    elif key in {"state"}:
                        value = self.default_state
                    elif key in {"country"}:
                        value = self.default_country
                    elif key in {"postal_code", "zip_code", "pincode"}:
                        value = self.default_postal_code
                    elif key in {"phone_extension", "extension", "ext"}:
                        value = "0"
                    elif input_type == "number":
                        value = "1"
                    else:
                        value = "NA"
                await field.click(timeout=1000)
                await field.fill(value)
                filled += 1
            except Exception:
                continue
        return filled

    async def _detect_external_submission_success(self, page: Page) -> bool:
        """Best-effort confirmation detection after clicking final submit on external portals."""
        try:
            text = ((await page.inner_text("body")) or "").lower()
        except Exception:
            text = ""
        success_tokens = (
            "thank you for applying",
            "application submitted",
            "successfully applied",
            "we have received your application",
            "application received",
            "your application has been submitted",
            "submission confirmed",
            "thanks for applying",
            "thank you, your application has been received",
            "your application is complete",
            "application complete",
            "you already applied for this job",
            "you've already applied for this job",
            "already applied for this job",
        )
        return any(tok in text for tok in success_tokens)

    async def _detect_external_submission_blocker(self, page: Page) -> Optional[str]:
        """
        Detect common post-submit blockers/validation messages that prevent final submission.
        Returns a short machine-friendly reason when detected.
        """
        try:
            text = ((await page.inner_text("body")) or "").lower()
        except Exception:
            text = ""
        if not text:
            return None

        if "video answers to finish processing before submitting your application" in text:
            return "video_processing_pending"
        if any(
            tok in text
            for tok in (
                "please complete this required field",
                "this field is required",
                "required fields are missing",
                "can't be blank",
            )
        ):
            return "required_fields_missing"
        if "this question is required" in text:
            return "required_questions_missing"
        if "how did you hear about us" in text and "required" in text:
            return "required_source_missing"
        if "invalid phone" in text or "phone number is invalid" in text or "enter a valid phone number" in text:
            return "required_fields_missing"
        if "postal code must be 6 digits" in text or ("postal code" in text and "must be" in text and "digits" in text):
            return "postal_code_format_error"
        if "zip code" in text and any(tok in text for tok in ("invalid", "required", "must be")):
            return "postal_code_format_error"
        if any(tok in text for tok in ("verification code", "one-time password", "one time password", "otp", "enter code sent")):
            return "verification_code_required"
        if any(tok in text for tok in ("sign in to continue", "create account", "log in to apply")):
            return "portal_login_required"
        if "there was a problem submitting" in text or "unable to submit" in text:
            return "submission_error"
        if "captcha" in text or "verify you are human" in text:
            return "captcha_required"
        # DOM-level fallback: many portals mark invalid/required controls without explicit error copy.
        try:
            invalid = page.locator(
                "[aria-invalid='true'], .input-wrapper--error, .helper-text--error, .application-error, [data-testid$='-error']"
            )
            if await invalid.count() > 0:
                return "required_fields_missing"
        except Exception:
            pass
        try:
            empty_required = page.locator(
                "input[required]:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
            )
            total = await empty_required.count()
            for idx in range(min(total, 40)):
                field = empty_required.nth(idx)
                try:
                    value = (await field.input_value() or "").strip()
                    if not value:
                        return "required_fields_missing"
                except Exception:
                    continue
        except Exception:
            pass
        try:
            required_selects = page.locator("select[required]")
            total = await required_selects.count()
            for idx in range(min(total, 40)):
                field = required_selects.nth(idx)
                try:
                    value = (await field.input_value() or "").strip()
                    if not value:
                        return "required_fields_missing"
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _submission_blocker_message(reason: str) -> str:
        mapping = {
            "video_processing_pending": "Portal is still processing video answers; retry after processing completes.",
            "required_fields_missing": "Required form fields are still missing.",
            "required_questions_missing": "Required screening questions are still unanswered.",
            "required_source_missing": "A required application source field is missing.",
            "postal_code_format_error": "Postal code format is invalid for this portal.",
            "verification_code_required": "A verification code is required before submit.",
            "portal_login_required": "Portal sign-in/account creation must be completed before submit.",
            "submission_error": "Portal reported a submission error.",
            "captcha_required": "CAPTCHA/verification is required before submit.",
        }
        return mapping.get(reason, reason)

    @staticmethod
    def _is_auto_resolvable_submission_blocker(reason: Optional[str]) -> bool:
        return (reason or "") in {
            "required_fields_missing",
            "required_questions_missing",
            "required_source_missing",
            "postal_code_format_error",
            "submission_error",
        }

    @staticmethod
    def _is_hard_submission_blocker(reason: Optional[str]) -> bool:
        return (reason or "") in {
            "verification_code_required",
            "portal_login_required",
            "captcha_required",
        }

    async def _progress_workday_apply_start(
        self, page: Page, resume_path: str, app: Application, db
    ) -> tuple[Page, bool]:
        """
        Workday apply flows often start with a "Start Your Application" page with CTAs:
        - Autofill with Resume
        - Apply Manually
        This clicks the best CTA to reach actual form fields.
        """
        url_l = (page.url or "").lower()
        if "myworkdayjobs.com" not in url_l or "/apply" not in url_l:
            return page, False

        try:
            container = page.locator("[data-automation-id='applyAdventurePage']")
            if await container.count() <= 0:
                # Not on the Workday "apply adventure" start screen.
                return page, False
        except Exception:
            return page, False

        # Dismiss cookie/privacy overlays which often block CTA clicks.
        try:
            await self._maybe_dismiss_portal_popups(page, app, db)
        except Exception:
            pass

        async def _click_cta(locator) -> tuple[Page, bool]:
            """Click a CTA that might open a new tab and return (active_page, clicked)."""
            try:
                await locator.scroll_into_view_if_needed()
            except Exception:
                pass
            before_url = page.url
            try:
                async with page.context.expect_page(timeout=3500) as new_page_info:
                    await locator.click(timeout=7000, force=True, no_wait_after=True)
                new_page = await new_page_info.value
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=45000)
                except Exception:
                    pass
                return new_page, True
            except Exception:
                try:
                    await locator.click(timeout=7000, force=True, no_wait_after=True)
                    await asyncio.sleep(1.5)
                except Exception:
                    return page, False

            # Same-tab navigation is common on Workday.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            try:
                # Give the SPA a moment to mount the next view.
                await asyncio.sleep(1.5)
            except Exception:
                pass
            # Heuristic: URL changed or the start container disappeared.
            try:
                if (page.url or "") != (before_url or ""):
                    return page, True
                adv = page.locator("[data-automation-id='applyAdventurePage']")
                if await adv.count() == 0:
                    return page, True
            except Exception:
                pass
            return page, True

        # For reliability: prefer "Apply Manually" when available; it still allows resume upload later.
        try:
            manual = page.locator("a[data-automation-id='applyManually']")
            try:
                await manual.first.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            if await manual.count() > 0 and await manual.first.is_visible():
                app.automation_log += "Workday: selecting 'Apply Manually'.\n"
                db.commit()
                new_page, clicked = await _click_cta(manual.first)
                if clicked:
                    return new_page, True
        except Exception:
            pass

        # Fallback: try "Autofill with Resume" if manual isn't present.
        if resume_path:
            try:
                auto = page.locator("a[data-automation-id='autofillWithResume']")
                try:
                    await auto.first.wait_for(state="visible", timeout=3000)
                except Exception:
                    pass
                if await auto.count() > 0 and await auto.first.is_visible():
                    app.automation_log += "Workday: selecting 'Autofill with Resume'.\n"
                    db.commit()
                    new_page, clicked = await _click_cta(auto.first)
                    if clicked:
                        return new_page, True
            except Exception:
                pass

        return page, False

    async def _fill_required_radios_and_checkboxes(
        self,
        container: Page | Frame,
        user: UserProfile,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Many flows (LinkedIn screening and external portals) block progression until a radio/checkbox is selected.
        This is a best-effort "choose minimally to proceed" helper.
        """
        filled = 0
        try:
            root_page = container.page if isinstance(container, Frame) else container
        except Exception:
            root_page = container  # type: ignore

        # Checkboxes: accept terms/consent if required to proceed.
        try:
            boxes = await container.query_selector_all("input[type='checkbox']")
            for cb in boxes[:60]:
                try:
                    if not await cb.is_visible():
                        continue
                    if await cb.get_attribute("disabled") is not None:
                        continue
                    checked = await cb.is_checked()
                    if checked:
                        continue
                    meta_parts = []
                    for attr in ("name", "id", "aria-label"):
                        try:
                            meta_parts.append((await cb.get_attribute(attr) or "").strip())
                        except Exception:
                            continue
                    # Try associated label text.
                    label_txt = ""
                    cb_id = (await cb.get_attribute("id") or "").strip()
                    if cb_id:
                        try:
                            lab = await root_page.query_selector(f"label[for='{cb_id}']")
                            if lab:
                                label_txt = ((await lab.inner_text()) or "").strip()
                        except Exception:
                            label_txt = ""
                    meta_parts.append(label_txt)
                    meta = " ".join([p for p in meta_parts if p]).lower()
                    if not meta:
                        continue

                    key, explicit = self._resolve_field_value(meta, "checkbox", user, answer_overrides)
                    explicit_bool = self._as_yes_no(explicit) if explicit is not None else None

                    # Only auto-check consent-like boxes; avoid toggling unrelated preferences unless explicit.
                    if explicit_bool == "Yes":
                        await cb.check(force=True)
                        filled += 1
                    elif explicit_bool == "No":
                        continue
                    elif any(k in meta for k in ("agree", "consent", "terms", "privacy", "acknowledge", "i certify")):
                        await cb.check(force=True)
                        filled += 1
                except Exception:
                    continue
        except Exception:
            pass

        # Radios: pick a value when none is selected.
        try:
            radios = await container.query_selector_all("input[type='radio']")
        except Exception:
            radios = []

        # Group by name for minimal selection.
        groups: dict[str, list] = {}
        for r in radios[:120]:
            try:
                if not await r.is_visible():
                    continue
                if await r.get_attribute("disabled") is not None:
                    continue
                name = (await r.get_attribute("name") or "").strip()
                if not name:
                    # Some portals omit name; treat each as its own group.
                    name = f"_anon_{id(r)}"
                groups.setdefault(name, []).append(r)
            except Exception:
                continue

        for name, opts in list(groups.items())[:60]:
            try:
                any_checked = False
                for r in opts:
                    try:
                        if await r.is_checked():
                            any_checked = True
                            break
                    except Exception:
                        continue
                if any_checked:
                    continue

                # Build meta from nearby text to infer yes/no or safe defaults.
                meta = name.lower()
                try:
                    # Sometimes the label is a sibling text node; use element handles if possible.
                    meta = " ".join([meta, ((await opts[0].get_attribute("aria-label") or "")).lower()]).strip()
                except Exception:
                    pass

                # Sensitive demographic questions: prefer "prefer not" if such an option exists.
                is_sensitive = any(
                    k in meta
                    for k in (
                        "gender",
                        "disability",
                        "veteran",
                        "race",
                        "ethnicity",
                        "religion",
                        "sexual",
                        "orientation",
                        "caste",
                    )
                )

                chosen = None
                if is_sensitive:
                    for r in opts:
                        try:
                            lab = (await r.get_attribute("aria-label") or "").lower()
                            if any(k in lab for k in ("prefer not", "decline", "not to say", "not specified")):
                                chosen = r
                                break
                        except Exception:
                            continue

                if chosen is None:
                    key, explicit = self._resolve_field_value(meta, "radio", user, answer_overrides)
                    binary = (self._as_yes_no(explicit) or self._preferred_binary(meta, user) or "no").lower()
                    for r in opts:
                        try:
                            lab = (await r.get_attribute("aria-label") or "").lower()
                            if binary == "yes" and any(k in lab for k in ("yes", "y", "authorized", "immediate", "willing")):
                                chosen = r
                                break
                            if binary == "no" and any(k in lab for k in ("no", "n", "not", "unwilling")):
                                chosen = r
                                break
                        except Exception:
                            continue

                if chosen is None:
                    # Prefer explicit "No" on ambiguous yes/no groups instead of defaulting to first option.
                    for r in opts:
                        try:
                            lab = (await r.get_attribute("aria-label") or "").lower()
                            if any(k in lab for k in ("no", "n", "not", "never")):
                                chosen = r
                                break
                        except Exception:
                            continue
                chosen = chosen or (opts[0] if opts else None)
                if chosen:
                    await chosen.check(force=True)
                    filled += 1
            except Exception:
                continue

        return filled

    async def _save_external_debug_artifacts(self, page: Page, app: Application, db, tag: str) -> None:
        """Save HTML + screenshot for post-mortem debugging. Best-effort, never raises."""
        try:
            os.makedirs("data/debug_artifacts", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"data/debug_artifacts/app_{app.id}_{tag}_{ts}"
            try:
                content = await page.content()
                with open(base + ".html", "w", encoding="utf-8") as f:
                    f.write(content)
                app.automation_log += f"Saved debug HTML: {base}.html\n"
            except Exception:
                pass
            try:
                await page.screenshot(path=base + ".png", full_page=True)
                app.automation_log += f"Saved debug screenshot: {base}.png\n"
            except Exception:
                pass
            db.commit()
        except Exception:
            pass

    async def _complete_external_apply_steps(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        safe_mode: bool,
        require_confirmation: bool,
        answer_overrides: Optional[dict[str, Any]] = None,
        max_steps: int = 6,
    ) -> bool:
        filled_count = 0
        resume_attached = False
        runtime_overrides = self._augment_overrides_with_defaults(
            user,
            answer_overrides,
            job=(app.job if app else None),
        )
        first_name = user.full_name.split()[0] if user.full_name else ""
        last_name = user.full_name.split()[-1] if user.full_name and len(user.full_name.split()) > 1 else ""
        field_specs = [
            ("full name", user.full_name or "", ["full name", "fullname", "full_name", "name"]),
            ("first name", first_name, ["first name", "firstname", "first_name", "given name", "given_name"]),
            ("last name", last_name, ["last name", "lastname", "last_name", "surname", "family name"]),
            ("local given name", first_name, ["local given name", "given name local", "local name first"]),
            ("local family name", last_name, ["local family name", "family name local", "local name last"]),
            ("email", user.email or "", ["email", "e-mail", "mail", "username", "user name", "login"]),
            ("phone", self._normalize_mobile_number(user.phone or ""), ["phone", "mobile", "telephone", "contact number", "contact_number"]),
            ("phone country code", self.default_phone_country_code, ["phone country code", "mobile country code", "country code", "dial code"]),
            ("phone type", "mobile", ["phone type", "contact type", "number type", "type of phone"]),
            ("phone extension", "0", ["phone extension", "extension", "ext"]),
            ("address line 1", runtime_overrides.get("address_line_1", self.default_address_line_1), ["address line 1", "street address", "address1", "street"]),
            ("address line 2", runtime_overrides.get("address_line_2", "NA"), ["address line 2", "address2", "suite", "apartment"]),
            ("city", runtime_overrides.get("city", ""), ["city", "town"]),
            ("state", runtime_overrides.get("state", ""), ["state", "province", "region"]),
            ("country", runtime_overrides.get("country", self.default_country), ["country"]),
            ("location", user.location or "", ["location", "city", "address"]),
            ("postal code", self.default_postal_code, ["postal code", "zip code", "zipcode", "pin code", "pincode"]),
            ("how did you hear", self.default_source_channel, ["how did you hear about us", "hear about us", "source of application", "job source", "referral source"]),
            ("source platform", self.default_source_platform, ["social media platform", "which social media", "source platform", "social channel"]),
            ("linkedin", user.linkedin_url or "", ["linkedin", "linkedin url", "linkedin profile"]),
        ]

        for step in range(1, max_steps + 1):
            if self._abort_if_stop_requested(db, app, f"external-step-{step}"):
                return False
            # Some challenge-gated sites can crash the renderer; reopen and resume at the last known URL.
            try:
                if page.is_closed():
                    last_url = getattr(page, "url", None) or ""
                    if last_url:
                        app.automation_log += "Apply window closed unexpectedly; reopening...\n"
                        db.commit()
                        page = await page.context.new_page()
                        await page.goto(last_url, wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(2)
            except Exception:
                pass

            challenge_reason = await self._detect_anti_bot_challenge(page)
            if challenge_reason:
                app.notes = (
                    "Application portal blocked by anti-bot verification challenge; "
                    "complete verification manually, then retry automation."
                )
                app.automation_log += (
                    f"Blocked during external apply step {step} by anti-bot challenge "
                    f"({challenge_reason}).\n"
                )
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="anti_bot_challenge",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "manual_challenge_verification",
                            "label": "Manual Verification",
                            "question": "Complete anti-bot verification in the opened browser window, then retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
                db.commit()
                return False

            app.automation_log += f"External apply step {step}\n"
            step_filled = 0
            try:
                await self._maybe_dismiss_portal_popups(page, app, db)
            except Exception:
                pass

            # Workday sign-in/create-account wall: requires manual intervention once per domain.
            # We can wait (headed mode) and then save storage_state so future runs proceed autonomously.
            try:
                if await self._detect_workday_login_wall(page):
                    if self.headless:
                        app.notes = (
                            "Workday portal requires sign-in/create account (often with CAPTCHA). "
                            "Re-run automation with headed browser mode to complete sign-in once."
                        )
                        app.automation_log += "Detected Workday login wall in headless mode; cannot proceed.\n"
                        await self._capture_blocker_details(
                            page,
                            app,
                            user,
                            db,
                            reason="portal_login_required",
                            message=app.notes,
                            required_inputs=[
                                {
                                    "key": "official_email",
                                    "label": "Official Email",
                                    "question": "Confirm official email to create/sign-in on the employer portal.",
                                    "type": "email",
                                    "required": True,
                                },
                                {
                                    "key": "portal_authenticated_session",
                                    "label": "Portal Sign-in",
                                    "question": "Complete sign-in/create-account in headed mode, then retry.",
                                    "type": "manual_action",
                                    "required": True,
                                },
                            ],
                        )
                        try:
                            self._record_issue_event(
                                db, app, app.job if app else None, user, app.notes, event_type="detected"
                            )
                        except Exception:
                            pass
                        db.commit()
                        return False
                    ok = await self._wait_for_workday_login(
                        page,
                        app,
                        db,
                        timeout_seconds=max(60, int(settings.external_challenge_timeout_seconds)),
                    )
                    if not ok:
                        app.notes = (
                            "Workday sign-in not completed in time. Complete sign-in in the opened window and retry."
                        )
                        await self._capture_blocker_details(
                            page,
                            app,
                            user,
                            db,
                            reason="portal_login_required",
                            message=app.notes,
                            required_inputs=[
                                {
                                    "key": "portal_authenticated_session",
                                    "label": "Portal Sign-in",
                                    "question": "Complete sign-in/create-account in the opened portal window.",
                                    "type": "manual_action",
                                    "required": True,
                                }
                            ],
                        )
                        try:
                            self._record_issue_event(
                                db, app, app.job if app else None, user, app.notes, event_type="detected"
                            )
                        except Exception:
                            pass
                        db.commit()
                        return False
                    # Persist the authenticated session for this portal host.
                    try:
                        state_path = self._external_storage_state_path(page.url or "")
                        if state_path:
                            await page.context.storage_state(path=state_path)
                            app.automation_log += "Saved Workday portal session state for future runs.\n"
                            try:
                                self._record_issue_event(
                                    db,
                                    app,
                                    app.job if app else None,
                                    user,
                                    "Workday login completed; saved portal session state.",
                                    event_type="resolved",
                                )
                            except Exception:
                                pass
                            db.commit()
                    except Exception:
                        pass
            except Exception:
                pass
            scopes = self._iter_scopes_prioritized(page)

            # Some portals show a job page first and require clicking an "Apply" CTA to open the form.
            if step == 1:
                try:
                    page = await self._ensure_external_apply_form_open(page, app, db)
                    try:
                        await self._maybe_dismiss_portal_popups(page, app, db)
                    except Exception:
                        pass
                    try:
                        # Workday apply flows frequently start with a "Start Your Application" CTA screen.
                        # This screen can mount asynchronously; try a few times before declaring "no fields".
                        for _ in range(3):
                            url_l = (page.url or "").lower()
                            if "myworkdayjobs.com" not in url_l or "/apply" not in url_l:
                                break
                            try:
                                adv = page.locator("[data-automation-id='applyAdventurePage']")
                                if await adv.count() <= 0:
                                    break
                            except Exception:
                                break
                            page, progressed = await self._progress_workday_apply_start(
                                page, resume_path or "", app, db
                            )
                            if progressed:
                                try:
                                    await self._maybe_dismiss_portal_popups(page, app, db)
                                except Exception:
                                    pass
                                try:
                                    await page.wait_for_load_state("domcontentloaded", timeout=30000)
                                except Exception:
                                    pass
                                await asyncio.sleep(2.0)
                            else:
                                await asyncio.sleep(1.5)
                    except Exception:
                        pass
                    # Workday can redirect to sign-in/create-account after selecting the CTA.
                    try:
                        if await self._detect_workday_login_wall(page):
                            if self.headless:
                                app.notes = (
                                    "Workday portal requires sign-in/create account (often with CAPTCHA). "
                                    "Re-run automation with headed browser mode to complete sign-in once."
                                )
                                app.automation_log += "Detected Workday login wall in headless mode; cannot proceed.\n"
                                await self._capture_blocker_details(
                                    page,
                                    app,
                                    user,
                                    db,
                                    reason="portal_login_required",
                                    message=app.notes,
                                    required_inputs=[
                                        {
                                            "key": "official_email",
                                            "label": "Official Email",
                                            "question": "Confirm official email to create/sign-in on the employer portal.",
                                            "type": "email",
                                            "required": True,
                                        },
                                        {
                                            "key": "portal_authenticated_session",
                                            "label": "Portal Sign-in",
                                            "question": "Complete sign-in/create-account in headed mode, then retry.",
                                            "type": "manual_action",
                                            "required": True,
                                        },
                                    ],
                                )
                                try:
                                    self._record_issue_event(
                                        db, app, app.job if app else None, user, app.notes, event_type="detected"
                                    )
                                except Exception:
                                    pass
                                db.commit()
                                return False
                            ok = await self._wait_for_workday_login(
                                page,
                                app,
                                db,
                                timeout_seconds=max(60, int(settings.external_challenge_timeout_seconds)),
                            )
                            if not ok:
                                app.notes = (
                                    "Workday sign-in not completed in time. Complete sign-in in the opened window and retry."
                                )
                                await self._capture_blocker_details(
                                    page,
                                    app,
                                    user,
                                    db,
                                    reason="portal_login_required",
                                    message=app.notes,
                                    required_inputs=[
                                        {
                                            "key": "portal_authenticated_session",
                                            "label": "Portal Sign-in",
                                            "question": "Complete sign-in/create-account in the opened portal window.",
                                            "type": "manual_action",
                                            "required": True,
                                        }
                                    ],
                                )
                                try:
                                    self._record_issue_event(
                                        db, app, app.job if app else None, user, app.notes, event_type="detected"
                                    )
                                except Exception:
                                    pass
                                db.commit()
                                return False
                            try:
                                state_path = self._external_storage_state_path(page.url or "")
                                if state_path:
                                    await page.context.storage_state(path=state_path)
                                    app.automation_log += "Saved Workday portal session state for future runs.\n"
                                    try:
                                        self._record_issue_event(
                                            db,
                                            app,
                                            app.job if app else None,
                                            user,
                                            "Workday login completed; saved portal session state.",
                                            event_type="resolved",
                                        )
                                    except Exception:
                                        pass
                                    db.commit()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    scopes = self._iter_scopes_prioritized(page)
                    # Allow JS-rendered forms (common on ATS portals) to mount.
                    await asyncio.sleep(2.0)
                    # Challenge pages can appear after clicking an external Apply CTA (e.g., Cloudflare on TTC portals).
                    challenge_reason = await self._detect_anti_bot_challenge(page)
                    if challenge_reason:
                        app.notes = (
                            "Application portal blocked by anti-bot verification challenge; "
                            "complete verification manually, then retry automation."
                        )
                        app.automation_log += (
                            f"Blocked after Apply CTA by anti-bot challenge ({challenge_reason}).\n"
                        )
                        await self._capture_blocker_details(
                            page,
                            app,
                            user,
                            db,
                            reason="anti_bot_challenge",
                            message=app.notes,
                            required_inputs=[
                                {
                                    "key": "manual_challenge_verification",
                                    "label": "Manual Verification",
                                    "question": "Complete anti-bot verification in the opened browser window, then retry.",
                                    "type": "manual_action",
                                    "required": True,
                                }
                            ],
                        )
                        db.commit()
                        return False
                except Exception:
                    pass

            for label, value, aliases in field_specs:
                try:
                    for scope in scopes:
                        did_fill = await self._fill_external_field(scope, aliases, value)
                        if did_fill:
                            app.automation_log += f"Filled {label}\n"
                            step_filled += 1
                            filled_count += 1
                            break
                except Exception:
                    continue

            try:
                # Reuse the same minimal-input filler for external portals (works on many forms).
                min_filled = 0
                for scope in scopes:
                    try:
                        min_filled += await self._fill_linkedin_modal_minimum_fields(
                            scope, user, answer_overrides=runtime_overrides
                        )
                    except Exception:
                        continue
                if min_filled:
                    app.automation_log += f"Filled {min_filled} minimal field(s)\n"
                    step_filled += min_filled
                    filled_count += min_filled
            except Exception:
                pass

            # Radios/checkboxes are very common on ATS portals (consent, yes/no, disclosures).
            try:
                rc_filled = 0
                for scope in scopes:
                    try:
                        rc_filled += await self._fill_required_radios_and_checkboxes(
                            scope, user, answer_overrides=runtime_overrides
                        )
                    except Exception:
                        continue
                if rc_filled:
                    app.automation_log += f"Filled {rc_filled} radio/checkbox field(s)\n"
                    step_filled += rc_filled
                    filled_count += rc_filled
                    db.commit()
            except Exception:
                pass

            # Non-native dropdowns (combobox/listbox patterns) are common on external portals.
            try:
                dd_filled = 0
                for scope in scopes:
                    try:
                        dd_filled += await self._fill_non_native_dropdowns(
                            scope, user, answer_overrides=runtime_overrides
                        )
                    except Exception:
                        continue
                if dd_filled:
                    app.automation_log += f"Filled {dd_filled} dropdown field(s)\n"
                    step_filled += dd_filled
                    filled_count += dd_filled
                    db.commit()
            except Exception:
                pass

            # Diagnostic self-heal pass: inspect on-page errors and fill known blockers.
            try:
                diag_filled = await self._diagnose_and_fill_known_portal_blockers(
                    page, user, app, db, answer_overrides=runtime_overrides
                )
                if diag_filled:
                    step_filled += diag_filled
                    filled_count += diag_filled
            except Exception:
                pass

            if resume_path and not resume_attached:
                try:
                    for scope in scopes:
                        file_input = await scope.query_selector("input[type='file']")
                        if file_input and await file_input.is_visible():
                            if self._is_supported_resume_upload(resume_path):
                                await file_input.set_input_files(os.path.abspath(resume_path))
                                app.automation_log += f"Attached resume: {os.path.basename(resume_path)}\n"
                                resume_attached = True
                                step_filled += 1
                                filled_count += 1
                                break
                            app.automation_log += (
                                f"Skipped resume upload for unsupported file type: {os.path.basename(resume_path)}\n"
                            )
                except Exception:
                    pass

            submit_btn, _ = None, ""
            tal_scopes = [
                scope for scope in scopes
                if "apply.talemetry.com" in self._scope_url(scope)
            ]
            nav_scopes = tal_scopes or scopes

            for scope in nav_scopes:
                try:
                    submit_btn, _ = await self._find_clickable_button(
                        scope,
                        ["submit application", "submit", "send application", "complete application"],
                    )
                except Exception:
                    submit_btn = None
                if submit_btn:
                    break
            if submit_btn:
                if safe_mode or require_confirmation:
                    app.notes = "Ready for final submission; review in browser."
                    app.automation_log += "Reached final submit control; safe mode kept final click manual.\n"
                    db.commit()
                    return False
                try:
                    max_submit_attempts = 4
                    for submit_attempt in range(1, max_submit_attempts + 1):
                        # Pre-submit remediation for newly surfaced required fields.
                        prefilled = 0
                        scopes_current = self._iter_scopes_prioritized(page)
                        for scope_current in scopes_current:
                            try:
                                prefilled += await self._fill_linkedin_modal_minimum_fields(
                                    scope_current, user, answer_overrides=runtime_overrides
                                )
                            except Exception:
                                continue
                        for scope_current in scopes_current:
                            try:
                                prefilled += await self._fill_required_radios_and_checkboxes(
                                    scope_current, user, answer_overrides=runtime_overrides
                                )
                            except Exception:
                                continue
                        for scope_current in scopes_current:
                            try:
                                prefilled += await self._fill_non_native_dropdowns(
                                    scope_current, user, answer_overrides=runtime_overrides
                                )
                            except Exception:
                                continue
                        if prefilled:
                            app.automation_log += (
                                f"Pre-submit remediation filled {prefilled} field(s) before submit attempt {submit_attempt}.\n"
                            )
                            db.commit()

                        await self._save_external_debug_artifacts(
                            page, app, db, tag=f"external_pre_submit_attempt{submit_attempt}"
                        )
                        before_url = page.url
                        await submit_btn.click()
                        await asyncio.sleep(2)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        await asyncio.sleep(2)

                        if await self._detect_external_submission_success(page):
                            app.notes = f"Submitted on external portal (detected). URL: {page.url}"
                            app.automation_log += f"Detected external submission success on attempt {submit_attempt}.\n"
                            db.commit()
                            return True

                        blocker = await self._detect_external_submission_blocker(page)
                        if blocker == "video_processing_pending":
                            app.automation_log += (
                                "Portal indicates video answers are still processing; waiting before retrying submit...\n"
                            )
                            db.commit()
                            for _ in range(20):
                                await asyncio.sleep(3)
                                blocker = await self._detect_external_submission_blocker(page)
                                if blocker != "video_processing_pending":
                                    break

                        if blocker and self._is_hard_submission_blocker(blocker):
                            msg = self._submission_blocker_message(blocker)
                            app.notes = f"Submission blocked: {msg}"
                            app.automation_log += f"Submission hard-blocked by portal: {msg}\n"
                            await self._capture_blocker_details(
                                page,
                                app,
                                user,
                                db,
                                reason=f"submission_blocked_{blocker}",
                                message=app.notes,
                            )
                            await self._save_external_debug_artifacts(page, app, db, tag="external_post_submit_hard_block")
                            db.commit()
                            return False

                        if blocker and self._is_auto_resolvable_submission_blocker(blocker):
                            remediation_filled = 0
                            scopes_retry = self._iter_scopes_prioritized(page)
                            for sc in scopes_retry:
                                try:
                                    remediation_filled += await self._fill_linkedin_modal_minimum_fields(
                                        sc, user, answer_overrides=runtime_overrides
                                    )
                                except Exception:
                                    continue
                            for sc in scopes_retry:
                                try:
                                    remediation_filled += await self._fill_required_radios_and_checkboxes(
                                        sc, user, answer_overrides=runtime_overrides
                                    )
                                except Exception:
                                    continue
                            for sc in scopes_retry:
                                try:
                                    remediation_filled += await self._fill_non_native_dropdowns(
                                        sc, user, answer_overrides=runtime_overrides
                                    )
                                except Exception:
                                    continue
                            remediation_filled += await self._diagnose_and_fill_known_portal_blockers(
                                page, user, app, db, answer_overrides=runtime_overrides
                            )
                            app.automation_log += (
                                f"Submission validation blocker '{blocker}' resolved with {remediation_filled} autofill update(s) on attempt {submit_attempt}.\n"
                            )
                            db.commit()
                            if submit_attempt < max_submit_attempts:
                                # Refresh submit button handle after DOM changes.
                                submit_btn = None
                                scopes_retry_submit = self._iter_scopes_prioritized(page)
                                tal_retry_scopes = [
                                    sc for sc in scopes_retry_submit
                                    if "apply.talemetry.com" in self._scope_url(sc)
                                ]
                                nav_retry_scopes = tal_retry_scopes or scopes_retry_submit
                                for sc in nav_retry_scopes:
                                    submit_btn, _ = await self._find_clickable_button(
                                        sc,
                                        [
                                            "submit application",
                                            "submit",
                                            "send application",
                                            "complete application",
                                            "finish",
                                            "review and submit",
                                        ],
                                    )
                                    if submit_btn:
                                        break
                                if submit_btn:
                                    continue

                        # Heuristic: if URL changed and submit button is no longer present, treat as likely submitted.
                        after_url = page.url
                        still_has_submit = False
                        scopes_after = self._iter_scopes_prioritized(page)
                        tal_after_scopes = [
                            sc for sc in scopes_after
                            if "apply.talemetry.com" in self._scope_url(sc)
                        ]
                        nav_after_scopes = tal_after_scopes or scopes_after
                        for sc in nav_after_scopes:
                            b, _ = await self._find_clickable_button(
                                sc,
                                [
                                    "submit application",
                                    "submit",
                                    "send application",
                                    "complete application",
                                    "finish",
                                    "review and submit",
                                ],
                            )
                            if b:
                                still_has_submit = True
                                break

                        if after_url != before_url and not still_has_submit:
                            app.notes = f"Submitted on external portal (likely). URL: {after_url}"
                            app.automation_log += (
                                f"URL changed and submit control disappeared after attempt {submit_attempt}; assuming submitted.\n"
                            )
                            db.commit()
                            return True

                        if blocker and submit_attempt >= max_submit_attempts:
                            msg = self._submission_blocker_message(blocker)
                            app.notes = f"Submission blocked: {msg}"
                            app.automation_log += (
                                f"Submission blocked after {submit_attempt} attempts: {msg}\n"
                            )
                            await self._capture_blocker_details(
                                page,
                                app,
                                user,
                                db,
                                reason=f"submission_blocked_{blocker}",
                                message=app.notes,
                            )
                            await self._save_external_debug_artifacts(page, app, db, tag="external_post_submit_blocked")
                            db.commit()
                            return False

                    await self._save_external_debug_artifacts(page, app, db, tag="external_post_submit_uncertain")
                    app.automation_log += (
                        "Clicked submit multiple times but could not confirm submission automatically; leaving for review.\n"
                    )
                    db.commit()
                    return False
                except Exception:
                    app.automation_log += "Submit interaction raised an exception; retrying with next-step logic.\n"
                    db.commit()

            next_btn, _ = None, ""

            # Avoid clicking "Next/Continue" controls on the parent posting page when an embedded
            # Talemetry apply iframe is present but still mounting.
            if tal_scopes and step_filled == 0:
                has_controls = False
                for tal_scope in tal_scopes:
                    try:
                        if await self._scope_has_fillable_controls(tal_scope):
                            has_controls = True
                            break
                    except Exception:
                        continue
                if not has_controls and step < max_steps:
                    app.automation_log += "Talemetry apply iframe still rendering form controls; waiting before retry.\n"
                    db.commit()
                    await asyncio.sleep(2.0)
                    continue

            # Workday uses custom navigation controls (`click_filter`) and hidden buttons.
            # Use dedicated detection before generic text matching.
            try:
                url_l = (page.url or "").lower()
                if "myworkdayjobs.com" in url_l and "/apply" in url_l:
                    try:
                        wd_btn, wd_label = await self._find_workday_navigation_control(page)
                        if wd_btn:
                            # If label implies submit, treat as final submit.
                            if any(k in wd_label for k in ("submit", "finish", "complete", "send")):
                                if safe_mode or require_confirmation:
                                    app.notes = "Ready for final submission; review in browser."
                                    app.automation_log += (
                                        "Reached Workday final submit control; safe mode kept final click manual.\n"
                                    )
                                    db.commit()
                                    return False
                                await self._save_external_debug_artifacts(page, app, db, tag="workday_pre_submit")
                                await wd_btn.click(timeout=7000, force=True, no_wait_after=True)
                                await asyncio.sleep(2.0)
                                if await self._detect_external_submission_success(page):
                                    app.notes = f"Submitted on external portal (detected). URL: {page.url}"
                                    app.automation_log += "Detected external submission success.\n"
                                    db.commit()
                                    return True
                                blocker = await self._detect_external_submission_blocker(page)
                                if blocker:
                                    msg = self._submission_blocker_message(blocker)
                                    app.notes = f"Submission blocked: {msg}"
                                    app.automation_log += f"Submission blocked by portal validation: {msg}\n"
                                    await self._capture_blocker_details(
                                        page,
                                        app,
                                        user,
                                        db,
                                        reason=f"submission_blocked_{blocker}",
                                        message=app.notes,
                                    )
                                    await self._save_external_debug_artifacts(page, app, db, tag="workday_post_submit_blocked")
                                    db.commit()
                                    return False
                                app.automation_log += "Clicked Workday submit but could not confirm automatically; leaving for review.\n"
                                db.commit()
                                return False

                            # Otherwise, progress to next step.
                            app.automation_log += f"Workday: clicking navigation control '{wd_label}'.\n"
                            db.commit()
                            await wd_btn.click(timeout=7000, force=True, no_wait_after=True)
                            await asyncio.sleep(2.0)
                            continue
                    except Exception:
                        pass
            except Exception:
                pass

            # Guard: on Workday, avoid clicking unrelated "next" controls when not on real apply flow.
            try:
                url_l = (page.url or "").lower()
                if "myworkdayjobs.com" in url_l and not await self._has_workday_apply_navigation(page):
                    if step == 1:
                        app.automation_log += (
                            "Workday page has no active application navigation after Apply action; not attempting generic Next clicks.\n"
                        )
                        db.commit()
            except Exception:
                pass

            allow_generic_next = True
            try:
                url_l = (page.url or "").lower()
                if "myworkdayjobs.com" in url_l and not await self._has_workday_apply_navigation(page):
                    allow_generic_next = False
            except Exception:
                allow_generic_next = True

            if not allow_generic_next:
                if tal_scopes and step < max_steps:
                    app.automation_log += "Talemetry step has no actionable navigation yet; retrying.\n"
                    db.commit()
                    await asyncio.sleep(2.0)
                    continue
                if step_filled == 0 and step == 1:
                    app.automation_log += "No recognizable form fields found on target page.\n"
                    await self._save_external_debug_artifacts(page, app, db, tag=f"external_step{step}_no_fields")
                else:
                    await self._save_external_debug_artifacts(page, app, db, tag=f"external_step{step}_stalled")
                app.automation_log += "Could not locate final submit button automatically.\n"
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="final_submit_detection_failed",
                    message="Could not locate final submit button automatically.",
                )
                db.commit()
                return False

            for scope in nav_scopes:
                try:
                    next_keywords = ["continue", "next", "review application", "review"]
                    try:
                        url_l = (page.url or "").lower()
                        if "myworkdayjobs.com" in url_l:
                            # Avoid false matches like "Continue Editing" on Workday warning dialogs.
                            next_keywords = [
                                "save and continue",
                                "continue to next step",
                                "next",
                                "review application",
                                "review and submit",
                                "review",
                            ]
                    except Exception:
                        pass
                    next_btn, _ = await self._find_clickable_button(
                        scope,
                        next_keywords,
                    )
                except Exception:
                    next_btn = None
                if next_btn:
                    break
            if next_btn:
                try:
                    app.automation_log += "Clicked next/review control on external application form.\n"
                    db.commit()
                    await next_btn.click()
                    await asyncio.sleep(2)
                    continue
                except Exception:
                    pass

            if tal_scopes and step < max_steps:
                app.automation_log += "Talemetry step stalled; retrying to allow iframe state to advance.\n"
                db.commit()
                await asyncio.sleep(2.0)
                continue

            challenge_reason = await self._detect_anti_bot_challenge(page)
            if challenge_reason:
                app.notes = (
                    "Application portal blocked by anti-bot verification challenge; "
                    "complete verification manually, then retry automation."
                )
                app.automation_log += (
                    f"Blocked near final-submit detection by anti-bot challenge "
                    f"({challenge_reason}).\n"
                )
                await self._capture_blocker_details(
                    page,
                    app,
                    user,
                    db,
                    reason="anti_bot_challenge",
                    message=app.notes,
                    required_inputs=[
                        {
                            "key": "manual_challenge_verification",
                            "label": "Manual Verification",
                            "question": "Complete anti-bot verification in the opened browser window, then retry.",
                            "type": "manual_action",
                            "required": True,
                        }
                    ],
                )
                db.commit()
                return False

            if step_filled == 0 and step == 1:
                app.automation_log += "No recognizable form fields found on target page.\n"
                await self._save_external_debug_artifacts(page, app, db, tag=f"external_step{step}_no_fields")
            else:
                # Even when we filled something, capture artifacts if we can't progress.
                await self._save_external_debug_artifacts(page, app, db, tag=f"external_step{step}_stalled")
            app.automation_log += "Could not locate final submit button automatically.\n"
            await self._capture_blocker_details(
                page,
                app,
                user,
                db,
                reason="final_submit_detection_failed",
                message="Could not locate final submit button automatically.",
            )
            db.commit()
            return False

        app.automation_log += "Reached external-apply step limit without final submit detection.\n"
        await self._capture_blocker_details(
            page,
            app,
            user,
            db,
            reason="external_step_limit_reached",
            message="Reached external-apply step limit without final submit detection.",
        )
        db.commit()
        return False

    async def _assist_external_challenge_and_retry(
        self,
        url: str,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        safe_mode: bool,
        require_confirmation: bool,
        answer_overrides: Optional[dict[str, Any]] = None,
    ) -> Optional[bool]:
        """
        Open a visible browser to let the user complete anti-bot verification,
        then retry form progression in the same interactive context.
        """
        if not settings.external_challenge_assist:
            return None
        if self._abort_if_stop_requested(db, app, "challenge-assist-start"):
            return False
        state_path = self._external_storage_state_path(url)

        app.automation_log += (
            "Opening interactive browser window for anti-bot verification. "
            "Please complete verification to continue...\n"
        )
        db.commit()

        timeout_seconds = max(60, int(settings.external_challenge_timeout_seconds))
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context_params = {}
                if state_path and os.path.exists(state_path):
                    context_params["storage_state"] = state_path
                context = await browser.new_context(**context_params)
                page = await context.new_page()

                crashed = {"value": False}

                def _on_crash():
                    crashed["value"] = True

                try:
                    page.on("crash", lambda: _on_crash())
                except Exception:
                    pass

                async def _ensure_page_open():
                    nonlocal page
                    if crashed["value"] or page.is_closed():
                        crashed["value"] = False
                        msg = "Apply window page crashed/closed; reopening and retrying verification..."
                        app.automation_log += msg + "\n"
                        try:
                            self._record_issue_event(db, app, app.job if app else None, user, msg, event_type="detected")
                        except Exception:
                            pass
                        db.commit()
                        try:
                            page = await context.new_page()
                            try:
                                page.on("crash", lambda: _on_crash())
                            except Exception:
                                pass
                        except Exception:
                            return False
                    return True

                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                deadline = asyncio.get_running_loop().time() + timeout_seconds
                challenge_reason = await self._detect_anti_bot_challenge(page)
                while challenge_reason and asyncio.get_running_loop().time() < deadline:
                    if self._abort_if_stop_requested(db, app, "challenge-assist-wait"):
                        await browser.close()
                        return False
                    await asyncio.sleep(2)
                    ok = await _ensure_page_open()
                    if not ok:
                        break
                    challenge_reason = await self._detect_anti_bot_challenge(page)

                if challenge_reason:
                    app.automation_log += (
                        "Interactive verification timed out before challenge cleared. "
                        f"Last reason: {challenge_reason}\n"
                    )
                    db.commit()
                    await browser.close()
                    return None

                if state_path:
                    try:
                        await context.storage_state(path=state_path)
                        app.automation_log += "Saved verified browser session for this portal domain.\n"
                    except Exception:
                        pass

                app.automation_log += "Verification cleared. Continuing application steps...\n"
                if app.notes and "anti-bot verification challenge" in app.notes.lower():
                    app.notes = None
                db.commit()
                submitted = await self._complete_external_apply_steps(
                    page=page,
                    user=user,
                    resume_path=resume_path,
                    app=app,
                    db=db,
                    safe_mode=safe_mode,
                    require_confirmation=require_confirmation,
                    answer_overrides=answer_overrides,
                )
                if self._abort_if_stop_requested(db, app, "challenge-assist-complete"):
                    await browser.close()
                    return False
                await browser.close()
                return submitted
        except Exception as e:
            app.automation_log += f"Interactive challenge assist failed: {e}\n"
            db.commit()
            return None

    # ------------------------------------------------------------------
    # Generic external apply
    # ------------------------------------------------------------------

    async def _handle_generic_apply(
        self,
        page: Page,
        user: UserProfile,
        resume_path: str,
        app: Application,
        db,
        answer_overrides: Optional[dict[str, Any]] = None,
        safe_mode: bool = True,
        require_confirmation: bool = True,
    ) -> bool:
        """Best-effort form filler for external sites."""
        if self._abort_if_stop_requested(db, app, "generic-apply-start"):
            return False
        app.automation_log += "Identifying form fields on external site...\n"
        db.commit()
        try:
            await self._wait_for_workday_hydration(page, app, db)
        except Exception:
            pass

        # If we are still on a board listing page, follow the outbound "apply" link first.
        board_hosts = {"himalayas.app", "remotive.com", "remoteok.com", "arbeitnow.com"}
        page_host = (urllib.parse.urlparse(page.url).hostname or "").lower()
        if any(host in page_host for host in board_hosts):
            anchors = await page.query_selector_all("a[href]")
            for anchor in anchors[:200]:
                href = (await anchor.get_attribute("href") or "").strip()
                if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                text = ((await anchor.inner_text()) or "").strip().lower()
                if "apply" not in text:
                    continue
                target_url = urllib.parse.urljoin(page.url, href)
                if target_url == page.url:
                    continue
                app.automation_log += f"Following apply link to {target_url}\n"
                db.commit()
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                break

        challenge_reason = await self._detect_anti_bot_challenge(page)
        if challenge_reason:
            app.notes = (
                "Application portal blocked by anti-bot verification challenge; "
                "complete verification manually, then retry automation."
            )
            app.automation_log += (
                f"Blocked by anti-bot challenge on apply page ({challenge_reason}). "
                "Manual browser verification required.\n"
            )
            await self._capture_blocker_details(
                page,
                app,
                user,
                db,
                reason="anti_bot_challenge",
                message=app.notes,
                required_inputs=[
                    {
                        "key": "manual_challenge_verification",
                        "label": "Manual Verification",
                        "question": "Complete anti-bot verification in the opened browser window, then retry.",
                        "type": "manual_action",
                        "required": True,
                    }
                ],
            )
            db.commit()
            assisted = await self._assist_external_challenge_and_retry(
                page.url,
                user,
                resume_path,
                app,
                db,
                safe_mode=safe_mode,
                require_confirmation=require_confirmation,
                answer_overrides=answer_overrides,
            )
            if assisted is not None:
                return assisted
            return False

        max_steps = 6
        try:
            url_l = (page.url or "").lower()
            if "myworkdayjobs.com" in url_l:
                # Workday typically has more steps (account/contact/resume/questions/review).
                max_steps = 14
        except Exception:
            pass

        submitted = await self._complete_external_apply_steps(
            page=page,
            user=user,
            resume_path=resume_path,
            app=app,
            db=db,
            safe_mode=safe_mode,
            require_confirmation=require_confirmation,
            answer_overrides=answer_overrides,
            max_steps=max_steps,
        )
        if self._abort_if_stop_requested(db, app, "generic-apply-end"):
            return False
        if submitted:
            return True

        if app.notes and "anti-bot verification challenge" in app.notes.lower():
            assisted = await self._assist_external_challenge_and_retry(
                page.url,
                user,
                resume_path,
                app,
                db,
                safe_mode=safe_mode,
                require_confirmation=require_confirmation,
                answer_overrides=answer_overrides,
            )
            if assisted is not None:
                return assisted
        return False

    # ------------------------------------------------------------------
    # LinkedIn field auto-fill helper
    # ------------------------------------------------------------------

    async def _fill_linkedin_fields(self, page: Page, user: UserProfile) -> int:
        first_name = user.full_name.split()[0] if user.full_name else ""
        last_name = user.full_name.split()[-1] if user.full_name and len(user.full_name.split()) > 1 else ""

        fields = [
            (["first name", "firstname", "first_name", "given name"], first_name),
            (["last name", "lastname", "last_name", "surname", "family name"], last_name),
            (["full name", "fullname", "full_name", "name"], user.full_name or ""),
            (["phone", "mobile", "telephone"], self._normalize_mobile_number(user.phone or "")),
            (["email", "e-mail"], user.email or ""),
            (["city", "location", "address"], user.location or ""),
        ]

        filled = 0
        for aliases, value in fields:
            if await self._fill_external_field(page, aliases, value):
                filled += 1
        return filled
