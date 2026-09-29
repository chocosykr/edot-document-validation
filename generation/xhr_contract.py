"""
Component 1 — deterministic XHR contract extraction.

Reads the page's inline JavaScript and extracts the wire contract of the
lookup call the page itself makes: endpoint URL + HTTP verb + param
encoding (query-string `send(null)` vs request body) + dynamic param names.

Kept deliberately narrow: a wrong extraction is worse than no extraction —
any partial match returns None and the pipeline falls back to the full-LLM
path unchanged.
"""

import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


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
    "send_data": re.compile(r"""\.\s*send\s*\(\s*(?!null\s*)\w+""", re.IGNORECASE),
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
