from __future__ import annotations
import re

# Common stop words to exclude from keyword extraction
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall", "can", "need",
    "dare", "ought", "used", "this", "that", "these", "those", "i", "you",
    "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "what", "which", "who",
    "whom", "when", "where", "why", "how", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "just", "about",
    "above", "after", "again", "also", "as", "if", "into", "through",
    "during", "before", "between", "up", "down", "out", "off", "over",
    "under", "then", "once", "here", "there", "any", "work", "working",
    "experience", "ability", "able", "including", "etc", "using", "new",
    "well", "role", "team", "company", "looking", "join", "opportunity",
}


def extract_keywords(text: str, min_length: int = 2, max_count: int = 50) -> list[str]:
    """Extract meaningful keywords from text using frequency analysis."""
    # Normalize text
    text = text.lower()
    # Split into words, keeping compound terms
    words = re.findall(r"\b[a-z][a-z+#./-]+\b", text)

    # Count frequencies, excluding stop words
    freq = {}
    for word in words:
        if word not in STOP_WORDS and len(word) >= min_length:
            freq[word] = freq.get(word, 0) + 1

    # Sort by frequency
    sorted_keywords = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [word for word, _ in sorted_keywords[:max_count]]


def extract_years_of_experience(text: str) -> int | None:
    """Extract required years of experience from job description."""
    patterns = [
        r"(\d+)\+?\s*(?:years?|yrs?)[\s\w]*(?:of\s+)?experience",
        r"experience[\s:]*(\d+)\+?\s*(?:years?|yrs?)",
        r"minimum\s+(?:of\s+)?(\d+)\s*(?:years?|yrs?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def normalize_skill(skill: str) -> str:
    """Normalize a skill name for comparison."""
    return skill.lower().strip().replace(".", "").replace("-", " ").replace("_", " ")


# Common skill synonyms for matching
SKILL_SYNONYMS = {
    "javascript": {"js", "ecmascript"},
    "typescript": {"ts"},
    "python": {"py"},
    "react": {"reactjs", "react.js"},
    "node": {"nodejs", "node.js"},
    "vue": {"vuejs", "vue.js"},
    "angular": {"angularjs", "angular.js"},
    "postgres": {"postgresql", "pg"},
    "mongo": {"mongodb"},
    "redis": {"redis db"},
    "aws": {"amazon web services"},
    "gcp": {"google cloud", "google cloud platform"},
    "azure": {"microsoft azure"},
    "docker": {"containerization"},
    "k8s": {"kubernetes"},
    "ci/cd": {"cicd", "continuous integration", "continuous deployment"},
    "ml": {"machine learning"},
    "ai": {"artificial intelligence"},
    "nlp": {"natural language processing"},
    "sql": {"structured query language"},
    "nosql": {"non relational"},
    "rest": {"restful", "rest api"},
    "graphql": {"gql"},
}
