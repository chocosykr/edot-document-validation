"""Test-suite configuration.

Tests must be hermetic: they never depend on the `.env` local/frontier toggle
and never spend frontier credits. Force LOCAL mode for the whole session
(`config.py` now loads `.env` with override=False, so this wins). Individual
tests may still patch the environment to exercise the frontier routing.
"""

import os
import sys

from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ["USE_LOCAL_LLM_ONLY"] = "true"
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("OCR_API_URL", "http://test.invalid/ocr")
os.environ.setdefault("VALIDATION_MAX_ATTEMPTS", "3")
