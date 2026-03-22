from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Engine and session are created lazily so that tests can override DATABASE_URL
# via os.environ before the first connection is made.
_engine = None
_SessionLocal = None


def reset_engine():
    """
    Discard the cached engine and session factory so the next call to
    _get_engine() creates a fresh one.  Used by the test suite to redirect
    to an isolated test database after overriding DATABASE_URL in os.environ.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        import os
        from job_search.config import settings
        # Prefer DATABASE_URL directly from the environment so tests can override
        # via os.environ before the engine is first created, even when the
        # Settings singleton was already instantiated with the default value.
        url = os.environ.get("DATABASE_URL") or settings.database_url
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
            echo=settings.debug,
        )
    return _engine


def _get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_get_engine())
    return _SessionLocal


# Keep module-level aliases so existing code that does
# `from job_search.database import engine` / `import SessionLocal` still works.
class _EngineProxy:
    """Thin proxy that forwards attribute access to the lazy engine."""
    def __getattr__(self, name):
        return getattr(_get_engine(), name)

    def __repr__(self):
        return repr(_get_engine())


class _SessionLocalProxy:
    """Thin proxy so `from job_search.database import SessionLocal` keeps working."""
    def __call__(self, *args, **kwargs):
        return _get_session_factory()(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(_get_session_factory(), name)


engine = _EngineProxy()
SessionLocal = _SessionLocalProxy()


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a database session."""
    db = _get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Called at startup."""
    import job_search.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(bind=_get_engine())
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Apply additive SQLite migrations for evolving profile fields."""
    eng = _get_engine()
    if not eng.url.drivername.startswith("sqlite"):
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
        "technical_manifesto": "TEXT",
        "preferred_team_style": "TEXT",
        "execution_preference": "TEXT",
        "company_stage_preference": "TEXT",
        "autonomy_preference": "TEXT",
        "frontier_tech_interest": "INTEGER",
    }

    application_columns = {
        "blocker_details": "JSON",
        "user_inputs": "JSON",
    }

    with eng.begin() as conn:
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
