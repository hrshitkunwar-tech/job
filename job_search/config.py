from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # App
    app_name: str = "Job Search"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: str = "sqlite:///data/job_search.db"

    # LLM
    llm_provider: str = "ollama"  # "claude", "openai", or "ollama"
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    llm_model: Optional[str] = "qwen2.5-coder:7b"
    ollama_base_url: str = "http://localhost:11434"

    # LinkedIn
    linkedin_email: Optional[str] = None
    linkedin_password: Optional[str] = None
    # Default to headed mode because LinkedIn/ATS portals often block headless browsers
    # and because interactive challenges/login may be required.
    browser_headless: bool = False
    scrape_delay_min: float = 2.0
    scrape_delay_max: float = 7.0
    apply_delay_min: float = 30.0
    apply_delay_max: float = 90.0
    external_challenge_assist: bool = True
    external_challenge_timeout_seconds: int = 240
    # Keep resume upload reliable by default: use user's original PDF unless explicitly enabled.
    resume_tailoring_enabled: bool = False

    # Matching
    min_match_score: float = 50.0
    auto_apply_min_score: float = 75.0

    # Security
    encryption_key: Optional[str] = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
