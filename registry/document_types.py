"""Canonical document and country keys used for source compatibility."""

import re

COUNTRY_CODES = {
    "INDIA": "IN",
    "IND": "IN",
    "INDONESIA": "ID",
    "BANGLADESH": "BD",
    "BAHAMAS": "BS",
    "CANADA": "CA",
    "UNITED KINGDOM": "GB",
    "UK": "GB",
    "UNITED STATES": "US",
    "USA": "US",
    "MYANMAR": "MM",
    "MM": "MM",
}


def country_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().upper())
    return COUNTRY_CODES.get(normalized, normalized)


DOCUMENT_TYPE_ALIASES = {
    "INDOS": "IN_INDOS",
    "INDOS CERTIFICATE": "IN_INDOS",
    "INDIAN NATIONAL DATABASE OF SEAFARERS INDOS CERTIFICATE": "IN_INDOS",
    "SEAFARERS IDENTITY DOCUMENT": "IN_SID",
    "SEAFARERS IDENTITY DOCUMENT SID": "IN_SID",
    "SID": "IN_SID",
    "CONTINUOUS DISCHARGE CERTIFICATE": "IN_CDC",
    "CDC": "IN_CDC",
    "CERTIFICATE OF COMPETENCY": "IN_COC",
    "COC": "IN_COC",
}

# Document types whose registry is defined PER COUNTRY: an Indian COC and a
# Myanmar COC are verified by different national authorities, so they must
# never share a document_type_key. (INDOS is deliberately NOT in this set —
# it is intrinsically an Indian database, so IN_INDOS is the only correct key.)
COUNTRY_SCOPED_SUFFIXES = {
    "IN_COC": "COC",
    "IN_CDC": "CDC",
    "IN_SID": "SID",
}


def document_type_key(value: str) -> str | None:
    text = str(value or "")
    normalized = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    key = DOCUMENT_TYPE_ALIASES.get(normalized)
    if key:
        return key
    # "Continuous Discharge Certificate (CDC)" normalizes to a string the
    # alias table does not carry (only the bare forms are listed). Try each
    # parenthesis-delimited section on its own — extraction output routinely
    # uses the "Full Name (ACRONYM)" pattern.
    for part in re.split(r"[()]", text):
        candidate = re.sub(r"[^A-Z0-9]+", " ", part.upper()).strip()
        if candidate:
            key = DOCUMENT_TYPE_ALIASES.get(candidate)
            if key:
                return key
    return None


def profile_document_type_key(profile: dict) -> str | None:
    # The model-emitted key is advisory only. Canonical routing is derived
    # exclusively from the classified document_type field.
    base = document_type_key(profile.get("document_type", ""))
    if base is None:
        return None

    # Country-scope the per-country document types: COC/CDC/SID keys bind to
    # the ISSUING country (Myanmar COC -> MM_COC, not the India-era IN_COC).
    # Without a country the India-era default is kept for backward
    # compatibility with existing single-country (India) flows.
    suffix = COUNTRY_SCOPED_SUFFIXES.get(base)
    if suffix:
        country = str(profile.get("issuing_country") or "").strip()
        if country:
            return f"{country_key(country)}_{suffix}"

    return base
