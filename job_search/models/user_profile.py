from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.sql import func

from job_search.database import Base


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=False)
    phone = Column(String(50), nullable=True)
    location = Column(String(200), nullable=True)
    linkedin_url = Column(String(500), nullable=True)
    headline = Column(String(500), nullable=True)
    summary = Column(Text, nullable=True)
    skills = Column(JSON, nullable=True)
    experience = Column(JSON, nullable=True)
    education = Column(JSON, nullable=True)
    target_roles = Column(JSON, nullable=True)
    target_locations = Column(JSON, nullable=True)
    min_salary = Column(Integer, nullable=True)
    linkedin_email = Column(String(200), nullable=True)
    linkedin_password_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
