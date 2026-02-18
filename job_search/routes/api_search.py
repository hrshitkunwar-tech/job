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

@router.post("/stop-all")
async def stop_all_searches():
    """Cancel all active searches."""
    count = 0
    for search_id in list(active_searches.keys()):
        active_searches[search_id]["cancelled"] = True
        count += 1
    return {"message": f"Requested stop for {count} active searches. Progress indicators should clear shortly."}

@router.post("/run")
async def run_search(request: SearchRunRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Trigger a LinkedIn job search. Runs in the background."""
    search_id = str(uuid.uuid4())
    
    keywords_str = ", ".join(request.keywords) if isinstance(request.keywords, list) else request.keywords

    # Create a persistent SearchQuery record to link jobs to this specific run
    try:
        db_query = SearchQuery(
            name=f"Run: {keywords_str}"[:50],
            keywords=keywords_str,
            locations=json.dumps(request.locations) if request.locations else None,
            work_types=json.dumps(request.work_types) if request.work_types else None,
            experience_levels=json.dumps(request.experience_levels) if request.experience_levels else None,
            portals=json.dumps(request.portals) if request.portals else None,
            custom_portal_urls=json.dumps(request.custom_portal_urls) if request.custom_portal_urls else None,
            date_posted=request.date_posted,
            easy_apply_only=request.easy_apply_only,
            results_count=0
        )
        db.add(db_query)
        db.commit()
        db.refresh(db_query)
        db_search_id = db_query.id
    except Exception as e:
        print(f"Failed to save search query: {e}")
        db_search_id = None

    # Track this search in memory with full context
    active_searches[search_id] = {
        "cancelled": False,
        "started_at": datetime.now(),
        "db_search_id": db_search_id,
        "params": request.model_dump()
    }
    
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
    from job_search.services.scraper import LinkedInScraper, GeneralWebScraper
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

        # Get latest user profile for matching
        profile_obj = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
        if not profile_obj:
            logger.error("No user profile found for scoring")
            return

        profile_data = {
            "skills": profile_obj.skills or [],
            "target_roles": profile_obj.target_roles or [],
            "target_locations": profile_obj.target_locations or [],
            "experience": profile_obj.experience or []
        }

        # Setup services
        scraper = LinkedInScraper()
        custom_scraper = GeneralWebScraper()
        llm = get_llm_client()
        matcher = JobMatcher(llm_client=llm)

        # Handle multiple roles (keywords)
        raw_keywords = params.get("keywords")
        keywords_list = raw_keywords if isinstance(raw_keywords, list) else [raw_keywords]
        
        # Handle portals
        portals_list = params.get("portals") or ["linkedin"]
        
        locations_list = params.get("locations") or [params.get("location", "")]
        work_types_list = params.get("work_types") or [params.get("work_type")]
        
        total_jobs_found = 0
        limit_per_search = params.get("limit", 5)

        # Loop through Portals -> Roles -> Locations
        for portal in portals_list:
            if portal in ["career_site", "web_url"]:
                custom_urls = params.get("custom_portal_urls") or []
                if not custom_urls:
                    logger.warning(f"Portal '{portal}' selected but no custom URLs provided.")
                    continue
                
                for custom_url in custom_urls:
                    if total_jobs_found >= limit_per_search: break
                    
                    logger.info(f"[{portal.upper()}] Scraping from: {custom_url}")
                    found_jobs = await custom_scraper.scrape_custom_url(
                        url=custom_url, 
                        keywords=keywords_list, 
                        locations=locations_list,
                        limit=limit_per_search - total_jobs_found
                    )
                    
                    # Process and save
                    for job_data in found_jobs:
                        try:
                            # Score job
                            match_result = matcher.score_job(job_data, profile_data)

                            job = Job(
                                external_id=job_data.get("external_id"),
                                source=job_data.get("source", portal),
                                title=job_data.get("title"),
                                company=job_data.get("company", "Unknown"),
                                location=job_data.get("location", ""),
                                work_type=job_data.get("work_type", "onsite"),
                                is_easy_apply=job_data.get("is_easy_apply", False),
                                apply_url=job_data.get("apply_url"),
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
                            total_jobs_found += 1
                        except Exception as inner_e:
                            logger.error(f"Failed to prepare custom job: {inner_e}")
                    
                    db.commit()
                continue

            if portal != "linkedin":
                logger.warning(f"Portal '{portal}' is not yet fully implemented for scraping. Skipping.")
                continue

            for keyword in keywords_list:
                for location in locations_list:
                    if total_jobs_found >= limit_per_search:
                        break
                        
                    # Check if cancelled
                    if active_searches.get(search_id, {}).get("cancelled"):
                        logger.info(f"Search {search_id} cancelled")
                        return

                    logger.info(f"[{portal.upper()}] Scraping '{keyword}' in '{location}'")

                    # Run scrape (only LinkedIn currently supported)
                    found_jobs = await scraper.scrape_jobs(
                        query=keyword,
                        location=location or "",
                        limit=limit_per_search - total_jobs_found,
                        filters={
                            "date_posted": params.get("date_posted"),
                            "work_types": params.get("work_types"),
                            "experience_levels": params.get("experience_levels")
                        },
                        check_cancelled=lambda: active_searches.get(search_id, {}).get("cancelled", False)
                    )

                    # Process and save this batch
                    for job_data in found_jobs:
                        try:
                            if not job_data.get("title"):
                                continue

                            # Check if exists
                            existing = db.query(Job).filter(Job.external_id == job_data.get("external_id")).first()
                            if existing:
                                if db_search_id:
                                    existing.search_query_id = db_search_id
                                total_jobs_found += 1
                                continue

                            # Score job
                            match_result = matcher.score_job(job_data, profile_data)

                            # Create model
                            job = Job(
                                external_id=job_data.get("external_id"),
                                source=job_data.get("source", portal),
                                title=job_data.get("title"),
                                company=job_data.get("company", "Unknown"),
                                location=job_data.get("location", ""),
                                work_type=job_data.get("work_type", "onsite"),
                                is_easy_apply=job_data.get("is_easy_apply", False),
                                apply_url=job_data.get("apply_url"),
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
                            total_jobs_found += 1
                            
                            if total_jobs_found >= limit_per_search:
                                break
                                
                        except Exception as inner_e:
                            logger.error(f"Failed to prepare job: {inner_e}")
                            continue
                    
                    # Commit the entire batch for this location/role
                    try:
                        db.commit()
                        logger.info(f"Committed batch for {location} [{keyword}]. Session total: {total_jobs_found}")
                    except Exception as e:
                        logger.error(f"Batch commit failed: {e}")
                        db.rollback()

                    if total_jobs_found >= limit_per_search:
                        break
                if total_jobs_found >= limit_per_search:
                    break
            if total_jobs_found >= limit_per_search:
                break

    except Exception as e:
        logger.exception(f"Search task failed: {e}")
    finally:
        if search_id in active_searches:
            del active_searches[search_id]
        db.close()

@router.get("/active")
def get_active_search():
    # Only return searches that aren't cancelled
    active = {sid: data for sid, data in active_searches.items() if not data.get("cancelled")}
    if not active:
        return {"active": False}
    
    # Sort by started_at so we always track the MOST RECENT search in the UI
    sorted_active = sorted(active.items(), key=lambda x: x[1].get("started_at", datetime.min), reverse=True)
    search_id, data = sorted_active[0]
    
    return {
        "active": True,
        "search_id": search_id,
        "db_search_id": data.get("db_search_id"),
        "keywords": data.get("params", {}).get("keywords") or data.get("keywords"),
        "limit": data.get("params", {}).get("limit", 50)
    }
