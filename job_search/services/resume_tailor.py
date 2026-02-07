from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from job_search.services.llm_client import LLMClient
from job_search.utils.text_processing import extract_keywords, normalize_skill

logger = logging.getLogger(__name__)


@dataclass
class TailoringResult:
    modified_sections: dict = field(default_factory=dict)
    keywords_added: list[str] = field(default_factory=list)
    sections_changed: list[str] = field(default_factory=list)
    tailoring_notes: str = ""
    confidence_score: float = 0.0


TAILOR_SYSTEM_PROMPT = """You are a professional resume tailoring assistant. Your job is to modify
a resume to better match a specific job description while maintaining truthfulness.

Rules:
- NEVER fabricate experience, skills, or achievements
- Only REFRAME existing experience using language from the job description
- Emphasize the most relevant aspects of existing experience
- Add JD keywords naturally where they apply to actual experience
- Rewrite the professional summary to target this specific role
- Reorder skills to prioritize those mentioned in the JD

Return JSON with:
{
    "summary": "rewritten professional summary",
    "skills": ["reordered", "skills", "list"],
    "experience": [
        {
            "title": "existing title",
            "company": "existing company",
            "start_date": "existing",
            "end_date": "existing",
            "description": "reframed description",
            "bullets": ["reframed bullet 1", "reframed bullet 2"]
        }
    ],
    "keywords_added": ["keyword1", "keyword2"],
    "sections_changed": ["summary", "skills", "experience"],
    "tailoring_notes": "brief explanation of changes made"
}"""


class ResumeTailor:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client

    async def tailor(
        self,
        parsed_resume: dict,
        job_description: str,
        job_title: str,
        company: str,
        match_result: Optional[dict] = None,
    ) -> TailoringResult:
        """Tailor a resume for a specific job."""
        try:
            return await self._tailor_with_llm(
                parsed_resume, job_description, job_title, company
            )
        except Exception as e:
            logger.warning(f"LLM tailoring failed, using keyword fallback: {e}")
            jd_keywords = extract_keywords(job_description)
            return self.tailor_keywords_only(parsed_resume, jd_keywords)

    async def _tailor_with_llm(
        self,
        resume_data: dict,
        job_description: str,
        job_title: str,
        company: str,
    ) -> TailoringResult:
        prompt = f"""Tailor this resume for the following job:

Job Title: {job_title}
Company: {company}
Job Description:
{job_description[:4000]}

Current Resume:
Name: {resume_data.get('name', '')}
Summary: {resume_data.get('summary', 'N/A')}
Skills: {', '.join(resume_data.get('skills', []))}
Experience:
"""
        for exp in resume_data.get("experience", []):
            prompt += f"\n- {exp.get('title', '')} at {exp.get('company', '')} ({exp.get('start_date', '')} - {exp.get('end_date', '')})"
            prompt += f"\n  {exp.get('description', '')}"
            for bullet in exp.get("bullets", []):
                prompt += f"\n  * {bullet}"

        result = await self.llm_client.complete_json(
            prompt=prompt,
            system=TAILOR_SYSTEM_PROMPT,
        )

        modified_sections = {}
        if "summary" in result:
            modified_sections["summary"] = result["summary"]
        if "skills" in result:
            modified_sections["skills"] = result["skills"]
        if "experience" in result:
            modified_sections["experience"] = result["experience"]

        return TailoringResult(
            modified_sections=modified_sections,
            keywords_added=result.get("keywords_added", []),
            sections_changed=result.get("sections_changed", []),
            tailoring_notes=result.get("tailoring_notes", ""),
            confidence_score=0.8,
        )

    def tailor_keywords_only(
        self, parsed_resume: dict, jd_keywords: list[str]
    ) -> TailoringResult:
        """Fallback: keyword-based tailoring without LLM."""
        skills = list(parsed_resume.get("skills", []))
        user_skills_normalized = {normalize_skill(s): s for s in skills}

        # Reorder skills: JD-matched first
        matched_skills = []
        other_skills = []
        keywords_added = []

        for skill in skills:
            if normalize_skill(skill) in {normalize_skill(k) for k in jd_keywords}:
                matched_skills.append(skill)
            else:
                other_skills.append(skill)

        reordered_skills = matched_skills + other_skills

        return TailoringResult(
            modified_sections={"skills": reordered_skills},
            keywords_added=keywords_added,
            sections_changed=["skills"],
            tailoring_notes=f"Reordered skills to prioritize {len(matched_skills)} JD-matched skills.",
            confidence_score=0.4,
        )
