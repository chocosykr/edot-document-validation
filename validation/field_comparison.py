"""Host-side comparison of live responses with the document's raw fields."""

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio

from execution.models import ExecutionDecisionStatus, ExecutionResult


_ERROR_MARKERS = (
    "could not find", "not found", "no record", "no match", "unable to process",
    "invalid", "error", "exception", "sorry",
)
STRONG_MATCH_THRESHOLD = 90
AMBIGUOUS_MATCH_THRESHOLD = 55


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _response_text(response: str) -> str:
    if not response:
        return ""
    soup = BeautifulSoup(response, "html.parser")
    return _clean(soup.get_text(" ", strip=True))


def _field_similarity(expected: str, actual: str) -> float:
    """Compare dates by calendar value before applying fuzzy text matching."""
    date_formats = (
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
        "%d/%b/%Y", "%d-%b-%Y", "%d/%B/%Y", "%d-%B-%Y",
    )
    for expected_format in date_formats:
        try:
            expected_date = datetime.strptime(expected.upper(), expected_format).date()
            for actual_format in date_formats:
                try:
                    if expected_date == datetime.strptime(actual.upper(), actual_format).date():
                        return 100.0
                except ValueError:
                    pass
        except ValueError:
            pass
    return ratio(expected, actual)


def _json_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_values(item)
    elif value is not None:
        yield _clean(value)


def _extract_response_fields(response: str) -> Dict[str, str]:
    """Extract labeled fields from JSON or HTML tables/forms."""
    fields: Dict[str, str] = {}
    if not response:
        return fields

    try:
        parsed = json.loads(response)
        values = list(_json_values(parsed))
        for key, value in (parsed.items() if isinstance(parsed, dict) else []):
            key_lower = _clean(key)
            if any(token in key_lower for token in ("name", "holder", "dob", "birth", "indos", "document", "certificate", "number", "id")):
                fields[key_lower] = _clean(value)
        if fields:
            return fields
        fields["response_values"] = " ".join(values)
        return fields
    except (json.JSONDecodeError, TypeError):
        pass

    soup = BeautifulSoup(response, "html.parser")
    for row in soup.find_all("tr"):
        cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0]:
            fields[cells[0]] = cells[1]
    for label in soup.find_all(["label", "dt"]):
        value = label.find_next_sibling(["input", "dd"])
        if value:
            fields[_clean(label.get_text(" ", strip=True))] = _clean(value.get("value") or value.get_text(" ", strip=True))
    if not fields:
        fields["response_text"] = _response_text(response)
    return fields


def _reference_fields(profile: Dict[str, str]) -> Dict[str, str]:
    aliases = {
        "document_number": ("document_number", "document_numbers", "indos", "certificate", "id"),
        "date_of_birth": ("date_of_birth", "dob", "birth"),
        "full_name": ("full_name", "name", "holder"),
    }
    result = {}
    for field, keys in aliases.items():
        for key in keys:
            if profile.get(key):
                result[field] = _clean(profile[key])
                break
    return result


def compare_response(
    result: ExecutionResult,
    raw_profile: Dict[str, str],
) -> ExecutionResult:
    """Classify an executor response without fixed success keywords."""
    if result.decision_status == ExecutionDecisionStatus.TECHNICAL_FAILURE:
        return result

    response = result.raw_response or ""
    reference = _reference_fields(raw_profile)
    extracted = _extract_response_fields(response)
    haystack = " ".join(extracted.values()) or _response_text(response)

    if not response or not haystack or any(marker in haystack for marker in _ERROR_MARKERS):
        status = (
            ExecutionDecisionStatus.TECHNICAL_FAILURE
            if not response
            else ExecutionDecisionStatus.REJECTED
        )
        return result.model_copy(update={
            "decision_status": status,
            "evidence": {**result.evidence, "comparison": "empty_or_error", "extracted_fields": extracted},
            "error": "Empty response body from verification endpoint." if not response else result.error,
        })

    response_values = list(extracted.values()) or [haystack]
    scores = {
        field: max(_field_similarity(value, candidate) for candidate in response_values)
        for field, value in reference.items()
        if value
    }
    strong = [score for score in scores.values() if score >= STRONG_MATCH_THRESHOLD]
    weak = [score for score in scores.values() if score >= AMBIGUOUS_MATCH_THRESHOLD]
    relevant_count = len(reference)

    if relevant_count and len(strong) == relevant_count:
        return result.model_copy(update={
            "decision_status": ExecutionDecisionStatus.VERIFIED,
            "evidence": {**result.evidence, "comparison": "field_match", "field_scores": scores, "extracted_fields": extracted},
        })
    if not weak:
        return result.model_copy(update={
            "decision_status": ExecutionDecisionStatus.REJECTED,
            "evidence": {**result.evidence, "comparison": "no_match", "field_scores": scores, "extracted_fields": extracted},
        })

    from utils.local_llm_client import generate_local_json
    judgment = generate_local_json(
        "Judge whether response fields match the raw document fields. Return only JSON with decision MATCH, NO_MATCH, or UNCLEAR.",
        json.dumps({"raw_profile": reference, "response_fields": extracted, "raw_response": response[:10000]}),
    ) or {}
    decision = judgment.get("decision")
    if decision == "MATCH":
        status = ExecutionDecisionStatus.VERIFIED
    elif decision == "NO_MATCH":
        status = ExecutionDecisionStatus.REJECTED
    else:
        status = ExecutionDecisionStatus.UNCERTAIN
    return result.model_copy(update={
        "decision_status": status,
        "evidence": {**result.evidence, "comparison": "local_llm", "field_scores": scores, "extracted_fields": extracted},
    })