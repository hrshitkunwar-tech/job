"""
Playwright E2E test fixtures.

Starts the FastAPI application on a random available port with an isolated
SQLite test database, then provides Playwright browser/page fixtures
pointing at that server.
"""

import os
import socket
import threading
import time

import pytest
import uvicorn


def _free_port() -> int:
    """Find and return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_port():
    """Return a free port for the test server."""
    return _free_port()


@pytest.fixture(scope="session")
def base_url(app_port):
    """Base URL for the running test server."""
    return f"http://127.0.0.1:{app_port}"


@pytest.fixture(scope="session", autouse=True)
def _start_server(app_port, tmp_path_factory):
    """
    Start the FastAPI app in a background thread with an isolated test DB.
    The server is torn down after the session completes.
    """
    # Use an isolated SQLite database so tests never touch production data.
    db_dir = tmp_path_factory.mktemp("test_db")
    db_path = db_dir / "test_job_search.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Ensure required directories exist for static file mounts.
    os.makedirs("job_search/static/uploads", exist_ok=True)
    os.makedirs("job_search/static/generated", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    os.makedirs("data/browser_state", exist_ok=True)

    # Import after env override so Settings picks up the test DB URL.
    from job_search.app import create_app

    app = create_app()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=app_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until the server is accepting connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", app_port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Test server did not start within 10 seconds")

    yield

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Playwright fixtures (override pytest-playwright defaults with base_url)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, base_url):
    """Inject base_url into every browser context."""
    return {**browser_context_args, "base_url": base_url}
