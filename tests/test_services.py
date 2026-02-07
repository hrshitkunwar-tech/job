import pytest
from unittest.mock import AsyncMock, MagicMock
from job_search.services.resume_parser import ResumeParser
from job_search.services.resume_tailor import ResumeTailor
from job_search.services.job_matcher import JobMatcher

@pytest.fixture
def mock_llm_client():
    client = AsyncMock()
    return client

# @pytest.mark.asyncio
def test_resume_parser_regex_fallback():
    # Test without LLM client to trigger regex fallback
    parser = ResumeParser(llm_client=None)
    
    # Simple markdown-like text
    resume_text = """
    John Doe
    john.doe@example.com | 123-456-7890
    linkedin.com/in/johndoe
    
    SKILLS
    Python, FastAPI, SQL, Docker
    
    EXPERIENCE
    Software Engineer at Tech Corp
    Developed web applications using Python.
    - Built a robust API with FastAPI
    - Optimized SQL queries
    """
    
    # We need a file to parse, but since we are testing the extraction logic, 
    # we can mock the file reading parts or just test the _structure_with_regex directly.
    data = parser._structure_with_regex(resume_text)
    
    assert data["name"] == "John Doe"
    assert data["email"] == "john.doe@example.com"
    assert "Python" in data["skills"]
    assert "FastAPI" in data["skills"]

# @pytest.mark.asyncio
def test_job_matcher_scoring():
    matcher = JobMatcher()
    
    job = {
        "title": "Senior Python Developer",
        "description": "We are looking for a Python expert with Experience in FastAPI and PostgreSQL.",
        "location": "Remote",
        "work_type": "Remote"
    }
    
    profile = {
        "skills": ["Python", "FastAPI", "SQL"],
        "target_roles": ["Python Developer", "Software Engineer"],
        "target_locations": ["Remote"],
        "experience": [{"title": "Junior Dev"}] * 3 # 6 years approx
    }
    
    result = matcher.score_job(job, profile)
    
    assert result.overall_score > 5
    assert "Python" in result.matched_skills
    assert result.recommendation in ["good_match", "strong_match"]

@pytest.mark.asyncio
async def test_resume_tailor_llm(mock_llm_client):
    tailor = ResumeTailor(llm_client=mock_llm_client)
    
    mock_llm_client.complete_json.return_value = {
        "summary": "Experienced Python dev targeting Tech Corp.",
        "skills": ["Python", "FastAPI", "AWS"],
        "experience": [],
        "keywords_added": ["AWS"],
        "sections_changed": ["summary", "skills"],
        "tailoring_notes": "Added AWS as requested by JD."
    }
    
    resume_data = {
        "name": "John Doe",
        "skills": ["Python", "FastAPI"],
        "experience": []
    }
    
    result = await tailor.tailor(
        resume_data, 
        "Looking for Python/AWS dev", 
        "Python Developer", 
        "Tech Corp"
    )
    
    assert result.modified_sections["summary"] == "Experienced Python dev targeting Tech Corp."
    assert "AWS" in result.modified_sections["skills"]
    assert mock_llm_client.complete_json.called
