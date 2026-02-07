from pydantic import BaseModel
from typing import Optional


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


class UserProfileResponse(UserProfileUpdate):
    id: int

    model_config = {"from_attributes": True}
