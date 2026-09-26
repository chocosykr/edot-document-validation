"""
Component 1b: deterministic fetch()-style contract extraction from SPA bundles.

The legacy extractor (generator._extract_xhr_contract) reads INLINE script
text. Modern React/Vue SPAs ship an empty shell page plus large external
bundles, and the real verification contract (e.g.
`fetch(`${Rt}/seafarer/sid/verify?inputvalue=${...}&captcha=${...}`)`) lives
inside a minified bundle the legacy regexes never see.

Same strictness rule as Component 1: a wrong extraction is worse than no
extraction. A bundle holds dozens of fetch() calls (auth, session, captcha,
analytics) — the extractor only accepts a call that is simultaneously:
  - a complete URL (resolvable to a same-origin absolute URL),
  - a known verb (GET/POST; body from the options object or the URL shape),
  - carrying at least one template-expression dynamic parameter.
Everything else is ranked and only the best candidate survives; ties are
broken deterministically. No network, no LLM — pure regex + string ops.

NOTE on implementation style: JS string literals are scanned with a small
character loop (_read_js_string_literal), NOT with quote/backreference
regexes. Minified-bundle parsing must not depend on fragile escaping.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Higher score = more likely the public verification call. Deliberately
# GENERIC tokens only — no site names, no hardcoded paths.
_VERIFY_TOKENS = (
    "verify", "verif", "validate", "lookup", "check", "search", "query",
    "status", "cert", "credential",
)
_CAPTCHA_TOKENS = ("captcha",)
_STATIC_NOISE_TOKENS = (
    "login", "logout", "auth", "session", "otp", "password", "register",
    "admin", "analytics", "telemetry", "refresh-token", "refresh-indos",
    "appointment", "payment", "invoice", "upload", "download", "assets",
)
_DYNAMIC_TOKEN = re.compile(r"\$\{[^}]{1,80}\}")
_CAPTCHA_PARAM_RE = re.compile(r"captcha", re.I)


def _score_candidate(path: str, params: List[str]) -> int:
    """Rank a fetch() candidate. Deterministic; higher wins."""
    low = path.lower()
    score = 0
    if any(tok in low for tok in _VERIFY_TOKENS):
        score += 4
    if low.rstrip("/").endswith(_VERIFY_TOKENS):
        score += 3  # path TERMINATES in a verify-like action
    if any(tok in low for tok in _CAPTCHA_TOKENS):
        score += 3
    if any(tok in low for tok in _STATIC_NOISE_TOKENS):
        score -= 5
    score += min(len(params), 3)  # per-document params raise confidence
    if "inputvalue" in low or "input_value" in low:
        score += 1  # common generic lookup-param naming
    return score


def _split_path_and_query(url_expr: str) -> Tuple[str, str]:
    """Split a template-literal URL into (path-part, query-string-part)."""
    expr = url_expr.strip()
    idx = expr.find("?")
    if idx == -1:
        return expr, ""
    return expr[:idx], expr[idx + 1:]


def _extract_template_params(query: str) -> List[str]:
    """
    Extract dynamic query-param names of the form
      ?name=${expr}&name2=${expr2}
    A param counts as dynamic when its value contains a ${...} template
    expression. Static params (?a=1) are intentionally ignored here — the
    legacy extractor derives those from inline JS; bundle static params are
    rare and a wrong guess would poison the request.
    """
    names: List[str] = []
    for seg in query.split("&"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        name = name.strip()
        if name and _DYNAMIC_TOKEN.search(value):
            if name not in names:
                names.append(name)
    return names


def _read_js_string_literal(tail: str) -> Optional[str]:
    """
    Read a JS string literal starting at tail[0] (single or double quote).
    Returns the decoded content, or None when tail does not start a literal.
    Character loop, not regex: no escaping fragility.
    """
    if not tail or tail[0] not in ('"', "'"):
        return None
    quote = tail[0]
    out: List[str] = []
    i = 1
    while i < len(tail):
        ch = tail[i]
        if ch == "\\" and i + 1 < len(tail):
            out.append(tail[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(out)
        if ch in "\r\n":
            return None  # unterminated on this line: not a simple literal
        out.append(ch)
        i += 1
    return None


def _resolve_base_var(var_name: str, js_text: str) -> Optional[str]:
    """
    Resolve the base-URL variable used in template literals like
    `${Rt}/seafarer/sid/verify`. Returns the assigned literal (possibly ""),
    "__ORIGIN__" for location.origin assignments, or None when unresolvable.

    Minified bundles usually declare variables in grouped declarator lists
    (`...children:e})},Rt=""`), so a declaration keyword is rarely adjacent
    to the variable. Both shapes are accepted:
        const Rt = "..."        (keyword-declared)
        ..., Rt = "...",        (grouped declarator; never `{ Rt = ...`,
                                which would be an object-literal property)
    Only simple literal assignments qualify (strictness rule); the FIRST
    assignment in the text wins.
    """
    if not var_name:
        return None
    esc = re.escape(var_name)
    for anchor in (
        r"(?:var|let|const)\s+" + esc + r"\s*=",
        r"[,;]\s*" + esc + r"\s*=",
    ):
        match = re.search(anchor, js_text)
        if not match:
            continue
        tail = js_text[match.end():match.end() + 300]
        literal = _read_js_string_literal(tail.lstrip())
        if literal is not None:
            return literal
        if re.match(r"(?:window\.)?location\.origin\b", tail.lstrip()):
            return "__ORIGIN__"
        return None  # first assignment wins; it is not a simple literal
    return None


def _resolve_url_expr(url_expr: str, js_text: str, page_url: str) -> Optional[str]:
    """
    Resolve a template-literal URL expression to an absolute URL.
    Accepted shapes (each must yield a same-origin http(s) path):
      ${base}/some/path           base resolves to "" or a literal/origin
      ${base}${xg}/some/path      chained base vars (e.g. xg="/seafarer")
      /some/path                  plain same-origin path
    Template expressions that are NOT a leading base variable make the URL
    unresolvable -> None (a path segment that varies per user is not a
    fixed endpoint).
    """
    expr = url_expr.strip()
    if not expr:
        return None

    # Collapse leading ${var} chains: ${a}${b}/path or ${a}/path
    consumed = 0
    base_literal = ""
    path_bearing_bases = 0
    while True:
        m = re.match(r"\$\{([A-Za-z_$][\w$]*)\}", expr[consumed:])
        if not m:
            break
        var_value = _resolve_base_var(m.group(1), js_text)
        if var_value == "__ORIGIN__":
            # location.origin equals the page origin; contributes no path.
            var_value = ""
        if var_value is None:
            return None
        if var_value:
            if "/" in var_value:
                path_bearing_bases += 1
                if path_bearing_bases > 1:
                    return None  # two path-bearing bases: ambiguous
            base_literal += var_value
        consumed += m.end()
    expr = expr[consumed:]

    # What remains must start with a literal path. Any further ${...} means
    # the PATH varies per call -> not a fixed endpoint.
    if _DYNAMIC_TOKEN.search(expr.split("?")[0]):
        return None

    expr = re.sub(r"^['\"`]+", "", expr)
    literal = re.match(r"(/?[A-Za-z0-9_\-./]*[A-Za-z0-9_\-])", expr)
    if not literal or not literal.group(1).strip("/"):
        return None
    path = literal.group(1)

    # Concatenate paths — urljoin would DISCARD a path-bearing base when the
    # joined path starts with "/" (e.g. base "/seafarer" + path "/sid/verify"
    # must yield "/seafarer/sid/verify", not "/sid/verify").
    if base_literal.startswith("http"):
        origin_and_base = base_literal.rstrip("/")
    else:
        parsed = urlparse(page_url)
        origin_and_base = parsed.scheme + "://" + parsed.netloc + base_literal
    resolved = origin_and_base + (path if path.startswith("/") else "/" + path)
    if not resolved.startswith(("http://", "https://")):
        return None
    return resolved


_FETCH_TEMPLATE_RE = re.compile(
    r"fetch\(\s*`([^`]{1,500})`\s*,?\s*(\{(?:[^{}]|\{[^{}]*\}){0,60}\})?",
    re.S,
)
_METHOD_IN_OPTS_RE = re.compile(r"method\s*:\s*['\"](GET|POST)['\"]", re.I)


def _candidates_from_bundle(js_text: str) -> List[Tuple[str, str, List[str]]]:
    """
    Yield (url_template, verb, dynamic_param_names) for every fetch() call
    with a template-literal URL. Bare (no-options) fetches default to GET —
    fetch()'s own default, not a guess.
    """
    candidates: List[Tuple[str, str, List[str]]] = []
    for match in _FETCH_TEMPLATE_RE.finditer(js_text):
        url_expr = match.group(1)
        opts = match.group(2) or ""
        verb_match = _METHOD_IN_OPTS_RE.search(opts)
        verb = (verb_match.group(1).upper() if verb_match else "GET")
        _, query = _split_path_and_query(url_expr)
        params = _extract_template_params(query)
        candidates.append((url_expr, verb, params))
    return candidates


def _split_captcha_params(params: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """
    Split fetch() query params into (profile-mappable dynamic params, captcha
    params). Captcha params are SESSION values produced by FETCH_CAPTCHA ->
    SOLVE_CAPTCHA at execution time — never document fields — so they must
    not reach the LLM field mapping. Deterministic by name:
      *id* -> {{captcha_id}}, otherwise -> {{captcha_text}}.
    """
    dynamic: List[str] = []
    captcha: Dict[str, str] = {}
    for name in params:
        if _CAPTCHA_PARAM_RE.search(name):
            captcha[name] = "{{captcha_id}}" if "id" in name.lower() else "{{captcha_text}}"
        else:
            dynamic.append(name)
    return dynamic, captcha


def _find_captcha_endpoint(js_text: str, page_url: str) -> Optional[str]:
    """
    Find the parameterless captcha-generate endpoint in the bundle (the call
    that feeds FETCH_CAPTCHA). Returns an absolute URL or None. Ranking is
    deterministic: generate/refresh/image paths, shortest wins.
    """
    best: Optional[str] = None
    for url_expr, _verb, params in _candidates_from_bundle(js_text):
        if params:
            continue  # generate endpoints are parameterless
        resolved = _resolve_url_expr(url_expr, js_text, page_url)
        if not resolved:
            continue
        low = resolved.lower()
        if "captcha" not in low:
            continue
        if not any(tok in low for tok in ("generate", "refresh", "image", "new", "get")):
            continue
        if best is None or len(resolved) < len(best):
            best = resolved
    return best


def _extract_xhr_contract_from_bundles(
    bundle_js: str,
    page_url: str,
) -> Optional[dict]:
    """
    Deterministic extraction of the verification contract from SPA bundle
    text (possibly multiple bundles joined by newlines). Returns the same
    contract dict shape as generator._extract_xhr_contract:
      {endpoint, verb, param_location, dynamic_params, static_params}
    or None when no candidate clears the strict bar.
    """
    if not bundle_js or not bundle_js.strip():
        return None

    best: Optional[Tuple[int, str, str, List[str]]] = None
    best_key: Optional[Tuple[int, int, str]] = None
    for url_expr, verb, params in _candidates_from_bundle(bundle_js):
        if not params:
            continue  # a verification call without per-document params is noise
        resolved = _resolve_url_expr(url_expr, bundle_js, page_url)
        if not resolved:
            continue
        score = _score_candidate(resolved, params)
        # Acceptance needs PATH evidence, not just parameter count: the path
        # must be verification-shaped (a verify/check/lookup token, or the
        # path terminates in one). A bare parameterized fetch — pagination,
        # prefs, anything — must never be elected just for having params.
        low = resolved.lower()
        path_signal = (
            any(tok in low for tok in _VERIFY_TOKENS)
            or low.rstrip("/").endswith(_VERIFY_TOKENS)
        )
        if not path_signal or score < 3:
            continue
        # Deterministic tie-break: score desc, then shortest URL, then lexical.
        key = (-score, len(resolved), resolved)
        if best_key is None or key < best_key:
            best_key = key
            best = (score, resolved, verb, params)

    if best is None:
        return None

    score, endpoint, verb, params = best
    dynamic_params, captcha_params = _split_captcha_params(params)
    if not dynamic_params:
        # Every parameter was a captcha session value — nothing maps to a
        # document field, so this cannot be a verification contract.
        return None

    contract = {
        "endpoint": endpoint,
        "verb": verb,
        "param_location": "query",
        "dynamic_params": dynamic_params,
        "static_params": {},
    }
    if captcha_params:
        contract["captcha_params"] = captcha_params
        contract["captcha_endpoint"] = _find_captcha_endpoint(bundle_js, page_url)
        logger.info(
            "Bundle contract carries captcha params %s (endpoint=%s)",
            sorted(captcha_params), contract["captcha_endpoint"],
        )
    logger.info(
        "Bundle contract extracted: %s %s params=%s (score=%d)",
        verb, endpoint, dynamic_params, score,
    )
    return contract
