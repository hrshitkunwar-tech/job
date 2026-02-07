from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from job_search.database import Base


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_type = Column(String(10), nullable=False)
    parsed_data = Column(JSON, nullable=True)
    raw_text = Column(Text, nullable=True)
    is_primary = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    versions = relationship("ResumeVersion", back_populates="base_resume")


class ResumeVersion(Base):
    __tablename__ = "resume_versions"

    id = Column(Integer, primary_key=True, index=True)
    base_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    file_path = Column(String(500), nullable=False)
    tailoring_notes = Column(Text, nullable=True)
    keywords_added = Column(JSON, nullable=True)
    sections_modified = Column(JSON, nullable=True)
    llm_model_used = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    base_resume = relationship("Resume", back_populates="versions")
    job = relationship("Job")
