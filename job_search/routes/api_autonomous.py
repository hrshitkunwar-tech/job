from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
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
