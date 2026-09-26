"""Host-side comparison of live responses with the document's raw fields.

Comparison is driven by a per-method **discovered field mapping** stored in
``expected_responses.field_mapping`` — NOT by a hardcoded global assumption
about which three fields every registry returns.

When a method has no discovered mapping yet, the system discovers one on the
fly using the local LLM (see ``response_mapping.py``). If discovery finds
zero mappable fields the result is UNCERTAIN (unable to verify), never a
silent false REJECTED from comparing against nonexistent fields.

Image fields (base64 BMP/PNG/JPEG) are compared via the shared vision_call
path, contributing to the overall verdict alongside text field scores.
"""

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
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %m %Y", "%Y %m %d",
        "%d/%b/%Y", "%d-%b-%Y", "%d/%B/%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y",
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
    """Legacy reference field extraction — used as fallback for methods
    without a discovered mapping (e.g. old INDOS methods)."""
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


def _parse_response_as_structured(response: str) -> Optional[Dict[str, Any]]:
    """Try to parse the response as JSON, returning the parsed dict or None."""
    if not response:
        return None
    data = response
    while isinstance(data, str):
        try:
            parsed = json.loads(data)
            if parsed == data:
                break
            data = parsed
        except (json.JSONDecodeError, TypeError):
            try:
                clean_str = data.replace('\\"', '"').replace('\\\\', '\\')
                parsed = json.loads(clean_str)
                if parsed == data:
                    break
                data = parsed
            except (json.JSONDecodeError, TypeError):
                break
    if isinstance(data, dict):
        return data
    return None


def _compare_with_discovered_mapping(
    response: str,
    raw_profile: Dict[str, str],
    field_mapping: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare response fields against profile using a discovered mapping.

    Returns a dict with 'scores', 'image_results', 'all_matched', 'any_matched'.
    """
    from validation.response_mapping import (
        _flatten_response,
        _is_base64_image,
        compare_image_field,
    )

    # Parse response
    parsed = _parse_response_as_structured(response)
    if parsed is None:
        return {
            "scores": {},
            "image_results": {},
            "all_matched": False,
            "any_matched": False,
            "total_fields": 0,
            "strong_count": 0,
            "weak_count": 0,
        }

    flat_response = _flatten_response(parsed)

    text_mappings = field_mapping.get("text_mappings", {})
    image_fields = field_mapping.get("image_fields", {})

    scores: Dict[str, float] = {}
    image_results: Dict[str, Dict[str, Any]] = {}

    # Compare text fields via the discovered mapping
    for resp_field, prof_field in text_mappings.items():
        resp_value = str(flat_response.get(resp_field, ""))
        prof_value = str(raw_profile.get(prof_field, ""))

        if not resp_value or not prof_value:
            continue

        score = _field_similarity(_clean(prof_value), _clean(resp_value))
        scores[f"{prof_field}←{resp_field}"] = score

    # Compare image fields via vision_call
    for resp_field, img_format in image_fields.items():
        img_b64 = str(flat_response.get(resp_field, ""))
        if not img_b64 or len(img_b64) < 50:
            continue

        result = compare_image_field(
            image_b64=img_b64,
            image_format=img_format,
            profile_context=raw_profile,
            field_name=resp_field,
        )
        image_results[resp_field] = result
        # Fold image score into overall scores
        scores[f"image:{resp_field}"] = result["score"]

    strong = [s for s in scores.values() if s >= STRONG_MATCH_THRESHOLD]
    weak = [s for s in scores.values() if s >= AMBIGUOUS_MATCH_THRESHOLD]

    return {
        "scores": scores,
        "image_results": image_results,
        "all_matched": len(strong) == len(scores) and len(scores) > 0,
        "any_matched": len(weak) > 0,
        "total_fields": len(scores),
        "strong_count": len(strong),
        "weak_count": len(weak),
    }


def compare_response(
    result: ExecutionResult,
    raw_profile: Dict[str, str],
    field_mapping: Optional[Dict[str, Any]] = None,
) -> ExecutionResult:
    """Classify an executor response using per-method field mapping.

    Args:
        result: The execution result from the Docker runner.
        raw_profile: The document's raw lookup credentials/profile fields.
        field_mapping: The method's discovered field mapping (from
            expected_responses.field_mapping). If None, falls back to the
            legacy three-field comparison for backward compatibility.
    """
    if result.decision_status == ExecutionDecisionStatus.TECHNICAL_FAILURE:
        return result

    response = result.raw_response or ""
    haystack = ""

    # --- Discovered mapping path (new general capability) ---
    if field_mapping is not None:
        # Check for error/empty response first
        parsed = _parse_response_as_structured(response)
        if parsed is None and not response:
            return result.model_copy(update={
                "decision_status": ExecutionDecisionStatus.TECHNICAL_FAILURE,
                "evidence": {**result.evidence, "comparison": "empty_response"},
                "error": "Empty response body from verification endpoint.",
            })

        # Check for error markers in the response text
        resp_text = _clean(json.dumps(parsed) if parsed else response)
        if any(marker in resp_text for marker in _ERROR_MARKERS):
            return result.model_copy(update={
                "decision_status": ExecutionDecisionStatus.REJECTED,
                "evidence": {**result.evidence, "comparison": "error_marker_in_response"},
            })

        comparison = _compare_with_discovered_mapping(response, raw_profile, field_mapping)

        if comparison["total_fields"] == 0:
            # No fields could be compared — unable to verify
            return result.model_copy(update={
                "decision_status": ExecutionDecisionStatus.UNCERTAIN,
                "evidence": {
                    **result.evidence,
                    "comparison": "no_comparable_fields",
                    "field_mapping": field_mapping,
                },
            })

        if comparison["all_matched"]:
            return result.model_copy(update={
                "decision_status": ExecutionDecisionStatus.VERIFIED,
                "evidence": {
                    **result.evidence,
                    "comparison": "discovered_field_match",
                    "field_scores": comparison["scores"],
                    "image_results": comparison.get("image_results", {}),
                    "field_mapping_used": field_mapping,
                },
            })

        if not comparison["any_matched"]:
            return result.model_copy(update={
                "decision_status": ExecutionDecisionStatus.REJECTED,
                "evidence": {
                    **result.evidence,
                    "comparison": "discovered_no_match",
                    "field_scores": comparison["scores"],
                    "image_results": comparison.get("image_results", {}),
                    "field_mapping_used": field_mapping,
                },
            })

        # Ambiguous band — use LLM judge
        from utils.local_llm_client import generate_local_json
        judgment = generate_local_json(
            "Judge whether response fields match the raw document fields. "
            "Return only JSON with decision MATCH, NO_MATCH, or UNCLEAR.",
            json.dumps({
                "raw_profile": raw_profile,
                "field_scores": comparison["scores"],
                "image_results": {k: {"score": v["score"], "description": v["description"]}
                                  for k, v in comparison.get("image_results", {}).items()},
                "field_mapping": field_mapping,
            }),
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
            "evidence": {
                **result.evidence,
                "comparison": "discovered_llm_judge",
                "field_scores": comparison["scores"],
                "image_results": comparison.get("image_results", {}),
                "field_mapping_used": field_mapping,
            },
        })

    # --- Legacy fallback path (no discovered mapping) ---
    # This preserves backward compatibility with methods that don't have a
    # discovered mapping yet (e.g. existing INDOS methods).
    extracted = _extract_response_fields(response)
    reference = _reference_fields(raw_profile)
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

    # A non-empty body that yields NO parseable record fields and is tiny
    # carries no evidence that a record lookup happened at all — it is as
    # likely a portal error fragment (e.g. the 19-byte "VerificationError"
    # that dmamyanmar.org returns on a broken token/session flow) as a
    # genuine not-found. Classifying it REJECTED would report INVALID with
    # HIGH evidence — the most dangerous wrong answer. Fail to
    # TECHNICAL_FAILURE instead. Genuine not-founds are unaffected: they are
    # either structured (fields extracted) or carry not-found markers.
    if not extracted and len(response.strip()) < 200:
        return result.model_copy(update={
            "decision_status": ExecutionDecisionStatus.TECHNICAL_FAILURE,
            "evidence": {
                **result.evidence,
                "comparison": "tiny_unparseable_response",
                "extracted_fields": extracted,
            },
            "error": (
                f"Response body ({len(response.strip())} chars) contained no "
                "parseable record data — refusing to classify as REJECTED."
            ),
        })

    if not reference:
        # No reference fields available at all — cannot verify with legacy path
        return result.model_copy(update={
            "decision_status": ExecutionDecisionStatus.UNCERTAIN,
            "evidence": {
                **result.evidence,
                "comparison": "no_reference_fields_legacy",
                "extracted_fields": extracted,
            },
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