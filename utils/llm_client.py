import os
import json
import re
import time
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

# Frontier mode (used when USE_LOCAL_LLM_ONLY is explicitly false) targets the
# SAME gateway as the local model — one URL, one API key — and only swaps the
# model name. `LLM_URL` serves both `AI_Local` (local) and
# `gemini/gemini-2.5-flash` (hosted frontier), so no second provider or key is
# needed.
FRONTIER_LLM_MODEL = os.getenv("FRONTIER_LLM_MODEL", "gemini/gemini-2.5-flash")


def _local_config() -> Dict[str, Any]:
    return {
        "url": os.getenv("LLM_URL") or LLM_URL,
        "model": os.getenv("LLM_MODEL") or LLM_MODEL or "AI_Local",
        "api_key": os.getenv("LLM_API_KEY") or LLM_API_KEY,
    }


def _frontier_config() -> Dict[str, Any]:
    """Frontier config: the local gateway with a different model name.

    The gateway at ``LLM_URL`` exposes both the local model (``AI_Local``) and
    the hosted frontier model (``gemini/gemini-2.5-flash``) behind one URL and
    one API key, so frontier mode only swaps ``LLM_MODEL`` for
    ``FRONTIER_LLM_MODEL``. ``FRONTIER_LLM_URL`` / ``FRONTIER_LLM_API_KEY``
    remain overrides for pointing at a genuinely different provider, but the
    default keeps everything on the same endpoint as local.
    """
    return {
        "url": os.getenv("FRONTIER_LLM_URL") or os.getenv("LLM_URL") or LLM_URL,
        "model": os.getenv("FRONTIER_LLM_MODEL") or FRONTIER_LLM_MODEL,
        "api_key": (
            os.getenv("FRONTIER_LLM_API_KEY")
            or os.getenv("LLM_API_KEY")
            or LLM_API_KEY
        ),
    }


def get_llm_config() -> Dict[str, Any]:
    """Single source of truth for the local/frontier model selection.

    EVERY LLM call site resolves its request config here so one `.env` switch
    drives them all:

      - ``USE_LOCAL_LLM_ONLY=true``  -> LOCAL mode: ``LLM_URL`` + ``LLM_MODEL``
        (``AI_Local``).
      - ``USE_LOCAL_LLM_ONLY=false`` -> FRONTIER mode: the SAME ``LLM_URL``
        gateway with ``FRONTIER_LLM_MODEL`` (default ``gemini/gemini-2.5-flash``)
        and the same ``LLM_API_KEY``. ``FRONTIER_LLM_URL`` /
        ``FRONTIER_LLM_MODEL`` / ``FRONTIER_LLM_API_KEY`` override the triple.

    Unset (or any value other than an explicit false) keeps the historical
    LOCAL behaviour, so the switch is opt-in.

    Values are read at CALL time, not import time, so a process that changes
    the env before calling (or a test that sets the toggle) is honoured.

    Returns ``is_frontier`` so callers can scrub PII before anything leaves
    the machine (local never needs it; frontier always does).
    """
    raw_toggle = (os.getenv("USE_LOCAL_LLM_ONLY", "true") or "").strip().lower()
    use_frontier = raw_toggle in ("false", "0", "no", "off")
    config = _frontier_config() if use_frontier else _local_config()
    config["use_local_only"] = not use_frontier
    config["is_frontier"] = use_frontier
    return config


# Transient provider failures (rate limits, gateway/overload) are retried with
# backoff; a definitive 4xx is not. Frontier endpoints return 503 under load
# (observed live on Gemini), which otherwise aborts an entire run.
_TRANSIENT_HTTP = {408, 409, 429, 500, 502, 503, 504}


def _should_scrub(config: Dict[str, Any]) -> bool:
    """Scrub PII on frontier calls unless explicitly disabled.

    Default is ON (safe): PII never leaves the machine in frontier mode. Set
    ``DVS_SCRUB_FRONTIER=false`` to let frontier calls see real values — needed
    by the response-field mapping / ambiguous-band judge, which compare the
    document's actual values against the registry response.
    """
    if not config.get("is_frontier"):
        return False
    return os.getenv("DVS_SCRUB_FRONTIER", "true").strip().lower() != "false"


def _backoff(attempt: int) -> None:
    time.sleep(min(30.0, 1.5 * (2 ** (attempt - 1))))


def _post_chat(url: str, headers: dict, payload: dict, timeout: int = 60) -> Optional[str]:
    """POST an OpenAI-compatible chat payload; retry transient failures.

    Returns the assistant message content, or None after exhausting retries or
    on a definitive (non-transient) failure.
    """
    attempts = max(1, int(os.getenv("LLM_MAX_ATTEMPTS", "4")))
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return (data["choices"][0]["message"]["content"] or "").strip()
        except urllib.error.HTTPError as e:
            if e.code in _TRANSIENT_HTTP and attempt < attempts:
                # Honour the provider's Retry-After when present (frontier
                # endpoints return it on 429); otherwise exponential backoff.
                retry_after = None
                try:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                except Exception:
                    retry_after = None
                delay = None
                try:
                    delay = float(retry_after) if retry_after else None
                except (TypeError, ValueError):
                    delay = None
                logger.warning(
                    "LLM transient HTTP %s; retry %d/%d (after %ss)",
                    e.code, attempt, attempts, delay if delay else "backoff",
                )
                if delay and delay > 0:
                    time.sleep(min(30.0, delay))
                else:
                    _backoff(attempt)
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            logger.warning("LLM HTTP Error: %s %s %s", e.code, e.reason, body)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < attempts:
                logger.warning(
                    "LLM network error (%s); retry %d/%d",
                    getattr(e, "reason", e), attempt, attempts,
                )
                _backoff(attempt)
                continue
            logger.warning("LLM URL Error: %s", getattr(e, "reason", e))
            return None
        except Exception as e:
            logger.warning("LLM unexpected error: %s", e)
            return None
    return None


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

    # Frontier mode sends data off the machine — scrub PII first (unless the
    # operator opted out via DVS_SCRUB_FRONTIER=false). Local is not scrubbed.
    if _should_scrub(config):
        from utils.log_scrubber import scrub_pii
        system_prompt = scrub_pii(system_prompt)
        user_prompt = scrub_pii(user_prompt)

    logger.info("LLM call: model=%r endpoint=%r frontier=%s", model, url, config["is_frontier"])

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

    content = _post_chat(url, headers, payload)
    if content is None:
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

    if _should_scrub(config):
        from utils.log_scrubber import scrub_pii
        prompt = scrub_pii(prompt)

    logger.info(
        "vision_call: model=%r endpoint=%r frontier=%s",
        config["model"], config["url"], config["is_frontier"],
    )

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

    content = _post_chat(
        config["url"],
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        payload,
        timeout=max(1, int(timeout_s)),
    )
    if content is None:
        logger.warning("vision_call failed after retries")
        return ""
    return content

