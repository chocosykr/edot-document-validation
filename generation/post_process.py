"""Deterministic post-generation enforcement for candidate methods.

The LLM proposes SHAPE; schema-critical invariants are enforced in code
(deterministic over probabilistic — a confident wrong method is worse than a
failed generation).
"""

import re
import logging
from typing import Dict, List, Set

from registry.models import MethodType, MethodStatus, ValidationMethod

logger = logging.getLogger(__name__)


def _sanitize_not_found_signatures(raw) -> List[dict]:
    """Validate declared not-found signatures; drop anything broad.

    Only entries carrying BOTH an integer `status` and a non-empty `contains`
    message survive. This structurally prevents a vague status-only rule (which
    could misread a CAPTCHA rejection, a rate limit, or a server error as a
    document verdict) from ever reaching the executor.
    """
    if not isinstance(raw, list):
        return []
    clean: List[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            status = int(entry.get("status"))
        except (TypeError, ValueError):
            continue
        contains = entry.get("contains")
        if not isinstance(contains, str) or not contains.strip():
            continue
        clean.append({"status": status, "contains": contains.strip()})
    return clean


# Canonical input-name aliases: models name required inputs after the target
# site's wire fields (CrewCDCNo, CrewPassport, Serial, ReplyEmail), but the
# engine's credential provider works in CANONICAL document keys. Rename the
# {{placeholder}} — never the wire param the site expects.
_INPUT_ALIASES = [
    ("cdc", "cdc_number"),
    ("passport", "passport_number"),
    ("serial", "document_number"),
    ("certificateno", "document_number"),
    ("certificate_no", "document_number"),
    ("indos", "document_number"),
    ("dateofbirth", "date_of_birth"),
    ("dob", "date_of_birth"),
    ("birthdate", "date_of_birth"),
    ("email", "email"),          # contact-only by content role
    ("phone", "phone"),
    ("mobile", "phone"),
]

_CONTACT_INPUTS = {"email", "phone"}

_RUNTIME_VARS = {
    "captcha_text", "captcha_id",          # FETCH_CAPTCHA/SOLVE_CAPTCHA
    "verification_url",                     # engine: QR methods
    "document_type_key", "issuing_country", # engine: context keys
}

_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _canonical_input_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    for marker, canonical in _INPUT_ALIASES:
        if marker in n:
            return canonical
    return str(name or "").strip()


def _rename_placeholder(value: str, rename_map: dict) -> str:
    if not isinstance(value, str) or not rename_map:
        return value
    def _sub(m):
        return "{{" + rename_map.get(m.group(1), m.group(1)) + "}}"
    return _PLACEHOLDER_RE.sub(_sub, value)


def _collect_referenced_placeholders(steps: list) -> Set[str]:
    referenced: Set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        for value in list((step.get("params") or {}).values()) + list(
            (step.get("json_body") or {}).values()
        ) + [step.get("url"), step.get("value"), step.get("html")]:
            if isinstance(value, str):
                referenced.update(_PLACEHOLDER_RE.findall(value))
    return referenced


def _collect_output_vars(steps: list) -> Set[str]:
    output_vars: Set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        ov = str(step.get("output_var") or "").strip()
        if ov:
            output_vars.add(ov)
    return output_vars


def _enforce_method_type(llm_output: dict) -> None:
    """Reject disallowed method types proposed by the LLM."""
    method_type_raw = str(llm_output.get("method_type") or "").upper()
    if method_type_raw == "BROWSER":
        raise RuntimeError(
            "LLM produced a BROWSER method (stub executor; cannot run). "
            "The page evidence did not yield an HTTP/form contract."
        )


def _enforce_resolvable_placeholders(llm_output: dict) -> None:
    """Promote unresolvable placeholders to required_inputs."""
    steps = llm_output.get("execution_steps") or []
    declared = {
        str(x).strip() for x in (llm_output.get("required_inputs") or [])
        if str(x or "").strip()
    }
    output_vars = _collect_output_vars(steps)
    referenced = _collect_referenced_placeholders(steps)
    resolvable = declared | _RUNTIME_VARS | output_vars
    unknown = referenced - resolvable
    if unknown:
        llm_output["required_inputs"] = sorted(declared | unknown)
        logger.warning(
            "Generator: placeholders %s were not resolvable; promoted to "
            "required_inputs.", sorted(unknown),
        )


def _enforce_required_inputs_present(llm_output: dict) -> None:
    if not llm_output.get("required_inputs"):
        raise RuntimeError(
            "LLM produced a method with no required_inputs and no "
            "{{placeholders}} — nothing would be looked up."
        )


def _normalize_input_names(llm_output: dict) -> None:
    """Rename declared inputs to canonical names and rewrite {{placeholders}}."""
    steps = llm_output.get("execution_steps") or []
    declared_inputs = {
        str(x).strip() for x in (llm_output.get("required_inputs") or [])
        if str(x or "").strip()
    }
    rename_map: dict = {}
    canonical_inputs: Set[str] = set()
    for name in declared_inputs:
        canonical = _canonical_input_name(name)
        canonical_inputs.add(canonical)
        if canonical != name:
            rename_map[name] = canonical
    if rename_map:
        logger.warning(
            "Generator: normalizing non-canonical input names %s -> %s",
            rename_map, sorted(canonical_inputs),
        )
        llm_output["required_inputs"] = sorted(canonical_inputs)
        # Rewrite {{placeholders}} through the same map (wire param NAMES
        # are untouched; only the template references change).
        for step in steps:
            if not isinstance(step, dict):
                continue
            for key in ("url", "value", "html"):
                if isinstance(step.get(key), str):
                    step[key] = _rename_placeholder(step[key], rename_map)
            for container_key in ("params", "json_body"):
                container = step.get(container_key)
                if isinstance(container, dict):
                    for k, v in container.items():
                        if isinstance(v, str):
                            container[k] = _rename_placeholder(v, rename_map)


def _reconcile_wire_param_assignments(llm_output: dict) -> None:
    """Align each wire param's {{placeholder}} with the canonical input its
    own param NAME implies (full-LLM path only).

    The mapping LLM sometimes declares inputs it never references and
    references placeholders it never declared (live case, 2026-09-29:
    dmamyanmar method declared CrewCDCNo/CrewPassport/Serial but its steps
    used {{document_number}}/{{serial}}/{{passport}} — an incoherent method
    the pipeline can never execute). Wire param names carry meaning: the
    same generic vocabulary the well-known-name fallback uses (cdc/passport/
    serial markers) resolves the intended input for each param. Only params
    whose canonical name matches a DECLARED input are rewritten — anything
    else (anti-forgery tokens, literal emails) is left alone. Runs BEFORE
    placeholder promotion so orphan placeholders cannot leak into
    required_inputs as phantom inputs.
    """
    steps = llm_output.get("execution_steps") or []
    declared = {
        str(x).strip() for x in (llm_output.get("required_inputs") or [])
        if str(x or "").strip()
    }
    if not declared:
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        for container_key in ("params", "json_body"):
            container = step.get(container_key)
            if not isinstance(container, dict):
                continue
            for param_name in list(container):
                canonical = _canonical_input_name(param_name)
                if canonical not in declared:
                    continue
                wanted = "{{" + canonical + "}}"
                if container[param_name] != wanted:
                    logger.warning(
                        "Generator: wire param %r reassigned to %s "
                        "(its name implies this input)", param_name, wanted,
                    )
                    container[param_name] = wanted


def _enforce_contact_only_declarations(llm_output: dict, expected_responses: dict) -> None:
    """Infer contact-only inputs from canonical names and add declarations."""
    contact_declared = set(
        (llm_output.get("expected_responses") or {}).get("contact_only_inputs") or []
    )
    canonical_inputs = {
        _canonical_input_name(x) for x in
        (llm_output.get("required_inputs") or [])
    }
    inferred_contact = (canonical_inputs & _CONTACT_INPUTS) - contact_declared
    if inferred_contact:
        expected_responses.setdefault("contact_only_inputs", []).extend(
            sorted(inferred_contact)
        )
        logger.info(
            "Generator: inferred contact_only_inputs %s", sorted(inferred_contact)
        )


def finalize_method(llm_output: dict) -> ValidationMethod:
    """Apply all deterministic enforcement passes and return a ValidationMethod.

    This is the single entry point for the post-generation phase. It:
    1. Normalizes input names to canonical forms
    2. Rewrites {{placeholders}} to match canonical names
    3. Infers and declares contact-only inputs
    4. Enforces the method type is not BROWSER
    5. Promotes unresolvable placeholders to required_inputs
    6. Ensures required_inputs is non-empty
    """
    # 1. Canonical input-name normalization
    _normalize_input_names(llm_output)

    # 1b. Wire-param/placeholder reconciliation (declared inputs vs the
    # placeholders the steps actually reference)
    _reconcile_wire_param_assignments(llm_output)

    # 2. Build expected_responses from the LLM output's keyword markers
    llm_keywords = (llm_output.get("expected_responses") or {})
    expected_responses = {
        "comparison_mode": "field_match",
        "method_schema": "field-comparison-v3",
        "document_type_key": llm_output.get("document_type_key", ""),
    }
    # Preserve not-found signatures (narrow, executor-validated)
    signatures = _sanitize_not_found_signatures(
        llm_keywords.get("not_found_signatures")
    )
    if signatures:
        expected_responses["not_found_signatures"] = signatures
    # Preserve contact-only declarations
    llm_contact = llm_keywords.get("contact_only_inputs")
    if isinstance(llm_contact, list) and llm_contact:
        expected_responses["contact_only_inputs"] = [
            str(x) for x in llm_contact if str(x or "").strip()
        ]
    # Preserve keyword markers (for structural probe verdict)
    if llm_keywords.get("failure_keywords"):
        expected_responses["failure_keywords"] = [
            str(x) for x in llm_keywords["failure_keywords"] if str(x or "").strip()
        ]
    if llm_keywords.get("success_keywords"):
        expected_responses["success_keywords"] = [
            str(x) for x in llm_keywords["success_keywords"] if str(x or "").strip()
        ]
    llm_output["expected_responses"] = expected_responses

    # 3. Contact-only declarations
    _enforce_contact_only_declarations(llm_output, expected_responses)

    # 4. Method type enforcement
    _enforce_method_type(llm_output)

    # 5. Placeholder resolvability
    _enforce_resolvable_placeholders(llm_output)

    # 6. Required inputs present
    _enforce_required_inputs_present(llm_output)

    return ValidationMethod(**llm_output)
