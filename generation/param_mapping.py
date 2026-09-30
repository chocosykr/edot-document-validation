"""
Component 3 — narrow field mapping.

Given a confirmed XHR contract (Component 1), the LLM's only job is to map
wire param names to {{profile_field}} placeholders. This module builds the
narrow mapping prompt, resolves the LLM's answer with a per-param
well-known-name fallback, and pins dispatch selectors (search-type selects)
to concrete page options using the page's own evidence.

Split out of generator.py; no portal-specific vocabulary here — selectors
are detected structurally and the token vocabulary is generic
seafarer-credential language.
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional

from registry.document_types import document_type_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Component 3: Narrow field-mapping prompt (used when extraction succeeded)
# ---------------------------------------------------------------------------

def _load_narrow_mapping_prompt() -> str:
    """Load the narrow mapping prompt from prompts/narrow_mapping.txt (C3)."""
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "prompts",
        "narrow_mapping.txt",
    )
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


_NARROW_MAPPING_PROMPT = _load_narrow_mapping_prompt()


def _build_narrow_mapping_payload(
    redacted_profile: dict,
    xhr_contract: dict,
    available_inputs: List[str] = None,
) -> tuple[str, dict]:
    """
    Build the narrow mapping prompt payload. Returns (user_prompt, extras)
    where extras carries expected_responses hints supplied by the caller
    (discriminator markers), if any.
    """
    _NARROW_MAPPING_PROMPT = _load_narrow_mapping_prompt()

    # Provide only the exact fields that the engine can actually inject.
    profile_fields = available_inputs if available_inputs is not None else [
        "document_number",
        "date_of_birth",
        "full_name",
        "document_type_key",
        "issuing_country",
        "verification_url"
    ]

    user_prompt = (
        _NARROW_MAPPING_PROMPT
        .replace("{{verb}}", xhr_contract["verb"])
        .replace("{{endpoint}}", xhr_contract["endpoint"])
        .replace("{{dynamic_params}}", json.dumps(xhr_contract["dynamic_params"]))
        .replace("{{static_params}}", "[]")
        .replace("{{param_location}}", xhr_contract["param_location"])
        .replace("{{workflow_options}}", json.dumps(xhr_contract.get("workflow_options", [])))
        .replace("{{profile_fields}}", json.dumps(profile_fields))
    )
    return user_prompt, {}


def _resolve_param_mapping(
    llm_mapping: Optional[dict],
    xhr_contract: dict,
    available_inputs: Optional[List[str]] = None,
) -> tuple[Dict[str, str], List[str]]:
    """
    Resolve the wire-param → {{profile_field}} mapping from the narrow LLM's
    answer, with a well-known-name fallback when the LLM returned nothing
    usable. Resolved ONCE so the discriminator probe and the method builder
    share the same mapping.
    """
    param_mapping = (llm_mapping or {}).get("param_mapping") or {}
    # Null-valued entries are the model saying "I don't know" — drop them
    # so the well-known-name fallback below can cover those params.
    # (Observed live: txtNo/dob answered null and were silently dropped
    # from the generated request, leaving a lookup with no document
    # number.)
    param_mapping = {
        k: v for k, v in param_mapping.items()
        if isinstance(v, str) and v.strip()
    }
    required_inputs = list((llm_mapping or {}).get("required_inputs") or [])

    aliases = {
        "document_numbers": "document_number",
        "document_holder.date_of_birth": "date_of_birth",
        "document_holder.name": "full_name",
    }
    normalized_mapping = {}
    for param, placeholder in param_mapping.items():
        if isinstance(placeholder, str) and placeholder.startswith("{{") and placeholder.endswith("}}"):
            field = aliases.get(placeholder[2:-2].strip(), placeholder[2:-2].strip())
            normalized_mapping[param] = f"{{{{{field}}}}}"
        else:
            normalized_mapping[param] = placeholder
    param_mapping = normalized_mapping

    cleaned_required = []
    for field in required_inputs:
        f_clean = field[2:-2].strip() if isinstance(field, str) and field.startswith("{{") and field.endswith("}}") else field
        cleaned_required.append(aliases.get(f_clean, f_clean))
    required_inputs = cleaned_required

    # Well-known-name fallback runs PER PARAM: it fills only the params the
    # model left unmapped, never overrides an explicit mapping, and —
    # crucially — also runs when SOME params were mapped (the previous
    # all-or-nothing condition let a model that mapped one param and punted
    # the rest silently drop the rest).
    # Vocabulary is generic seafarer-credential language (cdc/serial/passport
    # are document types, not portal names), matched as case-insensitive
    # substrings of the wire param name. Live case: dmamyanmar's form posts
    # CrewCDCNo (the holder's CDC book number) and Serial (the certificate's
    # serial) — the fallback now maps them to cdc_number/serial_number from
    # the param names alone; no portal-specific code.

    well_known = {
        "document_number": ["txtno", "txt_no", "docno", "doc_no", "docnumber", "doc_number", "number", "certno", "cert_no", "applicationno", "application_no", "appid", "app_id", "regno", "reg_no"],
        "date_of_birth": ["dob", "dateofbirth", "date_of_birth", "birth", "birthdate", "dobdate"],
        "cdc_number": ["cdcno", "cdc_no", "cdc", "sirb"],
        "serial_number": ["serial"],
        "passport_number": ["passport", "ppno", "pp_no"],
    }
    for param in xhr_contract.get("dynamic_params", []):
        if param in param_mapping:
            continue
        p_lower = param.lower()
        for field, aliases in well_known.items():
            # Availability gate: a fallback mapping for a field the source
            # document cannot supply would make the method unexecutable (the
            # engine refuses to submit redaction tokens). Live case,
            # 2026-09-29: a CDC document carries no date_of_birth, yet the
            # dob param was fallback-mapped and marked required — refusing
            # the whole run for a param the esamudra CDC lookup does not
            # even need. available_inputs=None keeps the old behavior.
            if available_inputs is not None and field not in available_inputs:
                continue
            if any(alias in p_lower for alias in aliases):
                param_mapping[param] = f"{{{{{field}}}}}"
                if field not in required_inputs:
                    required_inputs.append(field)
                break

    # Drop mappings for fields the source document cannot supply (explicit
    # LLM mappings included — a method that can never be executed is worse
    # than one built without the optional param).
    if available_inputs is not None:
        unavailable = [
            p for p, ph in param_mapping.items()
            if isinstance(ph, str) and ph.startswith("{{")
            and ph[2:-2].strip() not in available_inputs
        ]
        for p in unavailable:
            del param_mapping[p]
        required_inputs = [
            f for f in required_inputs if f in available_inputs
        ]

    # Filter inputs the model hallucinated from workflow params (a pinned
    # search-type selector is never a document-sourced input).
    required_inputs = [
        f for f in required_inputs
        if f not in ("document_type", "document_type_key")
    ]

    return param_mapping, required_inputs


def _infer_workflow_params(
    llm_mapping: Optional[dict],
    xhr_contract: dict,
    redacted_profile: dict,
) -> Dict[str, str]:
    """Resolve selector params from the model output and page options."""
    workflow_params = dict((llm_mapping or {}).get("workflow_params") or {})
    # A workflow param value must be a CONCRETE page-option value. A
    # placeholder ({{document_type_key}}) is the model punting — drop it so
    # the page-evidence pinning below decides. (Observed live: the literal
    # placeholder got pinned as a static param and the request went out as
    # searchType=IN_CDC, which the endpoint answers with an empty body.)
    for _k, _v in list(workflow_params.items()):
        if not isinstance(_v, str) or not _v.strip() or _v.strip().startswith("{{"):
            workflow_params.pop(_k)
    document_type = str(redacted_profile.get("document_type") or "").lower()
    param_mapping = (llm_mapping or {}).get("param_mapping") or {}

    for param in list(xhr_contract.get("dynamic_params", [])):
        if param in workflow_params:
            continue
        # Pinning is exclusively for DISPATCH SELECTORS: params that
        # correspond to a <select> on the source page (matched structurally
        # by shared vocabulary, see _is_dispatch_selector). Everything else
        # — document numbers, dates, punted params, hallucinated mappings —
        # must keep whatever {{placeholder}} mapping it has.
        # (Live case, 2026-09-29: non-selector params txtNo/dob fell into
        # this loop through a substring guard and an all-options fallback,
        # got pinned to the literal page values "Indos"/"CDC", and every
        # genuine document was reported not-found.)
        # A PUNTED mapping (null) must not skip selector pinning: the
        # page's own <select> is the evidence, not the model's guess.
        # (Live case, 2026-09-28: the mapping model returned
        # searchType=null, the pinning loop was skipped entirely, the
        # request went out WITHOUT the dispatch selector, and esamudra
        # answered 108 chars of "please try later".)
        if not _is_dispatch_selector(param, xhr_contract.get("workflow_options") or []):
            continue
        # Tokens naming THIS document type, from the canonical alias table
        # ("Continuous Discharge Certificate (CDC)" -> IN_CDC -> "CDC"),
        # plus the profile's own first word. An option matches when its
        # VALUE or TEXT names a token as a whole word — word-boundary
        # matching so a short token ("PP") cannot ride inside an unrelated
        # word ("shipping").
        _doc_key = document_type_key(redacted_profile.get("document_type", ""))
        doc_token = (
            _doc_key.split("_", 1)[1] if _doc_key and "_" in _doc_key else ""
        )
        first_word = (
            re.sub(r"[^A-Za-z]+", " ", document_type).split()[0]
            if document_type
            else ""
        )
        tokens = {t.lower() for t in (doc_token, first_word) if t}

        def _names_token(haystack: str) -> bool:
            return any(
                re.search(rf"\b{re.escape(t)}\b", haystack.lower())
                for t in tokens
            )

        # Only options from THE param's own <select> are eligible evidence:
        # a page can carry several selects (document type, port, year, …) and
        # an option from an unrelated one must never be pinned. If the param
        # matches NO select on the page it is not a dispatch selector at all
        # (live case, 2026-09-29: txtNo fell through to the all-options
        # fallback and got pinned to the literal "Indos"), so leave it alone.
        own_options = [
            o for o in xhr_contract.get("workflow_options", [])
            if _is_dispatch_selector(param, [o])
        ]
        if not own_options:
            continue
        for option in own_options:
            value = str(option.get("value") or "")
            text = str(option.get("text") or "")
            # Word-boundary matching ONLY. Loose substring checks are
            # wrong here: option value "DC" substring-matched the "(CDC)"
            # inside "Continuous Discharge Certificate (CDC)" and pinned
            # the wrong search type (live-confirmed bug). A token must be a
            # standalone word in the option's value/text.
            if value and _names_token(f"{value} {text}"):
                workflow_params[param] = value
                break
    return workflow_params


def _ident_tokens(name: str, widget_prefixes: set) -> set:
    """Tokenize an identifier the way its author wrote it: split on
    non-alphanumerics AND on camelCase boundaries, then lowercase.

    "cmbSearch_by" -> {"search", "by"}; "searchType" -> {"search", "type"}
    — the two share "search", which is exactly the structural signal the
    dispatch detector needs. Lowercasing before splitting (the previous
    behavior) glued camelCase words into single unmatchable tokens
    ("cmbsearch", "searchtype"), which made every selector unmatchable and
    silently disabled the whole structural dispatch path (live-confirmed
    2026-09-29)."""
    tokens = set()
    for part in re.split(r"[^A-Za-z0-9]+", str(name or "")):
        for word in re.findall(
            r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part
        ):
            w = word.lower()
            if len(w) >= 3 and w not in widget_prefixes:
                tokens.add(w)
    return tokens


def _is_dispatch_selector(param: str, workflow_options: list) -> bool:
    """True when ``param`` corresponds to a <select> on the source page.

    Purely structural: the param and the page's select share a vocabulary
    word (camelCase-aware token). "searchType" (servlet param) and
    "cmbSearch_by" (the page's select) share "search"; "CrewCDCNo" shares
    nothing with a select named "cmbSearch_by". The prefix tokens below are
    universal HTML naming conventions (cmb/ddl/drp = combo-box/dropdown
    widgets), not portal-specific knowledge.
    """
    _WIDGET_PREFIXES = {"cmb", "ddl", "drp", "select", "list"}
    p_tokens = _ident_tokens(param, _WIDGET_PREFIXES)
    if not p_tokens:
        return False
    for option in workflow_options or []:
        s_tokens = _ident_tokens(option.get("select") or "", _WIDGET_PREFIXES)
        if p_tokens & s_tokens:
            return True
    return False


def _doc_type_workflow_evidence(
    document_key: str,
    workflow_options: list,
    endpoint: str,
) -> bool:
    """
    Compatibility evidence for doc-type-guarded narrow-path generation.
    Accepted, in order of strength:
      1. A page <select> option naming the document type (legacy
         server-rendered pages), or
      2. the extracted API path itself carrying the doc-type token (SPA:
         options are rendered client-side and never appear in the HTML, but
         a path segment like "/sid/verify" is the site's own statement of
         what the endpoint verifies).
    Generic and data-driven: the token comes from the canonical document-type
    key (e.g. "IN_SID" -> "sid"), never from a hardcoded site name.
    """
    doc_token = document_key.split("_", 1)[1].lower() if "_" in document_key else ""
    option_text = " ".join(
        f"{item.get('value', '')} {item.get('text', '')}"
        for item in (workflow_options or [])
    ).lower()
    endpoint_path = "".join((endpoint or "").lower().split("/"))
    has_option_evidence = (
        doc_token in option_text or "seafarer identity" in option_text
    )
    has_path_evidence = bool(doc_token) and doc_token in endpoint_path
    return has_option_evidence or has_path_evidence
