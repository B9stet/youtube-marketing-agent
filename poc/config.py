from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".agent"
PROFILE_DIR = STATE_DIR / "profiles" / "youtube1"
PROFILES = ("youtube1", "youtube2", "youtube3")
CACHE_DIR = ROOT / ".cache"
OUTPUT_DIR = ROOT / "output"
DB_PATH = STATE_DIR / "poc.sqlite3"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(CACHE_DIR / "playwright"))
AI_BASE_URL = os.environ.get("POC_AI_BASE_URL", "http://127.0.0.1:10100/v1").rstrip("/")
AI_MODEL = os.environ.get("POC_AI_MODEL", "google-vertex/gemini-3.8-flash")
DAILY_LIMIT = 10


def profile_directory(profile: str) -> Path:
    if profile not in PROFILES:
        raise ValueError("유튜브 프로필을 확인해 주세요.")
    return STATE_DIR / "profiles" / profile
