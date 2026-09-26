import os
import json
import re
import urllib.request
import urllib.error
import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load env variables (assumes .env is in project root)
load_dotenv()

LLM_URL = os.getenv("LLM_URL", "https://ai.edot-solutions.com/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "AI_Local")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")


def get_llm_config() -> Dict[str, Any]:
    """Single source of truth for the local/frontier model-selection toggle.

    EVERY LLM call site in the codebase must resolve its request config here
    instead of reading the env itself, so the `.env` toggle drives them all
    identically:

      - The ENDPOINT never changes: it is always ``LLM_URL``.
      - ``LLM_MODEL`` is the switch: ``"AI_Local"`` selects local mode, a
        frontier model name (e.g. ``"gemini/gemini-2.5-flash"``) selects
        frontier mode. Same endpoint, different model in the payload.
      - There are no external providers and no fallback chain: this client
        only ever calls ``LLM_URL``. ``USE_LOCAL_LLM_ONLY`` is surfaced for
        callers/reporting but nothing escalates to a different model.

    Values are read at CALL time, not import time, so a process that changes
    the env before calling (or a test that sets the toggle) is honoured.
    """
    return {
        "url": os.getenv("LLM_URL") or LLM_URL,
        "model": os.getenv("LLM_MODEL") or LLM_MODEL or "AI_Local",
        "api_key": os.getenv("LLM_API_KEY") or LLM_API_KEY,
        "use_local_only": os.getenv("USE_LOCAL_LLM_ONLY", "false").lower() == "true",
    }


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract the first valid JSON object from text that may contain
    trailing commentary or markdown fences.
    """
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the outermost { ... } using brace counting
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None

    return None


def generate_json(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    """
    Call the configured OpenAI-compatible model endpoint and parse JSON.

    LOCAL-ONLY BY CONSTRUCTION: there is exactly one provider — ``LLM_URL``
    with the model selected by the ``LLM_MODEL`` toggle. The former Google AI
    Studio (native, hardcoded ``gemini-3.8-flash``) then Groq fallback chain
    was REMOVED, because it was a silent escalation to a frontier model on a
    local failure. If this call fails, the function returns None and the
    caller's existing failure handling applies — it never calls a different,
    costlier model.
    """
    config = get_llm_config()
    url = config["url"]
    model = config["model"]
    api_key = config["api_key"]

    logger.info("LLM call: model=%r endpoint=%r", model, url)

    if not url or not api_key:
        logger.error("LLM endpoint or API key is not configured; no call made.")
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        content = response_data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        logger.warning(f"LLM HTTP Error: {e.code} {e.reason}\n{e.read().decode('utf-8')}")
        return None
    except urllib.error.URLError as e:
        logger.warning(f"LLM URL Error: {e.reason}")
        return None
    except Exception as e:
        logger.warning(f"LLM unexpected error: {e}")
        return None

    result = _extract_json(content)
    if result is None:
        logger.error(f"Could not extract JSON from LLM response: {content[:200]}")
    return result


def vision_call(prompt: str, img_b64: str, timeout_s: int = 30) -> str:
    """Send an in-memory base64 image plus a prompt to the vision LLM.

    This is the ONE vision implementation in the codebase. It uses the same
    single endpoint (``LLM_URL``) and the same local/frontier toggle
    (``LLM_MODEL``) as every other call site — no hardcoded model name and no
    second endpoint. Used by the BROWSER executor's CAPTCHA solver and the
    HTTP executor's ``SOLVE_CAPTCHA`` step.

    Returns the model's text reply, or "" on any failure (the callers treat an
    empty reply as "could not solve", never as a document verdict).
    """
    config = get_llm_config()
    if not config["url"] or not config["api_key"]:
        logger.warning("vision_call skipped: LLM_URL/LLM_API_KEY not set")
        return ""

    logger.info("vision_call: model=%r endpoint=%r", config["model"], config["url"])

    payload = {
        "model": config["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        }],
        "temperature": 0,
        "max_tokens": 50,
    }

    req = urllib.request.Request(
        config["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout_s))) as response:
            data = json.loads(response.read().decode("utf-8"))
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.warning(f"vision_call failed: {e}")
        return ""

