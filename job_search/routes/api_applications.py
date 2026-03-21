from __future__ import annotations
from datetime import datetime
import os
import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from job_search.config import settings
from job_search.database import get_db
from job_search.models import Application, ApplicationStatus, Job, UserProfile, Resume, AutomationIssueEvent
from job_search.schemas.application import (
    ApplicationCreate,
    ApplicationUpdate,
    ApplicationResponse,
    ApplicationStatsResponse,
    AutomateRequest,
    BatchApplyRequest,
    BlockerAnswerRequest,
)

router = APIRouter()


def _sanitize_answer_value(value: Any) -> Any:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.lower() in {"", "null", "none"}:
            return None
        if trimmed.lower() in {"true", "yes"}:
            return True
        if trimmed.lower() in {"false", "no"}:
            return False
        return trimmed
    return value


def _answer_map(profile: Optional[UserProfile], application: Application) -> dict[str, Any]:
    profile_answers = profile.application_answers if profile and isinstance(profile.application_answers, dict) else {}
    app_answers = application.user_inputs if isinstance(application.user_inputs, dict) else {}

    merged: dict[str, Any] = {}
    for raw_k, raw_v in profile_answers.items():
        k = str(raw_k or "").strip().lower()
        if k:
            merged[k] = raw_v
    for raw_k, raw_v in app_answers.items():
        k = str(raw_k or "").strip().lower()
        if k:
            merged[k] = raw_v
    if profile:
        merged.update(
            {
                "full_name": profile.full_name,
                "official_email": profile.email,
                "email": profile.email,
                "phone": profile.phone,
                "location": profile.location,
                "linkedin_url": profile.linkedin_url,
                "expected_ctc_lpa": profile.expected_ctc_lpa,
                "current_ctc_lpa": profile.current_ctc_lpa,
                "notice_period_days": profile.notice_period_days,
                "can_join_immediately": profile.can_join_immediately,
                "willing_to_relocate": profile.willing_to_relocate,
                "requires_sponsorship": profile.requires_sponsorship,
                "work_authorization": profile.work_authorization,
            }
        )
    return merged


def _is_missing_required(item: dict[str, Any], merged_answers: dict[str, Any]) -> bool:
    key = str(item.get("key", "")).strip().lower()
    if not key:
        return True
    value = merged_answers.get(key)
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _sync_profile_from_answers(profile: UserProfile, answers: dict[str, Any]) -> None:
    field_map = {
        "full_name": "full_name",
        "email": "email",
        "official_email": "email",
        "phone": "phone",
        "location": "location",
        "linkedin_url": "linkedin_url",
        "expected_ctc_lpa": "expected_ctc_lpa",
        "current_ctc_lpa": "current_ctc_lpa",
        "notice_period_days": "notice_period_days",
        "can_join_immediately": "can_join_immediately",
        "willing_to_relocate": "willing_to_relocate",
        "requires_sponsorship": "requires_sponsorship",
        "work_authorization": "work_authorization",
    }
    for key, profile_field in field_map.items():
        if key not in answers:
            continue
        value = _sanitize_answer_value(answers[key])
        if value is None:
            continue
        if profile_field in {"expected_ctc_lpa", "current_ctc_lpa"}:
            try:
                value = float(value)
            except Exception:
                continue
        elif profile_field in {"notice_period_days"}:
            try:
                value = int(float(value))
            except Exception:
                continue
        elif profile_field in {"can_join_immediately", "willing_to_relocate", "requires_sponsorship"}:
            if isinstance(value, str):
                lv = value.strip().lower()
                if lv in {"yes", "true", "1"}:
                    value = True
                elif lv in {"no", "false", "0"}:
                    value = False
                else:
                    continue
        setattr(profile, profile_field, value)


@router.get("", response_model=list[ApplicationResponse])
def list_applications(
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Application)
    if status:
        query = query.filter(Application.status == status)
    if job_id:
        query = query.filter(Application.job_id == job_id)
    query = query.order_by(Application.created_at.desc())
    return query.offset((page - 1) * per_page).limit(per_page).all()


@router.get("/issues")
def list_automation_issues(
    limit: int = Query(100, ge=1, le=500),
    category: Optional[str] = None,
    event_type: Optional[str] = Query(None, pattern="^(detected|resolved)$"),
    db: Session = Depends(get_db),
):
    """Return recent automation issue events (detected/resolved) for learning/debugging."""
    q = db.query(AutomationIssueEvent).order_by(AutomationIssueEvent.id.desc())
    if category:
        q = q.filter(AutomationIssueEvent.category == category)
    if event_type:
        q = q.filter(AutomationIssueEvent.event_type == event_type)

    rows = q.limit(limit).all()
    return [
        {
            "id": row.id,
            "application_id": row.application_id,
            "job_id": row.job_id,
            "source": row.source,
            "domain": row.domain,
            "category": row.category,
            "event_type": row.event_type,
            "message": row.message,
            "required_user_inputs": row.required_user_inputs or [],
            "suggested_questions": row.suggested_questions or [],
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.post("", response_model=ApplicationResponse)
def create_application(request: ApplicationCreate, db: Session = Depends(get_db)):
    """Create a new application for a job."""
    job = db.query(Job).filter(Job.id == request.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    existing = db.query(Application).filter(Application.job_id == request.job_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Application already exists for this job")

    application = Application(
        job_id=request.job_id,
        status=ApplicationStatus.QUEUED,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


@router.patch("/{app_id}", response_model=ApplicationResponse)
def update_application(app_id: int, request: ApplicationUpdate, db: Session = Depends(get_db)):
    application = db.query(Application).filter(Application.id == app_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if request.status:
        try:
            application.status = ApplicationStatus(str(request.status).strip().lower())
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid status '{request.status}'")
    if request.notes is not None:
        application.notes = request.notes

    db.commit()
    db.refresh(application)
    return application


# ------------------------------------------------------------------
# Pre-apply preview (powers the confirmation modal)
# ------------------------------------------------------------------

@router.get("/{app_id}/preview")
def preview_application(app_id: int, db: Session = Depends(get_db)):
    """Return everything the user needs to see before confirming an application."""
    from job_search.services.applier import JobApplier

    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    job = db.query(Job).filter(Job.id == app.job_id).first()
    profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
    resumes = db.query(Resume).order_by(Resume.is_primary.desc()).all()
    primary_resume = resumes[0] if resumes else None
    applier = JobApplier()
    if profile or primary_resume:
        profile = applier._hydrate_profile_from_resume_if_needed(profile, primary_resume, db)
    if job and profile:
        applier.refresh_job_score_if_stale(job, profile, db, float(settings.auto_apply_min_score))

    # Best-effort profile fallback from parsed resume for preview display.
    fallback = primary_resume.parsed_data if primary_resume and primary_resume.parsed_data else {}
    effective_profile = {
        "full_name": (profile.full_name if profile and profile.full_name else fallback.get("name", "")),
        "email": (profile.email if profile and profile.email else fallback.get("email", "")),
        "phone": (profile.phone if profile and profile.phone else fallback.get("phone", "")),
        "location": (profile.location if profile and profile.location else fallback.get("location", "")),
        "linkedin_url": (profile.linkedin_url if profile and profile.linkedin_url else fallback.get("linkedin_url", "")),
    }

    # Determine tailoring strategy
    if settings.llm_provider == "ollama":
        tailoring_strategy = "AI-tailored (Ollama)"
    elif settings.llm_provider == "claude" and settings.anthropic_api_key:
        tailoring_strategy = "AI-tailored (Claude)"
    elif settings.llm_provider == "openai" and settings.openai_api_key:
        tailoring_strategy = "AI-tailored (OpenAI)"
    else:
        tailoring_strategy = "Keyword-optimized (no LLM)"

    return {
        "application": {
            "id": app.id,
            "status": app.status.value if hasattr(app.status, "value") else app.status,
        },
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "match_score": job.match_score,
            "is_easy_apply": job.is_easy_apply,
            "source": job.source,
            "match_details": job.match_details,
        } if job else None,
        "profile": effective_profile,
        "resumes": [
            {
                "id": r.id,
                "name": r.name,
                "file_type": r.file_type,
                "is_primary": r.is_primary,
            }
            for r in resumes
        ],
        "tailoring_strategy": tailoring_strategy,
    }


@router.get("/{app_id}/preflight")
async def preflight_application(app_id: int, resume_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Validate whether an application can be auto-filled safely."""
    from job_search.services.applier import JobApplier
    from job_search.services.apply_url_resolver import resolve_official_apply_url

    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    job = db.query(Job).filter(Job.id == app.job_id).first()
    profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()

    selected_resume = None
    if resume_id:
        selected_resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not selected_resume:
        selected_resume = db.query(Resume).filter(Resume.is_primary == True).first()

    applier = JobApplier()
    source_mode = applier.source_mode(job) if job else "manual"
    supported_sources = {"linkedin", "greenhouse", "lever", "generic"}
    source_supported = source_mode in supported_sources

    issues = []
    warnings = []

    if not selected_resume:
        issues.append("No resume is available.")
    elif not selected_resume.parsed_data:
        warnings.append("Selected resume is not parsed. Automation will upload original file without tailoring.")

    parsed = selected_resume.parsed_data if selected_resume and selected_resume.parsed_data else {}
    effective_name = (profile.full_name if profile and profile.full_name else parsed.get("name", ""))
    effective_email = (profile.email if profile and profile.email else parsed.get("email", ""))

    if not effective_name:
        issues.append("Full name is required (set profile or upload parseable resume).")
    if not effective_email:
        issues.append("Email is required (set profile or upload parseable resume).")

    if not source_supported:
        warnings.append(f"Source '{job.source if job else 'unknown'}' is not fully automated. Manual flow required.")
    elif source_mode == "linkedin":
        has_saved_state = any(
            os.path.exists(path) for path in ("data/browser_state/linkedin_state.json", "data/browser_state/linkedin.json")
        )
        has_credentials = bool(settings.linkedin_email and settings.linkedin_password)
        if not has_saved_state and not has_credentials:
            warnings.append(
                "LinkedIn login is not configured. Run `./venv/bin/python scripts/save_linkedin_session.py` "
                "or set LinkedIn credentials for reliable automation."
            )
    elif source_mode == "generic" and job:
        resolution = await resolve_official_apply_url(job.apply_url or job.url or "", job.source or "")
        if resolution.get("warnings"):
            warnings.extend(resolution["warnings"])
        if not resolution.get("resolved_url"):
            warnings.append(
                "Could not resolve official apply URL from this board listing. "
                f"Reason: {resolution.get('reason', 'unknown')}. "
                "Automation can still open this listing in safe/manual-assist mode."
            )

    threshold = float(settings.auto_apply_min_score)
    if job and profile:
        refreshed, old_score, new_score = applier.refresh_job_score_if_stale(job, profile, db, threshold)
        if refreshed:
            warnings.append(
                f"Job score was refreshed from {old_score if old_score is not None else 'N/A'} to {new_score}."
            )
    score_ok = job is not None and (job.match_score is None or float(job.match_score) >= threshold)
    if not score_ok:
        warnings.append(f"Job score is below auto-apply threshold ({threshold}).")

    ready = (len(issues) == 0) and source_supported and score_ok
    return {
        "application_id": app_id,
        "ready": ready,
        "issues": issues,
        "warnings": warnings,
        "source_mode": source_mode,
        "source_supported": source_supported,
        "score_ok": score_ok,
        "threshold": threshold,
        "resume_id": selected_resume.id if selected_resume else None,
    }


@router.get("/{app_id}/blockers")
def get_application_blockers(app_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
    blocker_details = app.blocker_details if isinstance(app.blocker_details, dict) else {}
    required_inputs = blocker_details.get("required_inputs") if isinstance(blocker_details, dict) else None

    if not required_inputs:
        latest_issue = (
            db.query(AutomationIssueEvent)
            .filter(
                AutomationIssueEvent.application_id == app.id,
                AutomationIssueEvent.event_type == "detected",
            )
            .order_by(AutomationIssueEvent.id.desc())
            .first()
        )
        issue_inputs = []
        if latest_issue and isinstance(latest_issue.required_user_inputs, list):
            for idx, key in enumerate(latest_issue.required_user_inputs):
                question = None
                if isinstance(latest_issue.suggested_questions, list) and idx < len(latest_issue.suggested_questions):
                    question = latest_issue.suggested_questions[idx]
                issue_inputs.append(
                    {
                        "key": str(key).strip().lower(),
                        "label": question or str(key).replace("_", " ").title(),
                        "question": question,
                        "type": "text",
                        "required": True,
                    }
                )
        required_inputs = issue_inputs

    if not isinstance(required_inputs, list):
        required_inputs = []
    normalized_required = [ri for ri in required_inputs if isinstance(ri, dict)]

    merged_answers = _answer_map(profile, app)
    unresolved = [item for item in normalized_required if _is_missing_required(item, merged_answers)]
    known_answers = {k: v for k, v in merged_answers.items() if v not in (None, "")}

    return {
        "application_id": app.id,
        "status": app.status.value if hasattr(app.status, "value") else str(app.status),
        "notes": app.notes,
        "blocker_details": blocker_details or None,
        "required_inputs": normalized_required,
        "unresolved_required_inputs": unresolved,
        "can_retry_now": len(unresolved) == 0,
        "known_answer_keys": sorted(known_answers.keys()),
        "known_answers": known_answers,
    }


@router.post("/{app_id}/blockers/answers")
async def save_blocker_answers(
    app_id: int,
    request: BlockerAnswerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    from job_search.services.applier import JobApplier

    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    job = db.query(Job).filter(Job.id == app.job_id).first()

    profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
    if not profile:
        profile = UserProfile(full_name="", email="")
        db.add(profile)
        db.commit()
        db.refresh(profile)

    sanitized_answers: dict[str, Any] = {}
    for raw_key, raw_value in (request.answers or {}).items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        value = _sanitize_answer_value(raw_value)
        if value is None:
            continue
        sanitized_answers[key] = value
    if not sanitized_answers and not (request.retry_now or request.retry_all_blocked):
        raise HTTPException(status_code=400, detail="No valid answers provided")

    app_inputs = app.user_inputs if isinstance(app.user_inputs, dict) else {}
    if sanitized_answers:
        app_inputs.update(sanitized_answers)
    app.user_inputs = app_inputs

    if request.apply_globally and sanitized_answers:
        profile_answers = profile.application_answers if isinstance(profile.application_answers, dict) else {}
        for key, value in sanitized_answers.items():
            # Keep OTP/verification codes app-scoped; they are usually one-time tokens.
            if any(token in key for token in ("otp", "verification", "pin", "two_factor", "2fa", "security_code")):
                continue
            profile_answers[key] = value
        profile.application_answers = profile_answers

    if sanitized_answers:
        _sync_profile_from_answers(profile, sanitized_answers)

    blocker_details = app.blocker_details if isinstance(app.blocker_details, dict) else {}
    required_inputs = blocker_details.get("required_inputs") if isinstance(blocker_details, dict) else []
    if not isinstance(required_inputs, list):
        required_inputs = []
    required_inputs = [ri for ri in required_inputs if isinstance(ri, dict)]

    merged_answers = _answer_map(profile, app)
    unresolved = [item for item in required_inputs if _is_missing_required(item, merged_answers)]

    blocker_details["last_answer_update_at"] = datetime.utcnow().isoformat()
    blocker_details["pending_required_inputs"] = unresolved
    app.blocker_details = blocker_details

    domain = None
    try:
        domain = (urllib.parse.urlparse((job.apply_url or job.url or "") if job else "").hostname or "").lower() or None
    except Exception:
        domain = None
    if sanitized_answers:
        db.add(
            AutomationIssueEvent(
                application_id=app.id,
                job_id=app.job_id,
                source=(job.source.lower() if job and job.source else None),
                domain=domain,
                category="user_inputs_provided",
                event_type="resolved",
                message=f"User provided blocker answers ({', '.join(sorted(sanitized_answers.keys())[:8])}).",
                required_user_inputs=[],
                suggested_questions=[],
            )
        )

    app.error_message = None
    db.commit()

    retried_application_ids: list[int] = []
    applier = JobApplier()
    if request.retry_now and len(unresolved) == 0:
        background_tasks.add_task(
            applier.run_automation,
            app.id,
            request.resume_id,
            request.safe_mode,
            request.require_confirmation,
        )
        retried_application_ids.append(app.id)

    if request.retry_all_blocked:
        blocked_apps = (
            db.query(Application)
            .filter(
                Application.id != app.id,
                Application.status.in_([ApplicationStatus.REVIEWED, ApplicationStatus.FAILED]),
            )
            .all()
        )
        for blocked in blocked_apps:
            details = blocked.blocker_details if isinstance(blocked.blocker_details, dict) else {}
            reqs = details.get("required_inputs") if isinstance(details, dict) else []
            if not isinstance(reqs, list):
                reqs = []
            reqs = [ri for ri in reqs if isinstance(ri, dict)]

            merged_for_blocked = _answer_map(profile, blocked)
            unresolved_blocked = [ri for ri in reqs if _is_missing_required(ri, merged_for_blocked)]
            if unresolved_blocked:
                continue

            background_tasks.add_task(
                applier.run_automation,
                blocked.id,
                request.resume_id,
                request.safe_mode,
                request.require_confirmation,
            )
            retried_application_ids.append(blocked.id)

    return {
        "application_id": app.id,
        "saved_answer_keys": sorted(sanitized_answers.keys()),
        "pending_required_inputs": unresolved,
        "retry_started_for_application_ids": retried_application_ids,
    }


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

@router.get("/stats", response_model=ApplicationStatsResponse)
def get_stats(db: Session = Depends(get_db)):
    total = db.query(Application).count()
    counts = (
        db.query(Application.status, sa_func.count())
        .group_by(Application.status)
        .all()
    )
    stats = {"total": total}
    for status, count in counts:
        status_val = status.value if hasattr(status, "value") else status
        if status_val in ("queued", "submitted", "interview", "rejected", "offer", "failed"):
            stats[status_val] = count
    return ApplicationStatsResponse(**stats)


# ------------------------------------------------------------------
# Batch apply
# ------------------------------------------------------------------

@router.post("/batch-apply")
def batch_apply(
    request: BatchApplyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue selected jobs for application, gated by a score threshold."""
    from job_search.services.applier import JobApplier

    created = []
    skipped = []
    automated = []
    min_score = request.min_score if request.min_score is not None else float(settings.auto_apply_min_score)

    for job_id in request.job_ids:
        existing = db.query(Application).filter(Application.job_id == job_id).first()
        if existing:
            skipped.append(job_id)
            continue
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            skipped.append(job_id)
            continue
        if job.match_score is not None and float(job.match_score) < min_score:
            skipped.append(job_id)
            continue
        app = Application(job_id=job_id, status=ApplicationStatus.QUEUED)
        db.add(app)
        created.append(job_id)

    db.commit()

    if request.auto_automate and created:
        applier = JobApplier()
        created_apps = (
            db.query(Application)
            .filter(Application.job_id.in_(created))
            .all()
        )
        for app in created_apps:
            background_tasks.add_task(
                applier.run_automation,
                app.id,
                request.resume_id,
                request.safe_mode,
                request.require_confirmation,
            )
            automated.append(app.job_id)

    return {
        "created": len(created),
        "skipped": len(skipped),
        "job_ids": created,
        "automated": automated,
        "min_score": min_score,
    }


# ------------------------------------------------------------------
# Automate (with resume selection)
# ------------------------------------------------------------------

@router.post("/{app_id}/automate")
async def automate_application(
    app_id: int,
    background_tasks: BackgroundTasks,
    request: Optional[AutomateRequest] = None,
    db: Session = Depends(get_db),
):
    """Launch automated application with optional resume selection."""
    from job_search.services.applier import JobApplier

    app = db.query(Application).filter(Application.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Clear stale stop flags from a previous run before starting again.
    user_inputs = dict(app.user_inputs or {})
    if user_inputs.get("__stop_requested"):
        user_inputs["__stop_requested"] = False
        user_inputs["__stop_requested_at"] = None
        user_inputs["__stop_reason"] = None
        app.user_inputs = user_inputs
        db.commit()

    resume_id = request.resume_id if request else None
    safe_mode = request.safe_mode if request else False
    require_confirmation = request.require_confirmation if request else False
    applier = JobApplier()
    background_tasks.add_task(applier.run_automation, app_id, resume_id, safe_mode, require_confirmation)
    return {
        "message": "Automation started",
        "application_id": app_id,
        "resume_id": resume_id,
        "safe_mode": safe_mode,
        "require_confirmation": require_confirmation,
    }


@router.post("/stop-active")
def stop_active_automations(db: Session = Depends(get_db)):
    """Request graceful stop for all currently running automations."""
    now = datetime.now().isoformat()
    running_apps = (
        db.query(Application)
        .filter(Application.status == ApplicationStatus.IN_PROGRESS)
        .all()
    )
    for app in running_apps:
        user_inputs = dict(app.user_inputs or {})
        user_inputs["__stop_requested"] = True
        user_inputs["__stop_requested_at"] = now
        user_inputs["__stop_reason"] = "Stopped from Jobs UI"
        app.user_inputs = user_inputs
        app.notes = "Stop requested from UI."
        app.status = ApplicationStatus.REVIEWED
        app.error_message = None
        app.automation_log = (app.automation_log or "") + "Stop requested from Jobs UI.\n"
    db.commit()
    return {
        "stopping_requested": len(running_apps),
        "application_ids": [a.id for a in running_apps],
    }
