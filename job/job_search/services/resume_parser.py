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
        import asyncio

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

        structured_data = None

        # Try LLM parsing with a 30s timeout, fall back to regex
        if self.llm_client:
            try:
                structured_data = await asyncio.wait_for(
                    self._structure_with_llm(raw_text),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning("LLM parsing timed out after 30s, using regex fallback")
            except Exception as e:
                logger.warning(f"LLM parsing failed: {e}, using regex fallback")

        if structured_data is None:
            structured_data = self._structure_with_regex(raw_text)

        return ParsedResume(
            raw_text=raw_text,
            structured_data=structured_data,
            file_type=file_type,
            source_path=str(file_path),
        )

    def _extract_text_pdf(self, file_path: Path) -> str:
        # Detect Apple Pages files disguised as PDF (PK zip header)
        with open(file_path, "rb") as f:
            header = f.read(4)
        if header == b"PK\x03\x04":
            raise ValueError(
                "This file appears to be an Apple Pages document saved with a .pdf extension. "
                "Please export it from Pages as a real PDF (File > Export To > PDF) or DOCX and re-upload."
            )

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
        """Fallback: regex-based extraction when LLM is unavailable."""
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

        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

        # Extract email
        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw_text)
        if email_match:
            data["email"] = email_match.group()

        # Extract phone
        phone_match = re.search(r"[\+]?[\d\s\-\(\)]{10,15}", raw_text)
        if phone_match:
            data["phone"] = phone_match.group().strip()

        # Extract LinkedIn URL
        linkedin_match = re.search(r"linkedin\.com/in/[\w-]+", raw_text)
        if linkedin_match:
            data["linkedin_url"] = "https://www." + linkedin_match.group()

        # Extract location (common patterns: City, State | City, Country)
        loc_match = re.search(
            r"([\w\s]+,\s*(?:India|USA|US|UK|Canada|Australia|[\w\s]+))\s*[\|\n]",
            raw_text[:500],
        )
        if loc_match:
            data["location"] = loc_match.group(1).strip()

        # First non-empty line is usually the name (skip if it looks like an email or URL)
        if lines:
            first = lines[0]
            if "@" not in first and "http" not in first and len(first) < 60:
                data["name"] = first

        # Second line is often the headline/title
        if len(lines) > 1:
            second = lines[1]
            if "@" not in second and "http" not in second and len(second) < 100:
                data["headline"] = second

        # Split text into sections by common headers
        section_pattern = re.compile(
            r"^(PROFESSIONAL\s+SUMMARY|SUMMARY|PROFILE|OBJECTIVE|"
            r"EXPERIENCE|WORK\s+EXPERIENCE|EMPLOYMENT|PROFESSIONAL\s+EXPERIENCE|"
            r"SKILLS|TECHNICAL\s+SKILLS|CORE\s+COMPETENCIES|COMPETENCIES|TECHNOLOGIES|"
            r"EDUCATION|ACADEMIC|"
            r"CERTIFICATIONS?|CERTIFICATES?|"
            r"PROJECTS?)\s*:?\s*$",
            re.IGNORECASE | re.MULTILINE,
        )

        sections = {}
        matches = list(section_pattern.finditer(raw_text))
        for i, m in enumerate(matches):
            name = m.group(1).strip().upper()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
            sections[name] = raw_text[start:end].strip()

        # Extract summary
        for key in ("PROFESSIONAL SUMMARY", "SUMMARY", "PROFILE", "OBJECTIVE"):
            if key in sections:
                data["summary"] = sections[key][:500]
                break

        # Extract skills
        for key in ("SKILLS", "TECHNICAL SKILLS", "CORE COMPETENCIES", "COMPETENCIES", "TECHNOLOGIES"):
            if key in sections:
                skills_text = sections[key]
                skills = re.split(r"[,|•·;\n]+", skills_text)
                data["skills"] = [s.strip() for s in skills if s.strip() and len(s.strip()) < 50]
                break

        # If no skills section found, try to extract from bullet points or inline mentions
        if not data["skills"]:
            # Look for comma-separated lists after skill-like labels
            skill_inline = re.findall(
                r"(?:Skills?|Technologies|Tools|Languages|Frameworks)[:\s]+([^\n]+)",
                raw_text,
                re.IGNORECASE,
            )
            for line in skill_inline:
                skills = re.split(r"[,|•·;]+", line)
                data["skills"].extend(s.strip() for s in skills if s.strip() and len(s.strip()) < 50)

        # Extract experience
        for key in ("EXPERIENCE", "WORK EXPERIENCE", "EMPLOYMENT", "PROFESSIONAL EXPERIENCE"):
            if key in sections:
                exp_text = sections[key]
                # Split by date patterns that typically start new entries
                entries = re.split(
                    r"\n(?=\S+.*(?:\d{4}\s*[-–]\s*(?:\d{4}|[Pp]resent|[Cc]urrent)))",
                    exp_text,
                )
                for entry in entries:
                    entry = entry.strip()
                    if not entry or len(entry) < 20:
                        continue
                    entry_lines = [l.strip() for l in entry.split("\n") if l.strip()]
                    if not entry_lines:
                        continue
                    title = entry_lines[0] if entry_lines else ""
                    company = entry_lines[1] if len(entry_lines) > 1 else ""
                    bullets = [l.lstrip("•-– ") for l in entry_lines[2:] if l.startswith(("•", "-", "–")) or len(l) > 30]
                    data["experience"].append({
                        "title": title,
                        "company": company,
                        "start_date": "",
                        "end_date": "",
                        "description": "",
                        "bullets": bullets[:10],
                    })
                break

        # Extract education
        for key in ("EDUCATION", "ACADEMIC"):
            if key in sections:
                edu_text = sections[key]
                edu_lines = [l.strip() for l in edu_text.split("\n") if l.strip()]
                for line in edu_lines:
                    if any(kw in line.lower() for kw in ("bachelor", "master", "b.s", "m.s", "b.tech", "m.tech", "mba", "phd", "degree", "diploma", "university", "college", "institute")):
                        year_match = re.search(r"(\d{4})", line)
                        data["education"].append({
                            "degree": line,
                            "school": "",
                            "year": year_match.group(1) if year_match else "",
                        })
                break

        return data
