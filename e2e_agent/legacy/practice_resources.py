"""Project-side public demo provider, intentionally excluded from replay support.

The practice page publishes these demonstration credentials. Their values still
enter runtime through resource references and never become model/plan inputs.
"""
from .site_profiles import PRACTICE_PROFILE, get_site_profile


def public_demo_resources(name=PRACTICE_PROFILE):
    profile = get_site_profile(name)
    if profile is None:
        raise ValueError("Public practice resources require the practice login profile")
    return {"username": "tomsmith", "password": "SuperSecretPassword!",
            "wrong_password": "InvalidPracticePassword!"}
