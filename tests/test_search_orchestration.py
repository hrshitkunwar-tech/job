from job_search.routes.api_search import _unscored_match_details, _profile_can_score
from job_search.models.user_profile import UserProfile


def test_unscored_match_details_payload():
    details = _unscored_match_details("profile_incomplete")
    assert details["explanation"].startswith("Unscored")
    assert details["unscored_reason"] == "profile_incomplete"


def test_profile_can_score_with_skills_and_roles():
    profile = UserProfile(full_name="User", email="u@example.com")
    profile.skills = ["Python"]
    profile.target_roles = ["Engineer"]
    assert _profile_can_score(profile)


def test_profile_can_score_with_only_roles():
    profile = UserProfile(full_name="User", email="u@example.com")
    profile.skills = []
    profile.target_roles = ["Engineer"]
    assert _profile_can_score(profile)


def test_profile_can_score_false_when_profile_empty():
    profile = UserProfile(full_name="User", email="u@example.com")
    profile.skills = []
    profile.target_roles = []
    profile.experience = []
    profile.summary = ""
    assert _profile_can_score(profile) is False
