from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import Job
from job_search.schemas.job import JobResponse, JobListResponse, JobScoreRequest

router = APIRouter()


@router.get("", response_model=JobListResponse)
def list_jobs(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    min_score: float = Query(0, ge=0),
    work_type: Optional[str] = None,
    is_archived: bool = False,
    sort: str = "match_score",
    db: Session = Depends(get_db),
):
    query = db.query(Job).filter(Job.is_archived == is_archived)

    if min_score > 0:
        query = query.filter(Job.match_score >= min_score)
    if work_type:
        query = query.filter(Job.work_type == work_type)

    total = query.count()

    if sort == "match_score":
        query = query.order_by(Job.match_score.desc().nullslast())
    elif sort == "posted_date":
        query = query.order_by(Job.posted_date.desc().nullslast())
    elif sort == "scraped_at":
        query = query.order_by(Job.scraped_at.desc())
    else:
        query = query.order_by(Job.id.desc())

    jobs = query.offset((page - 1) * per_page).limit(per_page).all()

    return JobListResponse(jobs=jobs, total=total, page=page, per_page=per_page)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/score")
async def score_job(job_id: int, request: JobScoreRequest, db: Session = Depends(get_db)):
    """Score a job against the user profile."""
    from job_search.models import UserProfile
    from job_search.services.job_matcher import JobMatcher
    from job_search.routes.api_resumes import _get_llm_client

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    profile = db.query(UserProfile).first()
    if not profile:
        raise HTTPException(status_code=400, detail="No profile found. Please set up your profile first.")

    profile_dict = {
        "skills": profile.skills or [],
        "experience": profile.experience or [],
        "target_roles": profile.target_roles or [],
        "target_locations": profile.target_locations or [],
    }

    job_dict = {
        "title": job.title,
        "description": job.description,
        "location": job.location,
        "work_type": job.work_type,
    }

    llm_client = _get_llm_client() if request.deep else None
    matcher = JobMatcher(llm_client=llm_client)

    if request.deep and llm_client:
        result = await matcher.score_job_deep(job_dict, profile_dict)
    else:
        result = matcher.score_job(job_dict, profile_dict)

    # Update job with scores
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

    return {
        "job_id": job.id,
        "match_score": result.overall_score,
        "details": job.match_details,
    }


@router.post("/rescore-all")
def rescore_all_jobs(db: Session = Depends(get_db)):
    """Re-score all non-archived jobs against the current profile."""
    from job_search.models import UserProfile
    from job_search.services.job_matcher import JobMatcher

    profile = db.query(UserProfile).first()
    if not profile:
        raise HTTPException(status_code=400, detail="No profile found.")

    profile_dict = {
        "skills": profile.skills or [],
        "experience": profile.experience or [],
        "target_roles": profile.target_roles or [],
        "target_locations": profile.target_locations or [],
    }

    matcher = JobMatcher()
    jobs = db.query(Job).filter(Job.is_archived == False).all()
    updated = 0

    for job in jobs:
        job_dict = {
            "title": job.title,
            "description": job.description or "",
            "location": job.location or "",
            "work_type": job.work_type or "",
        }
        result = matcher.score_job(job_dict, profile_dict)
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
        updated += 1

    db.commit()
    return {"message": f"Re-scored {updated} jobs", "updated": updated}


@router.post("/{job_id}/archive")
def archive_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.is_archived = True
    db.commit()
    return {"message": "Job archived", "job_id": job.id}
