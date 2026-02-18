from pydantic import BaseModel
from typing import Optional, Union, List


class SearchQueryCreate(BaseModel):
    name: str
    keywords: str
    locations: Optional[str] = None  # JSON string
    work_types: Optional[str] = None  # JSON string
    experience_levels: Optional[str] = None  # JSON string
    date_posted: Optional[str] = None
    easy_apply_only: bool = True


class SearchQueryResponse(SearchQueryCreate):
    id: int
    is_active: bool = True
    results_count: int = 0

    model_config = {"from_attributes": True}


class SearchRunRequest(BaseModel):
    keywords: Union[str, List[str]]
    locations: Optional[List[str]] = None
    work_types: Optional[List[str]] = None
    experience_levels: Optional[List[str]] = None
    date_posted: Optional[str] = None
    easy_apply_only: bool = True
    limit: int = 5
    portals: Optional[List[str]] = ["linkedin"]  # Default to linkedin
    custom_portal_urls: Optional[List[str]] = None
