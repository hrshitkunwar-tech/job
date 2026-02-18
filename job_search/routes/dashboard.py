from datetime import datetime
from typing import Optional
from sqlalchemy import or_
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from job_search.app import templates
from job_search.database import get_db
from job_search.models import Job, Application, ApplicationStatus, Resume, SearchQuery, UserProfile

router = APIRouter()


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
    show_all: bool = False,
    db: Session = Depends(get_db)
):
    # Default to latest search if no filters provided
    if not q and not search_id and not scraped_after and not show_all:
        latest_search = db.query(SearchQuery).order_by(SearchQuery.id.desc()).first()
        if latest_search:
            search_id = latest_search.id

    query = db.query(Job).filter(Job.is_archived == False)

    if search_id:
        query = query.filter(Job.search_query_id == search_id)

    if q:
        search_term = f"%{q}%"
        query = query.filter(or_(Job.title.ilike(search_term), Job.company.ilike(search_term)))

    if scraped_after:
        try:
            # Handle potentially URL-encoded or timezone-aware ISO strings
            dt = datetime.fromisoformat(scraped_after.replace("Z", "+00:00"))
            query = query.filter(Job.scraped_at >= dt)
        except Exception:
            pass

    jobs = query.order_by(Job.match_score.desc().nullslast()).all()
    
    # Get all search runs for the sidebar/dropdown
    search_runs = db.query(SearchQuery).order_by(SearchQuery.created_at.desc()).all()
    
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
        "q": q or "",
        "search_id": search_id,
        "search_runs": search_runs
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
    # Eager load jobs for display
    for app in apps:
        _ = app.job
    return templates.TemplateResponse("applications.html", {
        "request": request,
        "applications": apps,
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
