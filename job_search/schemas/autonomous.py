from pydantic import BaseModel, Field
from typing import Optional


class AutonomousRunRequest(BaseModel):
    job_ids: list[int] = Field(default_factory=list)
    resume_id: Optional[int] = None
    min_score: Optional[float] = None
    safe_mode: bool = False
    require_confirmation: bool = False
    max_retries: int = Field(default=2, ge=0, le=5)


class AutonomousRunResponse(BaseModel):
    run_id: int
    status: str
    total_jobs: int
    min_score: float


class AutonomousRunStatusResponse(BaseModel):
    run_id: int
    status: str
    total_jobs: int
    processed_jobs: int
    submitted_jobs: int
    failed_jobs: int
    skipped_jobs: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None


class AutonomousStopResponse(BaseModel):
    run_id: int
    status: str
