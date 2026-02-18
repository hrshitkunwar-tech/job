from __future__ import annotations
from typing import Optional

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from job_search.database import get_db
from job_search.models import Resume, ResumeVersion
from job_search.schemas.resume import ResumeResponse, ResumeVersionResponse, TailorRequest
from job_search.services.resume_parser import ResumeParser
from job_search.services.llm_client import LLMClient, LLMProvider
from job_search.config import settings

router = APIRouter()

UPLOAD_DIR = Path("job_search/static/uploads")


def _get_llm_client() -> Optional[LLMClient]:
    """Create an LLM client if API keys are configured."""
    if settings.llm_provider == "ollama":
        return LLMClient(LLMProvider.OLLAMA, None, settings.llm_model, settings.ollama_base_url)
    elif settings.llm_provider == "claude" and settings.anthropic_api_key:
        return LLMClient(LLMProvider.CLAUDE, settings.anthropic_api_key, settings.llm_model)
    elif settings.llm_provider == "openai" and settings.openai_api_key:
        return LLMClient(LLMProvider.OPENAI, settings.openai_api_key, settings.llm_model)
    return None


@router.post("/upload", response_model=ResumeResponse)
async def upload_resume(
    file: UploadFile = File(...),
    name: str = "My Resume",
    db: Session = Depends(get_db),
):
    """Upload and parse a resume (PDF or DOCX)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".pdf", ".docx", ".doc"):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported")

    # Save uploaded file
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOAD_DIR / file.filename
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Parse resume
    llm_client = _get_llm_client()
    parser = ResumeParser(llm_client=llm_client)

    try:
        parsed = await parser.parse(file_path)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to parse resume: {e}")

    # Save to database
    resume = Resume(
        name=name,
        file_path=str(file_path),
        file_type=parsed.file_type,
        parsed_data=parsed.structured_data,
        raw_text=parsed.raw_text,
        is_primary=db.query(Resume).count() == 0,  # First resume is primary
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


@router.get("", response_model=list[ResumeResponse])
def list_resumes(db: Session = Depends(get_db)):
    return db.query(Resume).order_by(Resume.created_at.desc()).all()


@router.get("/{resume_id}", response_model=ResumeResponse)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


@router.get("/{resume_id}/versions", response_model=list[ResumeVersionResponse])
def list_resume_versions(resume_id: int, db: Session = Depends(get_db)):
    return (
        db.query(ResumeVersion)
        .filter(ResumeVersion.base_resume_id == resume_id)
        .order_by(ResumeVersion.created_at.desc())
        .all()
    )


@router.get("/versions/{version_id}/download")
def download_version(version_id: int, db: Session = Depends(get_db)):
    version = db.query(ResumeVersion).filter(ResumeVersion.id == version_id).first()
    if not version:
        raise HTTPException(status_code=404, detail="Resume version not found")
    file_path = Path(version.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(file_path, filename=file_path.name, media_type="application/pdf")


@router.post("/{resume_id}/tailor", response_model=ResumeVersionResponse)
async def tailor_resume(resume_id: int, request: TailorRequest, db: Session = Depends(get_db)):
    """Generate a tailored resume version for a specific job."""
    from job_search.models import Job

    resume = db.query(Resume).filter(Resume.id == resume_id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not resume.parsed_data:
        raise HTTPException(status_code=400, detail="Resume has not been parsed")

    job = db.query(Job).filter(Job.id == request.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    llm_client = _get_llm_client()
    if not llm_client:
        raise HTTPException(status_code=500, detail="No LLM API key configured")

    # Import here to avoid circular imports at module level
    from job_search.services.resume_tailor import ResumeTailor
    from job_search.services.resume_generator import ResumeGenerator

    tailor = ResumeTailor(llm_client)
    result = await tailor.tailor(
        parsed_resume=resume.parsed_data,
        job_description=job.description,
        job_title=job.title,
        company=job.company,
    )

    # Generate PDF
    generator = ResumeGenerator()
    tailored_data = dict(resume.parsed_data)
    tailored_data.update(result.modified_sections)
    output_path = generator.generate_pdf(
        tailored_data,
        output_filename=f"resume_{resume_id}_job_{job.id}.pdf",
        require_pdf=False,
    )

    # Save version
    version = ResumeVersion(
        base_resume_id=resume.id,
        job_id=job.id,
        file_path=str(output_path),
        tailoring_notes=result.tailoring_notes,
        keywords_added=result.keywords_added,
        sections_modified=result.sections_changed,
        llm_model_used=llm_client.model,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version
