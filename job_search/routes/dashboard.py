import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional
from sqlalchemy import or_, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from job_search.app import templates
from job_search.database import get_db, init_db
from job_search.models import Job, Application, ApplicationStatus, Resume, SearchQuery, UserProfile
from job_search.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _normalize_application_status(raw_status) -> str:
    if raw_status is None:
        return "unknown"
    if hasattr(raw_status, "value"):
        try:
            return str(raw_status.value).strip().lower()
        except Exception:
            pass
    text_status = str(raw_status).strip()
    if text_status in ApplicationStatus.__members__:
        return ApplicationStatus[text_status].value
    lowered = text_status.lower().replace("-", "_").replace(" ", "_")
    if "." in lowered:
        lowered = lowered.split(".")[-1]
    for member in ApplicationStatus:
        if lowered in {member.value, member.name.lower()}:
            return member.value
    return lowered or "unknown"


def _search_time_bounds(search_time: Optional[str], now: Optional[datetime] = None) -> tuple[Optional[datetime], Optional[datetime]]:
    value = (search_time or "").strip().lower()
    if not value:
        return None, None
    current = now or datetime.now()
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "today":
        return today_start, None
    if value == "yesterday":
        yesterday_start = today_start - timedelta(days=1)
        return yesterday_start, today_start
    if value == "last_week":
        return current - timedelta(days=7), None
    return None, None


@router.get("/")
def index():
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    total_jobs = db.query(Job).filter(Job.is_archived == False).count()
    total_applied = db.query(Application).filter(
        Application.status == ApplicationStatus.SUBMITTED
    ).count()
    total_interviews = db.query(Application).filter(
        Application.status == ApplicationStatus.INTERVIEW
    ).count()
    total_apps = db.query(Application).count()
    success_rate = round((total_interviews / total_apps * 100) if total_apps > 0 else 0, 1)

    recent_jobs = (
        db.query(Job)
        .filter(Job.is_archived == False)
        .order_by(Job.match_score.desc().nullslast())
        .limit(5)
        .all()
    )
    recent_apps = (
        db.query(Application)
        .order_by(Application.created_at.desc())
        .limit(5)
        .all()
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "total_jobs": total_jobs,
        "total_applied": total_applied,
        "total_interviews": total_interviews,
        "success_rate": success_rate,
        "recent_jobs": recent_jobs,
        "recent_apps": recent_apps,
    })


@router.get("/jobs")
def jobs_page(
    request: Request, 
    q: Optional[str] = None, 
    scraped_after: Optional[str] = None,
    search_id: Optional[int] = None,
    app_status: Optional[str] = None,
    work_type: Optional[str] = None,
    search_time: Optional[str] = None,
    show_all: bool = False,
    db: Session = Depends(get_db)
):
    def _run_with_migration_retry(loader, default):
        try:
            return loader()
        except OperationalError as exc:
            logger.warning("Jobs page query failed with OperationalError; running init_db() and retrying: %s", exc)
            try:
                init_db()
                return loader()
            except Exception as retry_exc:
                logger.exception("Jobs page retry still failed after init_db(): %s", retry_exc)
                return default
        except SQLAlchemyError as exc:
            logger.exception("Jobs page SQLAlchemyError: %s", exc)
            return default
        except Exception as exc:
            logger.exception("Jobs page unexpected error: %s", exc)
            return default

    def _normalize_status(raw_status) -> str:
        if raw_status is None:
            return "unapplied"
        if hasattr(raw_status, "value"):
            try:
                return str(raw_status.value).strip().lower()
            except Exception:
                pass
        text_status = str(raw_status).strip()
        if text_status in ApplicationStatus.__members__:
            return ApplicationStatus[text_status].value
        lowered = text_status.lower().replace("-", "_").replace(" ", "_")
        if lowered in {s.value for s in ApplicationStatus}:
            return lowered
        return lowered or "unapplied"

    def _status_from_app(app_obj) -> str:
        if not app_obj:
            return "unapplied"
        status_attr = getattr(app_obj, "status", None)
        return _normalize_status(status_attr)

    def _app_stub(job_id: int, app_id: int, status_value: str, created_at, notes, error_message):
        return SimpleNamespace(
            id=app_id,
            job_id=job_id,
            status=SimpleNamespace(value=status_value),
            created_at=created_at,
            notes=notes,
            error_message=error_message,
        )

    # Default to latest search if no filters provided
    if not q and not search_id and not scraped_after and not show_all and not app_status and not work_type and not search_time:
        latest_search = _run_with_migration_retry(
            lambda: db.query(SearchQuery).order_by(SearchQuery.id.desc()).first(),
            None,
        )
        if latest_search:
            search_id = latest_search.id

    query = db.query(Job).filter(Job.is_archived == False)

    if search_id:
        query = query.filter(Job.search_query_id == search_id)

    if q:
        search_term = f"%{q}%"
        query = query.filter(or_(Job.title.ilike(search_term), Job.company.ilike(search_term), Job.location.ilike(search_term)))

    if work_type:
        work_type_value = work_type.strip().lower()
        if work_type_value:
            query = query.filter(sa_func.lower(Job.work_type) == work_type_value)

    if search_time:
        start_dt, end_dt = _search_time_bounds(search_time)
        if start_dt:
            query = query.filter(Job.scraped_at >= start_dt)
        if end_dt:
            query = query.filter(Job.scraped_at < end_dt)

    if scraped_after:
        try:
            # Handle potentially URL-encoded or timezone-aware ISO strings
            dt = datetime.fromisoformat(scraped_after.replace("Z", "+00:00"))
            query = query.filter(Job.scraped_at >= dt)
        except Exception:
            pass

    jobs = _run_with_migration_retry(
        lambda: query.order_by(Job.match_score.desc().nullslast()).all(),
        [],
    )

    # Attach application statuses so jobs can be filtered/handled separately in UI.
    job_ids = [j.id for j in jobs]
    app_by_job: dict[int, object] = {}
    if job_ids:
        try:
            app_rows = db.query(Application).filter(Application.job_id.in_(job_ids)).all()
        except OperationalError:
            # Handle stale sqlite schema when new app columns were added but startup migration
            # has not run in the current process yet.
            init_db()
            app_rows = db.query(Application).filter(Application.job_id.in_(job_ids)).all()
        except Exception as exc:
            logger.warning("Application ORM load failed on /jobs; falling back to raw query: %s", exc)
            placeholders = ", ".join([str(int(job_id)) for job_id in job_ids]) or "0"
            rows = _run_with_migration_retry(
                lambda: db.execute(
                    text(
                        "SELECT id, job_id, status, created_at, notes, error_message "
                        f"FROM applications WHERE job_id IN ({placeholders})"
                    )
                ).fetchall(),
                [],
            )
            app_rows = [
                _app_stub(
                    int(row.job_id),
                    int(row.id),
                    _normalize_status(getattr(row, "status", None)),
                    getattr(row, "created_at", None),
                    getattr(row, "notes", None),
                    getattr(row, "error_message", None),
                )
                for row in rows
            ]
        # Keep only the most recent application per job so filters/counters reflect current pipeline state.
        app_by_job = {}
        app_rows_sorted = sorted(
            app_rows,
            key=lambda a: (
                getattr(a, "job_id", 0),
                getattr(a, "status_updated_at", None) or getattr(a, "created_at", None) or datetime.min,
                getattr(a, "id", 0),
            ),
        )
        for app_row in app_rows_sorted:
            app_by_job[getattr(app_row, "job_id")] = app_row

    jobs_for_counts = list(jobs)

    if app_status:
        status_l = app_status.lower().strip()
        filtered_jobs = []
        for job in jobs:
            a = app_by_job.get(job.id)
            a_status = _status_from_app(a)
            if status_l == "unapplied":
                if not a:
                    filtered_jobs.append(job)
            elif status_l in {"submitted", "queued", "in_progress", "reviewed", "failed", "interview", "rejected", "offer", "withdrawn"}:
                if a_status == status_l:
                    filtered_jobs.append(job)
            elif status_l == "active_pipeline":
                if a_status in {"queued", "in_progress"}:
                    filtered_jobs.append(job)
            else:
                filtered_jobs.append(job)
        jobs = filtered_jobs

    status_order = {
        "queued": 0,
        "in_progress": 1,
        "reviewed": 2,
        "failed": 3,
        "unapplied": 4,
        "submitted": 5,
        "interview": 6,
        "offer": 7,
        "rejected": 8,
        "withdrawn": 9,
        "other": 10,
    }
    jobs = sorted(
        jobs,
        key=lambda j: (
            status_order.get(_status_from_app(app_by_job.get(j.id)), status_order["other"]),
            -(j.match_score or 0),
            -j.id,
        ),
    )

    status_labels = {
        "queued": "Queued",
        "in_progress": "In Progress",
        "reviewed": "Reviewed",
        "failed": "Failed",
        "unapplied": "Unapplied",
        "submitted": "Submitted",
        "interview": "Interview",
        "offer": "Offer",
        "rejected": "Rejected",
        "withdrawn": "Withdrawn",
        "other": "Other",
    }
    grouped_jobs_map: dict[str, list[Job]] = {k: [] for k in status_order.keys()}
    for job in jobs:
        job_status = _status_from_app(app_by_job.get(job.id))
        grouped_jobs_map.setdefault(job_status, []).append(job)
    grouped_jobs = [
        {
            "status": status_key,
            "label": status_labels.get(status_key, status_key.replace("_", " ").title()),
            "jobs": grouped_jobs_map.get(status_key, []),
            "count": len(grouped_jobs_map.get(status_key, [])),
        }
        for status_key in status_order.keys()
    ]

    app_status_counts = {
        "unapplied": 0,
        "queued": 0,
        "in_progress": 0,
        "submitted": 0,
        "reviewed": 0,
        "failed": 0,
        "other": 0,
    }
    for job in jobs_for_counts:
        a = app_by_job.get(job.id)
        if not a:
            app_status_counts["unapplied"] += 1
            continue
        s = _status_from_app(a)
        if s in app_status_counts:
            app_status_counts[s] += 1
        else:
            app_status_counts["other"] += 1
    
    # Get all search runs for the sidebar/dropdown
    search_runs = _run_with_migration_retry(
        lambda: db.query(SearchQuery).order_by(SearchQuery.created_at.desc()).all(),
        [],
    )
    resumes = _run_with_migration_retry(
        lambda: db.query(Resume).order_by(Resume.is_primary.desc(), Resume.created_at.desc()).all(),
        [],
    )
    
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
        "q": q or "",
        "search_id": search_id,
        "app_status": app_status or "",
        "work_type": (work_type or "").lower(),
        "search_time": search_time or "",
        "search_runs": search_runs,
        "resumes": resumes,
        "auto_apply_min_score": settings.auto_apply_min_score,
        "application_by_job": app_by_job,
        "app_status_counts": app_status_counts,
        "grouped_jobs": grouped_jobs,
    })


@router.get("/jobs/{job_id}")
def job_detail_page(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return RedirectResponse(url="/jobs")
    application = db.query(Application).filter(Application.job_id == job_id).first()
    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "job": job,
        "application": application,
    })


@router.get("/applications")
def applications_page(request: Request, db: Session = Depends(get_db)):
    apps = (
        db.query(Application)
        .order_by(Application.created_at.desc())
        .all()
    )
    status_counts = {
        "queued": 0,
        "in_progress": 0,
        "submitted": 0,
        "reviewed": 0,
        "failed": 0,
        "interview": 0,
        "rejected": 0,
        "offer": 0,
        "withdrawn": 0,
        "other": 0,
    }
    # Eager load jobs for display
    for app in apps:
        _ = app.job
        status_norm = _normalize_application_status(getattr(app, "status", None))
        setattr(app, "status_normalized", status_norm)
        if status_norm in status_counts:
            status_counts[status_norm] += 1
        else:
            status_counts["other"] += 1
    return templates.TemplateResponse("applications.html", {
        "request": request,
        "applications": apps,
        "status_counts": status_counts,
    })


@router.get("/resumes")
def resumes_page(request: Request, db: Session = Depends(get_db)):
    resumes = db.query(Resume).order_by(Resume.created_at.desc()).all()
    return templates.TemplateResponse("resumes.html", {
        "request": request,
        "resumes": resumes,
    })


@router.get("/profile")
def profile_page(request: Request, db: Session = Depends(get_db)):
    profile = db.query(UserProfile).first()
    return templates.TemplateResponse("profile.html", {
        "request": request,
        "profile": profile,
    })


@router.get("/search")
def search_page(request: Request, db: Session = Depends(get_db)):
    queries = db.query(SearchQuery).order_by(SearchQuery.created_at.desc()).all()
    
    # Separate genuine saved searches from run logs
    saved_searches = [q for q in queries if not q.name.startswith("Run:")]
    run_history = [q for q in queries if q.name.startswith("Run:")]
    
    return templates.TemplateResponse("search.html", {
        "request": request,
        "queries": saved_searches, # Default loop uses saved_searches logic
        "run_history": run_history,
        "all_queries": queries
    })
