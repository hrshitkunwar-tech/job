from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ResumeResponse(BaseModel):
    id: int
    name: str
    file_path: str
    file_type: str
    parsed_data: Optional[dict] = None
    is_primary: bool = False
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ResumeVersionResponse(BaseModel):
    id: int
    base_resume_id: int
    job_id: int
    file_path: str
    tailoring_notes: Optional[str] = None
    keywords_added: Optional[list[str]] = None
    sections_modified: Optional[list[str]] = None
    llm_model_used: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TailorRequest(BaseModel):
    job_id: int
