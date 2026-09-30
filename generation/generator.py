"""
Candidate method generation — orchestrator.

Pipeline (see ARCHITECTURE.md):
  1. Fetch and summarize the source page        → generation.page_fetch
  2. Extract the XHR contract deterministically → generation.xhr_contract
     (inline JS first, then SPA bundles via generation.bundle_contract)
  3. Narrow LLM mapping of params → profile fields
     → generation.param_mapping
  4. Assemble the ValidationMethod from the contract
  5. Deterministic post-generation enforcement  → generation.post_process
  Fallback: full-LLM generation when extraction is unavailable.

Split into focused modules; this file keeps the LLM interaction and the
assembly of the final method.
"""

import uuid
import json
import os
import logging
from typing import Dict, List

from registry.models import ValidationMethod, MethodType, MethodStatus, CURRENT_METHOD_SCHEMA
from registry.document_types import profile_document_type_key
from utils.llm_client import generate_json
from generation.bundle_contract import _extract_xhr_contract_from_bundles
from generation.post_process import finalize_method as _finalize_method

# Split modules (verbatim moves). Re-exported here so existing callers and
# tests that import/patch through generation.generator keep working.
from generation.page_fetch import _collect_inline_js, _fetch_page_structure
from generation.xhr_contract import (
    _XHR_PATTERNS,
    _CONCAT_SEGMENT,
    _collect_assignment_chain,
    _resolve_expression_to_literal,
    _is_runtime_value_expr,
    _resolve_js_url,
    _extract_dynamic_param_names,
    _extract_static_params,
    _extract_xhr_contract,
)
from generation.param_mapping import (
    _load_narrow_mapping_prompt,
    _build_narrow_mapping_payload,
    _resolve_param_mapping,
    _infer_workflow_params,
    _is_dispatch_selector,
    _doc_type_workflow_evidence,
)
from generation.discriminators import (
    _probe_wire_params,
    _probe_endpoint,
    _normalize_html_text,
    _diff_unique_chunks,
    _pick_marker,
    _extract_discriminators_by_diff,
    _fetch_idle_text,
    _discover_discriminators,
)

logger = logging.getLogger(__name__)

# Fake values used for the discriminator-discovery probe. MUST stay in sync
# with engine.validation_engine._GENERIC_TEST_CASES so the structural test
# reproduces the probe's fake response.
FAKE_PROBE_INPUTS = {
    "document_number": "TEST_STRUCTURAL_001",
    "date_of_birth": "01/01/1990",
}


def _build_method_from_contract(
    param_mapping: Dict[str, str],
    required_inputs: List[str],
    redacted_profile: dict,
    source_info: dict,
    xhr_contract: dict,
    expected_responses: dict,
) -> ValidationMethod:
    """
    Assemble the ValidationMethod from a confirmed XHR contract plus the
    resolved param→field mapping. The LLM is never trusted with the
    endpoint, verb, or encoding — only the mapping.
    """
    # Build execution steps — structure comes from the contract, not the LLM.
    # If the contract carries captcha session params, emit the
    # FETCH_CAPTCHA -> SOLVE_CAPTCHA -> REQUEST sequence; the executor makes
    # {{captcha_text}}/{{captcha_id}} available as template variables after
    # SOLVE_CAPTCHA.
    params: Dict[str, str] = dict(xhr_contract.get("static_params", {}))
    for param_name, placeholder in param_mapping.items():
        if placeholder:
            params[param_name] = placeholder

    steps: List[dict] = []
    captcha_params = xhr_contract.get("captcha_params") or {}
    if captcha_params:
        captcha_endpoint = xhr_contract.get("captcha_endpoint")
        if not captcha_endpoint:
            logger.warning(
                "Contract carries captcha params but no captcha endpoint; "
                "captcha steps omitted — the method will fail validation."
            )
        else:
            steps.append({
                "action": "FETCH_CAPTCHA",
                "url": captcha_endpoint,
                "response_format": "json",
            })
            steps.append({"action": "SOLVE_CAPTCHA"})
    for name, placeholder in captcha_params.items():
        params[name] = placeholder

    steps.append({
        "action": "REQUEST",
        "method": xhr_contract["verb"],
        "url": xhr_contract["endpoint"],
        "params": params,
        "param_location": xhr_contract["param_location"],
    })

    document_type = redacted_profile.get("document_type") or ""
    country = redacted_profile.get("issuing_country") or ""

    issuer = source_info.get("issuer")
    if not issuer and isinstance(source_info.get("source"), dict):
        issuer = source_info["source"].get("title")

    return ValidationMethod(
        method_id=f"M_{uuid.uuid4().hex[:8].upper()}",
        document_type=document_type,
        country=country,
        issuer=issuer,
        method_type=MethodType.HTTP,
        version=1,
        source_url=xhr_contract["endpoint"],
        required_inputs=required_inputs,
        execution_steps=steps,
        expected_responses=expected_responses,
        limitations=[],
        status=MethodStatus.TESTING,
    )


def generate_candidate_method(
    redacted_profile: dict,
    source_info: dict,
    available_inputs: List[str] = None,
) -> ValidationMethod:
    """
    Generates a candidate validation method.

    When the deterministic XHR extractor succeeds, the LLM's role is narrowed
    to mapping profile fields to the extracted param names (Component 3), and
    a one-time discriminator probe populates confirmed markers (Component 2).

    When extraction fails (partial/ambiguous/minified), the existing
    full-LLM path runs unchanged — graceful degradation, not a hard break.

    The redacted profile carries tokens, so any real-value probe input must
    come from an operator-supplied provider; values stay in memory and are
    never persisted.
    """
    source_url = source_info.get("source_url") or source_info.get("url")

    page_structure = None
    if source_url:
        page_structure = _fetch_page_structure(source_url)

    logger.info(
        "XHR extraction gate: source_url=%s page_structure_present=%s inline_js_present=%s",
        source_url,
        bool(page_structure),
        bool(page_structure and page_structure.get("inline_js")),
    )

    xhr_contract = None
    workflow_fixed_params = {}

    if page_structure and page_structure.get("inline_js"):
        try:
            xhr_contract = _extract_xhr_contract(
                page_structure["inline_js"],
                source_url,
                workflow_fixed_params=workflow_fixed_params,
            )
        except Exception as e:
            logger.warning("XHR extraction raised unexpectedly — falling back to LLM: %s", e)
            xhr_contract = None

    # Component 1b: when the page is an SPA shell (no usable inline JS), try
    # the deterministic fetch()-contract extractor over the external bundles
    # already downloaded for the endpoint hints. Same strictness rule: a
    # wrong extraction is worse than no extraction.
    if not xhr_contract and page_structure and page_structure.get("bundle_js"):
        try:
            xhr_contract = _extract_xhr_contract_from_bundles(
                page_structure["bundle_js"],
                source_url,
            )
            if xhr_contract:
                logger.info(
                    "Bundle-based XHR contract accepted (SPA shell page): %s",
                    xhr_contract["endpoint"],
                )
        except Exception as e:
            logger.warning("Bundle XHR extraction raised unexpectedly — falling back to LLM: %s", e)
            xhr_contract = None

    logger.info("XHR extractor result: source_url=%s xhr_contract=%r", source_url, xhr_contract)

    if xhr_contract:
        # ------------------------------------------------------------
        # Narrow path (Components 1-3)
        # ------------------------------------------------------------
        xhr_contract["workflow_options"] = page_structure.get("workflow_options", []) if page_structure else []
        document_key = profile_document_type_key(redacted_profile)
        # Country-scoped document types (SID/CDC/COC) must have their type
        # named by the SOURCE PAGE itself — a workflow option, an API path
        # token, or a pinned workflow param. Without this gate a reused
        # same-country source could produce a method whose search-type param
        # is left floating (never pinned to this document's type).
        _doc_token = document_key.split("_", 1)[1] if document_key and "_" in document_key else ""
        if _doc_token in ("SID", "CDC", "COC") and not (
            _doc_type_workflow_evidence(
                document_key, xhr_contract["workflow_options"], xhr_contract.get("endpoint") or ""
            )
        ):
            raise RuntimeError(
                f"Source page has no workflow option or API-path evidence "
                f"compatible with document type {document_key}."
            )
        user_prompt, _ = _build_narrow_mapping_payload(redacted_profile, xhr_contract, available_inputs=available_inputs)

        llm_mapping = generate_json(
            "You map document fields to API parameters. Return ONLY JSON.",
            user_prompt,
        )
        print(f"\n[DEBUG] Narrow mapping LLM output:\n{json.dumps(llm_mapping, indent=2)}\n")

        workflow_params = _infer_workflow_params(llm_mapping, xhr_contract, redacted_profile)
        param_mapping_output = (llm_mapping or {}).get("param_mapping") or {}
        for param_name, mapped_field in param_mapping_output.items():
            if (
                "search" in param_name.lower()
                and isinstance(mapped_field, str)
                and any(token in mapped_field.lower() for token in ("document_type", "document_type_key"))
                and param_name not in workflow_params
            ):
                raise RuntimeError(
                    f"Workflow parameter {param_name!r} was mapped to document_type "
                    "without a confirmed page option."
                )
        for name, value in workflow_params.items():
            if name in xhr_contract["dynamic_params"] and value:
                xhr_contract["dynamic_params"].remove(name)
                xhr_contract.setdefault("static_params", {})[name] = str(value)

        # A dispatch selector that is STILL unpinned means the page offered
        # a choice we could not resolve — generating now would ship a method
        # that submits incomplete requests (observed live: esamudra answers
        # a selector-less CDC query with a service-error fragment). Detected
        # STRUCTURALLY (param linked to a page <select> by shared vocabulary,
        # see _is_dispatch_selector) — no portal-specific naming knowledge.
        _unpinned = [
            p for p in xhr_contract.get("dynamic_params", [])
            if p not in workflow_params
            and _is_dispatch_selector(p, xhr_contract.get("workflow_options") or [])
        ]
        if _unpinned:
            raise RuntimeError(
                f"Dispatch selector(s) {_unpinned} could not be pinned to any "
                "page option; refusing to generate a method that would submit "
                "incomplete requests."
            )

        if llm_mapping:
            llm_mapping = dict(llm_mapping)
            llm_mapping["param_mapping"] = {
                key: value
                for key, value in (llm_mapping.get("param_mapping") or {}).items()
                if key not in workflow_params
            }
            llm_mapping["required_inputs"] = [
                field for field in (llm_mapping.get("required_inputs") or [])
                if field not in {"document_type", "document_type_key"}
            ]

        param_mapping, required_inputs = _resolve_param_mapping(
            llm_mapping, xhr_contract, available_inputs=available_inputs
        )
        expected_responses = {
            "comparison_mode": "field_match",
            "method_schema": CURRENT_METHOD_SCHEMA,
            "document_type_key": profile_document_type_key(redacted_profile),
        }

        method = _build_method_from_contract(
            param_mapping=param_mapping,
            required_inputs=required_inputs,
            redacted_profile=redacted_profile,
            source_info=source_info,
            xhr_contract=xhr_contract,
            expected_responses=expected_responses,
        )

        return method

    # ------------------------------------------------------------
    # Fallback: existing full-LLM path
    # ------------------------------------------------------------
    logger.info("XHR extraction unavailable — using full-LLM generation path.")

    prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "method_generation.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        system_prompt = f.read()

    page_structure_hint = page_structure.get("summary") if page_structure else None

    user_prompt = json.dumps({
        "redacted_profile": redacted_profile,
        "source_info": source_info,
        "page_structure_hint": page_structure_hint
    }, indent=2)

    llm_output = generate_json(system_prompt, user_prompt)

    print(f"\n[DEBUG] Raw LLM output from generator:\n{json.dumps(llm_output, indent=2)}\n")

    if not llm_output:
        raise RuntimeError("LLM failed to generate a candidate method.")

    # Ensure method_id is uniquely generated if the LLM provided a placeholder
    method_id = llm_output.get("method_id", "")
    if not method_id or method_id == "M_12345":
        llm_output["method_id"] = f"M_{uuid.uuid4().hex[:8].upper()}"

    # source_url is metadata about the CONFIRMED source — injected from
    # source_info rather than trusted to the model's echo. Same for the
    # document identity fields, which come from the classified profile.
    if not str(llm_output.get("source_url") or "").strip():
        llm_output["source_url"] = source_url or ""
    if not str(llm_output.get("country") or "").strip():
        llm_output["country"] = str(redacted_profile.get("issuing_country") or "")
    if not str(llm_output.get("document_type") or "").strip():
        llm_output["document_type"] = str(redacted_profile.get("document_type") or "")

    # Force status to TESTING regardless of what the LLM hallucinates
    llm_output["status"] = MethodStatus.TESTING
    # Set document_type_key on the output so _finalize_method can include it
    # in expected_responses without trusting the model's echo.
    llm_output["document_type_key"] = profile_document_type_key(redacted_profile)

    # ---- Deterministic post-generation enforcement --------------------
    # Delegated to generation.post_process so the generator stays focused on
    # the LLM interaction and contract extraction.
    return _finalize_method(llm_output)
