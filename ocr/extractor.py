import json
import logging
import os
import time
from typing import TypedDict

import requests

from utils.llm_client import get_llm_config

logger = logging.getLogger(__name__)


class RawLookupCredentials(TypedDict, total=False):
    """
    REAL lookup values (document number, DOB, and document-sourced identity
    fields) taken from the raw extraction.

    A deliberately distinct type from RedactedProfile: passing one where the
    other is expected should be a type error, not a silent runtime bug that
    submits redaction tokens like [DOCUMENT_NUMBER] to a live registry.
    A key is absent when no valid value was extracted; values are validated
    non-token strings by split_credentials.

    passport_number and serial_number are first-class credential keys since
    September 2026: registries such as Myanmar's DMA require the holder's
    passport number and the certificate serial as LOOKUP inputs. They are
    document-sourced identity fields (printed on the document or its
    annexes), so they belong here — without them the extraction silently
    dropped perfectly-read values and the method could never be fed.
    """
    document_number: str
    date_of_birth: str
    full_name: str
    passport_number: str
    serial_number: str
    cdc_number: str


class RedactedProfile(TypedDict, total=False):
    """
    The PII-free document profile downstream of split_credentials. All values
    are either redaction tokens ([DOCUMENT_NUMBER]-style), non-PII facts, or
    absent. A RedactedProfile must NEVER carry real document numbers or DOB.
    """
    document_type: str
    document_type_key: str
    issuing_country: str
    document_number: str  # redaction token, e.g. "[DOCUMENT_NUMBER]"
    date_of_birth: str    # redaction token, e.g. "[DATE_OF_BIRTH]"
    full_name: str        # redaction token
    identifying_fields: dict
    redaction_summary: dict

# The single in-memory field that may carry real PII (used by the engine to
# build executor inputs). Everything else in the extraction output is
# redacted. Credentials are never persisted and never logged — see
# prompts/local_extraction.txt ("RAW LOOKUP CREDENTIALS" section).
CREDENTIALS_KEY = "raw_lookup_credentials"


def load_prompt() -> str:
    with open("prompts/local_extraction.txt", "r", encoding="utf-8") as file:
        return file.read()


def extract_and_redact(ocr_result: dict) -> dict:

    prompt = load_prompt()

    prompt += "\n\nOCR OUTPUT:\n"
    prompt += json.dumps(
        ocr_result,
        ensure_ascii=False,
        indent=2
    )

    # Shared local/frontier toggle (utils.llm_client.get_llm_config): the
    # endpoint is always LLM_URL; LLM_MODEL selects the model.
    config = get_llm_config()

    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0
    }

    # Bounded retry loop: the extraction call intermittently returns an
    # EMPTY body with finish_reason=stop (observed repeatedly for one
    # document during a folder run — and never host-side for smaller
    # prompts), which crashes json.loads. The empty-completion is not
    # deterministic, so re-asking the identical question recovers it; up to
    # 3 attempts with a short backoff.
    log = logging.getLogger(__name__)
    content = None
    last_error: Exception | None = None
    attempts = max(1, int(os.getenv("LLM_MAX_ATTEMPTS", "4")))
    transient_status = {408, 409, 429, 500, 502, 503, 504}
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                config["url"],
                headers=headers,
                json=payload,
                timeout=180,
            )
        except requests.RequestException as e:
            # Transient transport failure — retry rather than crash the whole
            # run. (Previously a provider 503 here aborted extraction.)
            last_error = e
            log.warning("Extraction attempt %d/%d transport error: %s", attempt, attempts, e)
            time.sleep(min(30, 2 * attempt))
            continue
        if response.status_code in transient_status:
            last_error = requests.HTTPError(
                f"{response.status_code} {response.reason}", response=response
            )
            log.warning(
                "Extraction attempt %d/%d transient HTTP %s; retrying.",
                attempt, attempts, response.status_code,
            )
            time.sleep(min(30, 2 * attempt))
            continue
        response.raise_for_status()
        result = response.json()
        try:
            content = result["choices"][0]["message"]["content"]
            return parse_json_response(content)
        except (json.JSONDecodeError, TypeError, AttributeError, KeyError) as e:
            last_error = e
            finish = (result.get("choices") or [{}])[0].get("finish_reason")
            log.warning(
                "Extraction attempt %d/%d returned unusable content "
                "(finish_reason=%r, len=%r): %s",
                attempt, attempts, finish, len(content or ""), e,
            )
            time.sleep(2 * attempt)
    raise ValueError(
        f"Extraction LLM returned no parsable content after {attempts} attempts: {last_error}"
    )


def split_credentials(extracted: dict) -> tuple[RedactedProfile, RawLookupCredentials]:
    """
    Separate the redacted profile from the real lookup credentials.

    Returns (redacted_profile, credentials) as DISTINCT types: the profile is
    a RedactedProfile (tokens only), the credentials a RawLookupCredentials
    (real values only). The credentials dict is stripped out of the profile
    so downstream components (discovery agent, LLM prompts, reports, logs)
    never see the real values. Credentials exist in memory only and are
    consumed by the engine when building executor inputs.
    """
    creds = extracted.pop(CREDENTIALS_KEY, None)
    if not isinstance(creds, dict):
        creds = {}

    # Defense in depth: a redaction token must never ride along as a
    # "real" value, and values must be non-empty strings. A key with no
    # valid value is OMITTED (not empty) so the established contract —
    # empty credentials dict when nothing valid was extracted — is kept.
    clean: RawLookupCredentials = RawLookupCredentials()
    # Strict allowlist, NOT a passthrough filter: this block exists solely for
    # the real lookup values the extraction prompt enumerates (see
    # prompts/local_extraction.txt). Anything outside these keys is extractor
    # noise or mis-filed OCR text (a stray name/address/vessel fragment) that
    # must never masquerade as a "real" credential downstream — membership in
    # this dict is treated as ground truth by the engine (executor inputs,
    # field comparison) and by the debug-log PII scrub in main.py. If the
    # extraction prompt ever gains a fourth lookup key, extend this tuple
    # WITH the TypedDict and the test in tests/test_extractor_split.py.
    for key in ("document_number", "date_of_birth", "full_name",
                "passport_number", "serial_number", "cdc_number"):
        value = creds.get(key)
        if isinstance(value, str) and value.strip() and not value.strip().startswith("["):
            clean[key] = value.strip()

    return extracted, clean


def parse_json_response(content: str) -> dict:

    content = (content or "").strip()

    if content.startswith("```"):
        content = content.replace("```json", "")
        content = content.replace("```", "")
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # The model sometimes annotates the JSON ("Here is the extracted
    # document ..." before it, commentary after it) — fall back to
    # brace-counting extraction of the first {...} block.
    from utils.llm_client import _extract_json
    parsed = _extract_json(content)
    if isinstance(parsed, dict):
        return parsed
    raise json.JSONDecodeError("no JSON object found in content", content or "", 0)
