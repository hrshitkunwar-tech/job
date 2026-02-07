from sqlalchemy import Column, Integer, String, Text, DateTime, Float, JSON, Boolean
from sqlalchemy.sql import func

from job_search.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String(100), unique=True, index=True)
    source = Column(String(50), default="linkedin")
    title = Column(String(500), nullable=False)
    company = Column(String(300), nullable=False)
    company_url = Column(String(500), nullable=True)
    location = Column(String(300), nullable=True)
    work_type = Column(String(50), nullable=True)
    employment_type = Column(String(50), nullable=True)
    experience_level = Column(String(50), nullable=True)
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_currency = Column(String(10), nullable=True)
    description = Column(Text, nullable=False)
    description_html = Column(Text, nullable=True)
    url = Column(String(1000), nullable=False)
    apply_url = Column(String(1000), nullable=True)
    is_easy_apply = Column(Boolean, default=False)
    posted_date = Column(DateTime, nullable=True)
    scraped_at = Column(DateTime, server_default=func.now())

    # Matching
    match_score = Column(Float, nullable=True)
    match_details = Column(JSON, nullable=True)
    extracted_keywords = Column(JSON, nullable=True)
    extracted_requirements = Column(JSON, nullable=True)

    # Status
    is_archived = Column(Boolean, default=False)
    search_query_id = Column(Integer, nullable=True)
