from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class JobResponse(BaseModel):
    id: int
    external_id: str
    source: str
    title: str
    company: str
    location: Optional[str] = None
    work_type: Optional[str] = None
    employment_type: Optional[str] = None
    experience_level: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    description: str
    url: str
    is_easy_apply: bool = False
    posted_date: Optional[datetime] = None
    match_score: Optional[float] = None
    match_details: Optional[dict] = None
    extracted_keywords: Optional[list[str]] = None
    is_archived: bool = False
    scraped_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    per_page: int


class JobScoreRequest(BaseModel):
    deep: bool = False
