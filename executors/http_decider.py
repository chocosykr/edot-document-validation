"""
HTTP response classification for the HTTP executor: turns a raw response body
into VERIFIED / REJECTED / UNCERTAIN, honouring method-declared
success/failure keywords, JSON-path checks, and learned not-found signatures.

Split out of http_executor.py; in the Docker sandbox these files are copied
flat next to executor.py, so imports fall back to plain module names there.
"""

import json


def _matches_not_found_signature(status_code, body: str, expected: dict) -> bool:
    """Does this response match a method-declared not-found signature?

    A method may declare, in its own expected_responses:
        "not_found_signatures": [{"status": 400, "contains": "Application ID not found"}]

    BOTH fields are required and must match (status exactly, message as a
    case-insensitive substring). This is deliberately narrow and grounded in a
    specific endpoint's confirmed response: it is NOT a generic "any non-2xx
    with a JSON message = not found" rule. A CAPTCHA rejection, a rate limit,
    or a server error will not match unless the method explicitly names its
    exact message — so those stay TECHNICAL_FAILURE.
    """
    if not status_code or status_code < 400 or not body:
        return False
    signatures = expected.get("not_found_signatures")
    if not isinstance(signatures, list):
        return False
    # Whitespace-insensitive comparison: the same endpoint has been observed
    # to emit '{"message": "..."}' and '{"message":"..."}' for different
    # error classes, so an exact-substring match silently fails on serializer
    # formatting drift. Removing ALL whitespace makes the match depend on the
    # response CONTENT, not its JSON formatting (both sides get identical
    # treatment, so containment semantics are preserved).
    def _norm(text: str) -> str:
        return "".join(str(text).lower().split())

    body_norm = _norm(body)
    for signature in signatures:
        if not isinstance(signature, dict):
            continue
        try:
            signature_status = int(signature.get("status"))
        except (TypeError, ValueError):
            continue
        contains = signature.get("contains")
        if not isinstance(contains, str) or not contains.strip():
            continue  # a status-only rule is too broad — never honoured
        if signature_status == status_code and _norm(contains) in body_norm:
            return True
    return False


def _json_message_reports_not_found(body: str) -> bool:
    """Bootstrap-only: does a JSON 4xx body's message deterministically say
    the submitted value is not found?

    Scoped to methods with no learned not_found_signatures and no declared
    failure_keywords (checked by the caller): the structural probe needs to
    read the registry's business answer before any signature has been
    captured. Deliberately narrow — a SHORT JSON message explicitly saying
    "not found"; captcha failures (retryable noise), auth errors, rate
    limits and HTML error pages do NOT match and stay TECHNICAL_FAILURE.
    Once the validator captures the signature, the ordinary (preferred)
    signature channel takes over.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    message = parsed.get("message")
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return (
        len(message) <= 200
        and "not found" in lowered
        and "captcha" not in lowered
        and "unauthorized" not in lowered
        and "forbidden" not in lowered
    )


def decide(body: str, expected: dict) -> str:
    # When comparison_mode is field_match AND the method does not yet carry
    # discovered text_mappings, the validator has nothing to compare against
    # and will return TECHNICAL_FAILURE for any tiny response. That is correct
    # for a real document lookup whose fields we cannot match, but it kills the
    # structural probe: the known-fake values we submit are deliberately fake,
    # so the site's deterministic refusal of them is the one thing we CAN read
    # without a field mapping --- the ordinary keyword channel.
    #
    # Fall through to keyword scanning when there are no text_mappings, so a
    # method's own declared failure_keywords (e.g. "VerificationError") can
    # turn the structural probe into a definitive REJECTED, which the validator
    # then captures as a learned not_found_signature on the retest.
    if expected.get("comparison_mode") == "field_match":
        text_mappings = (expected.get("field_mapping") or {}).get("text_mappings")
        if not text_mappings:
            # No discovered mapping yet --- use keyword channel.
            body_lower = body.lower()
            for kw in expected.get("failure_keywords", []):
                if kw.lower() in body_lower:
                    return "REJECTED"
            for kw in expected.get("success_keywords", []):
                if kw.lower() in body_lower:
                    return "VERIFIED"
            return "UNCERTAIN"
        return "UNCERTAIN"

    # 1. JSON path check
    json_path = expected.get("success_json_path")
    json_val = expected.get("success_json_value")
    if json_path and json_val:
        try:
            parsed = json.loads(body)
            if str(parsed.get(json_path)) == str(json_val):
                return "VERIFIED"
        except (json.JSONDecodeError, AttributeError):
            pass

    body_lower = body.lower()

    # 2. Success keywords
    for kw in expected.get("success_keywords", []):
        if kw.lower() in body_lower:
            return "VERIFIED"

    # 3. Failure keywords
    for kw in expected.get("failure_keywords", []):
        if kw.lower() in body_lower:
            return "REJECTED"

    return "UNCERTAIN"
