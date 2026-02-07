# Project Structure and Changes Documentation

This document outlines the recent changes made to the `job_search` application, distinguishing between modifications implemented by **Antigravity** (this session) and **Cloud / Claude Code** (other recent changes).

## 📂 Project Structure Overview

The application is structured as a FastAPI backend with Jinja2 templates for the frontend.

- **`job_search/`**: Main application directory.
  - **`routes/`**: API endpoints.
  - **`services/`**: Core logic (Scraper, Parser, Matcher).
  - **`templates/`**: HTML frontend templates.
  - **`static/`**: Static assets (CSS, JS).
  - **`models/`**: Database models.
  - **`schemas/`**: Pydantic schemas for API requests/responses.

---

## 🛠 Changes by Antigravity (Current Session)

These changes focused on stabilizing the search tool, fixing critical UI/UX issues, and implementing robust control mechanisms.

### 1. **Search Tool Stability & Control**
- **Headless Mode**: Enforced headless browser execution to prevent disruptive pop-ups during scraping.
  - Modified `job_search/config.py` to default `browser_headless = True`.
  - Updated `.env` to set `BROWSER_HEADLESS=true`.
- **Stop Search Functionality**: Implemented a mechanism to immediately stop running searches.
  - **Backend**: Added `/api/search/stop/{search_id}` endpoint in `job_search/routes/api_search.py`.
  - **Logic**: Updated search loop to check for cancellation flag and terminate execution cleanly.
  - **Frontend**: Added a **"Stop Search"** button to the progress indicator in `job_search/templates/search.html`.

### 2. **UI/UX Enhancements**
- **Save Search Modal**: Replaced the native `prompt()` with a proper HTML modal for saving searches (in previous steps).
- **Progress Tracker**: Added real-time ETA calculation and visual progress updates.
- **Scrollable Saved Searches**: Fixed the "Saved Searches" list to be scrollable via CSS overflow utilities.

### 3. **Bug Fixes**
- **Filtering**: Fixed issue where "Work Type" (Remote/Hybrid) filters were not working correctly.
- **Configuration**: Fixed `.env` variable overrides for browser settings.

---

## ☁️ Changes by Cloud / Claude Code (Recent)

These changes appear to focus on core functionality extension, particularly in job listing and resume parsing.

### 1. **Job Management Endpoints**
- **New File**: `job_search/routes/api_jobs.py`
  - Implements endpoints for listing jobs with filtering (`/api/jobs`).
  - Implements job scoring against user profile (`/api/jobs/{id}/score`).
  - Implements job archiving (`/api/jobs/{id}/archive`).

### 2. **Resume Parsing Logic**
- **Updated File**: `job_search/services/resume_parser.py`
  - Enhanced logic for extracting education details (degrees, years).
  - Improved bullet point extraction for work experience.
  - Added robust checks for multiple common degree types.

---

## 🚀 Next Steps

1. **Testing**:
   - Verify the new "Stop Search" button works as expected.
   - Confirm that no browser windows open during search.
   - Test the new Job Listing usage via the UI (if connected).
   - Test Resume Uploads with the new parser logic.

2. **Merging**:
   - Once tested on branch `antigravity-fixes`, merge into `master`.
