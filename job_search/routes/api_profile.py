from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import UserProfile, AutomationIssueEvent
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


def _learning_summary(profile: UserProfile) -> dict:
    data = profile.application_answers if isinstance(profile.application_answers, dict) else {}
    learning = data.get("__learning") if isinstance(data, dict) else None
    if not isinstance(learning, dict):
        return {
            "enabled": False,
            "totals": {"runs": 0, "submitted": 0, "reviewed": 0, "failed": 0},
            "top_blockers": [],
            "top_missing_inputs": [],
            "top_domain_outcomes": [],
            "top_learned_field_values": {},
        }

    def _top_items(mapping: dict, limit: int = 8):
        if not isinstance(mapping, dict):
            return []
        ordered = sorted(mapping.items(), key=lambda item: int(item[1]) if str(item[1]).isdigit() else 0, reverse=True)
        return [{"key": k, "count": int(v) if str(v).isdigit() else 0} for k, v in ordered[:limit]]

    field_success = learning.get("field_success")
    top_values = {}
    if isinstance(field_success, dict):
        for field_key, value_counts in field_success.items():
            if not isinstance(value_counts, dict):
                continue
            best = sorted(
                value_counts.items(),
                key=lambda item: int(item[1]) if str(item[1]).isdigit() else 0,
                reverse=True,
            )
            if best:
                top_values[field_key] = {"value": best[0][0], "count": int(best[0][1]) if str(best[0][1]).isdigit() else 0}

    domain_stats = learning.get("domain_stats")
    top_domains = []
    if isinstance(domain_stats, dict):
        for domain, stats in domain_stats.items():
            if not isinstance(stats, dict):
                continue
            top_domains.append(
                {
                    "domain": domain,
                    "runs": int(stats.get("runs", 0)),
                    "submitted": int(stats.get("submitted", 0)),
                    "failed": int(stats.get("failed", 0)),
                    "reviewed": int(stats.get("reviewed", 0)),
                }
            )
        top_domains.sort(key=lambda item: item["runs"], reverse=True)

    return {
        "enabled": True,
        "totals": learning.get("totals") or {"runs": 0, "submitted": 0, "reviewed": 0, "failed": 0},
        "top_blockers": _top_items(learning.get("blocker_counts") or {}),
        "top_missing_inputs": _top_items(learning.get("missing_required_inputs") or {}),
        "top_domain_outcomes": top_domains[:10],
        "top_learned_field_values": top_values,
        "updated_at": learning.get("updated_at"),
    }


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


@router.get("/application-questions")
def application_questions(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Suggest targeted user questions based on observed automation issues + missing profile inputs.
    """
    profile = _get_or_create_profile(db)

    suggestions: list[dict] = []
    seen = set()

    issue_rows = (
        db.query(AutomationIssueEvent)
        .filter(AutomationIssueEvent.event_type == "detected")
        .order_by(AutomationIssueEvent.id.desc())
        .limit(limit)
        .all()
    )
    for row in issue_rows:
        for q in row.suggested_questions or []:
            key = ("issue", q)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                {
                    "question": q,
                    "category": row.category,
                    "source": row.source,
                    "domain": row.domain,
                    "reason": row.message,
                }
            )

    # Profile-driven baseline questionnaire for common screening blockers.
    profile_checks = [
        ("expected_ctc_lpa", "What is your expected CTC in LPA?"),
        ("current_ctc_lpa", "What is your current CTC in LPA?"),
        ("notice_period_days", "What is your notice period in days?"),
        ("can_join_immediately", "Can you join immediately?"),
        ("work_authorization", "What is your work authorization status?"),
        ("requires_sponsorship", "Do you require visa/work sponsorship?"),
        ("willing_to_relocate", "Are you willing to relocate if required?"),
    ]
    for attr, question in profile_checks:
        if getattr(profile, attr, None) is None or getattr(profile, attr, None) == "":
            key = ("profile", question)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                {
                    "question": question,
                    "category": "profile_input_missing",
                    "source": None,
                    "domain": None,
                    "reason": f"Missing profile field: {attr}",
                }
            )

    return {"count": len(suggestions), "questions": suggestions}


@router.get("/automation-learning")
def get_automation_learning(db: Session = Depends(get_db)):
    profile = _get_or_create_profile(db)
    return _learning_summary(profile)
