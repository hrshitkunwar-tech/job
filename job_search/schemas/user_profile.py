from pydantic import BaseModel
from typing import Any, Optional


class UserProfileUpdate(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    headline: Optional[str] = None
    summary: Optional[str] = None
    skills: Optional[list[str]] = None
    experience: Optional[list[dict]] = None
    education: Optional[list[dict]] = None
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    min_salary: Optional[int] = None
    current_ctc_lpa: Optional[float] = None
    expected_ctc_lpa: Optional[float] = None
    notice_period_days: Optional[int] = None
    can_join_immediately: Optional[bool] = None
    willing_to_relocate: Optional[bool] = None
    work_authorization: Optional[str] = None
    requires_sponsorship: Optional[bool] = None
    application_answers: Optional[dict[str, Any]] = None


class UserProfileResponse(UserProfileUpdate):
    id: int

    model_config = {"from_attributes": True}
