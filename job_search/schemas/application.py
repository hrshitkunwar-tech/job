from pydantic import BaseModel
from typing import Any, Optional
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
    automation_log: Optional[str] = None
    blocker_details: Optional[dict[str, Any]] = None
    user_inputs: Optional[dict[str, Any]] = None
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


class AutomateRequest(BaseModel):
    resume_id: Optional[int] = None
    safe_mode: bool = False
    require_confirmation: bool = False


class BatchApplyRequest(BaseModel):
    job_ids: list[int]
    min_score: Optional[float] = None
    auto_automate: bool = False
    resume_id: Optional[int] = None
    safe_mode: bool = False
    require_confirmation: bool = False


class BlockerAnswerRequest(BaseModel):
    answers: dict[str, Any]
    apply_globally: bool = True
    retry_now: bool = False
    retry_all_blocked: bool = False
    resume_id: Optional[int] = None
    safe_mode: bool = False
    require_confirmation: bool = False
