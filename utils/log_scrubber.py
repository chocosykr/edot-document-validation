"""Field-named redaction for logs, prompts and debug dumps.

Generated SCRIPT methods can `print()` anything, and healing prompts carry
container logs + raw responses. Any of those can contain a real document
number, name or DOB. Flat replacement ("[REDACTED]") destroys the diagnostic
value of a log; this scrubber replaces each secret with a placeholder that
NAMES THE FIELD it came from, so both a human reading the log and a frontier
model reading a scrubbed prompt still know what the value was:

    "ANUP KAMBOJ"  ->  ‹PERSON_NAME›
    "07-SEP-1992"  ->  ‹DATE_OF_BIRTH›
    "MUM 179416"   ->  ‹CDC_NUMBER›
    "a@b.com"      ->  ‹EMAIL_ADDRESS›

Names are deliberately spelled out rather than abbreviated (``CDC_NUMBER``,
not ``CDC:10text``): the earlier short-label-plus-shape form read as cryptic
noise to the model and to reviewers alike. Matches are literal substring
replacements (longest-first, case-insensitive), so a value embedded in a URL,
a JSON body or a query string is caught too; the URL-encoded form of each
value is scrubbed as well.
"""

import re
import urllib.parse
from typing import Any, Dict, Iterable, Optional

_ACTIVE_PROFILE: Dict[str, Any] = {}

_ALREADY_REDACTED = {
    "[REDACTED]",
    "[SECRET_VALUE]",
    "[PERSON_NAME]",
    "[FULL_NAME]",
    "[DATE_OF_BIRTH]",
    "[DOCUMENT_NUMBER]",
    "[PASSPORT_NUMBER]",
    "[INDOS_NUMBER]",
    "[CDC_NUMBER]",
    "[EMAIL_ADDRESS]",
    "[PHONE_NUMBER]",
}

# Key-name vocabulary -> descriptive placeholder name. Ordered so more
# specific markers win ("passport" before the generic "document_number").
_ROLE_LABELS = (
    ("passport", "PASSPORT_NUMBER"),
    ("document_number", "DOCUMENT_NUMBER"),
    ("certificateno", "DOCUMENT_NUMBER"),
    ("certificate", "DOCUMENT_NUMBER"),
    ("indos", "INDOS_NUMBER"),
    ("cdc", "CDC_NUMBER"),
    ("dateofbirth", "DATE_OF_BIRTH"),
    ("birthdate", "DATE_OF_BIRTH"),
    ("dob", "DATE_OF_BIRTH"),
    ("email", "EMAIL_ADDRESS"),
    ("phone", "PHONE_NUMBER"),
    ("mobile", "PHONE_NUMBER"),
    ("fullname", "PERSON_NAME"),
    ("name", "PERSON_NAME"),
)

# Marker that never names a specific field (unknown key, bare secret list).
_DEFAULT_LABEL = "SECRET_VALUE"


def set_active_profile(profile: Optional[Dict[str, Any]]) -> None:
    """Set the process-wide credential/profile whose values are scrub targets."""
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile or {}


def _label_for(key: Any, value: str) -> str:
    key_norm = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    for marker, label in _ROLE_LABELS:
        if marker.replace("_", "").replace("-", "") in key_norm:
            return label
    return _DEFAULT_LABEL


def _is_scrubbable(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return (
        len(v) >= 3
        and v.upper() not in _ALREADY_REDACTED
        and not v.startswith("‹")
        and not v.startswith("{{")
    )


def _collect(profile) -> Dict[str, str]:
    """value -> label, flattened from a (possibly nested) profile/credentials."""
    entries: Dict[str, str] = {}

    def add(key, value):
        if isinstance(value, list):
            for item in value:
                add(key, item)
            return
        if isinstance(value, dict):
            for k, v in value.items():
                add(k, v)
            return
        if _is_scrubbable(value):
            entries.setdefault(value.strip(), _label_for(key, value))

    if isinstance(profile, dict):
        for k, v in profile.items():
            add(k, v)
    elif isinstance(profile, (list, tuple, set)):
        for v in profile:
            add("secret", v)
    return entries


def scrub(text: str, secret_values=None) -> str:
    """Replace each secret in `text` with a field-named placeholder.

    `secret_values` may be a dict (keys name the secret field), a list of raw
    values, or None to use the active profile. Structure is preserved; only
    the values disappear.
    """
    if not text:
        return text
    entries = _collect(secret_values)
    if not entries:
        return text

    # Longest value first so "ANUP KAMBOJ" is replaced before "ANUP".
    for value in sorted(entries, key=len, reverse=True):
        label = entries[value]
        placeholder = f"‹{label}›"
        variants = {value}
        quoted = urllib.parse.quote(value, safe="")
        if quoted != value:
            variants.add(quoted)
        for variant in variants:
            if len(variant) < 3:
                continue
            text = re.sub(
                re.escape(variant), placeholder, text, flags=re.IGNORECASE
            )
    return text


def scrub_pii(text: str, profile: Optional[Dict[str, Any]] = None) -> str:
    """Backwards-compatible wrapper: scrub using `profile` or the active one."""
    return scrub(text, _ACTIVE_PROFILE if profile is None else profile)


# Alias for callers that pass an explicit iterable of secrets.
def scrub_values(text: str, values: Iterable[str]) -> str:
    return scrub(text, list(values))
