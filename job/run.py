import uvicorn

from job_search.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "job_search.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
