# CLAUDE.md — AI Job Search Platform

## Overview
AI-powered job application assistant: finds jobs, scores them, tailors resumes.

## Stack
- **Backend**: FastAPI + Jinja2 + SQLAlchemy (SQLite) + Playwright
- **LLM**: Claude/OpenAI/Ollama (configured via `.env`)
- **Python**: 3.9.6 — use `from __future__ import annotations` for union type syntax (`str | None`)

## Quick Reference
```bash
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python3 run.py              # starts server (port from .env, default 8005)
pytest                      # run tests
pytest tests/test_services.py::test_name  # single test
```

## Key Files
- `run.py` — entry point
- `job_search/app.py` — FastAPI factory
- `job_search/config.py` — settings from .env
- `job_search/database.py` — SQLAlchemy setup
- `job_search/services/scraper.py` — LinkedIn Playwright scraper (~67KB, largest file)
- `job_search/services/job_matcher.py` — scoring engine
- `job_search/services/resume_tailor.py` — AI resume rewriting
- `job_search/services/applier.py` — LinkedIn auto-apply (~308KB)
- `job_search/routes/api_search.py` — search orchestration

## Architecture
- `models/` — SQLAlchemy models (Job, Resume, Application, UserProfile, SearchQuery)
- `schemas/` — Pydantic schemas for API validation
- `routes/` — FastAPI routers (search, resumes, applications, profile)
- `services/` — business logic (scraper, matcher, tailor, applier, LLM client)
- `templates/` — Jinja2 HTML (dashboard, job detail, resumes, applications, profile)
- `static/` — CSS, JS, uploaded resumes, generated PDFs
- `scripts/` — utility scripts (DB management, testing)

## Conventions
- `.env` has real credentials (gitignored) — use `.env.example` as template
- Browser state persists in `data/browser_state/` (gitignored)
- All database files in `data/` are gitignored
