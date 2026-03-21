from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from job_search.database import SessionLocal
from job_search.models import (
    Application,
    ApplicationStatus,
    Job,
    Resume,
    UserProfile,
    AutonomousRun,
    AutonomousJobLog,
)
from job_search.services.job_matcher import JobMatcher
from job_search.services.applier import JobApplier
from job_search.services.apply_url_resolver import (
    is_official_submission_target,
    resolve_official_apply_url,
)

logger = logging.getLogger(__name__)


@dataclass
class JobAttributes:
    title: str
    company: str
    location: str
    source: str
    requirements: list[str]


class JobParserAgent:
    def parse(self, job: Job) -> JobAttributes:
        text = (job.description or "").lower()
        reqs = []
        for keyword in ["python", "sales", "customer success", "crm", "leadership", "sql", "aws"]:
            if keyword in text:
                reqs.append(keyword)
        return JobAttributes(
            title=job.title,
            company=job.company,
            location=job.location or "",
            source=job.source or "",
            requirements=reqs,
        )


class MatchScoringAgent:
    def __init__(self):
        self.matcher = JobMatcher()

    def score(self, job: Job, profile: Optional[UserProfile]) -> tuple[float, dict[str, Any]]:
        if not profile:
            return 50.0, {
                "explanation": "Unscored: profile missing",
                "unscored_reason": "profile_missing",
            }

        profile_dict = {
            "skills": profile.skills or [],
            "target_roles": profile.target_roles or [],
            "target_locations": profile.target_locations or [],
            "experience": profile.experience or [],
            "summary": profile.summary or "",
            "headline": profile.headline or "",
        }
        job_dict = {
            "title": job.title,
            "description": job.description or "",
            "location": job.location or "",
            "work_type": job.work_type or "",
        }
        result = self.matcher.score_job(job_dict, profile_dict)
        return result.overall_score, {
            "skill_score": result.skill_score,
            "title_score": result.title_score,
            "experience_score": result.experience_score,
            "location_score": result.location_score,
            "keyword_score": result.keyword_score,
            "explanation": result.explanation,
            "matched_skills": result.matched_skills,
            "missing_skills": result.missing_skills,
        }


class ResumeTailoringAgent:
    async def tailor_preview(self, resume: Optional[Resume], job: Job) -> dict[str, Any]:
        if not resume:
            return {"tailoring": "no_resume"}
        if not resume.parsed_data:
            return {"tailoring": "original_fallback"}
        # Final tailoring is handled by JobApplier during submission.
        return {
            "tailoring": "ready",
            "resume_id": resume.id,
            "has_parsed_data": True,
            "job_title": job.title,
        }


class ApplicationFormAgent:
    def prepare(self, job: Job, profile: Optional[UserProfile]) -> dict[str, Any]:
        return {
            "source": job.source,
            "apply_url": job.apply_url or job.url,
            "uses_real_identity": True,
            "identity_fields": {
                "full_name": bool(profile and profile.full_name),
                "email": bool(profile and profile.email),
                "phone": bool(profile and profile.phone),
            },
        }


class SubmissionAgent:
    def __init__(self):
        self.applier = JobApplier()

    async def submit(
        self,
        application_id: int,
        resume_id: Optional[int],
        safe_mode: bool,
        require_confirmation: bool,
    ) -> None:
        await self.applier.run_automation(
            application_id=application_id,
            resume_id=resume_id,
            safe_mode=safe_mode,
            require_confirmation=require_confirmation,
        )


class TrackerAgent:
    def record_confirmation(self, app: Application) -> dict[str, Any]:
        submission_audit = None
        if isinstance(getattr(app, "user_inputs", None), dict):
            submission_audit = app.user_inputs.get("__submission_audit")
        return {
            "application_id": app.id,
            "status": app.status.value if hasattr(app.status, "value") else str(app.status),
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "notes": app.notes,
            "error_message": app.error_message,
            "resume_version_id": app.resume_version_id,
            "submission_audit": submission_audit,
            "updated_at": datetime.now().isoformat(),
        }


class CoordinatorAgent:
    def __init__(self):
        self.parser = JobParserAgent()
        self.scorer = MatchScoringAgent()
        self.tailor = ResumeTailoringAgent()
        self.form = ApplicationFormAgent()
        self.submitter = SubmissionAgent()
        self.tracker = TrackerAgent()

    def _is_official_submission_target(self, job: Job) -> bool:
        """Block direct submission to job-board domains to prevent spam/test submissions."""
        url = job.apply_url or job.url or ""
        return is_official_submission_target(url, job.source or "")

    def _get_or_create_application(self, db: Session, job_id: int) -> Application:
        app = db.query(Application).filter(Application.job_id == job_id).first()
        if app:
            return app
        app = Application(job_id=job_id, status=ApplicationStatus.QUEUED)
        db.add(app)
        db.commit()
        db.refresh(app)
        return app

    def _upsert_log(self, db: Session, run_id: int, job_id: int) -> AutonomousJobLog:
        log_row = (
            db.query(AutonomousJobLog)
            .filter(AutonomousJobLog.run_id == run_id, AutonomousJobLog.job_id == job_id)
            .first()
        )
        if log_row:
            return log_row
        log_row = AutonomousJobLog(run_id=run_id, job_id=job_id, stage="queued", status="pending")
        db.add(log_row)
        db.commit()
        db.refresh(log_row)
        return log_row

    @staticmethod
    def _is_retryable_review_note(note: str) -> bool:
        text = (note or "").strip().lower()
        if not text:
            return False
        non_retryable_tokens = (
            "manual review",
            "no final submit control",
            "could not locate final submit button",
            "ready for final submission",
            "captcha",
            "anti-bot",
            "verification code",
            "login required",
            "sign-in",
            "blocked by anti-bot",
            "requires manual",
            "unsupported source",
        )
        if any(tok in text for tok in non_retryable_tokens):
            return False
        retryable_tokens = (
            "timeout",
            "temporar",
            "page crashed",
            "renderer crashed",
            "connection reset",
            "network",
            "stalled",
            "retry",
        )
        return any(tok in text for tok in retryable_tokens)

    async def run(
        self,
        run_id: int,
        job_ids: list[int],
        resume_id: Optional[int],
        min_score: float,
        safe_mode: bool,
        require_confirmation: bool,
        max_retries: int = 2,
    ):
        db = SessionLocal()
        try:
            run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
            if not run:
                return

            run.status = "running"
            run.started_at = datetime.now()
            run.total_jobs = len(job_ids)
            db.commit()

            profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
            resume = db.query(Resume).filter(Resume.id == resume_id).first() if resume_id else db.query(Resume).filter(Resume.is_primary == True).first()

            for job_id in job_ids:
                db.refresh(run)
                if run.status == "stopped":
                    run.finished_at = datetime.now()
                    db.commit()
                    return

                job = db.query(Job).filter(Job.id == job_id).first()
                if not job:
                    run.skipped_jobs += 1
                    run.processed_jobs += 1
                    db.commit()
                    continue

                log_row = self._upsert_log(db, run_id, job_id)
                log_row.attempts += 1
                log_row.stage = "parse"
                log_row.status = "running"
                db.commit()

                # Parse
                attrs = self.parser.parse(job)
                log_row.details = {"parsed": attrs.__dict__}
                db.commit()

                # Score
                log_row.stage = "score"
                score, details = self.scorer.score(job, profile)
                job.match_score = score
                merged = dict(job.match_details or {})
                merged.update(details)
                job.match_details = merged
                db.commit()

                if score < float(min_score):
                    log_row.stage = "decision"
                    log_row.status = "skipped"
                    log_row.message = f"Skipped: score {score} below threshold {min_score}"
                    run.skipped_jobs += 1
                    run.processed_jobs += 1
                    db.commit()
                    continue

                resolution = await resolve_official_apply_url(job.apply_url or job.url or "", job.source or "")
                details = dict(log_row.details or {})
                details["apply_target_resolution"] = resolution
                log_row.details = details

                if resolution.get("warnings"):
                    warning_text = "; ".join(resolution["warnings"])
                    log_row.message = f"Apply target warning: {warning_text}"

                resolved_url = resolution.get("resolved_url")
                if resolved_url:
                    if job.apply_url != resolved_url:
                        job.apply_url = resolved_url
                        db.commit()
                else:
                    log_row.stage = "decision"
                    log_row.status = "skipped"
                    reason = resolution.get("reason", "unknown")
                    if reason == "board_challenge_blocked":
                        log_row.message = (
                            "Skipped: board page blocked automation (anti-bot challenge). "
                            "Open the official apply page manually once and retry."
                        )
                    elif reason == "no_external_apply_link_found":
                        log_row.message = (
                            "Skipped: no official external apply link found on board page."
                        )
                    else:
                        log_row.message = f"Skipped: could not resolve official apply target ({reason})."
                    run.skipped_jobs += 1
                    run.processed_jobs += 1
                    db.commit()
                    continue

                # Tailor
                log_row.stage = "tailor"
                tail = await self.tailor.tailor_preview(resume, job)
                details = dict(log_row.details or {})
                details["tailoring"] = tail
                log_row.details = details
                db.commit()

                # Prepare form
                log_row.stage = "form"
                form_context = self.form.prepare(job, profile)
                details = dict(log_row.details or {})
                details["form_context"] = form_context
                log_row.details = details
                db.commit()

                # Submit with retries
                app = self._get_or_create_application(db, job.id)
                log_row.application_id = app.id
                db.commit()

                submitted = False
                review_required = False
                last_error = None
                for attempt in range(1, max_retries + 2):
                    db.refresh(run)
                    if run.status == "stopped":
                        log_row.status = "skipped"
                        log_row.message = "Stopped by user"
                        run.skipped_jobs += 1
                        run.processed_jobs += 1
                        db.commit()
                        return
                    log_row.stage = "submit"
                    log_row.status = "running"
                    log_row.message = f"Submission attempt {attempt}"
                    # Clear stale stop flags when a fresh run/attempt is intentionally started.
                    app_inputs = dict(app.user_inputs or {})
                    if app_inputs.get("__stop_requested"):
                        app_inputs["__stop_requested"] = False
                        app_inputs["__stop_requested_at"] = None
                        app_inputs["__stop_reason"] = None
                        app.user_inputs = app_inputs
                    db.commit()
                    try:
                        await self.submitter.submit(
                            application_id=app.id,
                            resume_id=resume.id if resume else None,
                            safe_mode=safe_mode,
                            require_confirmation=require_confirmation,
                        )
                        db.refresh(app)
                        if app.status == ApplicationStatus.SUBMITTED:
                            submitted = True
                            break
                        if app.status == ApplicationStatus.REVIEWED:
                            note_l = (app.notes or "").lower()
                            is_hard_block = any(
                                tok in note_l
                                for tok in (
                                    "verification code",
                                    "captcha",
                                    "sign-in",
                                    "login required",
                                    "anti-bot",
                                    "manual",
                                )
                            )
                            if is_hard_block:
                                review_required = True
                                last_error = app.notes or "Application requires manual review before submit"
                                break
                            if self._is_retryable_review_note(note_l) and attempt < (max_retries + 1):
                                last_error = app.notes or "Application in reviewed state; retrying automation."
                                await asyncio.sleep(1.0)
                                continue
                            last_error = app.notes or "Application requires review before submit"
                            review_required = True
                            break
                        last_error = app.error_message or f"Application ended with status {app.status}"
                    except Exception as e:
                        last_error = str(e)

                    await asyncio.sleep(1.0)

                log_row.stage = "track"
                db.refresh(app)
                confirmation = self.tracker.record_confirmation(app)
                log_row.confirmation = confirmation
                log_row.resume_version_id = app.resume_version_id

                if submitted:
                    log_row.status = "submitted"
                    log_row.message = "Application workflow completed"
                    run.submitted_jobs += 1
                elif review_required:
                    log_row.status = "skipped"
                    log_row.message = last_error or "Review required before final submit"
                    run.skipped_jobs += 1
                else:
                    log_row.status = "failed"
                    log_row.message = last_error or "Submission failed"
                    run.failed_jobs += 1

                run.processed_jobs += 1
                db.commit()

            run.status = "completed"
            run.finished_at = datetime.now()
            db.commit()

        except Exception as e:
            logger.exception(f"Autonomous run failed: {e}")
            run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
            if run:
                run.status = "failed"
                run.error_message = str(e)
                run.finished_at = datetime.now()
                db.commit()
        finally:
            db.close()
