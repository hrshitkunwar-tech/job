from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func

from job_search.database import Base


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    keywords = Column(String(500), nullable=False)
    locations = Column(String(1000), nullable=True)  # JSON array of locations
    work_types = Column(String(200), nullable=True)  # JSON array of work types
    experience_levels = Column(String(200), nullable=True)  # JSON array of experience levels
    date_posted = Column(String(50), nullable=True)
    easy_apply_only = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    results_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
