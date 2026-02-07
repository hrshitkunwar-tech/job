from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from job_search.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

PARSE_SYSTEM_PROMPT = """You are a resume parser. Extract structured data from the resume text.
Return a JSON object with exactly these fields:
{
    "name": "Full Name",
    "email": "email@example.com",
    "phone": "phone number or null",
    "location": "City, State or null",
    "linkedin_url": "linkedin URL or null",
    "summary": "professional summary paragraph or null",
    "headline": "current job title or professional headline",
    "skills": ["skill1", "skill2", ...],
    "experience": [
        {
            "title": "Job Title",
            "company": "Company Name",
            "start_date": "YYYY-MM or approximate",
            "end_date": "YYYY-MM or present",
            "description": "brief role description",
            "bullets": ["achievement 1", "achievement 2"]
        }
    ],
    "education": [
        {
            "degree": "Degree Name",
            "school": "School Name",
            "year": "graduation year"
        }
    ],
    "certifications": ["cert1", "cert2"],
    "projects": [
        {
            "name": "Project Name",
            "description": "brief description"
        }
    ]
}

Extract ALL information present. Use null for missing fields. For skills, be comprehensive
and include both explicitly listed skills and those implied by experience descriptions."""


@dataclass
class ParsedResume:
    raw_text: str
    structured_data: dict
    file_type: str
    source_path: str


class ResumeParser:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client

    async def parse(self, file_path: str | Path) -> ParsedResume:
        """Parse a resume file (PDF or DOCX) into structured data."""
        file_path = Path(file_path)
        file_type = file_path.suffix.lower().lstrip(".")

        if file_type == "pdf":
            raw_text = self._extract_text_pdf(file_path)
        elif file_type in ("docx", "doc"):
            raw_text = self._extract_text_docx(file_path)
            file_type = "docx"
        else:
            raise ValueError(f"Unsupported file type: {file_type}")

        if not raw_text.strip():
            raise ValueError("Could not extract text from file")

        if self.llm_client:
            structured_data = await self._structure_with_llm(raw_text)
        else:
            structured_data = self._structure_with_regex(raw_text)

        return ParsedResume(
            raw_text=raw_text,
            structured_data=structured_data,
            file_type=file_type,
            source_path=str(file_path),
        )

    def _extract_text_pdf(self, file_path: Path) -> str:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)

    def _extract_text_docx(self, file_path: Path) -> str:
        from docx import Document

        doc = Document(file_path)
        paragraphs = []
        for para in doc.paragraphs:
            if para.text.strip():
                paragraphs.append(para.text)
        return "\n".join(paragraphs)

    async def _structure_with_llm(self, raw_text: str) -> dict:
        return await self.llm_client.complete_json(
            prompt=f"Parse the following resume:\n\n{raw_text}",
            system=PARSE_SYSTEM_PROMPT,
        )

    def _structure_with_regex(self, raw_text: str) -> dict:
        """Fallback: basic regex-based extraction."""
        import re

        data = {
            "name": None,
            "email": None,
            "phone": None,
            "location": None,
            "linkedin_url": None,
            "summary": None,
            "headline": None,
            "skills": [],
            "experience": [],
            "education": [],
            "certifications": [],
            "projects": [],
        }

        # Extract email
        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw_text)
        if email_match:
            data["email"] = email_match.group()

        # Extract phone
        phone_match = re.search(r"[\+]?[\d\s\-\(\)]{10,}", raw_text)
        if phone_match:
            data["phone"] = phone_match.group().strip()

        # Extract LinkedIn URL
        linkedin_match = re.search(r"linkedin\.com/in/[\w-]+", raw_text)
        if linkedin_match:
            data["linkedin_url"] = "https://www." + linkedin_match.group()

        # First non-empty line is often the name
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
        if lines:
            data["name"] = lines[0]

        # Extract skills from common section headers
        skills_section = re.search(
            r"(?:SKILLS|TECHNICAL SKILLS|COMPETENCIES|TECHNOLOGIES)[:\s]*\n([\s\S]*?)(?:\n[A-Z]{2,}|\Z)",
            raw_text,
            re.IGNORECASE,
        )
        if skills_section:
            skills_text = skills_section.group(1)
            # Split by common delimiters
            skills = re.split(r"[,|•·\n]+", skills_text)
            data["skills"] = [s.strip() for s in skills if s.strip() and len(s.strip()) < 50]

        return data
