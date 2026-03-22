# CareerAgent (`job`)

End-to-end workflow automation for a messy, real-world problem: scrapes a data source for opportunities, scores them against a profile using a weighted model, tailors the output with an LLM, and executes the final action — with a human-in-the-loop checkpoint before submission.

The domain is job applications. The pattern is what Navigator is built on: context retrieval → scoring → tailoring → execution → logging. Works for a human running their own job search. Works for an agent running it autonomously.

> Part of **Navigator Lab** — an open research portfolio building AI execution infrastructure for software interfaces.

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

## What This Proves

The job search domain is a clean proof of the Navigator execution pattern — a complete, messy, real-world workflow with multiple tools, stateful context, human judgment at the edges, and structured logging throughout.

The same loop maps directly to GTM operations:

| Job domain | GTM equivalent |
|---|---|
| LinkedIn scrape | Gong transcript / Salesforce opportunity pull |
| 5-factor job score | Account health score / opportunity fit score |
| Resume tailoring | Follow-up email / renewal brief / QBR slide |
| 60-second pause before apply | Human review before send |
| SQLite decision log | CRM activity log / call outcome record |

If the pattern works on job applications, it works on customer-facing workflows. And it works the same whether the operator is a person or an agent.

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

---

## Navigator Lab

| Repo | Layer | What it does |
|---|---|---|
| [navigator](https://github.com/hrshitkunwar-tech/navigator) | Thesis | 5-layer architecture for AI execution on software interfaces |
| [VisionGuide](https://github.com/hrshitkunwar-tech/VisionGuide) | Perception | Screenshot → Gemini vision → real-time UI guidance |
| [job](https://github.com/hrshitkunwar-tech/job) | Applied | CareerAgent: score → tailor → apply, local-first |
| [saas-atlas](https://github.com/hrshitkunwar-tech/saas-atlas) | Data | Searchable directory of 200+ SaaS tools |
