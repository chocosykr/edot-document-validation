import json
import logging
from typing import TypedDict

import requests

from config import (
    LLM_URL,
    LLM_MODEL,
    LLM_API_KEY
)

logger = logging.getLogger(__name__)


class RawLookupCredentials(TypedDict, total=False):
    """
    REAL lookup values (document number, DOB) taken from the raw extraction.

    A deliberately distinct type from RedactedProfile: passing one where the
    other is expected should be a type error, not a silent runtime bug that
    submits redaction tokens like [DOCUMENT_NUMBER] to a live registry.
    Only these two keys may ever be present (a key is absent when no valid
    value was extracted); values are validated non-token strings by
    split_credentials.
    """
    document_number: str
    date_of_birth: str
    full_name: str


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

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0
    }

    response = requests.post(
        LLM_URL,
        headers=headers,
        json=payload,
        timeout=180
    )

    response.raise_for_status()

    result = response.json()

    content = result["choices"][0]["message"]["content"]

    return parse_json_response(content)


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
    for key in ("document_number", "date_of_birth", "full_name"):
        value = creds.get(key)
        if isinstance(value, str) and value.strip() and not value.strip().startswith("["):
            clean[key] = value.strip()

    return extracted, clean


def parse_json_response(content: str) -> dict:

    content = content.strip()

    if content.startswith("```"):
        content = content.replace("```json", "")
        content = content.replace("```", "")
        content = content.strip()

    return json.loads(content)
