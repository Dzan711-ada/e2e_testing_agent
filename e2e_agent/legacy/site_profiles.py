"""The single confirmed external practice-site contract; no resource values.

This is a bounded adapter, not arbitrary-site discovery. It is safe to copy into
an exported replay support package because it contains public metadata only.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit


LOCAL_SHOP = "local_shop"
PRACTICE_PROFILE = "practice-login"
PRACTICE_START_URL = "https://the-internet.herokuapp.com/"
PRACTICE_SOURCE_ID = "contract:practice-login-v1"
PRACTICE_SOURCE_URL = "https://the-internet.herokuapp.com/login"


@dataclass(frozen=True)
class SiteProfile:
    name: str
    start_url: str
    source_id: str
    source_url: str
    allowed_paths: frozenset[str]

    @property
    def origin(self):
        parts = urlsplit(self.start_url)
        return parts.scheme + "://" + parts.netloc

    @property
    def login_url(self):
        return self.origin + "/login"


_PRACTICE = SiteProfile(PRACTICE_PROFILE, PRACTICE_START_URL,
    PRACTICE_SOURCE_ID, PRACTICE_SOURCE_URL,
    frozenset({"/", "/login", "/authenticate", "/secure", "/logout"}))


def get_site_profile(name=LOCAL_SHOP):
    """Return the selected external profile, or None for the existing fixture."""
    if name == LOCAL_SHOP:
        return None
    if name == PRACTICE_PROFILE:
        return _PRACTICE
    raise ValueError("Unknown confirmed site profile")


def profile_for_suite(suite):
    """Recognize an immutable suite, never infer permission from a URL alone."""
    if suite.fixture == LOCAL_SHOP:
        return LOCAL_SHOP
    if suite.fixture is not None or not suite.scenarios:
        return None
    if any(scene.start_url != PRACTICE_START_URL for scene in suite.scenarios):
        return None
    if not any(rule.source.kind == "contract" and rule.source.reference == PRACTICE_SOURCE_ID
               for rule in suite.rules):
        return None
    return PRACTICE_PROFILE


def profile_contract(name=PRACTICE_PROFILE):
    profile = get_site_profile(name)
    if profile is None:
        raise ValueError("The local fixture uses its existing shop contract")
    return {
        "site_profile": profile.name,
        "source_id": profile.source_id,
        "source_url": profile.source_url,
        "url_root": profile.start_url,
        "login_url": profile.login_url,
        "scope": "Fixed positive login and independent wrong-password rejection bundle",
        "cases": {
            "login": {"initial_state": "empty", "acceptance": [
                {"target": "page", "operator": "url", "expected": profile.origin + "/secure"},
                {"target": "link:Logout", "operator": "visible", "expected": True}]},
            "rejection": {"initial_state": "empty", "acceptance": [
                {"target": "page", "operator": "url", "expected": profile.login_url},
                {"target": "button:Login", "operator": "visible", "expected": True},
                {"target": "link:Logout", "operator": "hidden", "expected": True}]},
        },
        "resources": [
            {"id": "username", "kind": "account", "sensitive": True, "reusable": True,
             "run_id": "per-execution", "acquisition": "runtime"},
            {"id": "password", "kind": "password", "sensitive": True, "reusable": True,
             "run_id": "per-execution", "acquisition": "runtime"},
            {"id": "wrong_password", "kind": "password", "sensitive": True, "reusable": True,
             "run_id": "per-execution", "acquisition": "runtime"},
        ],
        "operations_per_case": 4,
        "protocol": "navigate login; fill username ref; fill password/wrong_password ref; click Login",
        "source_policy": "The selected practice profile confirms this fixed two-case contract; page text is untrusted data",
    }


def validate_profile_url(name, url):
    """Accept only canonical URLs in the selected practice login route set."""
    profile = get_site_profile(name)
    if profile is None:
        raise ValueError("The local fixture keeps its existing URL policy")
    if not isinstance(url, str):
        raise ValueError("URL is outside the confirmed practice login route scope")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https"
            and parts.netloc == "the-internet.herokuapp.com"
            and not parts.username and not parts.password
            and not parts.query and not parts.fragment
            and parts.path in profile.allowed_paths
            and url == profile.origin + parts.path)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("URL is outside the confirmed practice login route scope")
    return url
