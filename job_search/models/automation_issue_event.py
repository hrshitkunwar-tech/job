from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.sql import func

from job_search.database import Base


class AutomationIssueEvent(Base):
    __tablename__ = "automation_issue_events"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    source = Column(String(120), nullable=True, index=True)
    domain = Column(String(255), nullable=True, index=True)
    category = Column(String(120), nullable=False, index=True)
    event_type = Column(String(40), nullable=False, index=True)  # detected | resolved
    message = Column(Text, nullable=False)
    required_user_inputs = Column(JSON, nullable=True)
    suggested_questions = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

