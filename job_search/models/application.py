import enum

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Enum as SAEnum, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from job_search.database import Base


class ApplicationStatus(str, enum.Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    FAILED = "failed"
    REVIEWED = "reviewed"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    resume_version_id = Column(Integer, ForeignKey("resume_versions.id"), nullable=True)
    status = Column(SAEnum(ApplicationStatus), default=ApplicationStatus.QUEUED)
    applied_at = Column(DateTime, nullable=True)
    status_updated_at = Column(DateTime, onupdate=func.now())
    notes = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    automation_log = Column(Text, nullable=True)
    blocker_details = Column(JSON, nullable=True)
    user_inputs = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    job = relationship("Job")
    resume_version = relationship("ResumeVersion")
