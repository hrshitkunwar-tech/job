from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from job_search.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=settings.debug,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Called at startup."""
    import job_search.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=engine)
