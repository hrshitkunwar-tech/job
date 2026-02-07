from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from job_search.database import get_db
from job_search.models import Application, ApplicationStatus, Job
from job_search.schemas.application import (
    ApplicationCreate,
    ApplicationUpdate,
    ApplicationResponse,
    ApplicationStatsResponse,
    BatchApplyRequest,
)

router = APIRouter()


@router.get("", response_model=list[ApplicationResponse])
def list_applications(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Application)
    if status:
        query = query.filter(Application.status == status)
    query = query.order_by(Application.created_at.desc())
    return query.offset((page - 1) * per_page).limit(per_page).all()


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
        application.status = request.status
    if request.notes is not None:
        application.notes = request.notes

    db.commit()
    db.refresh(application)
    return application


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


@router.post("/batch-apply")
def batch_apply(request: BatchApplyRequest, db: Session = Depends(get_db)):
    """Queue multiple jobs for application."""
    created = []
    skipped = []
    for job_id in request.job_ids:
        existing = db.query(Application).filter(Application.job_id == job_id).first()
        if existing:
            skipped.append(job_id)
            continue
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            skipped.append(job_id)
            continue
        app = Application(job_id=job_id, status=ApplicationStatus.QUEUED)
        db.add(app)
        created.append(job_id)

    db.commit()
    return {"created": len(created), "skipped": len(skipped), "job_ids": created}
