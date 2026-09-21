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


def document_type_key(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    if "INDOS" in normalized or "NATIONAL DATABASE OF SEAFARERS" in normalized:
        return "IN_INDOS"
    if "SEAFARERS IDENTITY" in normalized or normalized == "SID":
        return "IN_SID"
    if "CDC" in normalized or "CONTINUOUS DISCHARGE" in normalized:
        return "IN_CDC"
    if "CERTIFICATE OF COMPETENCY" in normalized or normalized == "COC":
        return "IN_COC"
    return "UNKNOWN"


def profile_document_type_key(profile: dict) -> str:
    value = profile.get("document_type_key")
    if value:
        normalized = str(value).upper()
        if normalized.startswith("IN_"):
            return normalized
        inferred = document_type_key(normalized)
        if inferred != "UNKNOWN":
            return inferred
    return document_type_key(profile.get("document_type", ""))
