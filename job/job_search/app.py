from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from job_search.config import settings
from job_search.database import init_db
from job_search.utils.logging_config import setup_logging

# Initialize Logging
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables and ensure directories exist
    Path("data").mkdir(exist_ok=True)
    Path("data/browser_state").mkdir(exist_ok=True)
    Path("job_search/static/uploads").mkdir(parents=True, exist_ok=True)
    Path("job_search/static/generated").mkdir(parents=True, exist_ok=True)
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # Static files
    app.mount(
        "/static",
        StaticFiles(directory="job_search/static"),
        name="static",
    )

    # Register routes
    from job_search.routes.dashboard import router as dashboard_router
    from job_search.routes.api_profile import router as profile_router
    from job_search.routes.api_resumes import router as resumes_router
    from job_search.routes.api_jobs import router as jobs_router
    from job_search.routes.api_applications import router as applications_router
    from job_search.routes.api_search import router as search_router

    app.include_router(dashboard_router)
    app.include_router(profile_router, prefix="/api/profile", tags=["profile"])
    app.include_router(resumes_router, prefix="/api/resumes", tags=["resumes"])
    app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(applications_router, prefix="/api/applications", tags=["applications"])
    app.include_router(search_router, prefix="/api/search", tags=["search"])

    return app


templates = Jinja2Templates(directory="job_search/templates")

# Add custom filters
import json

def from_json(value):
    """Parse JSON string to Python object"""
    if not value:
        return []
    try:
        return json.loads(value)
    except:
        return [value]

templates.env.filters['from_json'] = from_json
