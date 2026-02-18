# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a multi-project monorepo containing several related AI-powered tools, primarily focused on browser automation, job search, and knowledge organization.

## Projects

### job/ — AI Job Search Platform (Python, Production MVP)
- **Stack**: FastAPI + Jinja2 + SQLAlchemy (SQLite) + Playwright
- **Entry point**: `python3 run.py` (port from `.env`, currently 8005)
- **Package**: `job_search/` — all source code lives here
- **LLM**: Claude/OpenAI/Ollama (configured via `.env`)
- **Python**: 3.9.6 — use `from __future__ import annotations` for union type syntax (`str | None`)
- **Key services**: `services/scraper.py` (LinkedIn Playwright scraper, largest file ~67KB), `services/job_matcher.py`, `services/resume_tailor.py`, `services/applier.py` (~308KB)
- **Git branch**: `antigravity-fixes` (active development)

```bash
cd job && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python3 run.py
pytest                    # run tests
pytest tests/test_services.py::test_name  # single test
```

### MindFlow/ — Knowledge Organization (Next.js, Design Complete)
- **Stack**: Next.js 16 + React 19 + Tailwind CSS 4 + Supabase (PostgreSQL/pgvector) + OpenAI
- **Status**: Architecture & documentation complete, implementation in progress
- **Has its own CLAUDE.md** with detailed implementation phases

```bash
cd MindFlow
npm install && npm run dev    # localhost:3000
npm run build                 # production build
```

### MVP/backend/ — Navigator Procedural Intelligence (TypeScript, Fully Designed)
- **Stack**: n8n (orchestration) + Convex (database) + Express services
- **Architecture**: 4 specialized AI agents, deterministic procedure execution, anti-hallucination guarantees
- **Key docs**: `START_HERE.md`, `ARCHITECTURE.md`
- **Design principle**: Vision ≠ Reasoning ≠ Execution — agents recommend, n8n decides, tools validate

```bash
cd MVP/backend
npm install && npm run dev    # starts all services in parallel
```

### VisionGuide/ — AI Dashboard App (React + Vite)
- **Stack**: React 19 + Vite + Express + Supabase + Google Gemini
- **Includes**: Browser extension (`extension/`) + Express server (`server/`)

```bash
cd VisionGuide
npm install && npm run dev    # Vite dev server
```

### navigator-mvp/ — Browser Extension v2.1 (Chrome MV3)
- **Pure browser extension** with ZoneGuide recording system
- **Key shortcut**: Alt+Shift+R toggles ZoneGuide recording
- **Entry files**: `extension/background.js`, `extension/sidepanel.js`, `extension/content-script.js`

### Navigator_Ultimate_Blueprint/ — MCP Server Blueprints
- MCP (Model Context Protocol) server definitions for Cursor IDE + browser automation
- **Key pattern**: navigate → lock → interact → unlock (browser tab workflow)

## Cross-Project Patterns

- All projects use `.env` files for secrets (gitignored, `.env.example` provided)
- Browser extensions follow Chrome Manifest V3
- AI integrations are multi-provider (OpenAI, Claude, Gemini, Ollama) — check `.env` for active provider
- The `job/` project is the most mature; others are in design/early implementation phases
