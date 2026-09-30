"""Discovery LLM access: raw chat calls with rate-limit handling, prompt
loading, and JSON response parsing.

Split out of agent.py. The LLM key is validated at import time — importing
anything in the discovery package without LLM_API_KEY set fails fast (tests
set a dummy key before importing).
"""

import json
import time

import requests

from utils.llm_client import get_llm_config, _should_scrub

LLM_MAX_RETRIES = 3
LLM_MIN_INTERVAL_SECONDS = 1.0

last_llm_request_at = 0.0


if not get_llm_config()["api_key"]:
    raise ValueError(
        "LLM_API_KEY is not set in .env"
    )


# --------------------------------------------------
# Prompt loading
# --------------------------------------------------

def load_prompt(filename: str) -> str:
    with open(
        f"prompts/{filename}",
        "r",
        encoding="utf-8"
    ) as file:
        return file.read()


# --------------------------------------------------
# LLM API
# --------------------------------------------------

def call_llm(prompt: str) -> str:

    global last_llm_request_at

    # Shared toggle: endpoint is always LLM_URL; LLM_MODEL is the switch.
    config = get_llm_config()
    url = config["url"]
    model = config["model"]
    api_key = config["api_key"]

    print(f"[discovery] LLM call using model={model!r}")

    # This is a model EGRESS point. Discovery prompts carry live page text and
    # form input values, which can echo the real credential straight back (a
    # portal result page prints the number you searched for). Scrub here so a
    # frontier model only ever sees the KIND of credential, never a value.
    if _should_scrub(config):
        from utils.log_scrubber import scrub_pii
        prompt = scrub_pii(prompt)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0
    }

    for attempt in range(LLM_MAX_RETRIES + 1):

        elapsed = time.monotonic() - last_llm_request_at
        if elapsed < LLM_MIN_INTERVAL_SECONDS:
            time.sleep(
                LLM_MIN_INTERVAL_SECONDS - elapsed
            )

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=120
        )

        last_llm_request_at = time.monotonic()

        # Retry rate limits (429) AND transient provider failures (5xx); a
        # frontier endpoint returning 503 under load otherwise aborts the
        # whole discovery step.
        if response.status_code not in (429, 500, 502, 503, 504):
            break

        if attempt == LLM_MAX_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get(
            "Retry-After"
        )

        try:
            delay = float(retry_after) if retry_after else 0
        except (TypeError, ValueError):
            delay = 0
        if delay <= 0:
            # No/zero Retry-After header: back off exponentially instead of
            # hammering the provider immediately.
            delay = 2 ** (attempt + 1)

        print(
            f"LLM rate limit reached; retrying in "
            f"{delay:.0f}s..."
        )
        time.sleep(delay)

    response.raise_for_status()

    result = response.json()

    return result["choices"][0]["message"]["content"]


# --------------------------------------------------
# JSON parsing
# --------------------------------------------------

def parse_json_response(
    content: str
) -> dict:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, dict):

        raise ValueError(
            "LLM did not return a JSON object."
        )

    return result


def parse_json_array(
    content: str
) -> list[str]:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, list):

        raise ValueError(
            "LLM did not return a JSON array."
        )

    if not all(
        isinstance(query, str)
        for query in result
    ):

        raise ValueError(
            "All search queries must be strings."
        )

    return result
