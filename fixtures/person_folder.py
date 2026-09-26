"""
Person-folder fixtures
======================

A "person folder" is a person-scoped container for several independent
document fixtures (e.g. one person's SID, COC, and passport scan). It is NOT
a merged person profile: each document is extracted and persisted separately,
and verification still runs one document at a time. What the folder adds is
*cross-document lookup context*: while verifying one document, the engine may
read credential values extracted from the OTHER documents in the same
person's folder — e.g. a COC verification that requires a passport number can
resolve it from the person's separately-scanned passport document.

Design rules (see ARCHITECTURE.md, "Person folders" section):

- Every document in the folder has its own fixture file on disk:
  ``<person_folder>/<document_stem>.json``.
- Fixtures are gitignored (PII hygiene — never committed).
- A fixture records the source file's SHA-256 so a stale fixture (the PDF
  changed after extraction) is detected and refreshed, never silently reused.
- ``load_subject_document`` returns BOTH the subject fixture and a
  ``credential_provider``-compatible callable: the provider serves the
  subject's own raw credentials first, and fills ONLY required identity keys
  that the subject document itself could not supply from the subject's other
  documents. Contact-only fields are never filled from siblings (they are
  synthesized by the execution layer); unknown/extra keys are never injected.
"""

import hashlib
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from ocr.client import extract_document
from ocr.extractor import extract_and_redact, split_credentials

logger = logging.getLogger(__name__)

# Person folders live here (gitignored). Layout:
#   person_folders/<person_name>/<document_stem>.json
PERSON_FOLDERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "person_folders",
)


def _fixture_path(person_folder: str, document_path: str) -> str:
    stem = os.path.splitext(os.path.basename(document_path))[0]
    return os.path.join(PERSON_FOLDERS_DIR, person_folder, stem + ".json")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_document_fixture(document_path: str) -> Dict[str, Any]:
    """
    Run OCR + extraction/redaction for one document and split the result.
    Returns the fixture payload (redacted profile + in-memory credentials +
    integrity metadata). Credentials are persisted INSIDE the fixture by
    design: the fixture file is a gitignored local cache whose entire purpose
    is to let tests re-run without re-extracting. The folder is PII-bearing
    local state, exactly like the input scans themselves.
    """
    raw_ocr = extract_document(document_path)
    extracted = extract_and_redact(raw_ocr)
    redacted_profile, credentials = split_credentials(extracted)

    return {
        "source_file": os.path.basename(document_path),
        "source_sha256": _sha256_file(document_path),
        "redacted_profile": redacted_profile,
        "raw_lookup_credentials": dict(credentials or {}),
    }


def load_or_extract_fixture(
    document_path: str,
    person_folder: str,
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Load the persisted fixture for this document, or create it via OCR +
    extraction. A fixture whose recorded SHA-256 no longer matches the source
    file is re-extracted (staleness by content, not by timestamp).
    """
    fx_path = _fixture_path(person_folder, document_path)
    current_hash = _sha256_file(document_path)

    if not force_refresh and os.path.exists(fx_path):
        try:
            with open(fx_path, "r", encoding="utf-8") as f:
                fixture = json.load(f)
            if fixture.get("source_sha256") == current_hash:
                logger.info(
                    "Fixture cache hit for %s (person folder %s)",
                    document_path, person_folder,
                )
                return fixture
            logger.info(
                "Fixture for %s is stale (source changed); re-extracting.",
                document_path,
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Unreadable fixture %s (%s); re-extracting.", fx_path, e)

    fixture = _extract_document_fixture(document_path)
    os.makedirs(os.path.dirname(fx_path), exist_ok=True)
    with open(fx_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2, ensure_ascii=False)
    return fixture


# ---------------------------------------------------------------------------
# Cross-document credential lookup
# ---------------------------------------------------------------------------

# Credential keys that may be resolved from sibling documents. Everything
# else (names of ad-hoc fields the LLM invented, contact fields, etc.) is NOT
# looked up cross-document.
_KNOWN_IDENTITY_KEYS = {
    "document_number", "date_of_birth", "full_name",
    "passport_number", "serial_number", "cdc_number",
}


def _alias_key(key: str) -> str:
    """Normalize sibling credential keys onto the canonical identity keys."""
    k = str(key or "").lower().strip()
    aliases = {
        "cdc_no": "cdc_number",
        "book_no": "cdc_number",
        "sirb_no": "cdc_number",
        "document_no": "document_number",
        "doc_number": "document_number",
        "dob": "date_of_birth",
        "passport_no": "passport_number",
        "serial": "serial_number",
        "certificate_serial": "serial_number",
        "certificate_no": "serial_number",
    }
    return aliases.get(k, k)


def lookup_credential_across_siblings(
    person_folder: str,
    subject_document_path: str,
    key: str,
) -> Optional[Tuple[str, str]]:
    """
    Look a credential key up in every OTHER document fixture of this person's
    folder. Returns (value, source_document) or None.

    A sibling credential qualifies only if it is a non-empty string that is
    not itself a redaction token or placeholder (the execution-layer guard
    would refuse it anyway; refusing to serve it keeps the failure local and
    legible).
    """
    from execution.safety import is_placeholder_value

    wanted = _alias_key(key)
    if wanted not in _KNOWN_IDENTITY_KEYS:
        return None

    fx_dir = os.path.join(PERSON_FOLDERS_DIR, person_folder)
    if not os.path.isdir(fx_dir):
        return None

    subject_stem = os.path.splitext(os.path.basename(subject_document_path))[0]

    for name in sorted(os.listdir(fx_dir)):
        if not name.endswith(".json"):
            continue
        sibling_stem = name[:-len(".json")]
        if sibling_stem == subject_stem:
            continue  # the subject document is never its own sibling
        try:
            with open(os.path.join(fx_dir, name), "r", encoding="utf-8") as f:
                sibling = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        creds = sibling.get("raw_lookup_credentials") or {}
        for cred_key, value in creds.items():
            if _alias_key(cred_key) != wanted:
                continue
            v = str(value or "").strip()
            if v and not is_placeholder_value(v):
                return v, sibling_stem
    return None


def load_subject_document(
    document_path: str,
    person_folder: str,
    required_inputs: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], Callable[[], Optional[Dict[str, str]]]]:
    """
    The folder-mode replacement for main.py's inline credential_provider.

    Returns (subject_fixture, credential_provider). The provider serves:
      1. the subject document's own raw credentials (unchanged behavior), then
      2. for required identity keys the subject could not supply, values
         looked up from the subject's OTHER documents in the same folder.

    Cross-document values are lookup CONTEXT, never the subject of the
    verification: the subject fixture itself decides which document is being
    verified and how its response is compared.
    """
    subject = load_or_extract_fixture(document_path, person_folder)

    required = [str(k) for k in (required_inputs or [])]

    def credential_provider(required_inputs_arg=None) -> Optional[Dict[str, str]]:
        # The engine passes the current method's required_inputs at call time
        # (preferred); the constructor-time list is the zero-arg fallback.
        required_now = [
            str(k) for k in (required_inputs_arg if required_inputs_arg is not None else required)
        ]

        credentials: Dict[str, str] = {}
        own = subject.get("raw_lookup_credentials") or {}
        for k, v in own.items():
            s = str(v or "").strip()
            if s:
                credentials[k] = s

        # Fill ONLY required identity keys the subject could not supply from
        # the person's other documents. Everything else stays as-is.
        for req in required_now:
            if credentials.get(req):
                continue
            hit = lookup_credential_across_siblings(
                person_folder, document_path, req
            )
            if hit:
                value, source_doc = hit
                credentials[req] = value
                logger.info(
                    "Credential %r for %s resolved cross-document from %s",
                    req, os.path.basename(document_path), source_doc,
                )

        return credentials or None

    return subject, credential_provider
