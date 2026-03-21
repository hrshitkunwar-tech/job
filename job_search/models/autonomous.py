from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from job_search.database import Base


class AutonomousRun(Base):
    __tablename__ = "autonomous_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(40), nullable=False, default="queued")
    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    total_jobs = Column(Integer, nullable=False, default=0)
    processed_jobs = Column(Integer, nullable=False, default=0)
    submitted_jobs = Column(Integer, nullable=False, default=0)
    failed_jobs = Column(Integer, nullable=False, default=0)
    skipped_jobs = Column(Integer, nullable=False, default=0)
    min_score = Column(Integer, nullable=False, default=75)
    safe_mode = Column(Integer, nullable=False, default=1)  # bool-like for sqlite portability
    require_confirmation = Column(Integer, nullable=False, default=1)
    constraints = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    resume = relationship("Resume")
    logs = relationship("AutonomousJobLog", back_populates="run")


class AutonomousJobLog(Base):
    __tablename__ = "autonomous_job_logs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("autonomous_runs.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True)
    stage = Column(String(80), nullable=False, default="queued")
    status = Column(String(40), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    resume_version_id = Column(Integer, nullable=True)
    details = Column(JSON, nullable=True)
    confirmation = Column(JSON, nullable=True)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    run = relationship("AutonomousRun", back_populates="logs")
    job = relationship("Job")
    application = relationship("Application")
