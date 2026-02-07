from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import UserProfile
from job_search.schemas.user_profile import UserProfileUpdate, UserProfileResponse

router = APIRouter()


def _get_or_create_profile(db: Session) -> UserProfile:
    """Get the single user profile, or create a blank one."""
    profile = db.query(UserProfile).first()
    if not profile:
        profile = UserProfile(full_name="", email="")
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@router.get("", response_model=UserProfileResponse)
def get_profile(db: Session = Depends(get_db)):
    return _get_or_create_profile(db)


@router.put("", response_model=UserProfileResponse)
def update_profile(data: UserProfileUpdate, db: Session = Depends(get_db)):
    profile = _get_or_create_profile(db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/import-from-resume")
def import_from_resume(resume_id: int, db: Session = Depends(get_db)):
    """Populate profile fields from a parsed resume."""
    from job_search.models import Resume

    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not resume.parsed_data:
        raise HTTPException(status_code=400, detail="Resume has not been parsed yet")

    profile = _get_or_create_profile(db)
    parsed = resume.parsed_data

    field_mapping = {
        "full_name": "name",
        "email": "email",
        "phone": "phone",
        "location": "location",
        "linkedin_url": "linkedin_url",
        "headline": "headline",
        "summary": "summary",
        "skills": "skills",
        "experience": "experience",
        "education": "education",
    }

    for profile_field, resume_field in field_mapping.items():
        value = parsed.get(resume_field)
        if value:
            setattr(profile, profile_field, value)

    db.commit()
    db.refresh(profile)
    return {"message": "Profile updated from resume", "profile_id": profile.id}
