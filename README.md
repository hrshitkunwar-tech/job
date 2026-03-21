# CareerAgent (`job`)

AI-powered job application automation. Scrapes LinkedIn for matching positions, scores them against your profile, tailors your resume for each one, and submits applications automatically.

> Part of **Navigator Lab** alongside the public [navigator](https://github.com/hrshitkunwar-tech/navigator) thesis repo and internal demo surfaces for visual reasoning playback and outreach operations.

Runs entirely locally by default — no cloud API costs required.

---

## What It Does

```
1. Scrape LinkedIn (or any career site) for positions matching your search criteria
2. Score each job against your profile using a 5-factor weighted model
3. AI-tailor your resume for positions above your threshold
4. Auto-apply to high-confidence matches via LinkedIn Easy Apply
5. Log every decision to a local SQLite database + web dashboard
```

You set the thresholds. The bot handles the volume.

---

## Scoring Model

| Factor | Weight |
|---|---|
| Skill match | 25% |
| Vibe / culture match | 35% |
| Title match | 20% |
| Keyword overlap | 10% |
| Experience level fit | 5% |
| Location preference | 5% |

| Score | Band | Action |
|---|---|---|
| 75+ | Strong | Auto-apply (if `AUTO_APPLY_MIN_SCORE` configured) |
| 60–74 | Good | Stored for manual review |
| 40–59 | Weak | Stored, flagged |
| < 40 | Poor | Skipped |

Both thresholds are configurable in `.env`.

CareerAgent now treats **Vibe Match** as a first-class signal: startup velocity, ownership level, frontier-tech alignment, and team shape are scored against the candidate's manifesto and preferences.

---

## Resume Tailoring

For every application above threshold, the system sends the job description to an LLM and receives back:

- Rewritten summary paragraph
- Reordered skills section (most relevant first)
- Reframed experience bullets (same facts, better alignment)

**Hard constraint baked into the system prompt:** Never fabricate experience. Reframe what exists. This is enforced at the prompt level, not optional.

Confidence scoring:
- LLM tailoring path: **0.8** confidence
- Keyword-only fallback: **0.4** confidence
- Original resume (last resort): **0.0** confidence

---

## Stack

| Component | Technology |
|---|---|
| Backend | FastAPI + Uvicorn |
| Dashboard | Jinja2 (server-rendered HTML) |
| Database | SQLAlchemy 2.0 + SQLite |
| Browser automation | Playwright (Chromium) |
| LLM | Ollama / Claude / OpenAI GPT-4o-mini |
| PDF generation | WeasyPrint |
| Resume parsing | pypdf + python-docx |
| Config | pydantic-settings + python-dotenv |

---

## Setup

```bash
git clone https://github.com/hrshitkunwar-tech/job
cd job
pip install -r requirements.txt
playwright install chromium
```

Create `.env`:

```bash
# LLM provider: "claude", "openai", or "ollama" (default: ollama — free to run locally)
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5-coder:7b

# Only needed if using cloud providers
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# LinkedIn credentials (stored encrypted locally)
LINKEDIN_EMAIL=your@email.com
LINKEDIN_PASSWORD=yourpassword
ENCRYPTION_KEY=your-32-byte-encryption-key-here

# Scoring thresholds
MIN_MATCH_SCORE=50.0
AUTO_APPLY_MIN_SCORE=75.0

# Set to "false" to watch the browser work
BROWSER_HEADLESS=true
```

```bash
python run.py
# → Dashboard at localhost:8000
```

---

## Running Locally for Free

The default LLM is Ollama with `qwen2.5-coder:7b`. If you have [Ollama](https://ollama.ai) installed:

```bash
ollama pull qwen2.5-coder:7b
python run.py
```

No API keys. No cloud costs. Your resume and credentials never leave your machine.

---

## Architecture

```
run.py
  └── FastAPI app
        ├── /dashboard          → HTML overview (Jinja2)
        ├── /api/search         → triggers scraping pipeline (background task)
        ├── /api/search/stop    → cancel active search
        ├── /api/jobs           → job listings + scores
        ├── /api/applications   → application history
        └── /api/profile        → user profile + resume management

Background task flow:
  SearchQuery → LinkedInScraper → JobMatcher → ResumeTailor → Applier
                                       ↓
                               SQLite (all decisions logged)
```

---

## Design Decisions

**60-second pause before final submission.** The applier stops for 60 seconds before the last "Submit" click. You can review the pre-filled form and intervene. The tool aids your job search — it doesn't replace your judgment at the final step.

**Random delays.** Scraping uses 2–7 second random delays. Application submission uses 30–90 second delays. These mimic human timing and reduce detection risk.

**Session persistence.** LinkedIn browser state is stored to disk. Authenticate once; subsequent runs reuse the session. Handles security checkpoints by detecting them and waiting for manual resolution.

**Local-first default.** Ollama + SQLite means zero cloud dependency. Your resume, credentials, and application history stay on your machine unless you configure a cloud LLM provider.
