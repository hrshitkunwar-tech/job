# Job Search 🧭

Job Search is an AI-powered job application assistant that automates the tedious parts of the job search: finding relevant jobs, scoring them against your profile, and tailoring your resume.

## Features

- **Automated LinkedIn Scraping**: Finds jobs based on your target keywords and location.
- **AI Matching**: Uses LLMs (Claude or GPT-4) to score jobs against your skills and experience.
- **Resume Parsing**: Extracts structured data from PDF/DOCX resumes.
- **Resume Tailoring**: Automatically reframes your experience to match specific job descriptions.
- **PDF Generation**: Generates clean, professional resumes using customizable Jinja2 templates.

## Quick Start

### 1. Prerequisites
- Python 3.10+
- [Playwright](https://playwright.dev/python/) (for scraping)

### 2. Installation

1. Clone the repository and navigate to the directory.
2. Create a virtual environment and activate it:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Install Playwright browsers:
   ```bash
   playwright install chromium
   ```

### 3. Configuration

Create a `.env` file in the root directory (use `.env.example` as a template):

```ini
# App
DEBUG=true

# LLM Provider (claude or openai)
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=your_key_here
# OPENAI_API_KEY=your_key_here

# LinkedIn Credentials (Optional but recommended)
LINKEDIN_EMAIL=your@email.com
LINKEDIN_PASSWORD=your_password

# Browser Settings
BROWSER_HEADLESS=true
```

### 4. Running the Application

Start the FastAPI server:
```bash
python run.py
```

Visit `http://localhost:8000` to access the dashboard.

## Project Structure

- `job_search/`: Core application package.
  - `models/`: Database models (SQLAlchemy).
  - `services/`: Core logic (Scraper, LLM, Matching).
  - `routes/`: API and Web endpoints.
  - `templates/`: HTML templates for UI and Resume.
- `data/`: SQLite database and browser state storage.
- `tests/`: Test suite for services.

## Development

Run tests:
```bash
pytest
```
