"""
Centralised defaults and static lookup tables for job application automation.

All values that were previously hard-coded inside JobApplier.__init__ and
scattered across method bodies live here as named constants. Nothing in this
module has side-effects; it is safe to import from anywhere.
"""

# ---------------------------------------------------------------------------
# Personal / contact defaults
# ---------------------------------------------------------------------------

DEFAULT_MOBILE_NUMBER: str = "9319135101"
DEFAULT_PHONE_COUNTRY_CODE: str = "+91"
DEFAULT_PHONE_EXTENSION: str = "0"
DEFAULT_PHONE_TYPE: str = "mobile"

# ---------------------------------------------------------------------------
# Location defaults
# ---------------------------------------------------------------------------

DEFAULT_COUNTRY: str = "India"
DEFAULT_CITY: str = "Bangalore"
DEFAULT_STATE: str = "Karnataka"
DEFAULT_ADDRESS_LINE_1: str = "HSR Layout"
DEFAULT_ADDRESS_LINE_2: str = "NA"
DEFAULT_POSTAL_CODE: str = "560102"

# ---------------------------------------------------------------------------
# Application source defaults
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_CHANNEL: str = "Social Media"
DEFAULT_SOURCE_PLATFORM: str = "LinkedIn"
DEFAULT_SOURCE_ANSWER: str = "LinkedIn"

# ---------------------------------------------------------------------------
# City → state lookup (Indian cities)
# Extracted from _location_parts — this is data, not logic.
# ---------------------------------------------------------------------------

CITY_STATE_MAP: dict[str, str] = {
    "new delhi": "Delhi",
    "delhi": "Delhi",
    "gurgaon": "Haryana",
    "gurugram": "Haryana",
    "noida": "Uttar Pradesh",
    "mumbai": "Maharashtra",
    "bengaluru": "Karnataka",
    "bangalore": "Karnataka",
    "hyderabad": "Telangana",
    "chennai": "Tamil Nadu",
    "pune": "Maharashtra",
    "kolkata": "West Bengal",
    "ahmedabad": "Gujarat",
    "jaipur": "Rajasthan",
}

# ---------------------------------------------------------------------------
# City → 6-digit postal code lookup
# Extracted from _postal_code_from_location_text — same pattern.
# ---------------------------------------------------------------------------

CITY_POSTAL_MAP: dict[str, str] = {
    "hsr layout": "560102",
    "new delhi": "110001",
    "delhi": "110001",
    "gurgaon": "122001",
    "gurugram": "122001",
    "noida": "201301",
    "mumbai": "400001",
    "bengaluru": "560102",
    "bangalore": "560102",
    "hyderabad": "500001",
    "chennai": "600001",
    "pune": "411001",
    "kolkata": "700001",
    "ahmedabad": "380001",
    "jaipur": "302001",
}
