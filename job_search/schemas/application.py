from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ApplicationCreate(BaseModel):
    job_id: int
    auto_tailor: bool = True


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


class ApplicationResponse(BaseModel):
    id: int
    job_id: int
    resume_version_id: Optional[int] = None
    status: str
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApplicationStatsResponse(BaseModel):
    total: int = 0
    queued: int = 0
    submitted: int = 0
    interview: int = 0
    rejected: int = 0
    offer: int = 0
    failed: int = 0


class BatchApplyRequest(BaseModel):
    job_ids: list[int]
