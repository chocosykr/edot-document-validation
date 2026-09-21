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


def document_type_key(value: str) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    return DOCUMENT_TYPE_ALIASES.get(normalized)


def profile_document_type_key(profile: dict) -> str:
    # The model-emitted key is advisory only. Canonical routing is derived
    # exclusively from the classified document_type field.
    return document_type_key(profile.get("document_type", ""))
