from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from job_search.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
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
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Apply additive SQLite migrations for evolving profile fields."""
    if not engine.url.drivername.startswith("sqlite"):
        return

    required_columns = {
        "current_ctc_lpa": "REAL",
        "expected_ctc_lpa": "REAL",
        "notice_period_days": "INTEGER",
        "can_join_immediately": "INTEGER",
        "willing_to_relocate": "INTEGER",
        "work_authorization": "TEXT",
        "requires_sponsorship": "INTEGER",
        "application_answers": "JSON",
    }

    application_columns = {
        "blocker_details": "JSON",
        "user_inputs": "JSON",
    }

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(user_profiles)")).fetchall()
        existing = {row[1] for row in rows}
        for column, col_type in required_columns.items():
            if column in existing:
                continue
            conn.execute(
                text(f"ALTER TABLE user_profiles ADD COLUMN {column} {col_type}")
            )

        app_rows = conn.execute(text("PRAGMA table_info(applications)")).fetchall()
        app_existing = {row[1] for row in app_rows}
        for column, col_type in application_columns.items():
            if column in app_existing:
                continue
            conn.execute(
                text(f"ALTER TABLE applications ADD COLUMN {column} {col_type}")
            )
