from __future__ import annotations

from datetime import datetime
import json
import uuid
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import SearchQuery, Job, UserProfile
from job_search.schemas.search import SearchQueryCreate, SearchQueryResponse, SearchRunRequest

router = APIRouter()
logger = logging.getLogger(__name__)


# In-memory status tracker for running/recent searches.
active_searches: dict[str, dict[str, Any]] = {}


def _ensure_profile(db: Session) -> tuple[UserProfile, bool]:
    """Return latest profile, auto-creating a blank profile if needed."""
    profile = db.query(UserProfile).order_by(UserProfile.id.desc()).first()
    auto_created = False
    if not profile:
        profile = UserProfile(full_name="", email="")
        db.add(profile)
        db.commit()
        db.refresh(profile)
        auto_created = True
    return profile, auto_created


def _profile_can_score(profile: UserProfile | None) -> bool:
    if not profile:
        return False
    has_skills = bool(profile.skills)
    has_roles = bool(profile.target_roles)
    has_experience = bool(profile.experience)
    has_summary = bool(profile.summary)
    return has_skills or has_roles or has_experience or has_summary


def _profile_to_dict(profile: UserProfile | None) -> dict[str, Any]:
    if not profile:
        return {
            "skills": [],
            "target_roles": [],
            "target_locations": [],
            "experience": [],
            "summary": "",
            "headline": "",
        }
    return {
        "skills": profile.skills or [],
        "target_roles": profile.target_roles or [],
        "target_locations": profile.target_locations or [],
        "experience": profile.experience or [],
        "summary": profile.summary or "",
        "headline": profile.headline or "",
    }


def _unscored_match_details(reason: str) -> dict[str, Any]:
    return {
        "skill_score": None,
        "title_score": None,
        "explanation": "Unscored: profile incomplete",
        "matched_skills": [],
        "missing_skills": [],
        "unscored_reason": reason,
    }


def _infer_roles_from_keywords(raw_keywords: Any) -> list[str]:
    if raw_keywords is None:
        return []
    if isinstance(raw_keywords, str):
        items = [raw_keywords]
    elif isinstance(raw_keywords, list):
        items = [str(k) for k in raw_keywords if k]
    else:
        items = [str(raw_keywords)]
    roles: list[str] = []
    seen: set[str] = set()
    for item in items:
        role = item.strip()
        if not role:
            continue
        norm = role.lower()
        if norm in seen:
            continue
        seen.add(norm)
        roles.append(role)
    return roles


def _register_status_warning(search_id: str, warning: str):
    data = active_searches.get(search_id)
    if not data:
        return
    warnings = data.setdefault("warnings", [])
    if warning not in warnings:
        warnings.append(warning)


def _mark_completed(search_id: str, state: str):
    if search_id not in active_searches:
        return
    active_searches[search_id]["state"] = state
    active_searches[search_id]["finished_at"] = datetime.now().isoformat()


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
    if search_id in active_searches:
        active_searches[search_id]["cancelled"] = True
        active_searches[search_id]["state"] = "cancelled"
        return {"message": "Search stop requested", "search_id": search_id}
    raise HTTPException(status_code=404, detail="Search not found or already completed")


@router.post("/stop-all")
async def stop_all_searches():
    count = 0
    for search_id in list(active_searches.keys()):
        active_searches[search_id]["cancelled"] = True
        active_searches[search_id]["state"] = "cancelled"
        count += 1
    return {"message": f"Requested stop for {count} active searches. Progress indicators should clear shortly."}


@router.post("/run")
async def run_search(request: SearchRunRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Trigger a background job search across selected portals."""
    search_id = str(uuid.uuid4())

    keywords_str = ", ".join(request.keywords) if isinstance(request.keywords, list) else request.keywords

    db_search_id = None
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
            results_count=0,
        )
        db.add(db_query)
        db.commit()
        db.refresh(db_query)
        db_search_id = db_query.id
    except Exception as e:
        logger.error(f"Failed to save search query: {e}")

    active_searches[search_id] = {
        "cancelled": False,
        "started_at": datetime.now().isoformat(),
        "db_search_id": db_search_id,
        "params": request.model_dump(),
        "state": "queued",
        "saved_jobs": 0,
        "scored_jobs": 0,
        "unscored_jobs": 0,
        "warnings": [],
        "source_breakdown": {},
    }

    background_tasks.add_task(_run_search_task, request.model_dump(), search_id, db_search_id)
    return {
        "status": "started",
        "message": "Search task queued. Jobs will appear as they are found.",
        "params": request.model_dump(),
        "search_id": search_id,
        "db_search_id": db_search_id,
    }


@router.get("/status/{search_id}")
def get_search_status(search_id: str, db: Session = Depends(get_db)):
    data = active_searches.get(search_id)
    if data:
        return {
            "search_id": search_id,
            "state": data.get("state", "running"),
            "saved_jobs": data.get("saved_jobs", 0),
            "scored_jobs": data.get("scored_jobs", 0),
            "unscored_jobs": data.get("unscored_jobs", 0),
            "source_breakdown": data.get("source_breakdown", {}),
            "warnings": data.get("warnings", []),
            "db_search_id": data.get("db_search_id"),
        }

    # Fallback for completed search no longer in memory.
    raise HTTPException(status_code=404, detail="Search status not found")


async def _run_search_task(params: dict, search_id: str, db_search_id: int | None = None):
    from job_search.database import SessionLocal
    from job_search.services.scraper import LinkedInScraper, GeneralWebScraper, WebJobScraper
    from job_search.services.job_matcher import JobMatcher
    from job_search.services.llm_client import get_llm_client

    db = SessionLocal()
    try:
        status = active_searches.get(search_id)
        if not status:
            return
        status["state"] = "running"

        if status.get("cancelled"):
            _mark_completed(search_id, "cancelled")
            return

        raw_keywords = params.get("keywords")
        keywords_list = raw_keywords if isinstance(raw_keywords, list) else [raw_keywords]
        keywords_list = [str(k).strip() for k in keywords_list if k and str(k).strip()]

        profile_obj, auto_created = _ensure_profile(db)
        if auto_created:
            _register_status_warning(search_id, "Profile was missing and has been auto-created. Jobs will be saved even if scoring is limited.")

        can_score = _profile_can_score(profile_obj)
        if not can_score:
            _register_status_warning(search_id, "Profile is incomplete. Jobs are saved with neutral score and marked unscored.")

        profile_data = _profile_to_dict(profile_obj)
        if not profile_data.get("target_roles"):
            inferred_roles = _infer_roles_from_keywords(keywords_list)
            if inferred_roles:
                profile_data["target_roles"] = inferred_roles
                _register_status_warning(
                    search_id,
                    "Target roles were inferred from your search keywords for scoring.",
                )

        scraper = LinkedInScraper()
        custom_scraper = GeneralWebScraper()
        web_scraper = WebJobScraper()
        matcher = JobMatcher(llm_client=get_llm_client())

        portals_list = params.get("portals") or ["linkedin"]
        locations_list = params.get("locations") or [params.get("location", "")]
        limit_per_search = params.get("limit", 5)

        total_jobs_found = 0
        seen_in_run: set[tuple[str, str, str, str]] = set()

        def upsert_from_job_data(job_data: dict, default_source: str):
            nonlocal total_jobs_found
            if not job_data.get("title"):
                return

            src = job_data.get("source", default_source)
            ext_id = str(job_data.get("external_id") or "").strip()
            original_ext_id = ext_id
            url = str(job_data.get("url") or "").strip()
            title_norm = str(job_data.get("title") or "").strip().lower()
            company_norm = str(job_data.get("company") or "unknown").strip().lower()
            run_key = (src, ext_id or url, title_norm, company_norm)
            if run_key in seen_in_run:
                return
            seen_in_run.add(run_key)

            if db_search_id and ext_id:
                existing_in_run = (
                    db.query(Job)
                    .filter(
                        Job.search_query_id == db_search_id,
                        Job.source == src,
                        Job.external_id == ext_id,
                    )
                    .first()
                )
                if existing_in_run:
                    return

            # Preserve historical runs: if external_id already exists globally for another run,
            # create a run-scoped variant instead of re-linking the old row.
            if ext_id:
                existing_global = db.query(Job).filter(Job.external_id == ext_id).first()
                if existing_global and existing_global.search_query_id != db_search_id:
                    scope = str(db_search_id or search_id[:8])
                    ext_id = f"{original_ext_id}::run:{scope}"

            if can_score:
                match_result = matcher.score_job(job_data, profile_data)
                match_score = match_result.overall_score
                match_details = {
                    "skill_score": match_result.skill_score,
                    "title_score": match_result.title_score,
                    "explanation": match_result.explanation,
                    "matched_skills": match_result.matched_skills,
                    "missing_skills": match_result.missing_skills,
                }
                active_searches[search_id]["scored_jobs"] += 1
            else:
                match_score = 50.0
                match_details = _unscored_match_details("profile_incomplete")
                active_searches[search_id]["unscored_jobs"] += 1

            active_searches[search_id]["source_breakdown"][src] = active_searches[search_id]["source_breakdown"].get(src, 0) + 1

            job = Job(
                external_id=ext_id or None,
                source=src,
                title=job_data.get("title"),
                company=job_data.get("company", "Unknown"),
                location=job_data.get("location", ""),
                work_type=job_data.get("work_type", "onsite"),
                is_easy_apply=job_data.get("is_easy_apply", False),
                apply_url=job_data.get("apply_url"),
                description=job_data.get("description", ""),
                description_html=job_data.get("description_html", ""),
                url=job_data.get("url", ""),
                match_score=match_score,
                match_details=match_details,
                search_query_id=db_search_id,
            )
            db.add(job)
            total_jobs_found += 1
            active_searches[search_id]["saved_jobs"] = total_jobs_found

        for portal in portals_list:
            if total_jobs_found >= limit_per_search:
                break
            if active_searches.get(search_id, {}).get("cancelled"):
                _mark_completed(search_id, "cancelled")
                return

            if portal in ["career_site", "web_url"]:
                custom_urls = params.get("custom_portal_urls") or []
                if not custom_urls:
                    _register_status_warning(search_id, f"Portal '{portal}' selected but no custom URLs provided.")
                    continue

                for custom_url in custom_urls:
                    if total_jobs_found >= limit_per_search:
                        break
                    found_jobs = await custom_scraper.scrape_custom_url(
                        url=custom_url,
                        keywords=keywords_list,
                        locations=locations_list,
                        limit=limit_per_search - total_jobs_found,
                    )
                    for job_data in found_jobs:
                        upsert_from_job_data(job_data, portal)
                    db.commit()
                continue

            if portal == "web":
                for keyword in keywords_list:
                    if total_jobs_found >= limit_per_search:
                        break
                    location = locations_list[0] if locations_list else ""
                    found_jobs = await web_scraper.scrape_jobs(
                        query=keyword,
                        location=location,
                        limit=limit_per_search - total_jobs_found,
                        filters={
                            "date_posted": params.get("date_posted"),
                            "work_types": params.get("work_types"),
                        },
                    )

                    for w in getattr(web_scraper, "_last_warnings", []):
                        _register_status_warning(search_id, w)

                    for job_data in found_jobs:
                        upsert_from_job_data(job_data, "web")

                    if not found_jobs and total_jobs_found < limit_per_search:
                        _register_status_warning(
                            search_id,
                            f"Web sources returned low relevance for '{keyword}'. Trying LinkedIn fallback.",
                        )
                        fallback_jobs = await scraper.scrape_jobs(
                            query=keyword,
                            location=location or "",
                            limit=limit_per_search - total_jobs_found,
                            filters={
                                "date_posted": params.get("date_posted"),
                                "work_types": params.get("work_types"),
                                "experience_levels": params.get("experience_levels"),
                                "easy_apply_only": params.get("easy_apply_only"),
                            },
                            check_cancelled=lambda: active_searches.get(search_id, {}).get("cancelled", False),
                        )
                        for job_data in fallback_jobs:
                            upsert_from_job_data(job_data, "linkedin")
                    db.commit()
                continue

            if portal not in ("linkedin",):
                _register_status_warning(search_id, f"Portal '{portal}' is not fully implemented for scraping and was skipped.")
                continue

            for keyword in keywords_list:
                for location in locations_list:
                    if total_jobs_found >= limit_per_search:
                        break
                    if active_searches.get(search_id, {}).get("cancelled"):
                        _mark_completed(search_id, "cancelled")
                        return

                    found_jobs = await scraper.scrape_jobs(
                        query=keyword,
                        location=location or "",
                        limit=limit_per_search - total_jobs_found,
                        filters={
                            "date_posted": params.get("date_posted"),
                            "work_types": params.get("work_types"),
                            "experience_levels": params.get("experience_levels"),
                            "easy_apply_only": params.get("easy_apply_only"),
                        },
                        check_cancelled=lambda: active_searches.get(search_id, {}).get("cancelled", False),
                    )

                    for job_data in found_jobs:
                        upsert_from_job_data(job_data, portal)
                    db.commit()

                if total_jobs_found >= limit_per_search:
                    break

        if db_search_id:
            search_record = db.query(SearchQuery).filter(SearchQuery.id == db_search_id).first()
            if search_record:
                search_record.results_count = total_jobs_found
                db.commit()

        _mark_completed(search_id, "completed")

    except Exception as e:
        logger.exception(f"Search task failed: {e}")
        _register_status_warning(search_id, f"Search failed: {e}")
        _mark_completed(search_id, "failed")
    finally:
        db.close()


@router.get("/active")
def get_active_search():
    active = {
        sid: data
        for sid, data in active_searches.items()
        if not data.get("cancelled") and data.get("state") in {"queued", "running"}
    }
    if not active:
        return {"active": False}

    sorted_active = sorted(
        active.items(),
        key=lambda x: datetime.fromisoformat(x[1].get("started_at")) if x[1].get("started_at") else datetime.min,
        reverse=True,
    )
    search_id, data = sorted_active[0]

    return {
        "active": True,
        "search_id": search_id,
        "db_search_id": data.get("db_search_id"),
        "keywords": data.get("params", {}).get("keywords"),
        "limit": data.get("params", {}).get("limit", 50),
    }
