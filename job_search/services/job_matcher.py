from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from job_search.utils.text_processing import (
    extract_keywords,
    extract_years_of_experience,
    normalize_skill,
    SKILL_SYNONYMS,
)

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    overall_score: float = 0.0
    skill_score: float = 0.0
    title_score: float = 0.0
    experience_score: float = 0.0
    location_score: float = 0.0
    keyword_score: float = 0.0
    vibe_score: float = 0.0
    vibe_explanation: str = ""
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    extracted_keywords: list[str] = field(default_factory=list)
    recommendation: str = "poor_match"
    explanation: str = ""


class JobMatcher:
    WEIGHTS = {
        "skill": 0.25,
        "vibe": 0.35,
        "title": 0.20,
        "experience": 0.05,
        "location": 0.05,
        "keyword": 0.10,
    }

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def _build_effective_skills(self, profile: dict) -> list[str]:
        """Build a richer skill list from explicit skills + profile text."""
        explicit_skills = profile.get("skills", []) or []
        target_roles = profile.get("target_roles", []) or []
        summary = profile.get("summary", "") or ""
        headline = profile.get("headline", "") or ""
        experience = profile.get("experience", []) or []

        merged: list[str] = []
        seen: set[str] = set()

        def add_term(term: str):
            term = (term or "").strip()
            if not term:
                return
            norm = normalize_skill(term)
            if not norm or norm in seen:
                return
            seen.add(norm)
            merged.append(term)

        for skill in explicit_skills:
            if isinstance(skill, str):
                add_term(skill)

        for role in target_roles:
            if not isinstance(role, str):
                continue
            add_term(role)
            for token in re.split(r"[\s/|,&()-]+", role):
                if len(token) >= 4:
                    add_term(token)

        profile_text_parts = [summary, headline]
        if isinstance(experience, list):
            for item in experience[:8]:
                if isinstance(item, str):
                    profile_text_parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                for field in ("title", "company", "description"):
                    value = item.get(field)
                    if isinstance(value, str):
                        profile_text_parts.append(value)
                bullets = item.get("bullets")
                if isinstance(bullets, list):
                    profile_text_parts.extend([b for b in bullets if isinstance(b, str)])

        extracted = extract_keywords(" ".join(profile_text_parts), min_length=4, max_count=40)
        for kw in extracted:
            if len(kw) >= 4:
                add_term(kw)

        return merged

    def score_job(self, job: dict, profile: dict) -> MatchResult:
        """Score a job against user profile using keyword matching."""
        description = job.get("description", "")
        user_skills = self._build_effective_skills(profile)
        target_roles = profile.get("target_roles", [])
        target_locations = profile.get("target_locations", [])
        user_experience = profile.get("experience", [])

        skill_score, matched, missing = self._score_skills(description, user_skills)
        title_score = self._score_title(job.get("title", ""), target_roles)
        experience_score = self._score_experience(description, user_experience)
        location_score = self._score_location(
            job.get("location", ""), job.get("work_type", ""), target_locations
        )
        vibe_score, vibe_explanation = self._score_vibe(description, profile)

        keywords = extract_keywords(description)
        keyword_score = self._score_keyword_overlap(keywords, user_skills)

        overall = (
            skill_score * self.WEIGHTS["skill"]
            + vibe_score * self.WEIGHTS["vibe"]
            + title_score * self.WEIGHTS["title"]
            + experience_score * self.WEIGHTS["experience"]
            + location_score * self.WEIGHTS["location"]
            + keyword_score * self.WEIGHTS["keyword"]
        )

        if overall >= 75:
            recommendation = "strong_match"
        elif overall >= 60:
            recommendation = "good_match"
        elif overall >= 40:
            recommendation = "weak_match"
        else:
            recommendation = "poor_match"

        return MatchResult(
            overall_score=round(overall, 1),
            skill_score=round(skill_score, 1),
            vibe_score=round(vibe_score, 1),
            vibe_explanation=vibe_explanation,
            title_score=round(title_score, 1),
            experience_score=round(experience_score, 1),
            location_score=round(location_score, 1),
            keyword_score=round(keyword_score, 1),
            matched_skills=matched,
            missing_skills=missing,
            extracted_keywords=keywords[:20],
            recommendation=recommendation,
            explanation=f"Overall {recommendation.replace('_', ' ')}: {round(overall)}% match. "
                        f"Vibe Score: {round(vibe_score, 1)}. "
                        f"{vibe_explanation} "
                        f"{len(matched)} of your skills matched.",
        )

    def _score_skills(self, description: str, user_skills: list[str]) -> tuple:
        desc_lower = description.lower()
        matched = []
        missing = []

        # Build reverse synonym map
        synonyms_map = {}
        for canonical, syns in SKILL_SYNONYMS.items():
            synonyms_map[canonical] = syns
            for syn in syns:
                synonyms_map[syn] = {canonical} | syns

        for skill in user_skills:
            norm = normalize_skill(skill)
            found = norm in desc_lower

            if not found and norm in synonyms_map:
                found = any(syn in desc_lower for syn in synonyms_map[norm])

            if found:
                matched.append(skill)
            else:
                missing.append(skill)

        if not user_skills:
            return 50.0, matched, missing

        # Instead of dividing by total user skills (which penalizes broad profiles),
        # we check how many of the profile skills are found. 
        # Most JDs mention 6-10 keywords. Matching 8 is a "perfect" score.
        target_match_count = 8
        score = (len(matched) / target_match_count) * 100
        return min(score, 100.0), matched, missing

    def _score_title(self, job_title: str, target_roles: list[str]) -> float:
        if not target_roles:
            return 50.0

        job_words = set(job_title.lower().split())
        best_score = 0.0

        for role in target_roles:
            role_words = set(role.lower().split())
            if not role_words:
                continue
            overlap = len(job_words & role_words)
            score = (overlap / len(role_words)) * 100
            best_score = max(best_score, score)

        return min(best_score, 100.0)

    def _score_experience(self, description: str, user_experience: list[dict]) -> float:
        required_years = extract_years_of_experience(description)
        if required_years is None:
            return 70.0  # Neutral if not specified

        # Calculate user's total years from experience entries
        total_years = len(user_experience) * 2  # Rough estimate: 2 years per position

        if total_years >= required_years:
            return 100.0
        elif total_years >= required_years * 0.7:
            return 70.0
        elif total_years >= required_years * 0.5:
            return 40.0
        return 20.0

    def _score_location(self, job_location: str, work_type: str, target_locations: list[str]) -> float:
        if not target_locations:
            return 50.0

        # Remote jobs match anyone looking for remote
        if work_type and "remote" in work_type.lower():
            if any("remote" in loc.lower() for loc in target_locations):
                return 100.0
            return 80.0  # Remote is generally desirable

        if not job_location:
            return 50.0

        job_loc_lower = job_location.lower()
        for loc in target_locations:
            if loc.lower() in job_loc_lower or job_loc_lower in loc.lower():
                return 100.0

        return 20.0

    def _score_keyword_overlap(self, jd_keywords: list[str], user_skills: list[str]) -> float:
        if not jd_keywords or not user_skills:
            return 50.0

        user_normalized = {normalize_skill(s) for s in user_skills}
        matched = sum(1 for kw in jd_keywords[:20] if normalize_skill(kw) in user_normalized)
        return min((matched / min(len(jd_keywords), 20)) * 100, 100.0)

    def _score_vibe(self, description: str, profile: dict) -> tuple[float, str]:
        """Match job culture against the user's technical manifesto and working-style preferences."""
        vibe_keywords = [
            "autonomous", "agent", "frontier", "velocity", "ownership",
            "fast-paced", "zero to one", "founder", "ai-first", "agentic",
            "hacker", "sovereign", "builder", "startup"
        ]
        corporate_keywords = [
            "bureaucracy", "enterprise", "legacy", "red tape", "slow",
            "processes", "synergy", "corporate", "waterfall"
        ]

        desc_lower = description.lower()
        score = 50.0
        matched_signals: list[str] = []
        friction_signals: list[str] = []

        for kw in vibe_keywords:
            if kw in desc_lower:
                score += 15.0
                matched_signals.append(kw)

        for kw in corporate_keywords:
            if kw in desc_lower:
                score -= 10.0
                friction_signals.append(kw)

        manifesto = " ".join(
            filter(
                None,
                [
                    profile.get("technical_manifesto", ""),
                    profile.get("summary", ""),
                    profile.get("headline", ""),
                ],
            )
        ).lower()
        preferred_team_style = (profile.get("preferred_team_style") or "").lower()
        execution_preference = (profile.get("execution_preference") or "").lower()
        company_stage_preference = (profile.get("company_stage_preference") or "").lower()
        autonomy_preference = (profile.get("autonomy_preference") or "").lower()
        frontier_interest = profile.get("frontier_tech_interest") or 0

        startup_tokens = ["startup", "zero to one", "founder", "seed", "series a", "early stage"]
        frontier_tokens = ["frontier", "ai-first", "agent", "llm", "agents", "research"]
        ownership_tokens = ["ownership", "autonomous", "independent", "self-starter", "builder"]
        process_tokens = ["process", "structured", "compliance", "stakeholder", "cross-functional"]

        if preferred_team_style == "builder-led":
            if any(token in desc_lower for token in ownership_tokens):
                score += 12.0
                matched_signals.append("builder-led ownership")
            if "manager" in desc_lower and "approval" in desc_lower:
                score -= 6.0
                friction_signals.append("approval-heavy structure")
        elif preferred_team_style == "research-heavy":
            if any(token in desc_lower for token in frontier_tokens):
                score += 10.0
                matched_signals.append("research-forward environment")
        elif preferred_team_style == "mission-driven":
            if "mission" in desc_lower or "purpose" in desc_lower:
                score += 8.0
                matched_signals.append("mission-driven team")

        if execution_preference == "speed":
            if "fast-paced" in desc_lower or "velocity" in desc_lower:
                score += 10.0
                matched_signals.append("startup velocity")
            if any(token in desc_lower for token in process_tokens):
                score -= 5.0
                friction_signals.append("process-heavy execution")
        elif execution_preference == "process" and any(token in desc_lower for token in process_tokens):
            score += 6.0
            matched_signals.append("structured execution")

        if company_stage_preference == "startup" and any(token in desc_lower for token in startup_tokens):
            score += 12.0
            matched_signals.append("startup stage")
        elif company_stage_preference == "enterprise" and "enterprise" in desc_lower:
            score += 8.0
            matched_signals.append("enterprise scale")
        elif company_stage_preference == "growth" and ("series b" in desc_lower or "scale" in desc_lower):
            score += 8.0
            matched_signals.append("growth-stage scale")

        if autonomy_preference == "high" and any(token in desc_lower for token in ownership_tokens):
            score += 10.0
            matched_signals.append("high autonomy")
        elif autonomy_preference == "low" and any(token in desc_lower for token in process_tokens):
            score += 5.0
            matched_signals.append("guided execution")

        if frontier_interest >= 7 and any(token in desc_lower for token in frontier_tokens):
            score += 12.0
            matched_signals.append("frontier-tech exposure")
        elif frontier_interest <= 3 and any(token in desc_lower for token in frontier_tokens):
            score -= 3.0
            friction_signals.append("frontier-heavy role")

        if "sovereign" in manifesto and "sovereign" in desc_lower:
            score += 8.0
            matched_signals.append("sovereign systems")
        if "agent" in manifesto and "agent" in desc_lower:
            score += 8.0
            matched_signals.append("agentic workflows")
        if "velocity" in manifesto and "velocity" in desc_lower:
            score += 6.0
            matched_signals.append("developer velocity")

        score = min(max(score, 0.0), 100.0)
        # Python preserves insertion order; build a compact explanation without repeated labels.
        unique_matched = list(dict.fromkeys(matched_signals))
        unique_friction = list(dict.fromkeys(friction_signals))
        summary_parts = []
        if unique_matched:
            summary_parts.append(f"Aligned on {', '.join(unique_matched[:3])}")
        if unique_friction:
            summary_parts.append(f"Watchouts: {', '.join(unique_friction[:2])}")
        if not summary_parts:
            summary_parts.append("Neutral culture signal; role needs more manifesto detail")
        return score, ". ".join(summary_parts) + "."

    async def score_job_deep(self, job: dict, profile: dict) -> MatchResult:
        """LLM-assisted deep scoring."""
        if not self.llm_client:
            return self.score_job(job, profile)

        # Start with keyword-based scores as a baseline
        base_result = self.score_job(job, profile)

        prompt = f"""Analyze this job against the candidate profile and provide match scores.
Pay special attention to the "Vibe Score". The candidate is an autonomous agent builder who 
values "Sovereign AI", "Frontier Model Execution", and "Developer Velocity". Score the company culture 
and job description on how well it aligns with these vibes versus a traditional corporate bureaucracy.

Job Title: {job.get('title', '')}
Job Description:
{job.get('description', '')[:3000]}

Candidate Skills: {', '.join(profile.get('skills', []))}
Target Roles: {', '.join(profile.get('target_roles', []))}

Return JSON:
{{
    "overall_score": <0-100>,
    "skill_score": <0-100>,
    "vibe_score": <0-100>,
    "title_score": <0-100>,
    "experience_score": <0-100>,
    "explanation": "<2-3 sentence explanation including vibe analysis>",
    "missing_skills": ["skill1", "skill2"]
}}"""

        try:
            result = await self.llm_client.complete_json(prompt)
            base_result.overall_score = result.get("overall_score", base_result.overall_score)
            base_result.vibe_score = result.get("vibe_score", base_result.vibe_score)
            base_result.explanation = result.get("explanation", base_result.explanation)
            base_result.vibe_explanation = result.get("vibe_explanation", base_result.vibe_explanation or base_result.explanation)
            if result.get("missing_skills"):
                base_result.missing_skills = result["missing_skills"]
        except Exception as e:
            logger.warning(f"Deep scoring failed, using keyword scores: {e}")

        return base_result

    def batch_score(self, jobs: list[dict], profile: dict) -> list[MatchResult]:
        """Score multiple jobs using fast mode."""
        return [self.score_job(job, profile) for job in jobs]
