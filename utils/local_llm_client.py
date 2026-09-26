"""Local-only JSON model calls for decisions involving raw document data."""

import json
import urllib.request
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from utils.llm_client import get_llm_config, _extract_json

load_dotenv()


def generate_local_json(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """Call the configured OpenAI-compatible model endpoint, single-shot.

    Uses the SAME local/frontier toggle as every other call site
    (`utils.llm_client.get_llm_config`): the endpoint is always `LLM_URL` and
    `LLM_MODEL` selects the model. The only difference from
    `utils.llm_client.generate_json` is that this path never falls back to a
    different provider — which is why it is used for the ambiguous-band
    judgment, where the payload carries raw document fields.
    """
    config = get_llm_config()
    url = config["url"]
    api_key = config["api_key"]
    model = config["model"]
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
        return _extract_json(content)
    except Exception:
        return None