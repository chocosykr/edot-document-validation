import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Real environment variables win over .env (standard precedence), so the
# local/frontier switch can be flipped per-run via the environment without
# editing .env. .env still provides values when the env does not set them.
load_dotenv(BASE_DIR / ".env", override=False)

OCR_API_URL = os.getenv("OCR_API_URL")

LLM_URL = os.getenv("LLM_URL")
LLM_MODEL = os.getenv("LLM_MODEL")
LLM_API_KEY = os.getenv("LLM_API_KEY")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# Healing ladder budget (validation/validator.py reads this). Raising it
# gives the browser escalation run 1 (cheap rewrite) + run 2 (browser
# authoring) + at least one retest of the browser method.
VALIDATION_MAX_ATTEMPTS = os.getenv("VALIDATION_MAX_ATTEMPTS", "4")