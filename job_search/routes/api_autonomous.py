from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from job_search.config import settings
from job_search.database import get_db
from job_search.models import AutonomousRun, AutonomousJobLog, Resume, Job, Application, ApplicationStatus
from job_search.schemas.autonomous import (
    AutonomousRunRequest,
    AutonomousRunResponse,
    AutonomousRunStatusResponse,
    AutonomousStopResponse,
)
from job_search.services.workflow_agents import CoordinatorAgent

router = APIRouter()


HEARTBEAT_STAGE_MAP = {
    "queued": ("planner", "analyze"),
    "parse": ("perception", "analyze"),
    "score": ("matcher", "rank"),
    "decision": ("planner", "verify"),
    "tailor": ("matcher", "draft"),
    "form": ("executor", "verify"),
    "submit": ("executor", "submit"),
    "track": ("scheduler", "respond"),
}


def _normalize_heartbeat_status(run_status: str, log_status: str) -> str:
    if log_status in {"submitted", "completed"}:
        return "completed"
    if log_status in {"failed", "error"}:
        return "failed"
    if log_status in {"skipped", "blocked"}:
        return "blocked"
    if log_status == "running":
        return "running"
    if run_status == "queued":
        return "queued"
    return "running" if run_status == "running" else "queued"


def _build_heartbeat_summary(log: AutonomousJobLog) -> str:
    job = log.job
    job_label = f"{job.company if job else 'Unknown'} / {job.title if job else f'job #{log.job_id}'}"
    stage = (log.stage or "queued").lower()
    status = (log.status or "pending").lower()
    details = log.details if isinstance(log.details, dict) else {}
    score = details.get("score")
    strategy = details.get("tailoring")
    score_suffix = ""
    if score is not None:
        try:
            score_suffix = f" at {round(float(score))}%"
        except (TypeError, ValueError):
            score_suffix = ""

    if stage == "score":
        return f"Ranked {job_label}{score_suffix}."
    if stage == "decision" and status == "skipped":
        return f"Skipped {job_label} based on fit threshold or submission policy."
    if stage == "tailor":
        return f"Prepared tailored resume context for {job_label}{f' ({strategy})' if strategy else ''}."
    if stage == "form":
        return f"Prepared verified form data for {job_label}."
    if stage == "submit" and status == "running":
        return f"Submitting application for {job_label}."
    if stage == "track" and status == "submitted":
        return f"Submission completed for {job_label}."
    if status == "failed":
        return f"Automation failed for {job_label}: {log.message or 'execution error'}."
    return f"{stage.title()} stage {status} for {job_label}."


def _build_heartbeat_details(log: AutonomousJobLog) -> dict[str, Any]:
    details = log.details if isinstance(log.details, dict) else {}
    return {
        "job_id": log.job_id,
        "application_id": log.application_id,
        "attempts": log.attempts,
        "message": log.message,
        "details": details,
    }


def _heartbeat_event_from_log(run: AutonomousRun, log: AutonomousJobLog) -> dict[str, Any]:
    actor, stage = HEARTBEAT_STAGE_MAP.get((log.stage or "").lower(), ("executor", "verify"))
    event_status = _normalize_heartbeat_status(run.status, (log.status or "").lower())
    artifact_ref = None
    if isinstance(log.confirmation, dict):
        artifact_ref = log.confirmation.get("artifact_ref") or log.confirmation.get("url")
    if artifact_ref is None and isinstance(log.details, dict):
        artifact_ref = log.details.get("artifact_ref") or log.details.get("apply_url")

    return {
        "actor": actor,
        "stage": stage,
        "status": event_status,
        "summary": _build_heartbeat_summary(log),
        "details": _build_heartbeat_details(log),
        "confidence": 0.88 if event_status == "completed" else 0.74 if event_status == "running" else 0.55,
        "latency_ms": None,
        "artifact_ref": artifact_ref,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


@router.post("/runs", response_model=AutonomousRunResponse)
async def start_autonomous_run(
    request: AutonomousRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if not request.job_ids:
        raise HTTPException(status_code=400, detail="No job_ids provided")
    if len(request.job_ids) > 100:
        raise HTTPException(status_code=400, detail="Too many jobs in one run (max 100).")

    active_run = (
        db.query(AutonomousRun)
        .filter(AutonomousRun.status.in_(["queued", "running"]))
        .order_by(AutonomousRun.id.desc())
        .first()
    )
    if active_run:
        raise HTTPException(
            status_code=409,
            detail=f"Autonomous run #{active_run.id} is already {active_run.status}. Stop it before starting a new run.",
        )

    min_score = request.min_score if request.min_score is not None else float(settings.auto_apply_min_score)
    max_retries = max(0, min(int(request.max_retries), 5))

    selected_resume = None
    if request.resume_id:
        selected_resume = db.query(Resume).filter(Resume.id == request.resume_id).first()
        if not selected_resume:
            raise HTTPException(status_code=404, detail="Selected resume not found")
    else:
        selected_resume = db.query(Resume).filter(Resume.is_primary == True).first()
    if not selected_resume:
        raise HTTPException(status_code=400, detail="No resume found. Upload a resume before autonomous runs.")

    existing_job_ids = {row[0] for row in db.query(Job.id).filter(Job.id.in_(request.job_ids)).all()}
    missing_ids = [job_id for job_id in request.job_ids if job_id not in existing_job_ids]
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"Job IDs not found: {missing_ids}")

    run = AutonomousRun(
        status="queued",
        resume_id=selected_resume.id if selected_resume else request.resume_id,
        total_jobs=len(request.job_ids),
        processed_jobs=0,
        submitted_jobs=0,
        failed_jobs=0,
        skipped_jobs=0,
        min_score=int(min_score),
        safe_mode=1 if request.safe_mode else 0,
        require_confirmation=1 if request.require_confirmation else 0,
        constraints={
            "official_submission_only": True,
            "no_dummy_email_for_submission": True,
            "scrape_dummy_email_allowed": True,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    for job_id in request.job_ids:
        db.add(
            AutonomousJobLog(
                run_id=run.id,
                job_id=job_id,
                stage="queued",
                status="pending",
                attempts=0,
                details={"job_id": job_id},
            )
        )
    db.commit()

    coordinator = CoordinatorAgent()
    background_tasks.add_task(
        coordinator.run,
        run.id,
        request.job_ids,
        selected_resume.id if selected_resume else request.resume_id,
        min_score,
        request.safe_mode,
        request.require_confirmation,
        max_retries,
    )

    return AutonomousRunResponse(
        run_id=run.id,
        status=run.status,
        total_jobs=run.total_jobs,
        min_score=min_score,
    )


@router.get("/runs/{run_id}", response_model=AutonomousRunStatusResponse)
def get_autonomous_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Autonomous run not found")

    return AutonomousRunStatusResponse(
        run_id=run.id,
        status=run.status,
        total_jobs=run.total_jobs,
        processed_jobs=run.processed_jobs,
        submitted_jobs=run.submitted_jobs,
        failed_jobs=run.failed_jobs,
        skipped_jobs=run.skipped_jobs,
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        error_message=run.error_message,
    )


@router.get("/active-run")
def get_active_autonomous_run(db: Session = Depends(get_db)):
    run = (
        db.query(AutonomousRun)
        .filter(AutonomousRun.status.in_(["queued", "running"]))
        .order_by(AutonomousRun.id.desc())
        .first()
    )
    if not run:
        return {"run_id": None, "status": "idle"}
    return {
        "run_id": run.id,
        "status": run.status,
        "total_jobs": run.total_jobs,
        "processed_jobs": run.processed_jobs,
        "submitted_jobs": run.submitted_jobs,
        "failed_jobs": run.failed_jobs,
        "skipped_jobs": run.skipped_jobs,
    }


@router.get("/runs/{run_id}/logs")
def get_autonomous_logs(run_id: int, db: Session = Depends(get_db)):
    run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Autonomous run not found")

    logs = (
        db.query(AutonomousJobLog)
        .filter(AutonomousJobLog.run_id == run_id)
        .order_by(AutonomousJobLog.id.asc())
        .all()
    )

    return [
        {
            "id": row.id,
            "job_id": row.job_id,
            "application_id": row.application_id,
            "stage": row.stage,
            "status": row.status,
            "attempts": row.attempts,
            "resume_version_id": row.resume_version_id,
            "details": row.details,
            "confirmation": row.confirmation,
            "message": row.message,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in logs
    ]


@router.get("/heartbeat")
def get_autonomous_heartbeat(
    run_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    if run_id is not None:
        run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
    else:
        run = (
            db.query(AutonomousRun)
            .order_by(AutonomousRun.id.desc())
            .first()
        )

    if not run:
        return {
            "run_id": None,
            "status": "idle",
            "summary": "No autonomous run has been started yet.",
            "progress": None,
            "events": [],
        }

    logs = (
        db.query(AutonomousJobLog)
        .filter(AutonomousJobLog.run_id == run.id)
        .order_by(AutonomousJobLog.id.desc())
        .limit(limit)
        .all()
    )
    events = [_heartbeat_event_from_log(run, row) for row in logs]

    return {
        "run_id": run.id,
        "status": run.status,
        "summary": f"Run #{run.id} is {run.status} across {run.total_jobs} jobs.",
        "progress": {
            "total_jobs": run.total_jobs,
            "processed_jobs": run.processed_jobs,
            "submitted_jobs": run.submitted_jobs,
            "failed_jobs": run.failed_jobs,
            "skipped_jobs": run.skipped_jobs,
        },
        "events": events,
    }


@router.get("/thoughts/stream")
async def stream_thoughts(db: Session = Depends(get_db)):
    """SSE: stream CareerAgent's live reasoning about the current job search state."""
    from job_search.services.llm_client import get_llm_client
    from job_search.models import Job, Application, ApplicationStatus

    total_jobs = db.query(Job).count()
    total_applied = (
        db.query(Application)
        .filter(Application.status == ApplicationStatus.SUBMITTED)
        .count()
    )
    recent_run = db.query(AutonomousRun).order_by(AutonomousRun.id.desc()).first()
    run_context = (
        f"Most recent autonomous run: #{recent_run.id}, status={recent_run.status}, "
        f"{recent_run.submitted_jobs}/{recent_run.total_jobs} submitted."
        if recent_run
        else "No autonomous run started yet."
    )

    system = (
        "You are CareerAgent's internal reasoning system. Think step by step about the current "
        "job search state and what the agent should prioritize next. "
        "Be direct, specific, and terse. Short sentences. "
        "Think like an execution system — not a chatbot."
    )
    prompt = (
        f"State: {total_jobs} jobs in DB, {total_applied} applications submitted. "
        f"{run_context}\n"
        "Reason through the job search funnel: sourcing → scoring → tailoring → submission → follow-up. "
        "What is the highest-leverage next action and why?"
    )

    llm = get_llm_client()

    async def generate():
        yield f"data: {json.dumps({'type': 'start'})}\n\n"
        if llm is None:
            yield (
                f"data: {json.dumps({'type': 'token', 'text': 'No LLM configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OLLAMA_BASE_URL.'})}\n\n"
            )
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        try:
            async for token in llm.stream(prompt, system=system, max_tokens=600):
                yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
        except Exception as exc:
            logger.error("Live thoughts stream error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
        yield f"data: {json.dumps({'type': 'end'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/stop", response_model=AutonomousStopResponse)
def stop_autonomous_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(AutonomousRun).filter(AutonomousRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Autonomous run not found")
    if run.status in {"completed", "failed", "stopped"}:
        return AutonomousStopResponse(run_id=run.id, status=run.status)

    now_iso = datetime.now().isoformat()
    # Request stop on any currently running app under this run so browser loops terminate quickly.
    app_ids = [
        row[0]
        for row in (
            db.query(AutonomousJobLog.application_id)
            .filter(
                AutonomousJobLog.run_id == run.id,
                AutonomousJobLog.application_id.isnot(None),
            )
            .all()
        )
    ]
    if app_ids:
        apps = (
            db.query(Application)
            .filter(
                Application.id.in_(app_ids),
                Application.status.in_([ApplicationStatus.QUEUED, ApplicationStatus.IN_PROGRESS]),
            )
            .all()
        )
        for app in apps:
            user_inputs = dict(app.user_inputs or {})
            user_inputs["__stop_requested"] = True
            user_inputs["__stop_requested_at"] = now_iso
            user_inputs["__stop_reason"] = f"Autonomous run #{run.id} stopped from UI"
            app.user_inputs = user_inputs
            app.notes = "Automation stop requested from autonomous run control."
            app.status = ApplicationStatus.REVIEWED
            app.error_message = None
            app.automation_log = (app.automation_log or "") + "Stop requested from autonomous run control.\n"

    run.status = "stopped"
    run.finished_at = datetime.now()
    db.commit()
    return AutonomousStopResponse(run_id=run.id, status=run.status)
