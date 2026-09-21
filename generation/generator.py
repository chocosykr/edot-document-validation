import uuid
import json
import os
import re
import difflib
import logging
import requests
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from registry.models import ValidationMethod, MethodType, MethodStatus, CURRENT_METHOD_SCHEMA
from registry.document_types import document_type_key, profile_document_type_key
from utils.llm_client import generate_json

logger = logging.getLogger(__name__)

# Fake values used for the discriminator-discovery probe. MUST stay in sync
# with engine.validation_engine._GENERIC_TEST_CASES so the structural test
# reproduces the probe's fake response.
FAKE_PROBE_INPUTS = {
    "document_number": "TEST_STRUCTURAL_001",
    "date_of_birth": "01/01/1990",
}


def _collect_inline_js(soup) -> tuple[str, int]:
    """
    Collect inline <script> text from the page. External bundles are counted
    but never downloaded (out of scope — see implementation plan, Component 1
    non-goals). Returns (inline_js_text, external_custom_js_count).
    """
    inline_js = []
    external_custom_js = 0

    for script in soup.find_all("script"):
        src = script.get("src", "")
        if src:
            src_lower = src.lower()
            if not any(lib in src_lower for lib in ["jquery", "bootstrap", "react", "vue", "angular", "cdn"]):
                external_custom_js += 1
        else:
            text = script.string or ""
            if text.strip():
                inline_js.append(text.strip())

    return "\n\n".join(inline_js), external_custom_js


def _fetch_page_structure(url: str) -> dict:
    """
    Fetches the HTML from the given URL and returns a dict with:
      - "summary": human-readable summary of forms, inputs, buttons AND
        JavaScript logic for the LLM (same content as before the refactor).
      - "inline_js": the raw inline <script> text, for the deterministic
        XHR extractor (Component 1).
    """
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"Failed to fetch page structure for {url}: {e}")
        return {
            "summary": f"Could not fetch HTML (error: {e}). Proceed with blind generation.",
            "inline_js": "",
        }

    summary = []
    workflow_options = []

    # Extract form structure
    forms = soup.find_all("form")
    if not forms:
        summary.append("No <form> tags found on the page.")
    else:
        for i, form in enumerate(forms):
            form_id = form.get("id", "")
            form_name = form.get("name", "")
            form_action = form.get("action", "")
            form_method = form.get("method", "")
            summary.append(f"Form {i+1} (id='{form_id}', name='{form_name}', action='{form_action}', method='{form_method}'):")

            for tag in form.find_all(["input", "select", "textarea", "button"]):
                tag_name = tag.name
                tag_id = tag.get("id", "")
                tag_name_attr = tag.get("name", "")
                tag_type = tag.get("type", "")
                tag_class = tag.get("class", [])
                tag_onclick = tag.get("onclick", "")

                info = f"  <{tag_name}"
                if tag_type: info += f" type='{tag_type}'"
                if tag_id: info += f" id='{tag_id}'"
                if tag_name_attr: info += f" name='{tag_name_attr}'"
                if tag_class: info += f" class='{' '.join(tag_class)}'"
                if tag_onclick: info += f" onclick='{tag_onclick}'"
                info += ">"
                summary.append(info)
                if tag_name == "select":
                    for option in tag.find_all("option"):
                        workflow_options.append({
                            "select": tag_name_attr or tag_id,
                            "value": option.get("value", ""),
                            "text": option.get_text(" ", strip=True),
                        })
                        summary.append(
                            f"    <option value='{option.get('value', '')}'>{option.get_text(' ', strip=True)}</option>"
                        )

    # Extract JavaScript to find AJAX endpoints and form submission logic
    full_js_text, external_custom_js = _collect_inline_js(soup)

    if full_js_text or external_custom_js > 0:
        summary.append("\n--- JavaScript Logic (AJAX/form submission) ---")

        lines = full_js_text.split("\n")

        # Heuristics for minification and length
        total_chars = len(full_js_text)
        avg_line_length = total_chars / len(lines) if lines else 0

        is_minified = avg_line_length > 200
        is_too_long = total_chars > 40000  # Leaves plenty of room for 15k token budget

        # Legacy sites often have 2-4 custom utility scripts (e.g., date-picker.js).
        # SPAs typically have massive bundles. We'll rely on size/minification primarily.
        if is_minified or is_too_long:
            summary.append("[BROWSER_FALLBACK_REQUIRED: Modern SPA or heavily minified logic detected.]")
            summary.append("Do NOT attempt to guess the API endpoint. Generate a BROWSER method type.")
        else:
            summary.append(full_js_text)
            summary.append("\nIMPORTANT: If the JavaScript shows AJAX/XMLHttpRequest calls to a URL like a servlet or API endpoint, "
                          "the page does NOT use standard HTML form submission. "
                          "Generate an HTTP method type targeting that AJAX endpoint instead of WEB_FORM.")

        summary.append("--- End JavaScript ---")

    return {
        "summary": "\n".join(summary),
        "inline_js": full_js_text,
        "workflow_options": workflow_options,
    }


# ---------------------------------------------------------------------------
# Component 1: Deterministic XHR contract extraction
# ---------------------------------------------------------------------------

# Regex patterns for the legacy servlet XHR style (INDOS-confirmed) plus the
# common `fetch` variant. Kept deliberately narrow: a wrong extraction is
# worse than no extraction.
_XHR_PATTERNS = {
    # xmlHttp.open("POST", url, true) / xhr.open('GET', someUrl, true)
    "open_call": re.compile(
        r"""(?:xmlHttp|xhr|http|request)\s*\.\s*open\s*\(\s*["'](GET|POST)["']\s*,\s*([^,()]+?)\s*(?:,\s*(?:true|false)\s*)?\)""",
        re.IGNORECASE,
    ),
    # fetch("url", {method: "POST", ...}) / fetch(url, {method: 'POST'})
    "fetch_call": re.compile(
        r"""fetch\s*\(\s*([^,()]+?)\s*,\s*\{[^}]*?method\s*:\s*["'](GET|POST)["']""",
        re.IGNORECASE,
    ),
    # var url = "..." (assignment, possibly with concatenation)
    "url_assign": re.compile(
        r"""(?:var|let|const)\s+(\w+)\s*=\s*(["'][^"']*["'](?:\s*\+\s*[^;]+)?)\s*;?"""
    ),
    # .send(null)  →  params on the query string
    "send_null": re.compile(r"""\.\s*send\s*\(\s*null\s*\)""", re.IGNORECASE),
    # .send(data-ish)  →  params in a request body
    "send_data": re.compile(r"""\.\s*send\s*\(\s*(?!null\s*\))\w+""", re.IGNORECASE),
}

# URL-concatenation segments of the form "literal" + variable + "literal"
# e.g.  url + "?txtNo=" + txtNoVal + "&dob=" + dobVal
_CONCAT_SEGMENT = re.compile(
    r"""(["'][^"']*["'])\s*\+\s*([A-Za-z_$][\w$]*)"""
)


def _collect_assignment_chain(target: str, js_text: str) -> str:
    """
    Collect a complete sequential assignment chain for a variable, such as:
      url = "/x";
      url = url + "?txtNo=" + txtNoVal;
      url = url + "&dob=" + dobVal;
    Returns the RHS text of every assignment to that variable in order.
    """
    chain: List[str] = []
    pattern = re.compile(rf"(?<![?&\w]){re.escape(target)}\s*=\s*(.*?);", re.DOTALL)
    for match in pattern.finditer(js_text):
        rhs = match.group(1).strip()
        if rhs:
            chain.append(rhs)
    return " ".join(chain)


def _resolve_expression_to_literal(expr: str, js_text: str) -> Optional[str]:
    """
    Resolve a JS expression to a literal string if it is a fixed constant.

    Examples:
      - "PPIndosCheck" -> "PPIndosCheck"
      - processId -> "PPIndosCheck" (via assignment chain)
      - document.form.cmbSearch_by.value -> None (runtime/form-driven)
      - txtNoVal -> None unless it was assigned a fixed literal
    """
    expr = (expr or "").strip()
    if not expr:
        return None

    if "+" in expr:
        parts = [p.strip() for p in expr.split("+") if p.strip()]
        if not parts:
            return None
        resolved_parts: List[str] = []
        for part in parts:
            value = _resolve_expression_to_literal(part, js_text)
            if value is None:
                return None
            resolved_parts.append(value)
        return "".join(resolved_parts)

    if re.fullmatch(r"""["'][^"']*["']""", expr):
        return expr.strip("\"'")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", expr):
        return expr

    if any(token in expr for token in [".value", "document.", "form.", "getElementById"]):
        return None

    var_match = re.fullmatch(r"[A-Za-z_$][\w$]*", expr)
    if var_match:
        chain = _collect_assignment_chain(expr, js_text)
        if not chain:
            return None
        if any(token in chain for token in [".value", "document.", "form.", "getElementById"]):
            return None

        literal = re.search(r"""["']([^"']+)["']""", chain)
        if literal:
            return literal.group(1)

        nested = re.fullmatch(r"[A-Za-z_$][\w$]*", chain.strip())
        if nested:
            return _resolve_expression_to_literal(chain.strip(), js_text)
        return None

    return None


def _is_runtime_value_expr(expr: str, js_text: str) -> bool:
    """Return True when the param value is user/form/runtime driven."""
    expr = (expr or "").strip()
    if not expr:
        return False

    if any(token in expr for token in [".value", "document.", "form.", "getElementById"]):
        return True

    if re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        chain = _collect_assignment_chain(expr, js_text)
        if chain and any(token in chain for token in [".value", "document.", "form.", "getElementById"]):
            return True

    return False


def _resolve_js_url(
    url_expr: str,
    js_text: str,
    base_url: str,
) -> Optional[str]:
    """
    Resolve a JS URL expression to an absolute URL.

    Handles: plain literals ("...jsp"), variables assigned a literal
    (var url = "..."), and literal prefix concatenations
    (url = "...servlet" or similar). Returns None when the expression cannot
    be resolved to a concrete URL string.
    """
    expr = url_expr.strip()

    # Direct literal
    literal = re.fullmatch(r"""["']([^"']+)["']""", expr)
    if literal:
        return urljoin(base_url, literal.group(1))

    # Variable reference: find its assignment chain (first literal prefix wins)
    var_match = re.fullmatch(r"[A-Za-z_$][\w$]*", expr)
    if var_match:
        target = expr.strip()
        chain = _collect_assignment_chain(target, js_text)
        if chain:
            lead = re.search(r"""["']([^"']+)["']""", chain)
            if lead:
                return urljoin(base_url, lead.group(1))
        return None

    # Variable-leading concatenation:  url + "?txtNo=" + txtNoVal + ...
    # Resolve the base variable's literal prefix (the endpoint path).
    concat_var = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\+", expr)
    if concat_var:
        target = concat_var.group(1)
        chain = _collect_assignment_chain(target, js_text)
        if chain:
            lead = re.search(r"""["']([^"']+)["']""", chain)
            if lead:
                return urljoin(base_url, lead.group(1))
        return None

    # String concatenation starting with a literal: "prefix" + ...
    concat = re.match(r"""["']([^"']+)["']\s*\+""", expr)
    if concat:
        return urljoin(base_url, concat.group(1))

    return None


def _extract_dynamic_param_names(
    url_expr: str,
    js_text: str,
    workflow_fixed_params: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Extract dynamic parameter names appended to the URL, e.g.

        var url = "...";          or direct literal concatenation:
        xhr.open("POST", "endpoint?" + "txtNo=" + x ...)
        url + "?txtNo=" + val + "&dob=" + val2

    Returns param names found in concatenation segments (?name= / &name=)
    that are followed by a JS variable (a dynamic value).
    """
    names: List[str] = []
    search_text = url_expr

    var_match = re.fullmatch(r"\s*([A-Za-z_$][\w$]*)\s*", url_expr)
    if var_match:
        target = var_match.group(1)
        search_text = _collect_assignment_chain(target, js_text)

    workflow_fixed = workflow_fixed_params or {}

    for m in _CONCAT_SEGMENT.finditer(search_text):
        literal, following_var = m.group(1), m.group(2)
        for pm in re.finditer(r"""[?&](\w+)\s*=\s*["']?$""", literal):
            key = pm.group(1)
            if key in workflow_fixed:
                continue
            if _resolve_expression_to_literal(following_var, js_text) is not None:
                continue
            if key not in names:
                names.append(key)
        for pm in re.finditer(r"""[?&](\w+)=['"]$""", literal):
            key = pm.group(1)
            if key in workflow_fixed:
                continue
            if _resolve_expression_to_literal(following_var, js_text) is not None:
                continue
            if key not in names:
                names.append(key)

    seen = set()
    ordered = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _extract_static_params(
    url_expr: str,
    js_text: str,
    workflow_fixed_params: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Extract hardcoded params of the form ?key=value / &key=value where the
    value is a literal (not a concatenated variable), e.g.
    `...?processId=PPIndosCheck&searchType=Indos&txtNo=` — the trailing
    `txtNo=` with nothing after it is dynamic, not static.
    """
    static: Dict[str, str] = {}
    search_text = url_expr

    var_match = re.fullmatch(r"\s*([A-Za-z_$][\w$]*)\s*", url_expr)
    if var_match:
        target = var_match.group(1)
        search_text = _collect_assignment_chain(target, js_text)

    workflow_fixed = workflow_fixed_params or {}

    for match in re.finditer(r"[?&](\w+)=([^\"'&+\s]+)", search_text):
        key, value = match.group(1), match.group(2).strip()
        if key in workflow_fixed:
            static[key] = str(workflow_fixed[key])
        elif key not in static:
            static[key] = value

    for match in _CONCAT_SEGMENT.finditer(search_text):
        literal, following_var = match.group(1), match.group(2)
        for param_match in re.finditer(r"[?&](\w+)\s*=\s*[\"']?$", literal):
            key = param_match.group(1)
            if key in workflow_fixed:
                static[key] = str(workflow_fixed[key])
                continue
            resolved = _resolve_expression_to_literal(following_var, js_text)
            if resolved is not None and resolved not in {"null", "true", "false"}:
                static[key] = resolved

    for key, value in workflow_fixed.items():
        static[key] = str(value)

    return static


def _extract_xhr_contract(
    inline_js: str,
    base_url: str,
    workflow_fixed_params: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """
    Strict success definition (implementation plan, Component 1): endpoint
    URL + HTTP verb + at least one dynamic param must ALL be matched cleanly.
    Any partial match returns None and the pipeline falls back to the
    existing full-LLM path unchanged. A wrong extraction is worse than an
    incomplete one.
    """
    if not inline_js or not inline_js.strip():
        return None
    lines = inline_js.splitlines()
    if len(inline_js) > 40000 or (lines and len(inline_js) / len(lines) > 200):
        return None

    open_match = _XHR_PATTERNS["open_call"].search(inline_js)
    if open_match:
        url_expr = open_match.group(2).strip()
        verb = open_match.group(1).upper()
        send_after = inline_js[open_match.end():]
        send_null = _XHR_PATTERNS["send_null"].search(send_after)
        send_data = _XHR_PATTERNS["send_data"].search(send_after)
        if send_null and (not send_data or send_null.start() < send_data.start()):
            send_is_null = True
        elif send_data:
            send_is_null = False
        else:
            send_is_null = True
    else:
        fetch_match = _XHR_PATTERNS["fetch_call"].search(inline_js)
        if fetch_match:
            url_expr = fetch_match.group(1).strip()
            verb = fetch_match.group(2).upper()
            send_is_null = True
        else:
            return None

    if not verb or not url_expr:
        return None

    resolved_url = _resolve_js_url(url_expr, inline_js, base_url)
    if not resolved_url:
        logger.info("XHR extractor: could not resolve URL expression %r — fallback to LLM.", url_expr.strip())
        return None

    dynamic_params = _extract_dynamic_param_names(url_expr, inline_js, workflow_fixed_params)
    static_params = _extract_static_params(url_expr, inline_js, workflow_fixed_params)

    if not dynamic_params:
        logger.info("XHR extractor: no dynamic params found — fallback to LLM.")
        return None

    for name in dynamic_params:
        static_params.pop(name, None)

    param_location = "query" if send_is_null else "body"

    contract = {
        "endpoint": resolved_url,
        "verb": verb,
        "param_location": param_location,
        "dynamic_params": dynamic_params,
        "static_params": static_params,
    }
    logger.info("XHR extractor success: %s", json.dumps(contract))
    return contract


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
) -> tuple[str, dict]:
    """
    Build the narrow mapping prompt payload. Returns (user_prompt, extras)
    where extras carries expected_responses hints supplied by the caller
    (discriminator markers), if any.
    """
    _NARROW_MAPPING_PROMPT = _load_narrow_mapping_prompt()

    profile_fields: List[str] = []

    def _flatten(obj, prefix=""):
        for k, v in obj.items():
            name = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _flatten(v, name)
            elif isinstance(v, list):
                # List field names matter (e.g. document_numbers); values may
                # be redaction tokens and are not shown.
                profile_fields.append(name)
            elif isinstance(v, (str, int, float)) and v not in (None, ""):
                profile_fields.append(name)

    _flatten(redacted_profile)
    profile_fields = sorted(profile_fields)

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
) -> tuple[Dict[str, str], List[str]]:
    """
    Resolve the wire-param → {{profile_field}} mapping from the narrow LLM's
    answer, with a well-known-name fallback when the LLM returned nothing
    usable. Resolved ONCE so the discriminator probe and the method builder
    share the same mapping.
    """
    param_mapping = (llm_mapping or {}).get("param_mapping") or {}
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
    required_inputs = [aliases.get(field, field) for field in required_inputs]

    if not param_mapping:
        well_known = {
            "document_number": ["txtNo", "docno", "doc_no", "docnumber", "number", "cert", "certno"],
            "date_of_birth": ["dob", "dateofbirth", "birth", "birthdate", "dobdate"],
        }
        param_mapping = {}
        for param in xhr_contract["dynamic_params"]:
            p_lower = param.lower()
            for field, aliases in well_known.items():
                if any(alias in p_lower for alias in aliases):
                    param_mapping[param] = f"{{{{{field}}}}}"
                    if field not in required_inputs:
                        required_inputs.append(field)
                    break

    return param_mapping, required_inputs


def _infer_workflow_params(
    llm_mapping: Optional[dict],
    xhr_contract: dict,
    redacted_profile: dict,
) -> Dict[str, str]:
    """Resolve selector params from the model output and page options."""
    workflow_params = dict((llm_mapping or {}).get("workflow_params") or {})
    document_type = str(redacted_profile.get("document_type") or "").lower()
    param_mapping = (llm_mapping or {}).get("param_mapping") or {}

    for param in list(xhr_contract.get("dynamic_params", [])):
        mapped = str(param_mapping.get(param) or "").lower()
        if param in workflow_params or "search" not in param.lower():
            continue
        if "document_type" not in mapped and "document" not in mapped:
            continue
        if "document_type_key" in mapped:
            mapped = document_type_key(redacted_profile.get("document_type", ""))
        else:
            mapped = document_type_key(redacted_profile.get("document_type", ""))
        for option in xhr_contract.get("workflow_options", []):
            value = str(option.get("value") or "")
            text = str(option.get("text") or "")
            if value and (
                value.lower() in document_type
                or text.lower() in document_type
                or (mapped == "IN_INDOS" and value.lower() == "indos")
            ):
                workflow_params[param] = value
                break
    return workflow_params


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
    # Build execution steps — structure comes from the contract, not the LLM
    params: Dict[str, str] = dict(xhr_contract.get("static_params", {}))
    for param_name, placeholder in param_mapping.items():
        if placeholder:
            params[param_name] = placeholder

    steps = [{
        "action": "REQUEST",
        "method": xhr_contract["verb"],
        "url": xhr_contract["endpoint"],
        "params": params,
        "param_location": xhr_contract["param_location"],
    }]

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


# ---------------------------------------------------------------------------
# Component 2: Discriminator discovery (one-time probe, registry-cached)
# ---------------------------------------------------------------------------

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

    Callers pass either fake structural values or the seed credential from
    the raw extraction; probe inputs are never logged or persisted here.
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

    Real-input sourcing (seed credential policy):
      - The real document number / DOB come from the STORED SEED CREDENTIAL
        for this registry/document-type (encrypted at rest, decrypted by the
        engine-supplied real_inputs_provider) — NEVER from the redacted
        profile, whose values are tokens like [DOCUMENT_NUMBER], and never
        from the document currently being validated. The seed is supplied
        once per registry at onboarding by a consenting person; a seed valid
        for one registry proves nothing about any other registry.
      - Values exist in memory only: never logged, never persisted,
        discarded immediately after the probe.
      - The two-sided fake-vs-real diff confirms BOTH markers; without a
        seed the method is REJECTED-only (empty success_keywords) until a
        seed is onboarded and a manual re-probe (engine.upgrade_method) runs.
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

    # Seed credential: from the raw extraction via the engine's provider.
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
        # Discard the seed credential immediately after the probe (PII).
        del real_inputs

        if real_response:
            diff = _extract_discriminators_by_diff(fake_response, real_response)
            rejection_marker = (diff.get("rejection_marker") or "").strip()
            success_marker = (diff.get("success_marker") or "").strip()
        real_response = None  # drop response bodies from memory
    else:
        # REJECTED-only path: no seed credential. Diff the fake response
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


# ---------------------------------------------------------------------------
# Main generation entry point
# ---------------------------------------------------------------------------

def generate_candidate_method(
    redacted_profile: dict,
    source_info: dict,
    seed_provider: Optional[Callable[[], Optional[Dict[str, str]]]] = None,
) -> ValidationMethod:
    """
    Generates a candidate validation method.

    When the deterministic XHR extractor succeeds, the LLM's role is narrowed
    to mapping profile fields to the extracted param names (Component 3), and
    a one-time discriminator probe populates confirmed markers (Component 2).

    When extraction fails (partial/ambiguous/minified), the existing
    full-LLM path runs unchanged — graceful degradation, not a hard break.

    seed_provider: optional callable returning the STORED SEED CREDENTIAL
    (document_number, date_of_birth) for this registry/document-type, from
    the encrypted seed store. This is the ONLY real-value source for the
    discriminator probe — the redacted profile carries tokens, and the
    document being validated is never used as a probe input. Without a seed
    the method is saved REJECTED-only; engine.upgrade_method upgrades it
    after onboarding. Values stay in memory and are never persisted.
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

    logger.info("XHR extractor result: source_url=%s xhr_contract=%r", source_url, xhr_contract)

    if xhr_contract:
        # ------------------------------------------------------------
        # Narrow path (Components 1-3)
        # ------------------------------------------------------------
        xhr_contract["workflow_options"] = page_structure.get("workflow_options", []) if page_structure else []
        document_key = profile_document_type_key(redacted_profile)
        if document_key == "IN_SID":
            option_text = " ".join(
                f"{item.get('value', '')} {item.get('text', '')}"
                for item in xhr_contract["workflow_options"]
            ).lower()
            if "sid" not in option_text and "seafarer identity" not in option_text:
                raise RuntimeError(
                    "Source page has no workflow option compatible with document type IN_SID."
                )
        user_prompt, _ = _build_narrow_mapping_payload(redacted_profile, xhr_contract)

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

        if llm_mapping:
            llm_mapping = dict(llm_mapping)
            llm_mapping["param_mapping"] = {
                key: value
                for key, value in (llm_mapping.get("param_mapping") or {}).items()
                if key not in workflow_params
            }
            llm_mapping["required_inputs"] = [
                field for field in (llm_mapping.get("required_inputs") or [])
                if field not in {"document_type"}
            ]

        param_mapping, required_inputs = _resolve_param_mapping(llm_mapping, xhr_contract)
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

    # Force status to TESTING regardless of what the LLM hallucinates
    llm_output["status"] = MethodStatus.TESTING
    # Positive verification is always decided by response-field comparison;
    # generated keyword markers are not trusted for either path.
    llm_output["expected_responses"] = {
        "comparison_mode": "field_match",
        "method_schema": CURRENT_METHOD_SCHEMA,
        "document_type_key": profile_document_type_key(redacted_profile),
    }

    # Parse and validate through Pydantic
    return ValidationMethod(**llm_output)
