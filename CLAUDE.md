# CLAUDE.md - CareerAgent
> Guidelines for AI-assisted development on the CareerAgent Strategist.

## 🛠 Build & Run
- **Python**: 3.10+
- **Venv**: `source venv/bin/activate`
- **Install**: `pip install -r requirements.txt`
- **Browsers**: `playwright install chromium`
- **Run Server**: `python run.py` (FastAPI on port 8000)

## 🧪 Testing
- **Run all tests**: `pytest`
- **Run specific test**: `pytest tests/test_scraper.py`
- **Lint**: `flake8 job_search`

## 🎨 Coding Style
- **Type Hints**: Always use Python type hints for function arguments and return values.
- **Async**: Use `async/await` for all IO-bound operations (Playwright, HTTP client, Database).
- **Models**: Use SQLAlchemy 2.0 style for database models.
- **Imports**: Alphabetical order within sections (Standard, Third-party, Local).
- **Naming**: `snake_case` for variables/functions, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- **Errors**: Custom exceptions in `job_search/exceptions.py`. Use descriptive error messages.

## 🧠 Brain Structure
- `job_search/services/llm.py`: Main orchestration for Claude/GPT-4 matching.
- `job_search/services/scraper/`: Modular scraper architecture (Strategy pattern).
