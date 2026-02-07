from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import SearchQuery
from job_search.schemas.search import SearchQueryCreate, SearchQueryResponse, SearchRunRequest

from datetime import datetime
import uuid
import json

router = APIRouter()

# Global dictionary to track running searches
active_searches = {}


@router.get("/queries", response_model=list[SearchQueryResponse])
def list_queries(db: Session = Depends(get_db)):
    return db.query(SearchQuery).order_by(SearchQuery.created_at.desc()).all()


@router.post("/queries", response_model=SearchQueryResponse)
def create_query(request: SearchQueryCreate, db: Session = Depends(get_db)):
    query = SearchQuery(**request.model_dump())
    db.add(query)
    db.commit()
    db.refresh(query)
    return query


@router.get("/queries/{query_id}", response_model=SearchQueryResponse)
def get_query(query_id: int, db: Session = Depends(get_db)):
    query = db.query(SearchQuery).filter(SearchQuery.id == query_id).first()
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
    return query


@router.delete("/queries/{query_id}")
def delete_query(query_id: int, db: Session = Depends(get_db)):
    query = db.query(SearchQuery).filter(SearchQuery.id == query_id).first()
    if not query:
        raise HTTPException(status_code=404, detail="Search query not found")
    db.delete(query)
    db.commit()
    return {"message": "Deleted", "id": query_id}


@router.post("/stop/{search_id}")
async def stop_search(search_id: str):
    """Stop a running search."""
    if search_id in active_searches:
        # Set cancellation flag
        active_searches[search_id]["cancelled"] = True
        return {"message": "Search stop requested", "search_id": search_id}
    else:
        raise HTTPException(status_code=404, detail="Search not found or already completed")

@router.post("/run")
async def run_search(request: SearchRunRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Trigger a LinkedIn job search. Runs in the background."""
    search_id = str(uuid.uuid4())
    
    # Track this search in memory for cancellation
    active_searches[search_id] = {
        "cancelled": False,
        "started_at": datetime.now()
    }

    # Create a persistent SearchQuery record to link jobs to this specific run
    try:
        db_query = SearchQuery(
            name=f"Run: {request.keywords}"[:50],
            keywords=request.keywords,
            locations=json.dumps(request.locations) if request.locations else None,
            work_types=json.dumps(request.work_types) if request.work_types else None,
            experience_levels=json.dumps(request.experience_levels) if request.experience_levels else None,
            date_posted=request.date_posted,
            easy_apply_only=request.easy_apply_only,
            results_count=0
        )
        db.add(db_query)
        db.commit()
        db.refresh(db_query)
        db_search_id = db_query.id
    except Exception as e:
        # Fallback if DB save fails, though unlikely
        print(f"Failed to save search query: {e}")
        db_search_id = None
    
    background_tasks.add_task(_run_search_task, request.model_dump(), search_id, db_search_id)
    return {
        "status": "started",
        "message": "Search task queued. Jobs will appear as they are found.",
        "params": request.model_dump(),
        "search_id": search_id,
        "db_search_id": db_search_id
    }


async def _run_search_task(params: dict, search_id: str, db_search_id: int = None):
    """Background task to run LinkedIn scraping."""
    import logging
    from job_search.database import SessionLocal
    from job_search.services.scraper import LinkedInScraper
    from job_search.services.job_matcher import JobMatcher
    from job_search.services.llm_client import get_llm_client
    from job_search.models import Job, UserProfile

    logger = logging.getLogger(__name__)
    logger.info(f"Search task started with params: {params}")

    db = SessionLocal()
    try:
        # Check if cancelled before starting
        if active_searches.get(search_id, {}).get("cancelled"):
            logger.info(f"Search {search_id} was cancelled before starting")
            return

        # Load profile for matching
        profile_obj = db.query(UserProfile).first()
        if not profile_obj:
            logger.error("No user profile found. Cannot run search.")
            return

        profile_data = {
            "skills": profile_obj.skills or [],
            "target_roles": profile_obj.target_roles or [],
            "target_locations": profile_obj.target_locations or [],
            "experience": profile_obj.experience or []
        }

        # Setup services
        scraper = LinkedInScraper()
        llm = get_llm_client()
        matcher = JobMatcher(llm_client=llm)

        # Handle multiple locations, work types, and experience levels
        locations_list = params.get("locations") or [params.get("location", "")]
        work_types_list = params.get("work_types") or [params.get("work_type")]
        experience_levels_list = params.get("experience_levels") or [params.get("experience_level")]
        
        # If any are None, convert to empty list
        if not locations_list or locations_list == [None]:
            locations_list = [""]
        if not work_types_list or work_types_list == [None]:
            work_types_list = [None]
        if not experience_levels_list or experience_levels_list == [None]:
            experience_levels_list = [None]

        total_jobs_found = 0
        limit_per_search = params.get("limit", 50)

        # Run searches for each location, accumulating results
        all_jobs = []
        for location in locations_list:
            if total_jobs_found >= limit_per_search:
                break
                
            # Check if cancelled during iteration
            if active_searches.get(search_id, {}).get("cancelled"):
                logger.info(f"Search {search_id} cancelled during execution")
                break

            logger.info(f"Searching for '{params.get('keywords')}' in '{location}'")

            # Run scrape
            found_jobs = await scraper.scrape_jobs(
                query=params.get("keywords"),
                location=location or "",
                limit=min(limit_per_search - total_jobs_found, limit_per_search)
            )
            all_jobs.extend(found_jobs)
            total_jobs_found += len(found_jobs)
            logger.info(f"Completed search for location '{location}'. Total jobs found so far: {total_jobs_found}")

        for job_data in all_jobs:
            try:
                if not job_data.get("title"):
                    continue

                # Check if exists
                existing = db.query(Job).filter(Job.external_id == job_data.get("external_id")).first()
                if existing:
                    # Update the search_query_id to the current one so it appears in this search's results
                    if db_search_id:
                        existing.search_query_id = db_search_id
                        db.commit()
                    continue

                # Score job
                match_result = matcher.score_job(job_data, profile_data)

                # Save to DB
                job = Job(
                    external_id=job_data.get("external_id"),
                    source=job_data.get("source", "linkedin"),
                    title=job_data.get("title"),
                    company=job_data.get("company", "Unknown"),
                    location=job_data.get("location", ""),
                    work_type=job_data.get("work_type", "onsite"),
                    description=job_data.get("description", ""),
                    description_html=job_data.get("description_html", ""),
                    url=job_data.get("url", ""),
                    match_score=match_result.overall_score,
                    match_details={
                        "skill_score": match_result.skill_score,
                        "title_score": match_result.title_score,
                        "explanation": match_result.explanation,
                        "matched_skills": match_result.matched_skills,
                        "missing_skills": match_result.missing_skills
                    },
                    search_query_id=db_search_id
                )
                db.add(job)
                db.commit()
                logger.info(f"Saved job: {job.title} at {job.company} (Score: {job.match_score})")
            except Exception as inner_e:
                logger.error(f"Failed to process individual job: {inner_e}")
                db.rollback()
                continue

    except Exception as e:
        logger.exception(f"Search task failed: {e}")
    finally:
        if search_id in active_searches:
            del active_searches[search_id]
        db.close()

@router.get("/active")
def get_active_search():
    if not active_searches:
        return {"active": False}
    search_id, data = next(iter(active_searches.items()))
    return {
        "active": True,
        "search_id": search_id,
        "db_search_id": data.get("db_search_id"),
        "keywords": data.get("params", {}).get("keywords") or data.get("keywords")
    }
