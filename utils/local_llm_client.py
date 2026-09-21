"""Local-only JSON model calls for decisions involving raw document data."""

import json
import os
import urllib.request
from urllib.parse import urlparse
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()


def generate_local_json(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """Call only the configured local OpenAI-compatible model endpoint."""
    # This deployment exposes the trusted local model through LLM_URL. Keep
    # this call path separate from utils.llm_client.generate_json so raw PII
    # never enters the provider-fallback logic.
    url = os.getenv("LOCAL_LLM_URL") or os.getenv("LLM_URL", "")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "AI_Local")
    host = urlparse(url).hostname if url else None
    if not url or not api_key:
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        from utils.llm_client import _extract_json
        return _extract_json(content)
    except Exception:
        return None