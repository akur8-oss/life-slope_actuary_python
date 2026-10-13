import os
from pathlib import Path
from dotenv import load_dotenv

# Load SLOPE API credentials (and any optional settings such as SLOPE_WORKING_DIR / SLOPE_REPORTS_DIR)
# from a .env file at the repo root. Set SLOPE_ENV_FILE to point at a different file (e.g. to switch tenants).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = Path(os.environ.get("SLOPE_ENV_FILE", _REPO_ROOT / ".env"))
load_dotenv(_ENV_FILE)


# Returns the SLOPE API key and secret. Never log or print these values.
def get_api_credentials() -> tuple[str, str]:
    api_key = os.environ.get("SLOPE_API_KEY")
    api_secret = os.environ.get("SLOPE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            f"SLOPE_API_KEY and SLOPE_API_SECRET must be set. Copy 'env.example' to '{_ENV_FILE}' "
            "and fill in your SLOPE API credentials, or set SLOPE_ENV_FILE to point at your env file."
        )
    return api_key, api_secret
