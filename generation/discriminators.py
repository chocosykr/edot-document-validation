"""
Component 2 — discriminator discovery (one-time probe, registry-cached).

Fires the known-fake structural probe at an endpoint CONFIRMED by the XHR
extractor and derives rejection/success markers deterministically via
difflib — no LLM in the diff step, and the LLM elsewhere in the pipeline
never sees probe responses.

PII policy: real document values come only from a caller-supplied provider,
exist in memory for the duration of the probe, and are never logged or
persisted.
"""

import difflib
import logging
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _probe_wire_params(
    inputs: Dict[str, str],
    param_mapping: Optional[Dict[str, str]],
    static_params: Dict[str, str],
) -> Dict[str, str]:
    """
    Translate profile-field-keyed inputs into final wire-format params.

    param_mapping maps WIRE param names (txtNo, dob) to {{profile_field}}
    placeholders. Without this translation the probe would send
    document_number=/date_of_birth= to an endpoint expecting txtNo=/dob= —
    fake and real probes would produce identical responses and zero markers.
    """
    params = dict(static_params)
    for wire_name, placeholder in (param_mapping or {}).items():
        if not placeholder:
            continue
        field = placeholder.strip().strip("{}").strip()
        if field in inputs:
            params[wire_name] = inputs[field]
    return params


def _probe_endpoint(
    endpoint: str,
    verb: str,
    param_location: str,
    params: Dict[str, str],
    timeout: int = 15,
) -> Optional[str]:
    """
    Fire ONE probe request at an endpoint CONFIRMED by the XHR extractor
    (never a guessed URL). `params` are final wire-format params, already
    translated by _probe_wire_params. Returns the raw response body, or None.

    Callers pass either fake structural values or operator-supplied real
    inputs; probe inputs are never logged or persisted here.
    """
    from urllib.parse import urlencode

    url = endpoint
    data = None
    headers = {"User-Agent": "DVS/1.0"}

    if param_location == "query":
        separator = "&" if "?" in url else "?"
        url = url + separator + urlencode(params)
    else:
        data = urlencode(params).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    try:
        req = requests.Request(
            verb, url, data=data, headers=headers
        ).prepare()
        with requests.Session() as session:
            resp = session.send(req, timeout=timeout)
        return resp.text
    except Exception as e:
        logger.warning("Probe request to %s failed: %s", endpoint, e)
        return None


# ---------------------------------------------------------------------------
# Deterministic discriminator extraction (difflib — no LLM in the diff step)
# ---------------------------------------------------------------------------

# Classification aids applied ONLY to diff-isolated candidate lines — never
# to a raw page-wide scan. The diff has already proven each candidate differs
# between the fake and real responses; the patterns only rank/confirm them.
_REJECTION_PATTERNS = [
    "could not find",
    "not found",
    "no match",
    "no record",
    "does not exist",
    "doesn't exist",
    "unable to find",
    "invalid",
]
_SUCCESS_PATTERNS = [
    "search result",
    "record found",
    "found",
    "valid",
    "verified",
    "matched",
    "active",
]

_MIN_MARKER_LEN = 4
_MAX_MARKER_LEN = 200


def _normalize_html_text(html: str) -> str:
    """HTML → normalized line-based text (body only), for stable diffing."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body") or soup
    text = body.get_text(separator="\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _diff_unique_chunks(text_a: str, text_b: str) -> tuple[List[str], List[str]]:
    """
    Line-level difflib diff. Returns (lines_only_in_a, lines_only_in_b).
    """
    a_lines = text_a.splitlines()
    b_lines = text_b.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    a_only: List[str] = []
    b_only: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            a_only.extend(a_lines[i1:i2])
        if tag in ("insert", "replace"):
            b_only.extend(b_lines[j1:j2])
    return a_only, b_only


def _pick_marker(
    candidates: List[str],
    patterns: List[str],
    other_text: str,
    own_text: str,
) -> str:
    """
    Pick the shortest candidate that (a) contains a classification pattern,
    (b) appears in own_text, and (c) does NOT appear in other_text.
    Returns "" when nothing qualifies — never guess.
    """
    for cand in sorted({c.strip() for c in candidates if c and c.strip()}, key=len):
        if not (_MIN_MARKER_LEN <= len(cand) <= _MAX_MARKER_LEN):
            continue
        low = cand.lower()
        if not any(p in low for p in patterns):
            continue
        if cand in own_text and cand not in other_text:
            return cand
    return ""


def _extract_discriminators_by_diff(
    fake_response: Optional[str],
    real_response: Optional[str] = None,
) -> dict:
    """
    Deterministic discriminator extraction via difflib. NO LLM in the diff
    step (policy: the diff is a mechanical string operation; the LLM earlier
    in the pipeline never sees probe responses).

    Two-sided mode (fake + real): lines unique to the fake response are
    rejection-marker candidates; lines unique to the real response are
    success-marker candidates. One-sided mode returns no markers — a single
    response cannot be diffed against anything.
    """
    result = {"rejection_marker": "", "success_marker": ""}
    if not fake_response or not real_response:
        return result

    fake_text = _normalize_html_text(fake_response)
    real_text = _normalize_html_text(real_response)
    if not fake_text or not real_text:
        return result

    fake_only, real_only = _diff_unique_chunks(fake_text, real_text)
    result["rejection_marker"] = _pick_marker(
        fake_only, _REJECTION_PATTERNS, other_text=real_text, own_text=fake_text
    )
    result["success_marker"] = _pick_marker(
        real_only, _SUCCESS_PATTERNS, other_text=fake_text, own_text=real_text
    )
    return result


def _fetch_idle_text(source_url: str) -> str:
    """Fetch the idle (no-submission) page text for exclusion diffing."""
    try:
        resp = requests.get(source_url, timeout=10)
        return _normalize_html_text(resp.text)
    except Exception as e:
        logger.warning("Idle-page fetch failed for %s: %s", source_url, e)
        return ""


def _discover_discriminators(
    xhr_contract: dict,
    source_url: str,
    fake_inputs: Dict[str, str],
    param_mapping: Optional[Dict[str, str]] = None,
    real_inputs_provider: Optional[Callable[[], Optional[Dict[str, str]]]] = None,
) -> dict:
    """
    One-time probe at onboarding. Derives discriminator strings deterministically
    (difflib — no LLM in the diff step) and returns an expected_responses dict
    for the registry.

    Real-input sourcing (PII policy):
      - The real document number / DOB come from the caller-supplied
        real_inputs_provider (raw, locally-held values) — NEVER from the
        redacted profile, whose values are tokens like [DOCUMENT_NUMBER],
        and never from the document currently being validated. A real value
        valid for one registry proves nothing about any other registry.
      - Values exist in memory only: never logged, never persisted,
        discarded immediately after the probe.
      - The two-sided fake-vs-real diff confirms BOTH markers; without a
        provider the method is REJECTED-only (empty success_keywords).
    """
    static_params = xhr_contract.get("static_params", {})

    fake_params = _probe_wire_params(fake_inputs, param_mapping, static_params)
    fake_response = _probe_endpoint(
        endpoint=xhr_contract["endpoint"],
        verb=xhr_contract["verb"],
        param_location=xhr_contract["param_location"],
        params=fake_params,
    )
    if not fake_response:
        return {"success_keywords": [], "failure_keywords": []}

    # Real inputs: from the caller-supplied provider (raw extraction).
    real_inputs = None
    if real_inputs_provider is not None:
        try:
            real_inputs = real_inputs_provider()
        except Exception as e:
            logger.warning("real_inputs_provider failed: %s", e)
            real_inputs = None

    success_marker = ""
    rejection_marker = ""

    if isinstance(real_inputs, dict) and real_inputs.get("document_number"):
        real_params = _probe_wire_params(real_inputs, param_mapping, static_params)
        real_response = _probe_endpoint(
            endpoint=xhr_contract["endpoint"],
            verb=xhr_contract["verb"],
            param_location=xhr_contract["param_location"],
            params=real_params,
        )
        # Discard the real inputs immediately after the probe (PII).
        del real_inputs

        if real_response:
            diff = _extract_discriminators_by_diff(fake_response, real_response)
            rejection_marker = (diff.get("rejection_marker") or "").strip()
            success_marker = (diff.get("success_marker") or "").strip()
        real_response = None  # drop response bodies from memory
    else:
        # REJECTED-only path: no real-input provider. Diff the fake response
        # against the idle page so static page labels are excluded; without
        # the idle page, fall back to a pattern scan of the fake response
        # alone (weakest evidence — the structural test is the backstop).
        fake_text = _normalize_html_text(fake_response)
        idle_text = _fetch_idle_text(source_url)
        if idle_text:
            fake_only, _ = _diff_unique_chunks(fake_text, idle_text)
            rejection_marker = _pick_marker(
                fake_only, _REJECTION_PATTERNS, other_text=idle_text, own_text=fake_text
            )
        else:
            rejection_marker = _pick_marker(
                fake_text.splitlines(), _REJECTION_PATTERNS, other_text="", own_text=fake_text
            )

    expected_responses: dict = {
        "success_keywords": [success_marker] if success_marker else [],
        "failure_keywords": [rejection_marker] if rejection_marker else [],
    }

    if not expected_responses["failure_keywords"] and not expected_responses["success_keywords"]:
        # Probe produced nothing confirmable — method must not carry guessed
        # keywords. It will fail structural validation and never be promoted.
        logger.warning(
            "Discriminator probe produced no confirmed markers for %s",
            xhr_contract["endpoint"],
        )

    return expected_responses
